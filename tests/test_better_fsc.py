"""금융규제포털 회신사례(better_reply) 상세 수집 회귀 테스트.

네트워크에 의존하지 않는다 — 목록 JSON·상세 HTML·첨부 바이트를 돌려주는 가짜
fetcher 로 결정적으로 검증한다.

상세 HTML 은 **실제 공개 페이지에서 확인된 의미 구조(질의요지·회답·이유 + 첨부
링크)를 축약한 synthetic fixture 이며, raw DOM snapshot 이 아니다.** 실제 태그
구조는 이와 다를 수 있으므로 파서는 라벨-값 표와 라벨 헤딩 두 배치를 모두 본다.
첨부 URL 만은 실제 관찰된 형태(/fsc_new/file/displayFile.do?filePath=…&orgFileName=…
&sysFileName=…)를 그대로 쓴다.
"""
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import LLMConfig, SourceConfig
from src.fetcher import AttachmentTooLarge
from src.models import Post
from src.notifier import build_html, build_text
from src.scrapers.better_fsc import BetterReplyScraper
from src.summarizer import Summarizer, _prepare_body

LIST_URL = (
    "https://better.fsc.go.kr/fsc_new/replyCase/TotalReplyList.do"
    "?stNo=11&muNo=117&muGpNo=75"
)

# 라벨-값 표(th/td) 배치 + 공식 첨부 링크(상대 URL, 중복 포함).
DETAIL_HTML = """
<div id="content">
  <table class="tbl-view">
    <tbody>
      <tr><th>질의요지</th><td>겸영업무 신고 대상인지 여부를 질의함.</td></tr>
      <tr><th>회답</th><td>신고 대상에 해당하지 않습니다.</td></tr>
      <tr><th>이유</th><td>은행법 제28조는 겸영업무를 열거하고 있으며,
        해당 업무는 그 범위에 포함되지 않습니다.</td></tr>
      <tr><th>첨부파일</th><td>
        <a href="/fsc_new/file/displayFile.do?filePath=%2Freply%2F2026&amp;orgFileName=%ED%9A%8C%EC%8B%A0%EB%AC%B8.hwp&amp;sysFileName=20260820_001.hwp">회신문.hwp (42 KB)</a>
        <a href="/fsc_new/file/displayFile.do?filePath=%2Freply%2F2026&amp;orgFileName=%ED%9A%8C%EC%8B%A0%EB%AC%B8.hwp&amp;sysFileName=20260820_001.hwp">회신문.hwp</a>
        <a href="https://evil.example.com/fsc_new/file/displayFile.do?filePath=%2Fx&amp;sysFileName=e.hwp">외부첨부.hwp</a>
        <a href="javascript:fn_viewer('A1')">문서뷰어</a>
      </td></tr>
    </tbody>
  </table>
</div>
"""

# 라벨만 든 요소 뒤에 본문이 형제로 오는 배치(표가 아닌 레이아웃).
DETAIL_HTML_HEADING = """
<div id="content">
  <div class="view">
    <h4>□ 질의요지</h4>
    <p>전자금융업자의 겸영 가능 여부</p>
    <h4>□ 회답</h4>
    <p>가능합니다.</p>
    <h4>□ 이유</h4>
    <p>전자금융거래법상 제한 규정이 없습니다.</p>
  </div>
</div>
"""

# 부분 본문 — 질의요지만 있는 배치(회답·이유 없음).
DETAIL_HTML_ONLY_QUESTION = """
<div id="content"><table><tbody>
  <tr><th>질의요지</th><td>겸영업무 신고 대상인지 여부를 질의함.</td></tr>
</tbody></table></div>
"""

# 부분 본문 — 질의요지 + 회답만 있는 배치(이유 없음).
DETAIL_HTML_NO_REASON = """
<div id="content"><table><tbody>
  <tr><th>질의요지</th><td>겸영업무 신고 대상인지 여부를 질의함.</td></tr>
  <tr><th>회답</th><td>신고 대상에 해당하지 않습니다.</td></tr>
</tbody></table></div>
"""

