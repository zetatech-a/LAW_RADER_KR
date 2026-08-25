"""상세 시간예산(detail_budget_sec)은 **실제 의안 상세 작업**만 세어야 한다.

예산은 첫 enrich 에서 절대 마감시각으로 고정된다. 그런데 신규 의안 상세수집과 큐
재조회 사이에는 상세 수집이 아닌 일이 얼마든지 끼어든다 — config 순서에 따라 **뒤
소스의 목록/상세 수집**, 집계 로깅, 큐 선정, SMTP 선점검 등. 그 시간이 예산을
갉아먹으면 LIKMS 요청을 한 번도 하지 않았는데 예산이 소진되어 선택된 큐 항목이
전부 '요청 없이 ERROR' 가 되고 시도 횟수만 소모한 채 다음 간격까지 미뤄진다.

그래서 특정 활동(SMTP 등)만 골라 재지 않고, **마지막 상세 작업 이후의 간격 전체**를
resume_detail_budget() 으로 한 번에 뺀다. 중간에 무엇이 끼어들든 빠짐없이 제외되고
같은 시간을 두 번 빼는 일도 구조적으로 생기지 않는다.

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


# --- A1: Assembly 뒤에 오는 **다른 소스**의 시간도 예산에서 빠진다 ---


def test_A1_later_source_work_does_not_consume_the_detail_budget(clock, monkeypatch):
    """config 순서가 assembly_bill → 다른 소스 인 경우의 회귀.

    뒤 소스의 목록/상세 수집이 상세 예산보다 오래 걸려도, 큐 재조회는 정상적으로
    요청을 보낼 수 있어야 한다.
    """
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=40)

    sc.enrich(_post("PRC_NEW"))          # 신규 의안 상세 40초 → 남은 예산 80
    assert calls == ["PRC_NEW"]
    assert sc._detail_deadline - clock.now == pytest.approx(80)

    clock.advance(200)                   # 뒤 소스(금융위·금감원) 수집 200초 — 예산보다 길다
    clock.advance(10)                    # + SMTP 선점검 10초
    assert clock.now > sc._detail_deadline    # 보정이 없으면 이미 마감 초과

    sc.resume_detail_budget()            # 마지막 상세 작업 이후 간격 전체를 제외
    assert sc._detail_deadline - clock.now == pytest.approx(80)

    q = _post("PRC_Q")
    sc.enrich(q)                         # 실제로 요청이 나간다
    assert calls == ["PRC_NEW", "PRC_Q"]
    assert q.proposal_status is S.AVAILABLE


def test_A1_without_resume_the_queue_item_would_be_blocked(clock, monkeypatch):
    """보정이 없으면 요청 없이 ERROR 가 된다 — 이 회귀가 무엇을 막는지 못박는다."""
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=40)

    sc.enrich(_post("PRC_NEW"))
    clock.advance(200)                   # 뒤 소스 작업 — 보정 없음
    q = _post("PRC_Q")
    sc.enrich(q)
    assert calls == ["PRC_NEW"]          # 요청이 나가지 않았다
    assert q.proposal_status is S.ERROR
    assert "시간예산" in q.proposal_note


# --- A2: 느린 SMTP 만 있는 기존 경로(Assembly 가 마지막 소스)도 그대로 동작 ---


def test_A2_slow_smtp_alone_is_still_excluded(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=110)

    sc.enrich(_post("PRC_NEW"))          # 남은 예산 10초
    assert sc._detail_deadline - clock.now == pytest.approx(10)

    clock.advance(20)                    # SMTP 20초 — 보정이 없으면 마감 초과
    assert clock.now > sc._detail_deadline
    sc.resume_detail_budget()

    assert sc._detail_deadline - clock.now == pytest.approx(10)
    q = _post("PRC_Q")
    sc.enrich(q)
    assert calls == ["PRC_NEW", "PRC_Q"]
    assert q.proposal_status is S.AVAILABLE


def test_gap_is_excluded_exactly_once(clock, monkeypatch):
    """같은 간격을 두 번 빼지 않는다(SMTP 를 따로 또 제외하던 방식의 double-count 방지)."""
    sc = _scraper(detail_budget_sec=120)
    _fake_fill(sc, monkeypatch, clock=clock, cost=20)

    sc.enrich(_post("PRC_NEW"))          # 남은 100초
    clock.advance(60)                    # 뒤 소스 50초 + SMTP 10초 = 간격 60초
    sc.resume_detail_budget()
    assert sc._detail_deadline - clock.now == pytest.approx(100)

    # 한 번 더 불러도 (그 사이 흐른 시간이 0 이므로) 예산이 늘어나지 않는다.
    sc.resume_detail_budget()
    assert sc._detail_deadline - clock.now == pytest.approx(100)


# --- A3: 신규 상세수집이 없었던 경우 ---


def test_A3_no_prior_detail_work_starts_budget_at_first_retry(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=5)

    assert sc._detail_deadline is None
    clock.advance(300)                   # 다른 소스 + SMTP 가 아무리 오래 걸려도
    sc.resume_detail_budget()            # 상세를 한 번도 안 했으므로 no-op
    assert sc._detail_deadline is None

    sc.enrich(_post("PRC_Q"))            # 큐 재조회의 첫 요청에서 비로소 예산이 시작된다
    assert calls == ["PRC_Q"]
    assert sc._detail_deadline == pytest.approx(clock.now - 5 + 120)


# --- A4: 실제 Assembly 상세 작업은 여전히 예산을 소비한다 ---


def test_A4_real_assembly_work_still_exhausts_the_budget(clock, monkeypatch):
    """보정이 예산을 무력화하면 안 된다 — 상세 작업 자체로 소진되면 재조회는 막힌다."""
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=125)   # 신규 한 건이 예산 초과

    sc.enrich(_post("PRC_NEW"))
    sc.resume_detail_budget()            # 간격은 0 → 아무것도 되돌리지 않는다
    q = _post("PRC_Q")
    sc.enrich(q)
    assert calls == ["PRC_NEW"]          # 재조회는 요청 없이 차단된다
    assert q.proposal_status is S.ERROR
    assert "시간예산" in q.proposal_note


def test_A4_accumulated_assembly_work_is_shared_across_new_and_retry(clock, monkeypatch):
    """신규 + 재조회가 하나의 누적 예산을 공유한다(각자 120초가 아니다)."""
    sc = _scraper(detail_budget_sec=120)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=50)

    sc.enrich(_post("PRC_NEW"))          # 50초 소비 → 남은 70
    clock.advance(30)                    # 상세가 아닌 간격
    sc.resume_detail_budget()
    sc.enrich(_post("PRC_Q1"))           # 50초 더 → 남은 20
    sc.enrich(_post("PRC_Q2"))           # 50초 더 → 마감 초과
    blocked = _post("PRC_Q3")
    sc.enrich(blocked)
    assert calls == ["PRC_NEW", "PRC_Q1", "PRC_Q2"]
    assert blocked.proposal_status is S.ERROR
    assert "시간예산" in blocked.proposal_note


# --- A5: 예산 비활성 / 브레이커 불변 ---


def test_A5_budget_disabled_is_a_noop(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=0)
    calls = _fake_fill(sc, monkeypatch, clock=clock, cost=10_000)

    sc.enrich(_post("PRC_NEW"))
    sc.resume_detail_budget()
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
    clock.advance(15)                    # 상세와 무관한 대기 15초
    sc.resume_detail_budget()
    # 재시작이었다면 120초가 되었을 것이다. 실제로는 간격만큼만 밀려 남은 예산이
    # 그대로 20초다.
    assert sc._detail_deadline - clock.now == pytest.approx(20)


def test_A5_resume_does_not_touch_the_consecutive_failure_breaker(clock, monkeypatch):
    sc = _scraper(detail_budget_sec=120, detail_max_consecutive_failures=2)
    _fake_fill(sc, monkeypatch, clock=clock, cost=1, status=S.ERROR)

    sc.enrich(_post("PRC_1"))
    sc.enrich(_post("PRC_2"))
    assert sc._consecutive_failures == 2

    clock.advance(300)
    sc.resume_detail_budget()
    assert sc._consecutive_failures == 2      # 간격 보정이 실패 집계를 초기화하지 않는다

    blocked = _post("PRC_3")
    sc.enrich(blocked)
    assert blocked.proposal_status is S.ERROR
    assert "연속 실패" in blocked.proposal_note   # 브레이커는 그대로 동작한다
