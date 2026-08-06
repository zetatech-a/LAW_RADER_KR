"""의안 상세('제안이유 및 주요내용') 수집 테스트.

네트워크를 쓰지 않는다 — 상세 HTML fixture 를 돌려주는 가짜 fetcher 만 사용한다.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.models import Post
from src.scrapers.assembly import AssemblyBillScraper

_BILL_ID = "PRC_A2Z5C1D0"
_DETAIL_URL = f"https://likms.assembly.go.kr/bill/billDetail.do?billId={_BILL_ID}"
_REASON = (
    "현행법은 가상자산사업자의 이용자 예치금 보호 의무를 명확히 규정하고 있지 아니함. "
    "이에 예치금 분리보관과 지급보장 계약 체결을 의무화하려는 것임(안 제7조)."
)


def _scraper(fetcher):
    src = SourceConfig(
        key="assembly_bill",
        name="의안정보시스템 · 계류의안",
        type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/bi/bill/state/mooringBillPage.do",
        extra={"api_service": "svc"},
    )
    sc = AssemblyBillScraper(src, fetcher=fetcher)
    return sc


def _post(bill_id=_BILL_ID) -> Post:
    return Post(
        source_key="assembly_bill",
        source_name="의안정보시스템 · 계류의안",
        post_id=bill_id,
        title="가상자산이용자보호법 일부개정법률안 (홍길동)",
        url=f"https://likms.assembly.go.kr/bill/billDetail.do?billId={bill_id}",
        date="2026-07-01",
    )


class _Fetcher:
    """GET/POST 를 기록하고 미리 정한 HTML 을 돌려주는 가짜 fetcher."""

    def __init__(self, get_html, post_html=""):
        self._get_html = get_html
        self._post_html = post_html
        self.gets = []
        self.posts = []

    def get(self, url, referer=None, params=None, headers=None):
        self.gets.append({"url": url, "params": params, "headers": headers or {}})
        return ("get", len(self.gets))

    def post(self, url, referer=None, data=None, headers=None):
        self.posts.append({"url": url, "data": data or {}, "headers": headers or {}})
        return ("post", len(self.posts))

    def text(self, resp):
        return self._get_html if resp[0] == "get" else self._post_html


# --- 1) 상세 HTML 에 제안이유가 바로 있는 경우 ---


def test_inline_summary_is_taken_without_extra_request():
    html = f"""
    <html><body>
      <div class="tabCont"><pre id="prntSummary">{_REASON}</pre></div>
    </body></html>"""
    f = _Fetcher(html)
    p = _post()
    _scraper(f).enrich(p)

    assert "예치금 분리보관" in p.body
    assert len(f.gets) == 1      # 상세 1회만
    assert f.posts == []         # 추가 요청 없음
    assert p.details == []       # 제안이유는 details 에 담지 않는다


def test_empty_summary_container_is_not_treated_as_found():
    # 값이 채워지기 전의 빈 컨테이너를 '수집 성공'으로 보면 안 된다.
    html = """<html><body><div id="summaryContentDiv">   </div></body></html>"""
    f = _Fetcher(html)
    p = _post()
    _scraper(f).enrich(p)
    assert p.body == ""


# --- 2) 별도 요청(폼 재전송)으로 채워지는 경우 ---

_FORM_PAGE = f"""
<html><head>
  <meta name="_csrf_header" content="X-CSRF-TOKEN"/>
  <meta name="_csrf_parameter" content="_csrf"/>
  <meta name="_csrf" content="tok-abc-123"/>
</head><body>
  <form name="searchForm" action="/bill/bi/search/searchList.do" method="post">
    <input type="hidden" name="keyword" value=""/>
  </form>
  <form id="summaryForm" action="/bill/summaryPopup.do" method="post">
    <input type="hidden" name="billId" value=""/>
    <input type="hidden" name="sessionToken" value="s-9"/>
    <input type="text" name="notHidden" value="ignored"/>
    <input type="hidden" value="이름없음"/>
  </form>
  <div id="summaryContentDiv"></div>
