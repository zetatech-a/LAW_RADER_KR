"""제안이유 상태(ProposalContentStatus) 판정 테스트.

핵심 요구: **본문 없음과 수집 실패를 같은 것으로 취급하지 않는다.**
  - 접수 직후라 원문이 아직 없다        → PENDING (실패 아님)
  - 셀렉터 변경·네트워크·HTTP 오류      → ERROR

fixture 는 tests/fixtures/synthetic/ 의 손으로 만든 구조 HTML 을 쓴다(실제 캡처가
아니며, 라이브 계약 검증이 아니라 판정기 검증용이다 — 그 디렉터리 README 참고).
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.models import ASSEMBLY_SOURCE_KEY, Post, ProposalContentStatus as S
from src.notifier import build_html, build_text
from src.scrapers.assembly import AssemblyBillScraper

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


def _fx(name: str) -> str:
    return (SYNTH / name).read_text(encoding="utf-8")


class _Fetcher:
    def __init__(self, get_html, post_html=""):
        self._get_html = get_html
        self._post_html = post_html
        self.gets = []
        self.posts = []

    def get(self, url, referer=None, params=None, headers=None):
        self.gets.append(url)
        return ("get", len(self.gets))

    def post(self, url, referer=None, data=None, headers=None):
        self.posts.append(url)
        return ("post", len(self.posts))

    def text(self, resp):
        return self._get_html if resp[0] == "get" else self._post_html


def _scraper(fetcher):
    return AssemblyBillScraper(
        SourceConfig(
            key=ASSEMBLY_SOURCE_KEY,
            name="의안정보시스템 · 계류의안",
            type="assembly_bill",
            list_url="https://likms.assembly.go.kr/bill/bi/bill/state/mooringBillPage.do",
            extra={},
        ),
        fetcher=fetcher,
    )


def _post(bill_id="PRC_TEST") -> Post:
    return Post(
        source_key=ASSEMBLY_SOURCE_KEY,
        source_name="의안정보시스템 · 계류의안",
        post_id=bill_id,
        title="테스트 법률안 (홍길동)",
        url=f"https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={bill_id}",
        date="2026-08-06",
    )


def _run(get_html, post_html="", tmp_path=None, monkeypatch=None, bill_id="PRC_TEST"):
    if tmp_path is not None:
        monkeypatch.chdir(tmp_path)   # 디버그 덤프를 tmp 로
    f = _Fetcher(get_html, post_html)
    p = _post(bill_id)
    _scraper(f).enrich(p)
    return p, f


# --- 기본값 ---


def test_default_status_is_unknown():
    p = _post()
    assert p.proposal_status is S.UNKNOWN
    assert p.proposal_note == ""


def test_non_assembly_posts_stay_unknown():
    p = Post(source_key="fsc_press", source_name="금융위", post_id="1",
             title="t", url="https://x", body="본문")
    assert p.proposal_status is S.UNKNOWN


# --- AVAILABLE ---


def test_available_when_proposal_reason_is_present():
    p, f = _run(_fx("bill_available.html"))
    assert p.proposal_status is S.AVAILABLE
    assert "예치금" in p.body
    assert len(p.body) > 50
    assert f.posts == []            # 추가 요청 없이 확보


# --- PENDING ---


def test_pending_when_billinfo_has_no_summary_section(tmp_path, monkeypatch):
    """라이브 확인(Action #13): 등록 전에는 제안이유 섹션이 **아예 생성되지 않는다**.

    HTTP 200 정상 심사정보 HTML 이고 의안번호·제안일자·제안자는 정상인데
    #prntsummary-sect 와 pre#prntSummary 만 없다 = 등록 대기.
    """
    p, f = _run(
        _fx("bill_detail_page.html"), _fx("billinfo_pending.html"),
        tmp_path=tmp_path, monkeypatch=monkeypatch, bill_id="PRC_SYNTH_0001",
    )
    assert len(f.posts) == 1        # 확정 endpoint 에 실제로 물어봤다
    assert p.proposal_status is S.PENDING
    assert p.body == ""
    assert "제안이유 섹션이 아직 없음" in p.proposal_note
    # 등록 대기는 진단 덤프를 남기지 않는다(장애가 아니므로 잡음만 된다)
    assert not (tmp_path / "debug").exists()


def test_empty_initial_html_alone_is_never_pending():
    """초기 HTML 이 비었다는 이유로 PENDING 처리하면 안 된다 — 그게 정상 상태다.

    현행 페이지는 초기 HTML 의 컨테이너가 비어 있고 내용은 billInfo.do 가 준다.
    확정 endpoint 를 물어보기 전에는 등록 대기인지 알 수 없다.
    """
    p, f = _run(
        _fx("bill_detail_page.html"), _fx("billinfo_available.html"),
        bill_id="PRC_SYNTH_0001",
    )
    assert len(f.posts) == 1        # 반드시 물어본다
    assert p.proposal_status is S.AVAILABLE
    assert "예치금" in p.body


# --- ERROR ---


@pytest.mark.parametrize(
    "fixture,marker",
    [
        ("billinfo_section_without_pre.html", "#prntsummary-sect"),
        ("billinfo_marker_without_selector.html", "표식은 있는데"),
        ("billinfo_malformed_shell.html", "정상 심사정보 응답이 아님"),
    ],
)
def test_error_when_billinfo_structure_changed(tmp_path, monkeypatch, fixture, marker):
    """구조가 바뀐 응답은 ERROR. 등록 대기로 위장하면 고장이 묻힌다."""
    p, _f = _run(
        _fx("bill_detail_page.html"), _fx(fixture),
        tmp_path=tmp_path, monkeypatch=monkeypatch, bill_id="PRC_SYNTH_0001",
    )
    assert p.proposal_status is S.ERROR, p.proposal_note
    assert marker in p.proposal_note
    assert (tmp_path / "debug").exists()      # 진단 덤프는 남긴다


def test_error_when_page_says_bill_not_found(tmp_path, monkeypatch):
    """'해당 의안 정보가 존재하지 않습니다' 는 등록 대기가 아니라 잘못된 주소다."""
    p, _f = _run(
        _fx("bill_not_found.html"), tmp_path=tmp_path, monkeypatch=monkeypatch
    )
    assert p.proposal_status is S.ERROR
    assert "의안 정보 없음" in p.proposal_note


def test_error_on_network_failure():
    class _Boom:
        def get(self, *a, **k):
            raise ConnectionError("연결 실패")

        def text(self, resp):  # pragma: no cover
            raise AssertionError

    p = _post()
    _scraper(_Boom()).enrich(p)
    assert p.proposal_status is S.ERROR
    assert "ConnectionError" in p.proposal_note


def test_error_on_http_failure():
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("404 Client Error")

        def text(self, resp):  # pragma: no cover
            raise AssertionError

    p = _post()
    _scraper(_Boom()).enrich(p)
    assert p.proposal_status is S.ERROR


def test_error_when_billinfo_request_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class _PostFails(_Fetcher):
        def post(self, *a, **k):
            raise RuntimeError("HTTP 400")

    f = _PostFails(_fx("bill_detail_page.html"), "")
    p = _post("PRC_SYNTH_0001")
    _scraper(f).enrich(p)
    assert p.proposal_status is S.ERROR
    assert "HTTP 400" in p.proposal_note


def test_error_on_unexpected_response_content(tmp_path, monkeypatch):
    # 짧은데 안내문도 아니다 — 뭘 받은 건지 알 수 없다.
    reply = '<html><body><pre id="prntSummary">XZ9#@!</pre></body></html>'
    p, _f = _run(
        _fx("bill_detail_page.html"), reply,
        tmp_path=tmp_path, monkeypatch=monkeypatch, bill_id="PRC_SYNTH_0001",
    )
    assert p.proposal_status is S.ERROR
    assert "예상과 다름" in p.proposal_note


def test_error_when_follow_up_response_has_no_container(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    page = """
    <html><body>
      <form name="popup" action="/bill/summaryPopup.do" method="post">
        <input type="hidden" name="billId" value=""/>
      </form>
    </body></html>"""
    p, _f = _run(
        page, "<html><body>알 수 없는 응답</body></html>",
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert p.proposal_status is S.ERROR


# --- 요구 5: 정황만으로 PENDING 을 확정하지 않는다 ---


def test_todays_date_alone_does_not_make_it_pending(tmp_path, monkeypatch):
    """제안일이 오늘이어도, 컨테이너가 없으면 ERROR 다(정황은 근거가 아니다)."""
    monkeypatch.chdir(tmp_path)
    f = _Fetcher(_fx("bill_detail_page.html"), _fx("billinfo_malformed_shell.html"))
    p = _post("PRC_SYNTH_0001")
    p.date = "2026-08-06"          # 오늘
    _scraper(f).enrich(p)
    assert p.proposal_status is S.ERROR


def test_missing_committee_text_alone_does_not_make_it_pending(tmp_path, monkeypatch):
    """'소관위 미확정'·'문서 없음' 같은 문구가 페이지에 있어도 PENDING 이 아니다."""
    monkeypatch.chdir(tmp_path)
    page = """
    <html><body>
      <dl><dt>소관위원회</dt><dd>미확정</dd></dl>
      <div class="file">첨부된 문서가 없습니다.</div>
    </body></html>"""
    p, _f = _run(page, tmp_path=tmp_path, monkeypatch=monkeypatch)
    # 컨테이너가 없으므로 ERROR — 페이지 다른 곳의 '없습니다' 문구에 넘어가면 안 된다
    assert p.proposal_status is S.ERROR


# --- 요구 6: PENDING 은 Gemini 배치에서 제외 ---


def test_pending_bills_are_excluded_from_batch():
    from src.assembly_summary import summarize_assembly_bills
    from src.summarizer import Summarizer
    from tests.test_assembly_summary import _cfg, _bill, _reply, _ids_in

    available = [_bill(0), _bill(1)]
    pending = _bill(2, body="")
    pending.proposal_status = S.PENDING
    # 방어: 본문이 남아 있어도 PENDING 이면 제외되어야 한다
    stubborn = _bill(3)
    stubborn.proposal_status = S.PENDING

    posts = available + [pending, stubborn]
    s = Summarizer(_cfg())
    seen = []

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        seen.append(_ids_in(prompt, posts))
        return _reply(seen[-1])

    s._generate = _generate
    ok = summarize_assembly_bills(s, {"의안정보시스템 · 계류의안": posts})

    assert ok == 2
    assert seen == [["PRC_0000", "PRC_0001"]]
    assert pending.summary == [] and stubborn.summary == []


# --- 요구 7: 메일 문구 ---


def test_email_shows_pending_notice():
    p = _post()
    p.proposal_status = S.PENDING
    grouped = {p.source_name: [p]}

    html = build_html(grouped)
    text = build_text(grouped)

    assert ">제안이유 및 주요내용 · 등록 대기</div>" in html
    assert "의안정보시스템에 제안이유 및 주요내용이 아직 공개되지 않았습니다." in html
    assert "[제안이유 및 주요내용 · 등록 대기]" in text
    assert "의안정보시스템에 제안이유 및 주요내용이 아직 공개되지 않았습니다." in text
    # AI 생성물이 아니므로 AI 라벨·유의사항은 붙지 않는다
    assert "AI" not in text
    assert "생성형 AI" not in html


def test_error_status_does_not_show_pending_notice():
    """수집 실패를 '아직 공개되지 않았습니다'로 알리면 고장이 은폐된다."""
    p = _post()
    p.proposal_status = S.ERROR
    html = build_html({p.source_name: [p]})
    text = build_text({p.source_name: [p]})
    assert "등록 대기" not in html and "등록 대기" not in text
    assert "아직 공개되지" not in html


def test_available_post_shows_summary_not_pending_notice():
    p = _post()
    p.proposal_status = S.AVAILABLE
    p.summary = ["첫째 문장임", "둘째 문장임", "셋째 문장임"]
    html = build_html({p.source_name: [p]})
    assert ">제안이유 및 주요내용 · AI 3줄 요약</div>" in html
    assert "등록 대기" not in html


def test_pending_notice_is_escaped_and_not_for_other_sources():
    # 다른 소스는 PENDING 값을 가질 일이 없지만, 가지더라도 의안 문구를 쓰지 않는다.
    p = Post(source_key="fsc_press", source_name="금융위 · 보도자료", post_id="1",
             title="t", url="https://x")
    p.proposal_status = S.PENDING
    assert "등록 대기" not in build_text({p.source_name: [p]})


# --- 요구 8·9: 집계 ---


@pytest.mark.parametrize(
    "available,pending,failed,expect_error",
    [
        (0, 0, 3, True),    # 전부 실패 → ERROR
        (0, 3, 0, False),   # 전부 등록 대기 → ERROR 아님
        (0, 2, 1, False),   # available=0 이어도 pending>0 이면 ERROR 아님
        (1, 0, 2, False),   # 일부 성공
        (0, 0, 0, False),   # 시도 없음
    ],
)
def test_assembly_error_only_when_no_available_and_no_pending(
    available, pending, failed, expect_error, caplog
):
    import logging

    from src.main import _DetailStats, _log_run_summary
    from tests.test_run_summary import _Cfg, _llm

    stats = _DetailStats(
        attempted=available + pending + failed, succeeded=available, pending=pending
    )
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(_Cfg(_llm()), {}, _DetailStats(), stats)

    errors = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.ERROR and "의안" in r.getMessage()
    ]
    assert bool(errors) is expect_error
    assert (
        f"attempted {stats.attempted} / available {available} / "
        f"pending {pending} / failed {failed}" in caplog.text
    )


def test_pending_is_not_counted_as_failure():
    from src.main import _DetailStats

    st = _DetailStats()
    st.count(True)                    # 성공
    st.count(False, pending=True)     # 등록 대기
    st.count(False)                   # 실패
    assert (st.attempted, st.succeeded, st.pending, st.failed) == (3, 1, 1, 1)


def test_pending_does_not_trigger_overall_zero_success_error(caplog):
    import logging

    from src.main import _DetailStats, _log_run_summary
    from tests.test_run_summary import _Cfg, _llm

    # 전체 상세가 0건 성공이지만 전부 등록 대기 → 전면 실패 ERROR 를 내면 안 된다.
    overall = _DetailStats(attempted=4, succeeded=0, pending=4)
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(_Cfg(_llm()), {}, overall, _DetailStats(4, 0, 4))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