# 부분 본문인데 첨부는 있는 배치(첨부 수집은 계속되어야 한다).
DETAIL_HTML_PARTIAL_WITH_FILE = """
<div id="content"><table><tbody>
  <tr><th>질의요지</th><td>겸영업무 신고 대상인지 여부를 질의함.</td></tr>
  <tr><th>첨부파일</th><td>
    <a href="/fsc_new/file/displayFile.do?filePath=%2Freply&amp;orgFileName=a.hwp&amp;sysFileName=1.hwp">첨부.hwp</a>
  </td></tr>
</tbody></table></div>
"""

# 포털 오류 페이지.
ERROR_HTML = """
<html><head><title>ERROR PAGE</title></head>
<body><div class="error">
  <h1>ERROR PAGE</h1>
  <p>요청하신 페이지는 사용할 수 없거나 찾을 수 없는 페이지 입니다.</p>
</div></body></html>
"""


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Fetcher:
    """목록 JSON 과 상세 HTML 을 돌려주는 최소 fetcher.

    Fetcher 의 실제 시그니처(get/post/text/download, referer 키워드)를 그대로 따른다.
    """

    def __init__(self, records=(), html="", blob=b"HWP", download_error=None):
        self.records = list(records)
        self.html = html
        self.blob = blob
        self.download_error = download_error
        self.get_calls = []
        self.downloaded = []
        self.get_error = None

    def post(self, url, *, referer=None, **kw):
        return _Resp({"data": self.records})

    def get(self, url, *, referer=None, **kw):
        self.get_calls.append((url, referer))
        if self.get_error is not None:
            raise self.get_error
        return object()

    def text(self, resp):
        return self.html

    def download(self, url, *, referer=None, **kw):
        self.downloaded.append((url, referer))
        if self.download_error is not None:
            raise self.download_error
        return self.blob


def _scraper(fetcher):
    src = SourceConfig(
        key="better_reply",
        name="금융규제포털 · 법령해석·비조치의견서 회신사례",
        type="better_reply",
        list_url=LIST_URL,
    )
    return BetterReplyScraper(src, fetcher=fetcher)


def _record(gubun, idx="5051", title="겸영업무 해당 여부"):
    return {
        "rownumber": 1,
        "pastreqType": gubun,
        "title": title,
        "replyRegDate": "2026-08-20",
        "dataIdx": idx,
    }


def _list_one(gubun, idx="5051"):
    sc = _scraper(_Fetcher(records=[_record(gubun, idx)]))
    posts = sc.fetch_list(10)
    assert len(posts) == 1
    return sc, posts[0]


def _post(url, key="better_reply"):
    return Post(
        source_key=key,
        source_name="금융규제포털 · 법령해석·비조치의견서 회신사례",
        post_id="dataIdx:5051",
        title="[법령해석] 겸영업무 해당 여부",
        url=url,
        date="2026-08-20",
    )


# --- 1. 법령해석 상세 URL ---------------------------------------------------
def test_lawreq_detail_url_uses_dataidx_as_lawreqidx():
    _, post = _list_one("법령해석", idx="5051")
    parsed = urlparse(post.url)
    assert parsed.netloc == "better.fsc.go.kr"
    assert parsed.path == "/fsc_new/replyCase/LawreqDetail.do"
    q = parse_qs(parsed.query)
    assert q["lawreqIdx"] == ["5051"]
    # 목록 URL 의 내비게이션 파라미터가 그대로 유지된다.
    assert q["stNo"] == ["11"] and q["muNo"] == ["117"] and q["muGpNo"] == ["75"]
    assert "opinionIdx" not in q


def test_lawreq_title_and_date_format_unchanged():
    """기존 제목 포맷·post_id·날짜 파싱이 그대로여야 한다(seen 처리 호환)."""
    _, post = _list_one("법령해석")
    assert post.title == "[법령해석] 겸영업무 해당 여부"
    assert post.post_id == "dataIdx:5051"
    assert post.date == "2026-08-20"


