"""상세 시간예산(detail_budget_sec)은 **실제 의안 상세 작업**만 세어야 한다.

예산은 첫 enrich 에서 절대 마감시각으로 고정된다. 그런데 신규 의안 상세수집과 큐
재조회 사이에는 SMTP 선점검처럼 상세 수집이 아닌 대기가 끼어든다. 그 대기가 예산을
갉아먹으면, LIKMS 요청을 한 번도 하지 않았는데 예산이 소진되어 선택된 큐 항목이
전부 '요청 없이 ERROR' 가 되고 시도 횟수만 소모한 채 다음 간격까지 미뤄진다.

실제 시간을 기다리지 않는다 — time.monotonic 을 가짜 시계로 갈아 끼운다.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.models import Post, ProposalContentStatus as S
from src.scrapers import assembly as asm
from src.scrapers.assembly import AssemblyBillScraper


class _Clock:
    """테스트가 직접 돌리는 단조시계."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(asm.time, "monotonic", c)
    return c


def _scraper(**extra):
    src = SourceConfig(
        key="assembly_bill", name="의안정보시스템 · 계류의안", type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/bi/bill/state/mooringBillPage.do",
        extra=extra,
    )
    return AssemblyBillScraper(src, fetcher=None)


def _post(pid="PRC_1"):
    return Post(
        source_key="assembly_bill", source_name="의안정보시스템 · 계류의안",
        post_id=pid, title=f"{pid} 법률안",
        url=f"https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={pid}",
    )


def _fake_fill(scraper, monkeypatch, *, clock, cost, status=S.AVAILABLE):
    """_fill_proposal_reason 을 '시간만 쓰는' 가짜로 바꾼다(네트워크 없음)."""
    calls = []

    def _fill(post):
        calls.append(post.post_id)
        clock.advance(cost)
        post.proposal_status = status
        post.body = "제안이유 본문" if status is S.AVAILABLE else ""

    monkeypatch.setattr(scraper, "_fill_proposal_reason", _fill)
    return calls


# --- A1: 신규 상세 이후의 SMTP 시간이 예산에서 빠진다 ---


def test_A1_smtp_time_is_excluded_after_new_enrich(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=40)

    sc.enrich(_post("PRC_NEW"))          # 신규 상세 40초 소비 → 남은 예산 80
    assert calls == ["PRC_NEW"]
    remaining_before = sc._detail_deadline - clock.now
    assert remaining_before == pytest.approx(80)

    # SMTP 선점검 30초(상세 작업 아님)
    started = clock.now
    clock.advance(30)
    sc.exclude_detail_idle_time(clock.now - started)

    # 재조회 시작 시점의 남은 상세 예산은 그대로 80초여야 한다(30초가 차감되지 않음).
    assert sc._detail_deadline - clock.now == pytest.approx(80)

    sc.enrich(_post("PRC_Q"))            # 그리고 실제로 요청이 나간다
    assert calls == ["PRC_NEW", "PRC_Q"]


# --- A2: 느린 SMTP 가 예전 마감시각을 넘겼을 상황 (핵심 회귀) ---


def test_A2_slow_smtp_would_have_crossed_the_old_deadline(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=110)

    sc.enrich(_post("PRC_NEW"))          # 남은 예산 10초
    assert sc._detail_deadline - clock.now == pytest.approx(10)

    started = clock.now
    clock.advance(20)                    # SMTP 20초 — 보정이 없으면 이미 마감 초과
    assert clock.now > sc._detail_deadline
    sc.exclude_detail_idle_time(clock.now - started)

    # 보정 후에는 다시 10초가 남고, 큐 재조회가 실제로 요청을 보낸다.
    assert sc._detail_deadline - clock.now == pytest.approx(10)
    q = _post("PRC_Q")
    sc.enrich(q)
    assert calls == ["PRC_NEW", "PRC_Q"]
    assert q.proposal_status is S.AVAILABLE


def test_A2_without_exclusion_the_queue_item_would_be_blocked(clock, monkeypatch):
    """보정을 하지 않으면 요청 없이 ERROR 가 된다 — 이 회귀가 무엇을 막는지 못박는다."""
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=110)

    sc.enrich(_post("PRC_NEW"))
    clock.advance(20)                    # SMTP — 보정 없음
    q = _post("PRC_Q")
    sc.enrich(q)
    assert calls == ["PRC_NEW"]          # 요청이 나가지 않았다
    assert q.proposal_status is S.ERROR
    assert "시간예산" in q.proposal_note


# --- A3: 신규 상세수집이 없었던 경우 ---


def test_A3_no_prior_detail_work_starts_budget_at_first_retry(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=5)

    assert sc._detail_deadline is None
    sc.exclude_detail_idle_time(30)      # SMTP 30초 — 아직 예산이 시작되지 않았으므로 no-op
    assert sc._detail_deadline is None

    sc.enrich(_post("PRC_Q"))            # 여기서 비로소 예산이 시작된다
    assert calls == ["PRC_Q"]
    assert sc._detail_deadline == pytest.approx(clock.now - 5 + 120)


# --- A4: 예산 비활성 ---


def test_A4_budget_disabled_is_a_noop(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=0)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=10_000)

    sc.enrich(_post("PRC_NEW"))
    sc.exclude_detail_idle_time(9_999)
    assert sc._detail_deadline is None   # 예산이 없으면 마감시각도 만들지 않는다

    sc.enrich(_post("PRC_Q"))            # 무제한 semantics 그대로
    assert calls == ["PRC_NEW", "PRC_Q"]


def test_exclusion_ignores_zero_and_negative_elapsed(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=120)
    _fake_fill(sc, monkeypatch, clock=clock, cost=10)
    sc.enrich(_post("PRC_NEW"))
    before = sc._detail_deadline
    sc.exclude_detail_idle_time(0)
    sc.exclude_detail_idle_time(-5)
    assert sc._detail_deadline == before


def test_exclusion_shifts_the_deadline_it_does_not_reset_it(clock, monkeypatch):
    """예산 재시작이 아니다 — 누적 예산 공유(신규+재조회)가 유지되어야 한다."""
    sc = _scraper(detail_budget_sec=120)
    _fake_fill(sc, monkeypatch, clock=clock, cost=100)

    sc.enrich(_post("PRC_NEW"))          # 남은 20초
    started = clock.now
    clock.advance(15)                    # 상세와 무관한 대기 15초
    sc.exclude_detail_idle_time(clock.now - started)
    # 재시작이었다면 120초가 되었을 것이다. 실제로는 대기시간만큼만 밀려 남은 예산이
    # 그대로 20초다.
    assert sc._detail_deadline - clock.now == pytest.approx(20)


# --- A5: 연속 실패 브레이커는 건드리지 않는다 ---


def test_A5_exclusion_does_not_touch_the_consecutive_failure_breaker(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=120, detail_max_consecutive_failures=2)
    _fake_fill(sc, monkeypatch, clock=clock, cost=1, status=S.ERROR)

    sc.enrich(_post("PRC_1"))
    sc.enrich(_post("PRC_2"))
    assert sc._consecutive_failures == 2

    sc.exclude_detail_idle_time(30)
    assert sc._consecutive_failures == 2      # SMTP 때문에 실패 집계가 초기화되지 않는다

    blocked = _post("PRC_3")
    sc.enrich(blocked)
    assert blocked.proposal_status is S.ERROR
    assert "연속 실패" in blocked.proposal_note   # 브레이커는 그대로 동작한다
