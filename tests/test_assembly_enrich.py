"""의안 상세('제안이유 및 주요내용') 수집 테스트 — 확정된 billInfo.do 계약 기준.

네트워크를 쓰지 않는다 — 상세 HTML 을 돌려주는 가짜 fetcher 만 사용한다.

2026-08 Playwright 캡처로 확정된 계약:
    GET  billDetailPage.do?billId=...          (초기 HTML 에 제안이유 없음)
    POST https://likms.assembly.go.kr/bill/bi/bill/detail/billInfo.do
      payload : 상세 HTML form#form 의 named hidden input 전체(URL-encoded)
      header  : X-CSRF-TOKEN = meta[name="_csrf"], Referer = 상세 URL
      응답    : HTML, 본문은 pre#prntSummary
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from src.config import SourceConfig
from src.models import Post, ProposalContentStatus as S
from src.scrapers.assembly import (
    _BILLINFO_ENDPOINT,
    _CSRF_HEADER,
    AssemblyBillScraper,
    SummaryRequestError,
)

_BILL_ID = "PRC_A2Z5C1D0"
_TOKEN = "csrf-token-value-should-never-be-logged"
_REASON = (
    "현행법은 가상자산사업자의 이용자 예치금 보호 의무를 명확히 규정하고 있지 아니함. "
    "이에 예치금 분리보관과 지급보장 계약 체결을 의무화하려는 것임(안 제7조)."
)


def _detail_page(
    bill_id=_BILL_ID, token=_TOKEN, *, with_form=True, with_meta=True, extra_inputs=""
) -> str:
    """확정된 계약의 구조를 그대로 가진 상세 HTML."""
    meta = f'<meta name="_csrf" content="{token}"/>' if with_meta else ""
    form = (
        f'<form id="form" action="" method="get">'
        f'<input type="hidden" name="billId" value="{bill_id}"/>'
        f'<input type="hidden" name="ageFrom" value="22"/>'
        f'<input type="hidden" name="ageTo" value="22"/>'
        f'<input type="text" name="notHidden" value="ignored"/>'
        f'<input type="hidden" value="이름없음"/>'
        f"{extra_inputs}"
        f"</form>"
        if with_form
        else ""
    )
    return (
        f"<html><head><meta charset='utf-8'>{meta}</head><body>{form}"
        f'<div id="tab_billInfo_sect"></div></body></html>'
    )


# 정상 심사정보(billInfo.do) 응답의 뼈대. 제안이유 등록 여부와 무관하게 항상 있다.
_SHELL = (
    '<div id="tab_billInfo_sect">'
    '<form id="billInfoForm" method="post" action="">'
    '<input type="hidden" name="billId" value="PRC_A2Z5C1D0"/></form>'
    "<dl><dt>의안번호</dt><dd>2200123</dd></dl>"
    '<div id="stage_list"><ul><li>접수</li></ul></div>'
    '<div id="rcp_list"><table><tbody>'
    '<tr id="insc-rcp-row"><td>2026-08-06</td></tr></tbody></table></div>'
)


def _billinfo_response(text=_REASON, *, section=True, shell=True) -> str:
    """billInfo.do 응답. section=False 면 제안이유 섹션이 아예 없다(등록 대기)."""
    inner = _SHELL if shell else '<div id="tab_billInfo_sect"></div>'
    if section:
        inner += (
            '<div id="prntsummary-sect"><h4>제안이유 및 주요내용</h4>'
            f'<pre id="prntSummary">{text}</pre></div>'
        )
    return f"<html><body>{inner}</div></body></html>"


def _scraper(fetcher):
    return AssemblyBillScraper(
        SourceConfig(
            key="assembly_bill",
            name="의안정보시스템 · 계류의안",
            type="assembly_bill",
            list_url="https://likms.assembly.go.kr/bill/bi/bill/state/mooringBillPage.do",
            extra={"api_service": "svc"},
        ),
        fetcher=fetcher,
    )


def _post(bill_id=_BILL_ID) -> Post:
    return Post(
        source_key="assembly_bill",
        source_name="의안정보시스템 · 계류의안",
        post_id=bill_id,
        title="가상자산이용자보호법 일부개정법률안 (홍길동)",
        url=f"https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={bill_id}",
        date="2026-07-01",
    )


class _Resp:
    """requests.Response 흉내 — 실제 코드가 최종 URL(resp.url)을 보기 때문에 필요하다."""

    def __init__(self, kind, n, url):
        self.kind = kind
        self.n = n
        self.url = url


class _Fetcher:
    """GET/POST 를 기록하고 미리 정한 HTML 을 돌려주는 가짜 fetcher."""

    def __init__(self, get_html, post_html=""):
        self._get_html = get_html
        self._post_html = post_html
        self.gets = []
        self.posts = []

    # redirect 를 흉내내려면 응답의 '최종 URL' 이 요청 URL 과 달라야 한다.
    final_url = None

    def get(self, url, referer=None, params=None, headers=None):
        self.gets.append(
            {"url": url, "params": params, "referer": referer, "headers": headers or {}}
        )
        return _Resp("get", len(self.gets), self.final_url or url)

    def post(self, url, referer=None, data=None, headers=None):
        self.posts.append(
            {"url": url, "data": data or {}, "referer": referer, "headers": headers or {}}
        )
        return _Resp("post", len(self.posts), url)

    def text(self, resp):
        return self._get_html if resp.kind == "get" else self._post_html


def _run(get_html, post_html="", tmp_path=None, monkeypatch=None, bill_id=_BILL_ID):
    if tmp_path is not None:
        monkeypatch.chdir(tmp_path)
    f = _Fetcher(get_html, post_html)
    p = _post(bill_id)
    _scraper(f).enrich(p)
    return p, f


# --- 확정된 요청 계약 ---


def test_posts_to_confirmed_endpoint_with_form_payload_and_csrf_header():
    p, f = _run(_detail_page(), _billinfo_response())

    assert len(f.posts) == 1
    sent = f.posts[0]
    # endpoint 와 method
    assert sent["url"] == _BILLINFO_ENDPOINT
    assert sent["url"] == (
        "https://likms.assembly.go.kr/bill/bi/bill/detail/billInfo.do"
    )
    # payload = form#form 의 named hidden input 전체
    assert sent["data"] == {"billId": _BILL_ID, "ageFrom": "22", "ageTo": "22"}
    # hidden 이 아니거나 name 이 없는 input 은 보내지 않는다
    assert "notHidden" not in sent["data"]
    # CSRF 헤더
    assert sent["headers"] == {_CSRF_HEADER: _TOKEN}
    assert _CSRF_HEADER == "X-CSRF-TOKEN"
    # Referer 는 해당 상세 URL
    assert sent["referer"] == p.url
    # 결과
    assert p.proposal_status is S.AVAILABLE
    assert "예치금 분리보관" in p.body


def test_form_action_and_method_are_not_replayed():
    """form#form 은 payload 원천일 뿐이다 — 빈 action·기본 GET 을 replay 하면 안 된다."""
    _p, f = _run(_detail_page(), _billinfo_response())
    assert len(f.gets) == 1                      # 상세 GET 1회뿐(추가 GET 없음)
    assert f.gets[0]["params"] is None
    assert len(f.posts) == 1
    assert f.posts[0]["url"] == _BILLINFO_ENDPOINT   # 상세 URL 로 되쏘지 않는다