# --- 2. 비조치의견서 상세 URL -----------------------------------------------
def test_opinion_detail_url_uses_dataidx_as_opinionidx():
    _, post = _list_one("비조치의견서", idx="2285")
    parsed = urlparse(post.url)
    assert parsed.path == "/fsc_new/replyCase/OpinionDetail.do"
    q = parse_qs(parsed.query)
    assert q["opinionIdx"] == ["2285"]
    assert q["stNo"] == ["11"] and q["muNo"] == ["117"] and q["muGpNo"] == ["75"]
    assert "lawreqIdx" not in q


# --- 3. 미검증 유형 ---------------------------------------------------------
def test_unverified_pastreq_type_falls_back_to_list_url():
    """'현장건의 과제' 등 상세 주소가 확인되지 않은 구분은 URL 을 지어내지 않는다."""
    for gubun in ("현장건의 과제", "법령해석(2014이전)", ""):
        _, post = _list_one(gubun)
        assert post.url == LIST_URL, gubun
        assert "Detail.do" not in post.url


def test_unverified_type_enrich_is_a_no_op():
    """폴백 URL 은 상세 요청조차 하지 않는다(목록 페이지를 본문으로 삼지 않는다)."""
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    post = _post(LIST_URL)
    sc.enrich(post)
    assert fetcher.get_calls == []
    assert post.body == "" and post.attachments == []


def test_non_numeric_dataidx_is_not_turned_into_a_url():
    _, post = _list_one("법령해석", idx="abc")
    assert post.url == LIST_URL


# --- 4. 상세 본문 추출 ------------------------------------------------------
def test_detail_body_contains_three_sections_in_order():
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    assert post.body.startswith("[질의요지]\n")
    assert "[회답]\n신고 대상에 해당하지 않습니다." in post.body
    assert "[이유]\n은행법 제28조" in post.body
    assert post.body.index("[질의요지]") < post.body.index("[회답]") < post.body.index("[이유]")


def test_detail_body_from_heading_layout():
    sc = _scraper(_Fetcher(html=DETAIL_HTML_HEADING))
    post = _post(LIST_URL.replace("TotalReplyList.do", "OpinionDetail.do"))
    sc.enrich(post)
    assert "[질의요지]\n전자금융업자의 겸영 가능 여부" in post.body
    assert "[회답]\n가능합니다." in post.body
    assert "[이유]\n전자금융거래법상 제한 규정이 없습니다." in post.body


def test_partial_sections_leave_body_empty(caplog):
    """질의요지만 있으면 본문을 만들지 않는다 — 회답 없는 요약은 결론을 지어낸다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML_ONLY_QUESTION))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == ""
    assert "회답" in caplog.text and "이유" in caplog.text     # 누락 항목을 명시
    assert _prepare_body(_llm_cfg(), post) == ""              # 요약 대상이 아니다


def test_two_of_three_sections_still_leave_body_empty(caplog):
    sc = _scraper(_Fetcher(html=DETAIL_HTML_NO_REASON))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == ""
    # 누락된 항목만 이름이 오른다(이미 찾은 질의요지·회답은 빠진다).
    assert "상세에서 이유 를 찾지 못해" in caplog.text


def test_all_three_sections_fill_body():
    """세 항목이 모두 있을 때만 본문이 채워진다(위 두 케이스와 같은 파서)."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    assert post.body != ""
    for label in ("[질의요지]", "[회답]", "[이유]"):
        assert label in post.body


def test_partial_sections_still_collect_attachments():
    """본문이 비어도 첨부 수집·다운로드는 계속한다."""
    fetcher = _Fetcher(html=DETAIL_HTML_PARTIAL_WITH_FILE)
    sc = _scraper(fetcher)
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    assert post.body == ""
    assert [a.filename for a in post.attachments] == ["첨부.hwp"]
    assert post.attachments[0].data == b"HWP"