</body></html>"""

_POST_RESULT = f"""<html><body><pre id="prntSummary">{_REASON}</pre></body></html>"""


def test_form_is_resubmitted_with_all_hidden_inputs_and_csrf():
    f = _Fetcher(_FORM_PAGE, _POST_RESULT)
    p = _post()
    _scraper(f).enrich(p)

    assert "예치금 분리보관" in p.body
    assert len(f.posts) == 1
    sent = f.posts[0]
    # POST URL 은 폼의 action 을 그대로 따른다(코드에 박지 않는다)
    assert sent["url"] == "https://likms.assembly.go.kr/bill/summaryPopup.do"
    # hidden input 전체가 실린다
    assert sent["data"]["sessionToken"] == "s-9"
    # 폼에 있던 빈 의안 ID 칸은 채워 준다
    assert sent["data"]["billId"] == _BILL_ID
    # CSRF: meta 가 지정한 파라미터명·헤더명을 그대로 쓴다
    assert sent["data"]["_csrf"] == "tok-abc-123"
    assert sent["headers"]["X-CSRF-TOKEN"] == "tok-abc-123"
    # hidden 이 아니거나 name 이 없는 input 은 보내지 않는다
    assert "notHidden" not in sent["data"]
    # 무관한 폼(searchForm)의 필드가 섞이지 않는다
    assert "keyword" not in sent["data"]


def test_csrf_hidden_input_is_carried_without_meta():
    # CSRF 가 hidden input 으로만 오는 사이트도 있다 — 이름을 몰라도 그대로 실린다.
    page = """
    <html><body>
      <form id="summaryForm" action="/bill/summaryPopup.do" method="post">
        <input type="hidden" name="billId" value="PRC_A2Z5C1D0"/>
        <input type="hidden" name="OWASP_CSRFTOKEN" value="zzz-999"/>
      </form>
    </body></html>"""
    f = _Fetcher(page, _POST_RESULT)
    _scraper(f).enrich(_post())
    assert f.posts[0]["data"]["OWASP_CSRFTOKEN"] == "zzz-999"


def _form_page(method_attr: str) -> str:
    """method 속성만 다른 동일한 폼 페이지."""
    return f"""
    <html><body>
      <form id="summaryForm" action="/bill/summaryPopup.do"{method_attr}>
        <input type="hidden" name="billId" value=""/>
      </form>
    </body></html>"""


def _follow_get(f):
    """두 번째 GET(후속 요청)만 제안이유를 돌려주도록 바꾼다."""
    original = f.text

    def _text(resp):
        if resp[0] == "get" and resp[1] == 2:
            return _POST_RESULT
        return original(resp)

    f.text = _text


def test_form_method_post_is_sent_as_post():
    f = _Fetcher(_form_page(' method="post"'), _POST_RESULT)
    p = _post()
    _scraper(f).enrich(p)

    assert len(f.posts) == 1
    assert len(f.gets) == 1                     # 상세 GET 1회뿐
    assert f.posts[0]["data"]["billId"] == _BILL_ID
    assert "예치금 분리보관" in p.body


def test_form_method_get_is_sent_as_get_with_params():
    f = _Fetcher(_form_page(' method="GET"'), "")   # 대소문자 무관
    _follow_get(f)
    p = _post()
    _scraper(f).enrich(p)

    assert f.posts == []                        # POST 하지 않는다
    assert len(f.gets) == 2
    assert f.gets[1]["params"]["billId"] == _BILL_ID
    assert "예치금 분리보관" in p.body


def test_form_without_method_defaults_to_get():
    # HTML 표준상 form 의 method 기본값은 GET 이다. POST 로 보내면 브라우저와 다른
    # 요청이 되어 405/빈 응답을 받는다.
    f = _Fetcher(_form_page(""), "")
    _follow_get(f)
    p = _post()
    _scraper(f).enrich(p)

    assert f.posts == []
    assert len(f.gets) == 2
    assert f.gets[1]["params"]["billId"] == _BILL_ID
    assert "예치금 분리보관" in p.body


@pytest.mark.parametrize("method_attr", ['', ' method=""', ' method="  "', ' method="PUT"'])
def test_unknown_or_missing_method_falls_back_to_get(method_attr):
    # 빈 값·공백·지원하지 않는 method 도 표준 기본값(GET)으로 처리한다.
    f = _Fetcher(_form_page(method_attr), "")
    _follow_get(f)
    _scraper(f).enrich(_post())
    assert f.posts == []
    assert len(f.gets) == 2


def test_build_summary_request_shape_excludes_values():
    # 캡처 도구가 저장소에 남기는 '형태'에는 토큰·세션값이 들어가면 안 된다.
    from bs4 import BeautifulSoup

    f = _Fetcher(_FORM_PAGE, _POST_RESULT)
    sc = _scraper(f)
    p = _post()
    req = sc.build_summary_request(BeautifulSoup(_FORM_PAGE, "lxml"), p)

    assert req is not None
    shape = req.shape()
    assert shape["method"] == "post"
    assert shape["action"] == "https://likms.assembly.go.kr/bill/summaryPopup.do"
    assert shape["data_keys"] == ["_csrf", "billId", "sessionToken"]
    assert shape["header_keys"] == ["X-CSRF-TOKEN"]
    # 값은 어디에도 없다
    blob = repr(shape)
    assert "tok-abc-123" not in blob and "s-9" not in blob


# --- 3) 폼 action 검증 ---


@pytest.mark.parametrize(
    "action",
    [
        "http://likms.assembly.go.kr/bill/summaryPopup.do",   # HTTPS 아님
        "https://evil.example.com/bill/summaryPopup.do",      # 다른 호스트
        "https://likms.assembly.go.kr.evil.com/summary.do",   # 접미사 위장
    ],
)
def test_unsafe_form_action_is_not_used(action, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    page = f"""
    <html><body>
      <form id="summaryForm" action="{action}" method="post">
        <input type="hidden" name="billId" value=""/>
        <input type="hidden" name="sessionToken" value="s-9"/>
      </form>
    </body></html>"""
    f = _Fetcher(page, _POST_RESULT)
    p = _post()
    _scraper(f).enrich(p)

    assert f.posts == []   # 세션 식별자·CSRF 를 외부/평문으로 보내지 않는다
    assert p.body == ""


def test_unrelated_form_is_not_submitted(tmp_path, monkeypatch):
    # 이 의안과의 연관 근거가 없는 폼(검색 등)에는 되쏘지 않는다.
    monkeypatch.chdir(tmp_path)
    page = """
    <html><body>
      <form name="searchForm" action="/bill/bi/search/searchList.do" method="post">
        <input type="hidden" name="keyword" value=""/>
      </form>
    </body></html>"""
    f = _Fetcher(page, _POST_RESULT)
    p = _post()
    _scraper(f).enrich(p)
    assert f.posts == []
    assert p.body == ""


def test_form_matched_by_bill_id_value_without_summary_name(tmp_path, monkeypatch):
    # 폼 이름에 'summary' 가 없어도 이 의안 ID 를 들고 있으면 연관 폼으로 본다.
    monkeypatch.chdir(tmp_path)
    page = f"""
    <html><body>
      <form name="detailForm" action="/bill/bi/detail/contents.do" method="post">
        <input type="hidden" name="someId" value="{_BILL_ID}"/>
      </form>
    </body></html>"""
    f = _Fetcher(page, _POST_RESULT)
    p = _post()
    _scraper(f).enrich(p)
    assert len(f.posts) == 1
    assert "예치금 분리보관" in p.body


# --- 4) 실패 처리 ---


def test_missing_summary_dumps_debug_per_bill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _Fetcher("<html><body>제안이유 없음</body></html>")
    p = _post()
    _scraper(f).enrich(p)

    dump = tmp_path / "debug" / f"assembly_bill_detail_{_BILL_ID}.txt"
    assert dump.exists()
    assert "제안이유 없음" in dump.read_text(encoding="utf-8")
    assert p.body == ""


def test_debug_dump_includes_follow_up_response(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _Fetcher(_FORM_PAGE, "<html><body>후속 응답에도 없음</body></html>")
    p = _post()
    _scraper(f).enrich(p)

    dump = (tmp_path / "debug" / f"assembly_bill_detail_{_BILL_ID}.txt").read_text(
        encoding="utf-8"
    )
    assert "summaryForm" in dump            # 최초 GET
    assert "후속 응답에도 없음" in dump      # 후속 응답
    assert p.body == ""


def test_debug_dump_filename_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _Fetcher("<html><body>없음</body></html>")
    p = _post(bill_id="../../etc/passwd")
    _scraper(f).enrich(p)

    # 경로 구분자가 사라져 debug/ 밖으로 나가지 못한다(파일명 한 개로 눌린다)
    written = list((tmp_path / "debug").iterdir())
    assert len(written) == 1
    assert written[0].resolve().parent == (tmp_path / "debug").resolve()
    assert "/" not in written[0].name
    assert not (tmp_path / "etc").exists()


def test_enrich_never_raises_so_other_bills_continue():
    class _Broken:
        def get(self, *a, **k):
            raise RuntimeError("상세 페이지 500")

        def text(self, resp):  # pragma: no cover - 도달하지 않음
            raise AssertionError

    ok_f = _Fetcher(f'<html><pre id="prntSummary">{_REASON}</pre></html>')
    broken, good = _post("PRC_BAD"), _post("PRC_OK")

    _scraper(_Broken()).enrich(broken)   # 예외가 밖으로 나오면 안 된다
    _scraper(ok_f).enrich(good)

    assert broken.body == ""
    assert "예치금 분리보관" in good.body


def test_post_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class _PostFails(_Fetcher):
        def post(self, url, referer=None, data=None, headers=None):
            raise RuntimeError("403 Forbidden")

    f = _PostFails(_FORM_PAGE, _POST_RESULT)
    p = _post()
    _scraper(f).enrich(p)   # 예외 없음
    assert p.body == ""