def test_build_summary_request_shape_excludes_values():
    """캡처 도구가 저장소에 남기는 '형태'에는 토큰·세션값이 들어가면 안 된다."""
    sc = _scraper(_Fetcher(""))
    req = sc.build_summary_request(BeautifulSoup(_detail_page(), "lxml"), _post())

    shape = req.shape()
    assert shape["method"] == "post"
    assert shape["action"] == _BILLINFO_ENDPOINT
    assert shape["data_keys"] == ["ageFrom", "ageTo", "billId"]
    assert shape["header_keys"] == [_CSRF_HEADER]
    assert _TOKEN not in repr(shape)


def test_extra_hidden_inputs_are_all_forwarded():
    extra = '<input type="hidden" name="tabMenuType" value="billSimpleAndCost"/>'
    _p, f = _run(_detail_page(extra_inputs=extra), _billinfo_response())
    assert f.posts[0]["data"]["tabMenuType"] == "billSimpleAndCost"


# --- 요청을 만들 수 없으면 보내지 않는다 ---


def test_no_form_means_no_request_and_error(tmp_path, monkeypatch):
    p, f = _run(
        _detail_page(with_form=False), _billinfo_response(),
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert f.posts == []
    assert p.proposal_status is S.ERROR
    assert "form#form" in p.proposal_note
    assert p.body == ""


def test_missing_csrf_means_no_request_and_error(tmp_path, monkeypatch):
    p, f = _run(
        _detail_page(with_meta=False), _billinfo_response(),
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert f.posts == []
    assert p.proposal_status is S.ERROR
    assert "_csrf" in p.proposal_note


def test_empty_csrf_content_means_no_request_and_error(tmp_path, monkeypatch):
    p, f = _run(
        _detail_page(token=""), _billinfo_response(),
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert f.posts == []
    assert p.proposal_status is S.ERROR


def test_bill_id_mismatch_means_no_request_and_error(tmp_path, monkeypatch):
    """폼의 billId 가 목록의 BILL_ID 와 다르면 남의 의안을 조회하게 된다 — 보내지 않는다."""
    p, f = _run(
        _detail_page(bill_id="PRC_SOMEONE_ELSE"), _billinfo_response(),
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert f.posts == []
    assert p.proposal_status is S.ERROR
    assert "다름" in p.proposal_note
    assert p.body == ""


def test_missing_bill_id_field_means_no_request_and_error(tmp_path, monkeypatch):
    page = (
        '<html><head><meta name="_csrf" content="t"/></head><body>'
        '<form id="form"><input type="hidden" name="ageFrom" value="22"/></form>'
        "</body></html>"
    )
    p, f = _run(page, _billinfo_response(), tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert f.posts == []
    assert p.proposal_status is S.ERROR
    assert "billId" in p.proposal_note


def test_form_without_named_hidden_inputs_means_no_request(tmp_path, monkeypatch):
    page = (
        '<html><head><meta name="_csrf" content="t"/></head><body>'
        '<form id="form"><input type="hidden" value="이름없음"/></form>'
        "</body></html>"
    )
    p, f = _run(page, _billinfo_response(), tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert f.posts == []
    assert p.proposal_status is S.ERROR


@pytest.mark.parametrize(
    "page",
    [
        _detail_page(with_form=False),
        _detail_page(with_meta=False),
        _detail_page(bill_id="PRC_OTHER"),
    ],
)
def test_build_summary_request_raises_instead_of_returning_none(page):
    sc = _scraper(_Fetcher(""))
    with pytest.raises(SummaryRequestError):
        sc.build_summary_request(BeautifulSoup(page, "lxml"), _post())


# --- 응답 판정 ---


def test_available_requires_at_least_20_chars():
    short = "짧은 내용"                       # 20자 미만
    assert len(short) < 20
    p, _f = _run(_detail_page(), _billinfo_response(short))
    assert p.proposal_status is not S.AVAILABLE
    assert p.body == ""


def test_available_at_20_chars():
    text = "가" * 20
    p, _f = _run(_detail_page(), _billinfo_response(text))
    assert p.proposal_status is S.AVAILABLE
    assert p.body == text


def test_malformed_shell_response_is_error(tmp_path, monkeypatch):
    """정상 심사정보 shell 자체가 없으면 ERROR — '등록 전'인지 구분할 근거가 없다."""
    p, _f = _run(
        _detail_page(), "<html><body>알 수 없는 응답</body></html>",
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert p.proposal_status is S.ERROR
    assert "정상 심사정보 응답이 아님" in p.proposal_note
    assert (tmp_path / "debug").exists()


# --- POST 응답 selector 는 fail-closed: pre#prntSummary 만 인정 ---


@pytest.mark.parametrize(
    "extra",
    [
        '<div id="summaryContentDiv"></div>',                     # 빈 폴백 컨테이너만
        '<div id="summaryContentDiv">본문임</div>',                # 내용까지 있어도
        '<div id="prntSummary"></div>',                           # pre 가 아닌 태그
        '<div id="prntSummary">현행법은 예치금 보호 의무를 규정하지 아니함</div>',
        '<span id="summaryContentDiv"><span id="prntSummary"></span></span>',
    ],
)
def test_fallback_selectors_are_not_accepted_in_billinfo_response(
    tmp_path, monkeypatch, extra
):
    """billInfo.do 응답에는 폴백 selector 를 인정하지 않는다.

    정상 shell 이 있어도 #summaryContentDiv / #prntSummary(비-pre) 는 본문으로 쓰지
    않는다. 폴백으로 받아 주면 마크업 변경이 조용히 통과한다.
    다만 이들만으로는 '섹션이 생성됐다'는 증거가 아니므로, 정상 shell 이 온전하면
    등록 대기(PENDING)로 본다 — 라이브에서 등록 전 응답에는 섹션이 아예 없다.
    """
    p, _f = _run(
        _detail_page(),
        _billinfo_response(section=False).replace("</div></body>", f"{extra}</div></body>"),
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert p.body == ""                       # 폴백 selector 를 본문으로 쓰지 않는다
    assert p.proposal_status is S.PENDING     # 정상 shell + 섹션 없음


def test_summary_section_without_pre_is_error(tmp_path, monkeypatch):
    """#prntsummary-sect 는 있는데 pre#prntSummary 만 없으면 마크업 변경 = ERROR."""
    body = _billinfo_response(section=False).replace(
        "</div></body>",
        '<div id="prntsummary-sect"><div class="summaryBody">본문</div></div></div></body>',
    )
    p, _f = _run(_detail_page(), body, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert p.proposal_status is S.ERROR, p.proposal_note
    assert "#prntsummary-sect" in p.proposal_note


def test_summary_marker_without_selector_is_error(tmp_path, monkeypatch):
    """제안이유 표식은 있는데 예상 selector 가 없으면 마크업 변경 = ERROR."""
    body = _billinfo_response(section=False).replace(
        "</div></body>",
        "<div><h4>제안이유 및 주요내용</h4><p>현행법은…</p></div></div></body>",
    )
    p, _f = _run(_detail_page(), body, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert p.proposal_status is S.ERROR, p.proposal_note
    assert "표식은 있는데" in p.proposal_note


def test_normal_shell_without_summary_section_is_pending(tmp_path, monkeypatch):
    """라이브 확인(Action #13): 등록 전에는 섹션과 pre 가 **아예 생성되지 않는다**."""
    p, f = _run(
        _detail_page(), _billinfo_response(section=False),
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert p.proposal_status is S.PENDING, p.proposal_note
    assert len(f.posts) == 1
    assert p.body == ""
    assert not (tmp_path / "debug").exists()   # 등록 대기는 덤프하지 않는다


def test_empty_pre_prnt_summary_is_pending(tmp_path, monkeypatch):
    """pre#prntSummary 가 존재하고 완전히 비어 있어도 PENDING 이다."""
    for text in ("", "   ", "\n\n"):
        p, f = _run(
            _detail_page(), _billinfo_response(text),
            tmp_path=tmp_path, monkeypatch=monkeypatch,
        )
        assert p.proposal_status is S.PENDING, (repr(text), p.proposal_note)
        assert len(f.posts) == 1
        assert p.body == ""


def test_empty_pre_without_normal_shell_is_error_not_pending(tmp_path, monkeypatch):
    """빈 pre 만 남고 정상 심사정보 shell 이 없으면 등록 대기가 아니라 ERROR 다.

    회귀: 오류·중간 페이지가 빈 pre 하나만 남긴 채 오면 이전 코드는 PENDING 으로
    판정했다. 그러면 덤프도 남지 않고 실패 집계에도 안 잡힌 채 그 의안이 seen 으로
    확정되어, 제안이유를 영영 받지 못한다.
    """
    for text in ("", "   ", "\n\n"):
        p, _f = _run(
            _detail_page(), _billinfo_response(text, shell=False),
            tmp_path=tmp_path, monkeypatch=monkeypatch,
        )
        assert p.proposal_status is S.ERROR, (repr(text), p.proposal_note)
        assert "정상 심사정보" in (p.proposal_note or "")
        assert (tmp_path / "debug").exists()   # ERROR 는 덤프를 남긴다


def test_found_body_does_not_require_normal_shell(tmp_path, monkeypatch):
    """본문을 이미 확보했다면 주변 마크업이 바뀌어도 AVAILABLE 이다.

    shell 검사는 '비어 있음'을 PENDING 으로 인정할 때만 필요하다. 본문이 손에 있는데
    주변 구조가 달라졌다는 이유로 정상 수집을 실패로 뒤집으면 손해만 본다.
    """
    p, _f = _run(
        _detail_page(), _billinfo_response(shell=False),
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert p.proposal_status is S.AVAILABLE, p.proposal_note
    assert "예치금 분리보관" in p.body


def test_short_leftover_text_in_pre_is_error_not_pending(tmp_path, monkeypatch):
    """pre 에 짧은 잔여 텍스트가 있으면 뭘 받은 건지 알 수 없다 → ERROR."""
    for notice in ("등록 예정입니다.", "준비 중입니다", "자료가 없습니다"):
        p, _f = _run(
            _detail_page(), _billinfo_response(notice),
            tmp_path=tmp_path, monkeypatch=monkeypatch,
        )
        assert p.proposal_status is S.ERROR, (notice, p.proposal_note)


def test_initial_get_keeps_fallback_selectors_for_legacy_pages():
    """초기 상세 GET 은 구형 페이지 지원을 위해 폴백 selector 를 유지한다."""
    long_text = "현행법은 가상자산사업자의 예치금 분리보관 의무를 규정하지 아니함."
    for container in (
        f'<div id="summaryContentDiv">{long_text}</div>',
        f'<div id="prntSummary">{long_text}</div>',
        f'<pre id="prntSummary">{long_text}</pre>',
    ):
        p, f = _run(f"<html><body>{container}</body></html>")
        assert p.proposal_status is S.AVAILABLE, container
        assert f.posts == []          # inline 이면 추가 요청 없음


def test_not_found_response_is_error(tmp_path, monkeypatch):
    p, _f = _run(
        _detail_page(), "<html><body>해당 의안 정보가 존재하지 않습니다.</body></html>",
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert p.proposal_status is S.ERROR


def test_post_failure_is_error_not_pending(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class _PostFails(_Fetcher):
        def post(self, *a, **k):
            raise RuntimeError("403 Forbidden")

    f = _PostFails(_detail_page(), _billinfo_response())
    p = _post()
    _scraper(f).enrich(p)
    assert p.proposal_status is S.ERROR
    assert "403" in p.proposal_note


# --- 구형 페이지(inline) 지원 유지 ---


def test_inline_summary_is_taken_without_extra_request():
    html = f'<html><body><pre id="prntSummary">{_REASON}</pre></body></html>'
    p, f = _run(html)
    assert p.proposal_status is S.AVAILABLE
    assert "예치금 분리보관" in p.body
    assert len(f.gets) == 1
    assert f.posts == []          # 이미 있으면 추가 요청 없음
    assert p.details == []        # 제안이유는 details 에 담지 않는다


def test_empty_initial_html_is_not_pending(tmp_path, monkeypatch):
    """초기 HTML 이 비었다는 이유로 PENDING 처리하면 안 된다 — 그게 정상이다."""
    page = _detail_page(with_form=False) .replace(
        '<div id="tab_billInfo_sect"></div>',
        '<div id="summaryContentDiv"></div>',
    )
    p, _f = _run(page, tmp_path=tmp_path, monkeypatch=monkeypatch)
    # form#form 이 없으므로 요청을 만들 수 없다 → ERROR (PENDING 아님)
    assert p.proposal_status is S.ERROR


def test_empty_initial_container_still_posts_to_billinfo():
    """초기 컨테이너가 비어 있어도 확정된 endpoint 를 반드시 물어본다."""
    page = _detail_page().replace(
        '<div id="tab_billInfo_sect"></div>', '<pre id="prntSummary"></pre>'
    )
    p, f = _run(page, _billinfo_response())
    assert len(f.posts) == 1
    assert p.proposal_status is S.AVAILABLE
    assert "예치금 분리보관" in p.body


# --- 실패 격리 / 진단 ---


def test_enrich_never_raises_so_other_bills_continue():
    class _Broken:
        def get(self, *a, **k):
            raise RuntimeError("상세 페이지 500")

        def text(self, resp):  # pragma: no cover
            raise AssertionError

    broken = _post("PRC_BAD")
    _scraper(_Broken()).enrich(broken)          # 예외가 밖으로 나오면 안 된다
    assert broken.proposal_status is S.ERROR

    good, _f = _run(_detail_page(), _billinfo_response())
    assert good.proposal_status is S.AVAILABLE


def test_missing_summary_dumps_debug_per_bill(tmp_path, monkeypatch):
    _p, _f = _run(
        _detail_page(), "<html><body>제안이유 없음</body></html>",
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    dump = tmp_path / "debug" / f"assembly_bill_detail_{_BILL_ID}.txt"
    assert dump.exists()
    assert "제안이유 없음" in dump.read_text(encoding="utf-8")


def test_debug_dump_filename_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _Fetcher(_detail_page(bill_id="../../etc/passwd"), "")
    p = _post(bill_id="../../etc/passwd")
    _scraper(f).enrich(p)

    written = list((tmp_path / "debug").iterdir())
    assert len(written) == 1
    assert written[0].resolve().parent == (tmp_path / "debug").resolve()
    assert "/" not in written[0].name
    assert not (tmp_path / "etc").exists()


def test_token_is_never_written_to_debug_dump(tmp_path, monkeypatch):
    """토큰 값이 fixture·덤프에 남으면 안 된다."""
    _p, _f = _run(
        _detail_page(), "<html><body>알 수 없는 응답</body></html>",
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    dump = (tmp_path / "debug" / f"assembly_bill_detail_{_BILL_ID}.txt").read_text(
        encoding="utf-8"
    )
    # 덤프에는 POST 응답만 담긴다(토큰이 든 상세 HTML 이 아니라)
    assert _TOKEN not in dump


def test_token_is_never_logged(caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        _run(_detail_page(), _billinfo_response(""))
    assert _TOKEN not in caplog.text


# --- 받아온 페이지가 정말 이 의안의 것인가 (inline 채택 전 확인) ---


_INLINE = f'<pre id="prntSummary">{_REASON}</pre>'


def _inline_page(bill_id=_BILL_ID, *, with_form=True) -> str:
    """제안이유가 상세 HTML 에 이미 들어 있는 구형 페이지."""
    return _detail_page(bill_id, with_form=with_form).replace(
        '<div id="tab_billInfo_sect"></div>', _INLINE
    )


def test_inline_rejected_when_link_url_points_to_another_bill(tmp_path, monkeypatch):
    """LINK_URL 이 다른 의안을 가리키면 그 페이지의 제안이유를 쓰면 안 된다.

    회귀: inline 경로는 build_summary_request 의 billId 대조 **전에** 값을 채우고
    끝나 버려서, A 의안 알림에 B 의안의 제안이유가 실릴 수 있었다.
    """
    monkeypatch.chdir(tmp_path)
    f = _Fetcher(_inline_page("PRC_OTHER_BILL"))
    p = _post()                                    # 목록의 BILL_ID 는 _BILL_ID
    p.url = "https://likms.assembly.go.kr/bill/bi/other.do?billId=PRC_OTHER_BILL"
    _scraper(f).enrich(p)

    assert p.proposal_status is S.ERROR, p.proposal_note
    assert "다른 의안" in p.proposal_note
    assert p.body == ""                            # 남의 본문을 싣지 않는다
    assert f.posts == []                           # 요청도 보내지 않는다


def test_inline_rejected_when_redirected_to_another_bill(tmp_path, monkeypatch):
    """redirect 로 다른 의안 페이지에 끌려간 경우도 막는다(요청 URL 만 봐서는 못 잡는다)."""
    monkeypatch.chdir(tmp_path)
    f = _Fetcher(_inline_page(with_form=False))
    f.final_url = (
        "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_REDIRECTED"
    )
    p = _post()
    _scraper(f).enrich(p)

    assert p.proposal_status is S.ERROR, p.proposal_note
    assert "다른 의안" in p.proposal_note
    assert p.body == ""


def test_inline_rejected_when_bill_id_cannot_be_confirmed(tmp_path, monkeypatch):
    """근거가 하나도 없으면 확인된 것이 아니다 — 채택하지 않는다(fail-closed)."""
    monkeypatch.chdir(tmp_path)
    f = _Fetcher(f"<html><body>{_INLINE}</body></html>")   # form 없음
    p = _post()
    p.url = "https://likms.assembly.go.kr/bill/bi/unknownDetail.do"   # billId 없음
    _scraper(f).enrich(p)

    assert p.proposal_status is S.ERROR, p.proposal_note
    assert "확인할 수 없음" in p.proposal_note
    assert p.body == ""


def test_inline_accepted_when_form_confirms_the_bill():
    """form#form 의 billId 가 일치하면 채택한다(구형 페이지 지원은 유지)."""
    p, f = _run(_inline_page())
    assert p.proposal_status is S.AVAILABLE, p.proposal_note
    assert "예치금 분리보관" in p.body
    assert f.posts == []


def test_inline_accepted_when_only_the_url_confirms_the_bill():
    """form 이 없어도 요청·최종 URL 의 billId 가 일치하면 확인된 것이다."""
    p, f = _run(_inline_page(with_form=False))
    assert p.proposal_status is S.AVAILABLE, p.proposal_note
    assert "예치금 분리보관" in p.body
    assert f.posts == []


def test_mismatch_blocks_the_billinfo_path_too(tmp_path, monkeypatch):
    """inline 이 없더라도 남의 페이지면 billInfo.do 를 물어볼 이유가 없다."""
    monkeypatch.chdir(tmp_path)
    f = _Fetcher(_detail_page())
    f.final_url = (
        "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_REDIRECTED"
    )
    p = _post()
    _scraper(f).enrich(p)

    assert p.proposal_status is S.ERROR, p.proposal_note
    assert f.posts == []


# --- 진단 덤프에 살아 있는 토큰이 남으면 안 된다 ---
#
# debug/ 는 verify 워크플로가 아티팩트로 업로드한다. 덤프가 남는 때가 곧 '상세 HTML 을
# 통째로 저장하는' 때이므로, 그 안의 CSRF 토큰·세션값이 그대로 실려 나갈 수 있다.


def _dump_text(tmp_path, bill_id=_BILL_ID) -> str:
    return (tmp_path / "debug" / f"assembly_bill_detail_{bill_id}.txt").read_text(
        encoding="utf-8"
    )


def test_detail_html_dump_redacts_csrf_token(tmp_path, monkeypatch):
    """상세 HTML 을 덤프하는 실패 경로에서 meta[name=_csrf] 값이 지워져야 한다."""
    # billId hidden 만 없는 폼 → 요청을 만들 수 없어 상세 HTML 을 덤프한다.
    page = (
        f"<html><head><meta name='_csrf' content='{_TOKEN}'/></head><body>"
        '<form id="form"><input type="hidden" name="ageFrom" value="22"/></form>'
        '<div id="tab_billInfo_sect"></div></body></html>'
    )
    p, f = _run(page, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert p.proposal_status is S.ERROR
    assert f.posts == []

    dump = _dump_text(tmp_path)
    assert _TOKEN not in dump
    assert 'name="_csrf"' in dump          # 이름·구조는 진단에 필요하므로 남는다
    assert 'name="ageFrom"' in dump
    assert 'value="22"' in dump            # 공개 값은 지우지 않는다


def test_detail_html_dump_redacts_hidden_token_fields(tmp_path, monkeypatch):
    """hidden input 에 담긴 토큰 값도 이름 기준으로 지운다."""
    secret = "9f1c-SESSION-TOKEN-VALUE"
    page = (
        f"<html><head><meta name='_csrf' content='{_TOKEN}'/></head><body>"
        '<form id="form">'
        '<input type="hidden" name="ageFrom" value="22"/>'
        f'<input type="hidden" name="sessionToken" value="{secret}"/>'
        "</form>"
        '<div id="tab_billInfo_sect"></div></body></html>'
    )
    _p, _f = _run(page, tmp_path=tmp_path, monkeypatch=monkeypatch)
    dump = _dump_text(tmp_path)
    assert secret not in dump
    assert _TOKEN not in dump
    assert 'name="sessionToken"' in dump


def test_detail_html_dump_redacts_inline_script_and_session_values(
    tmp_path, monkeypatch
):
    """인라인 JS 에 박힌 토큰과 쿠키 문자열도 남지 않는다."""
    page = (
        f"<html><head><meta name='_csrf' content='{_TOKEN}'/></head><body>"
        '<form id="form"><input type="hidden" name="ageFrom" value="22"/></form>'
        f'<script>var csrf = "{_TOKEN}"; var s = "JSESSIONID=ABC123DEF456";</script>'
        '<div id="tab_billInfo_sect"></div></body></html>'
    )
    _p, _f = _run(page, tmp_path=tmp_path, monkeypatch=monkeypatch)
    dump = _dump_text(tmp_path)
    assert _TOKEN not in dump
    assert "ABC123DEF456" not in dump


def test_dump_keeps_csrf_header_and_parameter_names(tmp_path, monkeypatch):
    """_csrf_header / _csrf_parameter 의 content 는 토큰이 아니라 '이름'이라 남긴다."""
    page = (
        "<html><head>"
        f"<meta name='_csrf' content='{_TOKEN}'/>"
        "<meta name='_csrf_header' content='X-CSRF-TOKEN'/>"
        "<meta name='_csrf_parameter' content='_csrf'/>"
        "</head><body>"
        '<form id="form"><input type="hidden" name="ageFrom" value="22"/></form>'
        '<div id="tab_billInfo_sect"></div></body></html>'
    )
    _p, _f = _run(page, tmp_path=tmp_path, monkeypatch=monkeypatch)
    dump = _dump_text(tmp_path)
    assert "X-CSRF-TOKEN" in dump
    assert _TOKEN not in dump


def test_billinfo_response_dump_still_readable(tmp_path, monkeypatch):
    """정화가 진단을 망치면 안 된다 — 응답 본문·구조는 그대로 남는다."""
    _p, _f = _run(
        _detail_page(), "<html><body><div>알 수 없는 응답 구조</div></body></html>",
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    dump = _dump_text(tmp_path)
    assert "알 수 없는 응답 구조" in dump