def test_detail_request_uses_list_url_as_referer():
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    url = LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do")
    sc.enrich(_post(url))
    assert fetcher.get_calls == [(url, LIST_URL)]


def test_detail_does_not_fill_details():
    """details 는 비워 둔다 — 채우면 요약이 만들어져도 메일에 보이지 않는다.

    (정확히는 summarizer 의 실제 호출 경로 _summarize_general 은 details 를 보지 않고
     body 길이만 본다. 문제는 notifier 가 details 를 summary 보다 먼저 렌더하고,
     집계 로그 ai_target_count 만 details 가 있는 글을 대상에서 빼 로그와 실제 호출이
     어긋난다는 점이다.)
    """
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    assert post.details == []


# --- 5. Gemini 요약 eligibility --------------------------------------------
def _llm_cfg(**over) -> LLMConfig:
    base = dict(
        enabled=True, model="gemini-flash-latest", lines=3, max_line_chars=90,
        min_body_chars=80, max_input_chars=8000, max_posts=40, rpm=0,
        timeout_sec=5, max_retries=0, retry_backoff_sec=0, api_key="k",
    )
    base.update(over)
    return LLMConfig(**base)


def test_enriched_post_is_eligible_for_general_summary():
    """production summarizer 를 바꾸지 않고 기존 판정 함수로 검증한다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    assert len(post.body) >= 80
    assert _prepare_body(_llm_cfg(), post) != ""


def test_post_without_body_is_not_eligible():
    assert _prepare_body(_llm_cfg(), _post(LIST_URL)) == ""


def _envelope(text: str) -> dict:
    """Gemini generateContent 응답 봉투(테스트에서 _generate 를 대체할 때 쓴다)."""
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


def test_enriched_post_goes_through_real_general_summary_path():
    """_prepare_body 만이 아니라 실제 일반 요약 경로(summarize_all)를 태운다.

    네트워크·Gemini 호출은 없다 — Summarizer._generate 만 가짜 응답으로 대체한다
    (production summarizer 는 수정하지 않는다).
    """
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)

    summarizer = Summarizer(_llm_cfg())
    prompts: list[str] = []

    def _generate(prompt, deadline=None, **kw):
        prompts.append(prompt)
        return _envelope('{"summary": ["신고 대상 아님", "은행법 제28조 근거", "회신 요지"]}')

    summarizer._generate = _generate
    ok = summarizer.summarize_all({post.source_name: [post]})

    assert ok == 1
    assert len(prompts) == 1                       # 이 글로 실제 호출이 일어났다
    assert "질의요지" in prompts[0]                 # 수집한 본문이 프롬프트에 실렸다
    assert post.summary == ["신고 대상 아님", "은행법 제28조 근거", "회신 요지"]


def test_unsupported_type_post_is_not_summarized():
    """본문이 없는(미지원 구분) 글은 요약 호출 대상이 아니다."""
    summarizer = Summarizer(_llm_cfg())
    called = []
    summarizer._generate = lambda *a, **kw: called.append(1) or _envelope("{}")
    assert summarizer.summarize_all({"회신사례": [_post(LIST_URL)]}) == 0
    assert called == []


def test_summary_is_rendered_in_mail_body():
    """details 를 비워 둔 덕분에 메일에 AI 요약이 그대로 실린다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    post.summary = ["신고 대상 아님", "은행법 제28조 근거", "회신 요지"]

    html = build_html({post.source_name: [post]})
    text = build_text({post.source_name: [post]})
    for line in post.summary:
        assert line in html
        assert line in text


# --- 6. ERROR PAGE ----------------------------------------------------------
def test_error_page_is_not_stored_as_body(caplog):
    sc = _scraper(_Fetcher(html=ERROR_HTML))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == ""
    assert post.attachments == []
    assert "ERROR PAGE" in caplog.text
    assert _prepare_body(_llm_cfg(), post) == ""


