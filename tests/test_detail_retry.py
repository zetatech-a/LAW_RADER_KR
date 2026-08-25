"""상세 재조회 큐의 선정 규칙(간격/건수 상한/순서)과 Post 재구성.

이 모듈의 계약:
  - detail_retry_interval_sec 안에 이미 시도한 항목은 이번 실행에서 건너뛴다(0=매번 허용).
  - detail_retry_max_per_run 을 넘겨 처리하지 않는다(0=건수 상한 없음).
  - 처리 순서는 결정적이다(마지막 시도가 오래된 것 먼저 → BILL_ID 사전순).
  - 재조회는 목록(Open API) 가시성에 의존하지 않는다 — 저장된 스냅샷만으로 Post 를
    재구성해 **기존** enrich() 경로를 그대로 부른다.
  - 오래 남은 항목은 경고로 드러내되 **삭제하지 않는다.**

시간은 전부 주입한다(실제 sleep 금지).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.detail_retry import (
    QueuedBill,
    build_post,
    load_queue,
    log_long_waiters,
    retry_bills,
    retry_limits,
    select_due,
)
from src.models import ASSEMBLY_SOURCE_KEY, Post, ProposalContentStatus as S
from src.scrapers.assembly import (
    DETAIL_RETRY_INTERVAL_SEC,
    DETAIL_RETRY_MAX_PER_RUN,
    _DETAIL,
)
from src.state import State, parse_ts

NAME = "의안정보시스템 · 계류의안"
NOW = parse_ts("2026-08-24T12:00:00Z")


def _q(bill_id, last="2026-08-24T09:00:00Z", **over):
    kw = dict(
        bill_id=bill_id,
        title=f"{bill_id} 법률안",
        url=f"https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={bill_id}",
        date="2026-08-24",
        status="pending",
        attempts=1,
        first_seen_at="2026-08-24T09:00:00Z",
        last_attempt_at=last,
    )
    kw.update(over)
    return QueuedBill(**kw)


# --- interval ---


def test_recent_attempt_is_deferred():
    # 10분 전에 시도 → 30분 간격에서는 아직 아니다.
    queue = [_q("PRC_1", last="2026-08-24T11:50:00Z")]
    sel = select_due(queue, now_ts=NOW, interval_sec=1800, max_per_run=0)
    assert sel.selected == []
    assert sel.deferred_by_interval == 1
    assert sel.queued == 1 and sel.due == 0


def test_elapsed_interval_becomes_due():
    queue = [_q("PRC_1", last="2026-08-24T11:29:59Z")]   # 30분 1초 전
    sel = select_due(queue, now_ts=NOW, interval_sec=1800, max_per_run=0)
    assert [q.bill_id for q in sel.selected] == ["PRC_1"]
    assert sel.deferred_by_interval == 0


def test_interval_boundary_is_inclusive():
    """정확히 간격만큼 지났으면 due 다(경계에서 영원히 밀리지 않도록)."""
    queue = [_q("PRC_1", last="2026-08-24T11:30:00Z")]
    sel = select_due(queue, now_ts=NOW, interval_sec=1800, max_per_run=0)
    assert [q.bill_id for q in sel.selected] == ["PRC_1"]


def test_interval_zero_allows_every_run():
    """0 = '간격 제한 없음'. config 계약대로 매 실행 재시도를 허용한다."""
    queue = [_q("PRC_1", last="2026-08-24T11:59:59Z")]
    sel = select_due(queue, now_ts=NOW, interval_sec=0, max_per_run=0)
    assert [q.bill_id for q in sel.selected] == ["PRC_1"]
    assert sel.deferred_by_interval == 0


def test_unreadable_timestamp_is_treated_as_due():
    """시각을 읽을 수 없다고 재조회를 영구히 미루면 누락 방지에 반한다."""
    queue = [_q("PRC_1", last="언제였더라")]
    sel = select_due(queue, now_ts=NOW, interval_sec=1800, max_per_run=0)
    assert [q.bill_id for q in sel.selected] == ["PRC_1"]


# --- per-run cap ---


def test_cap_limits_items_processed():
    queue = [
        _q("PRC_3", last="2026-08-24T09:03:00Z"),
        _q("PRC_1", last="2026-08-24T09:01:00Z"),
        _q("PRC_2", last="2026-08-24T09:02:00Z"),
    ]
    sel = select_due(queue, now_ts=NOW, interval_sec=1800, max_per_run=2)
    assert [q.bill_id for q in sel.selected] == ["PRC_1", "PRC_2"]
    assert sel.due == 3
    assert sel.capped == 1


def test_cap_zero_means_no_item_limit():
    queue = [_q(f"PRC_{i}") for i in range(25)]
    sel = select_due(queue, now_ts=NOW, interval_sec=0, max_per_run=0)
    assert len(sel.selected) == 25
    assert sel.capped == 0


# --- deterministic order ---


def test_order_is_oldest_attempt_first_then_bill_id():
    queue = [
        _q("PRC_B", last="2026-08-24T09:00:00Z"),
        _q("PRC_A", last="2026-08-24T09:00:00Z"),   # 동시각 → ID 사전순
        _q("PRC_C", last="2026-08-24T08:00:00Z"),   # 가장 오래됨
    ]
    sel = select_due(queue, now_ts=NOW, interval_sec=0, max_per_run=0)
    assert [q.bill_id for q in sel.selected] == ["PRC_C", "PRC_A", "PRC_B"]


def test_order_is_stable_across_input_shuffles():
    a = [_q("PRC_A", last="2026-08-24T09:00:00Z"), _q("PRC_B", last="2026-08-24T08:00:00Z")]
    b = list(reversed(a))
    order = lambda qs: [q.bill_id for q in select_due(qs, now_ts=NOW, interval_sec=0, max_per_run=0).selected]
    assert order(a) == order(b) == ["PRC_B", "PRC_A"]


# --- state 연동 ---


def test_load_queue_reads_state_snapshot(tmp_path):
    st = State(tmp_path / "seen.json")
    st.queue_detail(
        ASSEMBLY_SOURCE_KEY, "PRC_1", title="t", url="u", date="d",
        status="pending", note="n", now="2026-08-24T09:00:00Z",
    )
    (item,) = load_queue(st, ASSEMBLY_SOURCE_KEY)
    assert (item.bill_id, item.title, item.status, item.attempts) == ("PRC_1", "t", "pending", 1)
    assert item.last_attempt_ts == parse_ts("2026-08-24T09:00:00Z")


def test_load_queue_empty_for_legacy_state(tmp_path):
    st = State(tmp_path / "seen.json")
    st.mark_seen(ASSEMBLY_SOURCE_KEY, ["PRC_OLD"])
    assert load_queue(st, ASSEMBLY_SOURCE_KEY) == []


# --- Post 재구성: 목록에 없어도 상세를 직접 부를 수 있어야 한다 ---


def test_build_post_uses_saved_snapshot():
    item = _q("PRC_1")
    p = build_post(item, source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert p.post_id == "PRC_1"
    assert p.title == "PRC_1 법률안"
    assert p.date == "2026-08-24"
    assert p.url == item.url
    assert p.source_key == ASSEMBLY_SOURCE_KEY and p.source_name == NAME
    assert p.body == "" and p.proposal_status is S.UNKNOWN


def test_build_post_rebuilds_url_when_missing():
    p = build_post(_q("PRC_1", url=""), source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert p.url == _DETAIL.format(bill_id="PRC_1")


def test_build_post_replaces_dead_legacy_url():
    """LINK_URL 이 죽은 구 경로로 저장돼 있어도 현재 경로로 되살린다."""
    dead = "https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_1"
    p = build_post(_q("PRC_1", url=dead), source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert p.url == _DETAIL.format(bill_id="PRC_1")


def test_build_post_falls_back_to_bill_id_title():
    p = build_post(_q("PRC_1", title=""), source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert p.title == "PRC_1"


# --- retry_bills: 기존 enrich 경로 재사용 ---


class _Scraper:
    """enrich 결과를 지정하는 대역. 실제 HTTP 는 부르지 않는다."""

    detail_retry_interval_sec = 1800.0
    detail_retry_max_per_run = 10
    detail_url = _DETAIL

    def __init__(self, results):
        self.results = results
        self.calls = []

    def enrich(self, post):
        self.calls.append(post.post_id)
        status, body = self.results[post.post_id]
        post.proposal_status = status
        post.body = body
        post.proposal_note = "note"


def _sel(*items):
    return select_due(list(items), now_ts=NOW, interval_sec=0, max_per_run=0)


def test_retry_splits_recovered_and_still_queued():
    sc = _Scraper({
        "PRC_A": (S.AVAILABLE, "제안이유 본문"),
        "PRC_B": (S.PENDING, ""),
        "PRC_C": (S.ERROR, ""),
        "PRC_D": (S.UNKNOWN, ""),
    })
    sel = _sel(_q("PRC_A"), _q("PRC_B"), _q("PRC_C"), _q("PRC_D"))
    out = retry_bills(sc, sel, source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)

    assert [p.post_id for p in out.recovered] == ["PRC_A"]
    assert sorted(p.post_id for p in out.still_queued) == ["PRC_B", "PRC_C", "PRC_D"]
    assert out.attempted == 4 and out.pending == 1 and out.failed == 2
    assert out.recovered[0].body == "제안이유 본문"


def test_retry_calls_enrich_once_per_selected_bill():
    sc = _Scraper({"PRC_A": (S.PENDING, "")})
    retry_bills(sc, _sel(_q("PRC_A")), source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert sc.calls == ["PRC_A"]


def test_retry_does_nothing_when_nothing_is_due():
    sc = _Scraper({})
    sel = select_due([_q("PRC_A", last="2026-08-24T11:59:00Z")], now_ts=NOW,
                     interval_sec=1800, max_per_run=0)
    out = retry_bills(sc, sel, source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert sc.calls == [] and out.attempted == 0


def test_retry_survives_enrich_raising():
    """한 건이 예외를 던져도 나머지 재조회가 멈추면 안 된다."""

    class _Boom(_Scraper):
        def enrich(self, post):
            if post.post_id == "PRC_A":
                raise RuntimeError("boom")
            return super().enrich(post)

    sc = _Boom({"PRC_B": (S.AVAILABLE, "본문")})
    out = retry_bills(sc, _sel(_q("PRC_A"), _q("PRC_B")),
                      source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert [p.post_id for p in out.recovered] == ["PRC_B"]
    assert [p.post_id for p in out.still_queued] == ["PRC_A"]
    assert out.still_queued[0].proposal_status is S.ERROR


def test_retry_logs_counts(caplog):
    sc = _Scraper({"PRC_A": (S.AVAILABLE, "본문"), "PRC_B": (S.PENDING, "")})
    with caplog.at_level(logging.INFO, logger="law_rader"):
        retry_bills(sc, _sel(_q("PRC_A"), _q("PRC_B")),
                    source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "queued=2" in text and "attempted=2" in text
    assert "recovered=1" in text and "pending=1" in text
    assert "AVAILABLE 복구" in text


# --- 상한값 출처 ---


def test_retry_limits_come_from_scraper():
    class _S:
        detail_retry_interval_sec = 60.0
        detail_retry_max_per_run = 3

    assert retry_limits(_S()) == (60.0, 3)


def test_retry_limits_fall_back_to_code_defaults():
    """대역 스크래퍼처럼 속성이 없으면 코드 기본값(한 곳에만 있는 숫자)을 쓴다."""
    assert retry_limits(object()) == (DETAIL_RETRY_INTERVAL_SEC, DETAIL_RETRY_MAX_PER_RUN)


# --- 자동 만료 금지 ---


def test_long_waiters_are_logged_but_not_removed(caplog):
    queue = [_q("PRC_OLD", first_seen_at="2026-08-01T00:00:00Z")]
    with caplog.at_level(logging.WARNING, logger="law_rader"):
        log_long_waiters(queue, now_ts=NOW, warn_after_sec=7 * 24 * 3600)
    assert "PRC_OLD" in caplog.text
    assert "자동 삭제하지 않습니다" in caplog.text
    assert len(queue) == 1          # 목록은 그대로다


def test_long_waiters_quiet_for_fresh_entries(caplog):
    with caplog.at_level(logging.WARNING, logger="law_rader"):
        log_long_waiters([_q("PRC_NEW")], now_ts=NOW, warn_after_sec=7 * 24 * 3600)
    assert caplog.records == []


# --- 손상된 큐 항목의 격리 (PR #25 Codex P2) -------------------------------
#
# state 에 저장된 스냅샷은 시간이 지나며 손상될 수 있다. 재구성(build_post)이 예외를
# 던지면 그 항목 하나가 재조회 루프 전체를 중단시켜서는 안 된다.

# urlparse 가 ValueError("Invalid IPv6 URL") 를 던지는 실제 입력.
BAD_URL = "http://["


def test_build_post_raises_on_malformed_url():
    """이 회귀가 막는 실제 예외를 먼저 못박는다(가정이 아니라 사실이다)."""
    import pytest

    with pytest.raises(ValueError):
        build_post(_q("PRC_BAD", url=BAD_URL),
                   source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)


def test_B1_malformed_entry_does_not_block_later_items():
    sc = _Scraper({"PRC_GOOD": (S.AVAILABLE, "제안이유 본문")})
    sel = _sel(_q("PRC_BAD", url=BAD_URL), _q("PRC_GOOD"))
    out = retry_bills(sc, sel, source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)

    # 손상된 항목은 요청을 보내지 않는다 — 뒤 항목만 실제로 조회된다.
    assert sc.calls == ["PRC_GOOD"]
    assert [p.post_id for p in out.recovered] == ["PRC_GOOD"]
    assert [p.post_id for p in out.still_queued] == ["PRC_BAD"]


def test_B2_reconstruction_failure_becomes_an_error_outcome():
    sc = _Scraper({})
    out = retry_bills(sc, _sel(_q("PRC_BAD", url=BAD_URL)),
                      source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)

    (bad,) = out.still_queued
    assert bad.post_id == "PRC_BAD"
    assert bad.proposal_status is S.ERROR
    assert "ValueError" in bad.proposal_note          # 진단에 쓸 수 있는 사유
    assert len(bad.proposal_note) <= 200              # 길이 제한
    assert out.recovered == []
    # 상태 기록용 Post 이므로 저장된 스냅샷을 그대로 담는다(임의 URL 로 바꾸지 않는다).
    assert bad.url == BAD_URL
    assert bad.title == "PRC_BAD 법률안"


def test_B2_malformed_item_never_reaches_enrich():
    """손상된 URL 을 조용히 canonical URL 로 바꿔 다시 요청하지 않는다(fail-closed)."""
    sc = _Scraper({})
    retry_bills(sc, _sel(_q("PRC_BAD", url=BAD_URL)),
                source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert sc.calls == []


def test_B4_all_reconstruction_failures_still_return_an_outcome():
    sc = _Scraper({})
    sel = _sel(_q("PRC_B1", url=BAD_URL), _q("PRC_B2", url=BAD_URL),
               _q("PRC_B3", url=BAD_URL))
    out = retry_bills(sc, sel, source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)

    assert sc.calls == []
    assert out.attempted == 3
    assert len(out.recovered) == 0
    assert out.failed == 3            # ERROR 이므로 pending 이 아니라 failed 로 센다
    assert out.pending == 0


def test_malformed_entry_is_logged_without_the_raw_exception_dump(caplog):
    sc = _Scraper({"PRC_GOOD": (S.PENDING, "")})
    with caplog.at_level(logging.WARNING, logger="law_rader"):
        retry_bills(sc, _sel(_q("PRC_BAD", url=BAD_URL), _q("PRC_GOOD")),
                    source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert "PRC_BAD" in caplog.text
    assert "요청을 만들지 못함" in caplog.text
    assert "Traceback" not in caplog.text


# --- 저장된 큐 URL vs 현재 detail_url override (PR #25 Codex P2) -------------
#
# 큐에는 등록 당시에는 정상이었던 상세 URL 이 남는다. 사이트가 경로를 옮겨 운영자가
# config 의 detail_url 을 고쳐도, 저장된 URL 이 유효한 http(s) 이면 canonicalize 계약상
# 그대로 보존되어 큐 항목만 은퇴한 경로를 계속 두드린다.

NEW_TEMPLATE = "https://likms.assembly.go.kr/bill/bi/newDetailPage.do?billId={bill_id}"
OLD_VALID = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_1"
LEGACY = "https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_1"


class _OverrideScraper(_Scraper):
    """운영자가 config 로 detail_url 을 명시한 스크래퍼."""

    def __init__(self, results, template=NEW_TEMPLATE, overridden=True):
        super().__init__(results)
        self.detail_url = template
        self.detail_url_overridden = overridden
        self.urls = []

    def enrich(self, post):
        self.urls.append(post.url)
        super().enrich(post)


def test_B1_explicit_override_supersedes_a_stale_queued_url():
    sc = _OverrideScraper({"PRC_1": (S.AVAILABLE, "본문")})
    retry_bills(sc, _sel(_q("PRC_1", url=OLD_VALID)),
                source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert sc.urls == [NEW_TEMPLATE.format(bill_id="PRC_1")]


def test_B2_without_override_a_valid_stored_url_is_preserved():
    """override 가 없으면 우리가 모르는 공식 URL 을 임의로 갈아끼우지 않는다(기존 계약)."""
    unknown_official = "https://likms.assembly.go.kr/bill/bi/somethingNew.do?billId=PRC_1"
    sc = _OverrideScraper({"PRC_1": (S.AVAILABLE, "본문")}, overridden=False)
    retry_bills(sc, _sel(_q("PRC_1", url=unknown_official)),
                source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert sc.urls == [unknown_official]


def test_B3_override_does_not_rescue_a_malformed_stored_url():
    """손상된 state 를 조용히 정상 URL 로 갈아치워 '복구 성공'처럼 만들지 않는다."""
    sc = _OverrideScraper({"PRC_GOOD": (S.AVAILABLE, "본문")})
    out = retry_bills(
        sc, _sel(_q("PRC_BAD", url=BAD_URL), _q("PRC_GOOD")),
        source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME,
    )
    # 손상 항목은 요청 0회 + ERROR + 큐 유지, 뒤 항목은 계속 처리된다.
    assert [p.post_id for p in out.still_queued] == ["PRC_BAD"]
    assert out.still_queued[0].proposal_status is S.ERROR
    assert [p.post_id for p in out.recovered] == ["PRC_GOOD"]
    assert sc.urls == [NEW_TEMPLATE.format(bill_id="PRC_GOOD")]


def test_B4_override_replaces_a_known_legacy_route():
    sc = _OverrideScraper({"PRC_1": (S.PENDING, "")})
    retry_bills(sc, _sel(_q("PRC_1", url=LEGACY)),
                source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert sc.urls == [NEW_TEMPLATE.format(bill_id="PRC_1")]


def test_B5_override_is_used_for_an_empty_stored_url():
    sc = _OverrideScraper({"PRC_1": (S.PENDING, "")})
    retry_bills(sc, _sel(_q("PRC_1", url="")),
                source_key=ASSEMBLY_SOURCE_KEY, source_name=NAME)
    assert sc.urls == [NEW_TEMPLATE.format(bill_id="PRC_1")]


def test_build_post_prefer_template_validates_before_substituting():
    """prefer_template 이어도 검증이 먼저다 — 손상된 URL 은 여전히 예외를 던진다."""
    import pytest

    with pytest.raises(ValueError):
        build_post(_q("PRC_1", url=BAD_URL), source_key=ASSEMBLY_SOURCE_KEY,
                   source_name=NAME, detail_url_template=NEW_TEMPLATE,
                   prefer_template=True)


def test_build_post_default_does_not_prefer_the_template():
    """기본값은 기존 동작 — 저장된 정상 URL 보존."""
    p = build_post(_q("PRC_1", url=OLD_VALID), source_key=ASSEMBLY_SOURCE_KEY,
                   source_name=NAME, detail_url_template=NEW_TEMPLATE)
    assert p.url == OLD_VALID


def test_scraper_reports_whether_detail_url_was_overridden():
    from src.config import SourceConfig
    from src.scrapers.assembly import AssemblyBillScraper, _DETAIL

    def _make(**extra):
        src = SourceConfig(key=ASSEMBLY_SOURCE_KEY, name=NAME, type="assembly_bill",
                           list_url="https://likms.assembly.go.kr/", extra=extra)
        return AssemblyBillScraper(src, fetcher=None)

    default = _make()
    assert default.detail_url == _DETAIL and default.detail_url_overridden is False
    # 빈 문자열/공백은 '지정 안 함'으로 본다(기존 config 관용).
    blank = _make(detail_url="   ")
    assert blank.detail_url == _DETAIL and blank.detail_url_overridden is False
    over = _make(detail_url=NEW_TEMPLATE)
    assert over.detail_url == NEW_TEMPLATE and over.detail_url_overridden is True