# --- 7~8. 첨부 --------------------------------------------------------------
def test_attachments_are_absolute_deduped_and_named():
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    url = LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do")
    post = _post(url)
    sc.enrich(post)
    assert len(post.attachments) == 1                       # 중복 URL 제거
    att = post.attachments[0]
    assert att.url == (
        "https://better.fsc.go.kr/fsc_new/file/displayFile.do"
        "?filePath=%2Freply%2F2026&orgFileName=%ED%9A%8C%EC%8B%A0%EB%AC%B8.hwp"
        "&sysFileName=20260820_001.hwp"
    )                                                       # 상대 → 절대
    assert att.filename == "회신문.hwp"                      # 크기 표기 제거
    assert att.data == b"HWP"
    assert fetcher.downloaded == [(att.url, url)]           # referer = 상세 URL


def test_external_origin_attachment_is_not_downloaded():
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    urls = [a.url for a in post.attachments] + [u for u, _ in fetcher.downloaded]
    assert not any("evil.example.com" in u for u in urls)


# --- 9. AttachmentTooLarge --------------------------------------------------
def test_attachment_too_large_keeps_metadata_and_body():
    fetcher = _Fetcher(html=DETAIL_HTML, download_error=AttachmentTooLarge(999, 10))
    sc = _scraper(fetcher)
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)                                   # 예외가 새어 나오지 않는다
    assert len(post.attachments) == 1
    assert post.attachments[0].filename == "회신문.hwp"
    assert post.attachments[0].url.startswith(
        "https://better.fsc.go.kr/fsc_new/file/displayFile.do"
    )
    assert post.attachments[0].data is None
    assert "[회답]" in post.body                        # 본문 수집은 그대로 성공


# --- 10. graceful degradation ----------------------------------------------
def test_detail_request_failure_does_not_raise():
    fetcher = _Fetcher(html=DETAIL_HTML)
    fetcher.get_error = RuntimeError("boom")
    sc = _scraper(fetcher)
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    assert post.body == "" and post.attachments == []


def test_attachment_download_failure_does_not_raise():
    fetcher = _Fetcher(html=DETAIL_HTML, download_error=RuntimeError("net"))
    sc = _scraper(fetcher)
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    sc.enrich(post)
    assert post.attachments[0].data is None
    assert "[질의요지]" in post.body


def test_unparseable_detail_leaves_body_empty_and_warns(caplog):
    sc = _scraper(_Fetcher(html="<html><body><div>내용 없음</div></body></html>"))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == ""
    assert "질의요지·회답·이유 를 찾지 못해" in caplog.text


# --- per-post 상세 수집 대상 훅 ---
def test_supports_enrich_is_per_post():
    """소스는 상세 수집을 하지만, 상세 주소가 없는 글은 통계에서 빠져야 한다."""
    sc = _scraper(_Fetcher())
    assert sc.supports_enrich(_post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do")))
    assert sc.supports_enrich(_post(LIST_URL.replace("TotalReplyList.do", "OpinionDetail.do")))
    assert not sc.supports_enrich(_post(LIST_URL))


def test_base_scraper_hook_defaults_to_source_flag():
    """기존 스크래퍼는 훅을 오버라이드하지 않아도 동작·통계가 그대로여야 한다."""
    from src.config import SourceConfig as _SC
    from src.scrapers.base import BaseScraper

    class _Plain(BaseScraper):
        pass

    plain = _Plain(_SC(key="k", name="n", type="t", list_url="https://x/"), fetcher=None)
    assert plain.supports_enrich(_post("https://x/any")) is True

    class _Off(BaseScraper):
        SUPPORTS_ENRICH = False

    off = _Off(_SC(key="k", name="n", type="t", list_url="https://x/"), fetcher=None)
    assert off.supports_enrich(_post("https://x/any")) is False


# --- 기존 기능 보존 ---------------------------------------------------------
def test_pagination_and_enrich_flags_unchanged():
    sc = _scraper(_Fetcher())
    assert sc.PAGE_PARAM is None
    assert sc.paginates is True
    assert sc.SUPPORTS_ENRICH is True     # 이제 상세 수집 파이프라인을 탄다
