"""금융규제포털 회신사례(better_reply) 상세 수집 회귀 테스트.

네트워크에 의존하지 않는다 — 목록 JSON·상세 HTML·첨부 바이트를 돌려주는 가짜
fetcher 로 결정적으로 검증한다.

상세 HTML 은 **실제 공개 페이지에서 확인된 의미 구조(질의요지·회답·이유 + 첨부
링크)를 축약한 synthetic fixture 이며, raw DOM snapshot 이 아니다.** 실제 태그
구조는 이와 다를 수 있으므로 파서는 라벨-값 표와 라벨 헤딩 두 배치를 모두 본다.
첨부 URL 만은 실제 관찰된 형태(/fsc_new/file/displayFile.do?filePath=…&orgFileName=…
&sysFileName=…)를 그대로 쓴다.

동일 게시물 검증(identity guard)을 실제 production path 로 태우기 위해, 상세 fixture 는
외부에서 관찰된 항목(상세 제목, '회신일' 라벨/값)을 함께 담는다. 새 selector 를 지어내지
않고 이미 쓰는 라벨-값 구조에만 얹는다.
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
  <h2 class="title">겸영업무 해당 여부</h2>
  <table class="tbl-view">
    <tbody>
      <tr><th>처리구분</th><td>완료</td></tr>
      <tr><th>소관부서</th><td>은행과</td></tr>
      <tr><th>회신일</th><td>2026-08-20</td></tr>
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
#
# 마지막 항목('이유') 뒤에 **같은 부모 아래** 첨부·목록/이전글 버튼·URL 복사·푸터가
# 이어진다. 다음 본문 라벨이 없으므로, 경계 처리가 없으면 이 텍스트가 전부 법률적
# '이유' 본문이 되어 Gemini 로 넘어간다.
DETAIL_HTML_HEADING = """
<div id="content">
  <div class="view">
    <h4>제목</h4>
    <p>전자금융업자 겸영 가능 여부</p>
    <h4>회신일</h4>
    <p>2026-07-15</p>
    <h4>□ 질의요지</h4>
    <p>전자금융업자의 겸영 가능 여부</p>
    <h4>□ 회답</h4>
    <p>가능합니다.</p>
    <h4>□ 이유</h4>
    <p>전자금융거래법상 제한 규정이 없습니다.</p>
    <div class="file">
      <a href="/fsc_new/file/displayFile.do?filePath=%2Freply&amp;orgFileName=b.hwp&amp;sysFileName=2.hwp">회신문_전자금융.hwp</a>
    </div>
    <div class="btn-area">
      <a href="/fsc_new/replyCase/TotalReplyList.do">목록</a>
      <button type="button">URL 복사</button>
    </div>
    <footer>금융위원회 금융규제·법령해석포털 · 대표전화 1234-5678</footer>
  </div>
</div>
"""

# heading 레이아웃인데 회신일이 표(라벨-값)로 오는 변형 — identity guard 는 구조적
# 라벨-값에서 회신일을 읽으므로 이쪽도 통과해야 한다.
DETAIL_HTML_HEADING_TABLE_DATE = """
<div id="content">
  <h2>전자금융업자 겸영 가능 여부</h2>
  <table><tbody><tr><th>회신일</th><td>2026.07.15</td></tr></tbody></table>
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
<div id="content"><h2>겸영업무 해당 여부</h2><table><tbody>
  <tr><th>회신일</th><td>2026-08-20</td></tr>
  <tr><th>질의요지</th><td>겸영업무 신고 대상인지 여부를 질의함.</td></tr>
</tbody></table></div>
"""

# 부분 본문 — 질의요지 + 회답만 있는 배치(이유 없음).
DETAIL_HTML_NO_REASON = """
<div id="content"><h2>겸영업무 해당 여부</h2><table><tbody>
  <tr><th>회신일</th><td>2026-08-20</td></tr>
  <tr><th>질의요지</th><td>겸영업무 신고 대상인지 여부를 질의함.</td></tr>
  <tr><th>회답</th><td>신고 대상에 해당하지 않습니다.</td></tr>
</tbody></table></div>
"""

# 부분 본문인데 첨부는 있는 배치(첨부 수집은 계속되어야 한다).
DETAIL_HTML_PARTIAL_WITH_FILE = """
<div id="content"><h2>겸영업무 해당 여부</h2><table><tbody>
  <tr><th>회신일</th><td>2026-08-20</td></tr>
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


def _detail(url_file="LawreqDetail.do"):
    return LIST_URL.replace("TotalReplyList.do", url_file)


def _opinion_post():
    """DETAIL_HTML_HEADING 의 상세와 동일 게시물인 목록 Post."""
    return _post(
        _detail("OpinionDetail.do"),
        title="[비조치의견서] 전자금융업자 겸영 가능 여부",
        date="2026-07-15",
    )


def _post(url, key="better_reply", title=None, date="2026-08-20"):
    return Post(
        source_key=key,
        source_name="금융규제포털 · 법령해석·비조치의견서 회신사례",
        post_id="dataIdx:5051",
        title="[법령해석] 겸영업무 해당 여부" if title is None else title,
        url=url,
        date=date,
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
    post = _opinion_post()
    sc.enrich(post)
    assert "[질의요지]\n전자금융업자의 겸영 가능 여부" in post.body
    assert "[회답]\n가능합니다." in post.body
    assert "[이유]\n전자금융거래법상 제한 규정이 없습니다." in post.body


def test_heading_layout_reason_stops_before_trailing_controls():
    """마지막 항목('이유') 뒤의 첨부·버튼·푸터가 법률 본문에 섞이면 안 된다.

    다음 본문 라벨이 없어 예전 구현은 남은 형제를 전부 '이유'로 삼켰다.
    """
    sc = _scraper(_Fetcher(html=DETAIL_HTML_HEADING))
    post = _opinion_post()
    sc.enrich(post)

    reason = post.body.split("[이유]\n", 1)[1]
    assert reason == "전자금융거래법상 제한 규정이 없습니다."   # 정상 이유 문장만
    for garbage in ("회신문_전자금융.hwp", "목록", "URL 복사", "1234-5678", "대표전화"):
        assert garbage not in post.body, garbage


def test_heading_layout_still_collects_the_trailing_attachment():
    """본문 경계를 끊어도 첨부 수집 자체는 정상 동작해야 한다."""
    fetcher = _Fetcher(html=DETAIL_HTML_HEADING)
    sc = _scraper(fetcher)
    post = _opinion_post()
    sc.enrich(post)
    assert [a.filename for a in post.attachments] == ["회신문_전자금융.hwp"]
    assert post.attachments[0].data == b"HWP"


def test_heading_layout_with_table_reply_date():
    """회신일이 표로 오는 heading 변형도 동일 게시물로 통과한다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML_HEADING_TABLE_DATE))
    post = _opinion_post()          # 목록 날짜 2026-07-15 ↔ 상세 '2026.07.15'
    sc.enrich(post)
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


# 동일 게시물인 것은 확인되지만 세 항목을 하나도 못 찾는 경우(마크업 변경 신호).
DETAIL_HTML_NO_SECTIONS = """
<div id="content"><h2>겸영업무 해당 여부</h2>
  <table><tbody><tr><th>회신일</th><td>2026-08-20</td></tr></tbody></table>
  <div>내용 없음</div>
</div>
"""


def test_unparseable_detail_leaves_body_empty_and_warns(caplog):
    sc = _scraper(_Fetcher(html=DETAIL_HTML_NO_SECTIONS))
    post = _post(LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do"))
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == ""
    assert "질의요지·회답·이유 를 찾지 못해" in caplog.text


# --- 동일 게시물 검증(identity guard) ---
#
# 목록 dataIdx 를 lawreqIdx/opinionIdx 로 쓰는 매핑은 아직 라이브로 확인되지 않았다.
# 그 가정이 틀리면 목록 A 의 제목 밑에 상세 B 의 회답·첨부가 실릴 수 있다.

# 세 항목과 첨부가 모두 있는 '멀쩡해 보이는' 다른 게시물의 상세.
OTHER_POST_HTML = """
<div id="content"><h2>전혀 다른 사안에 대한 질의</h2><table><tbody>
  <tr><th>회신일</th><td>2020-01-02</td></tr>
  <tr><th>질의요지</th><td>다른 게시물의 질의입니다.</td></tr>
  <tr><th>회답</th><td>다른 게시물의 회답입니다.</td></tr>
  <tr><th>이유</th><td>다른 게시물의 이유입니다.</td></tr>
  <tr><th>첨부파일</th><td>
    <a href="/fsc_new/file/displayFile.do?filePath=%2Fx&amp;orgFileName=other.hwp&amp;sysFileName=9.hwp">다른회신문.hwp</a>
  </td></tr>
</tbody></table></div>
"""

# 제목은 맞는데 회신일이 다른 상세(같은 제목의 다른 회차 등).
WRONG_DATE_HTML = DETAIL_HTML.replace("<td>2026-08-20</td>", "<td>2019-03-04</td>")

# 무해한 표기 차이(줄바꿈·중복 공백·NBSP)만 있는 제목.
SPACED_TITLE_HTML = DETAIL_HTML.replace(
    "<h2 class=\"title\">겸영업무 해당 여부</h2>",
    "<h2 class=\"title\">겸영업무\n   해당&nbsp;&nbsp;여부</h2>",
)


def test_identity_ok_collects_body_and_attachments():
    """A. 제목·회신일이 모두 맞으면 정상 수집."""
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())
    sc.enrich(post)
    assert "[회답]" in post.body
    assert len(post.attachments) == 1 and post.attachments[0].data == b"HWP"


def test_identity_rejects_other_post_content(caplog):
    """B/D. 세 라벨과 첨부가 다 있어도 목록 제목과 다르면 절대 붙이지 않는다."""
    fetcher = _Fetcher(html=OTHER_POST_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == ""
    assert post.attachments == []
    assert fetcher.downloaded == []                  # 다운로드 호출 자체가 없다
    assert "상세 제목이 목록과 다름" in caplog.text
    assert "다른 게시물의 회답" not in post.body


def test_identity_rejects_wrong_reply_date(caplog):
    """C. 제목이 맞아도 회신일이 다르면 거부."""
    fetcher = _Fetcher(html=WRONG_DATE_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []
    assert "회신일" in caplog.text and "다름" in caplog.text


def test_identity_rejects_missing_reply_date(caplog):
    """회신일을 아예 확인할 수 없으면 fail-open 하지 않는다."""
    html = DETAIL_HTML.replace("<tr><th>회신일</th><td>2026-08-20</td></tr>", "")
    fetcher = _Fetcher(html=html)
    sc = _scraper(fetcher)
    post = _post(_detail())
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []
    assert "동일 게시물 확인 불가" in caplog.text


def test_identity_strips_only_our_own_title_prefix():
    """E. 우리가 붙인 [법령해석]/[비조치의견서] 접두어만 제거한다."""
    # 접두어가 붙은 목록 제목 → 제거 후 상세 제목과 일치
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    with_prefix = _post(_detail(), title="[법령해석] 겸영업무 해당 여부")
    sc.enrich(with_prefix)
    assert "[회답]" in with_prefix.body

    # 접두어가 없는 원 제목도 그대로 통과한다(접두어를 요구하지 않는다)
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    without_prefix = _post(_detail(), title="겸영업무 해당 여부")
    sc.enrich(without_prefix)
    assert "[회답]" in without_prefix.body


def test_identity_rejects_gubun_endpoint_mismatch(caplog):
    """E/4. 목록 구분과 상세 endpoint 가 어긋나면 거부한다."""
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail("LawreqDetail.do"), title="[비조치의견서] 겸영업무 해당 여부")
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []
    assert "어긋남" in caplog.text


def test_identity_tolerates_harmless_whitespace_differences():
    """F. 줄바꿈·중복 공백·NBSP 정도의 표기 차이는 정상 허용."""
    sc = _scraper(_Fetcher(html=SPACED_TITLE_HTML))
    post = _post(_detail())
    sc.enrich(post)
    assert "[회답]" in post.body


# --- 본문 문단 안의 인라인 링크는 경계가 아니다 ---
#
# 회신문의 '이유'에는 '은행법 <a>제28조</a>에 따릅니다' 처럼 법령 링크가 흔하다.
# 링크가 있다는 이유만으로 문단을 끊으면 정상 법률 본문이 통째로 잘려 Gemini 가
# 불완전한 이유를 요약한다. 반면 목록/이전글 같은 링크 전용 블록과 공식 첨부 컨트롤은
# 여전히 경계여야 한다.

INLINE_LINK_HTML = """
<div id="content">
  <h2>겸영업무 해당 여부</h2>
  <table><tbody><tr><th>회신일</th><td>2026-08-20</td></tr></tbody></table>
  <div class="view">
    <h4>질의요지</h4>
    <p>겸영업무 신고 대상인지 여부를 질의함.</p>
    <h4>회답</h4>
    <p>신고 대상에 해당하지 않습니다.</p>
    <h4>이유</h4>
    <p>은행법 <a href="/law/28">제28조</a>에 따라 허용됩니다.</p>
  </div>
</div>
"""

# 인라인 링크가 중간에 있고 그 뒤에 추가 문단이 이어지는 경우 + 그 뒤 조작 블록.
INLINE_LINK_MULTI_PARA_HTML = """
<div id="content">
  <h2>겸영업무 해당 여부</h2>
  <table><tbody><tr><th>회신일</th><td>2026-08-20</td></tr></tbody></table>
  <div class="view">
    <h4>질의요지</h4>
    <p>겸영업무 신고 대상인지 여부를 질의함.</p>
    <h4>회답</h4>
    <p>신고 대상에 해당하지 않습니다.</p>
    <h4>이유</h4>
    <p>법령 <a href="/law/28">제28조</a>에 따릅니다.</p>
    <p>따라서 이 경우에는 허용됩니다.</p>
    <div class="links">
      <a href="/fsc_new/replyCase/TotalReplyList.do">목록</a>
      <a href="/fsc_new/replyCase/LawreqDetail.do?lawreqIdx=1">이전글</a>
    </div>
    <p>이 문단은 조작 블록 뒤이므로 본문이 아니다.</p>
  </div>
</div>
"""

# 인라인 링크 문단 뒤에 공식 첨부 다운로드 컨트롤이 오는 경우.
INLINE_LINK_THEN_FILE_HTML = """
<div id="content">
  <h2>겸영업무 해당 여부</h2>
  <table><tbody><tr><th>회신일</th><td>2026-08-20</td></tr></tbody></table>
  <div class="view">
    <h4>질의요지</h4>
    <p>질의 본문입니다.</p>
    <h4>회답</h4>
    <p>회답 본문입니다.</p>
    <h4>이유</h4>
    <p>은행법 <a href="/law/28">제28조</a>에 따라 허용됩니다.</p>
    <div class="file">
      <a href="/fsc_new/file/displayFile.do?filePath=%2Fa&amp;orgFileName=%ED%9A%8C%EC%8B%A0%EB%AC%B8.hwp&amp;sysFileName=1.hwp">회신문.hwp</a>
    </div>
    <p>첨부 컨트롤 뒤 안내문이라 본문이 아니다.</p>
  </div>
</div>
"""


def test_inline_link_does_not_truncate_section():
    """1. 문단 안의 법령 링크가 있어도 세 항목이 모두 잡히고 본문이 온전하다."""
    sc = _scraper(_Fetcher(html=INLINE_LINK_HTML))
    post = _post(_detail())
    sc.enrich(post)
    for label in ("[질의요지]", "[회답]", "[이유]"):
        assert label in post.body
    assert "은행법" in post.body
    assert "제28조" in post.body
    assert "허용됩니다" in post.body


def test_inline_link_keeps_following_prose_paragraph():
    """2. 인라인 링크 문단 뒤의 추가 문단도 같은 항목에 포함된다."""
    sc = _scraper(_Fetcher(html=INLINE_LINK_MULTI_PARA_HTML))
    post = _post(_detail())
    sc.enrich(post)
    reason = post.body.split("[이유]\n", 1)[1]
    assert "제28조" in reason
    assert "따라서 이 경우에는 허용됩니다." in reason


def test_link_only_control_block_still_bounds_the_section():
    """3. 목록/이전글 같은 링크 전용 블록은 여전히 경계다."""
    sc = _scraper(_Fetcher(html=INLINE_LINK_MULTI_PARA_HTML))
    post = _post(_detail())
    sc.enrich(post)
    assert "목록" not in post.body
    assert "이전글" not in post.body
    assert "조작 블록 뒤이므로" not in post.body


def test_attachment_control_still_bounds_the_section():
    """4. 공식 displayFile.do 컨트롤 뒤 텍스트는 이유에 들어가지 않는다."""
    fetcher = _Fetcher(html=INLINE_LINK_THEN_FILE_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())
    sc.enrich(post)
    assert "허용됩니다" in post.body                    # 링크 있는 본문 문단은 보존
    assert "첨부 컨트롤 뒤 안내문" not in post.body
    assert [a.filename for a in post.attachments] == ["회신문.hwp"]


# --- 첨부 파일명: generic UI 라벨 대신 orgFileName ---
#
# 외부에서 확인된 사실: 공식 다운로드 URL 은 /fsc_new/file/displayFile.do 이고
# filePath·orgFileName·sysFileName 파라미터를 싣는다. 아래 HTML 구조 자체는
# 테스트용으로 구성한 synthetic fixture 다.

_FILE_BASE = "/fsc_new/file/displayFile.do?filePath=%2Freply&sysFileName=1.hwp"


def _file_detail_html(*anchors):
    rows = "\n".join(anchors)
    return f"""
<div id="content"><h2>겸영업무 해당 여부</h2><table><tbody>
  <tr><th>회신일</th><td>2026-08-20</td></tr>
  <tr><th>질의요지</th><td>질의 본문입니다.</td></tr>
  <tr><th>회답</th><td>회답 본문입니다.</td></tr>
  <tr><th>이유</th><td>이유 본문입니다.</td></tr>
  <tr><th>첨부파일</th><td>{rows}</td></tr>
</tbody></table></div>
"""


def _filenames(html):
    sc = _scraper(_Fetcher(html=html))
    post = _post(_detail())
    sc.enrich(post)
    return [a.filename for a in post.attachments]


def test_generic_download_label_uses_org_filename():
    """1. anchor text 가 '다운로드' 여도 orgFileName 의 원본 파일명을 쓴다."""
    href = (
        _FILE_BASE
        + "&orgFileName=%EB%B2%95%EB%A0%B9%ED%95%B4%EC%84%9D+%ED%9A%8C%EC%8B%A0%EB%AC%B8.hwpx"
    )
    assert _filenames(_file_detail_html(f'<a href="{href}">다운로드</a>')) == [
        "법령해석 회신문.hwpx"
    ]


def test_generic_attachment_label_uses_org_filename():
    """2. '첨부파일' 라벨도 마찬가지("+" 는 공백으로 디코딩된다)."""
    href = (
        _FILE_BASE
        + "&orgFileName=%EB%B9%84%EC%A1%B0%EC%B9%98%EC%9D%98%EA%B2%AC%EC%84%9C"
        "+%ED%9A%8C%EC%8B%A0%EB%AC%B8.hwp"
    )
    assert _filenames(_file_detail_html(f'<a href="{href}">첨부파일</a>')) == [
        "비조치의견서 회신문.hwp"
    ]


def test_two_generic_labels_keep_distinct_original_filenames():
    """3. 같은 '다운로드' 라벨이라도 첨부끼리 구분된다."""
    a1 = f'<a href="{_FILE_BASE}&orgFileName=A.hwp">다운로드</a>'
    a2 = f'<a href="{_FILE_BASE}&orgFileName=B.hwpx&sysFileName=2.hwp">다운로드</a>'
    assert _filenames(_file_detail_html(a1, a2)) == ["A.hwp", "B.hwpx"]


def test_meaningful_anchor_text_is_kept_without_org_filename():
    """4. 의미 있는 앵커 텍스트는 그대로 쓰고 크기 표기만 제거한다."""
    assert _filenames(
        _file_detail_html(f'<a href="{_FILE_BASE}">회신문.hwp (42 KB)</a>')
    ) == ["회신문.hwp"]


def test_generic_label_without_org_filename_falls_back():
    """5. orgFileName 이 없으면 generic 라벨 대신 명확한 기본값을 쓴다."""
    assert _filenames(_file_detail_html(f'<a href="{_FILE_BASE}">다운로드</a>')) == [
        "첨부파일"
    ]


def test_org_filename_is_reduced_to_a_basename():
    """외부 문자열이므로 경로 조각은 떼고 파일명만 남긴다."""
    href = _FILE_BASE + "&orgFileName=..%2F..%2Fetc%2Fpasswd"
    assert _filenames(_file_detail_html(f'<a href="{href}">다운로드</a>')) == ["passwd"]


# --- REVIEW 2: 제목 대조는 페이지 전체가 아니라 '정식 제목' 자리에서 ---
#
# 잘못된 상세 B 가 와도 그 페이지의 이전글/다음글·관련글·푸터에 A 의 제목이 있으면
# 페이지 전체 substring 검색은 통과해 버린다. 회신일까지 같으면 B 의 회답·첨부가
# A 밑에 실린다 — 이 가드가 막아야 하는 바로 그 상황이다.

# Codex repro: 정식 제목은 B 인데, 이전글/다음글 내비게이션에 A 의 제목이 있고
# 회신일도 A 와 같다.
NAV_CONTAINS_OTHER_TITLE_HTML = """
<div id="content">
  <h2>B 사건에 대한 질의</h2>
  <table><tbody>
    <tr><th>회신일</th><td>2026-08-20</td></tr>
    <tr><th>질의요지</th><td>B 사건의 질의입니다.</td></tr>
    <tr><th>회답</th><td>B 사건의 회답입니다.</td></tr>
    <tr><th>이유</th><td>B 사건의 이유입니다.</td></tr>
    <tr><th>첨부파일</th><td>
      <a href="/fsc_new/file/displayFile.do?filePath=%2Fb&amp;orgFileName=b.hwp&amp;sysFileName=b.hwp">B첨부.hwp</a>
    </td></tr>
  </tbody></table>
  <nav class="prev-next">
    <a href="/fsc_new/replyCase/LawreqDetail.do?lawreqIdx=1">이전글 겸영업무 해당 여부</a>
    <a href="/fsc_new/replyCase/LawreqDetail.do?lawreqIdx=3">다음글 겸영업무 해당 여부</a>
  </nav>
</div>
"""

# 푸터에 A 의 제목이 있는 변형.
FOOTER_CONTAINS_OTHER_TITLE_HTML = """
<div id="content">
  <h2>B 사건에 대한 질의</h2>
  <table><tbody>
    <tr><th>회신일</th><td>2026-08-20</td></tr>
    <tr><th>질의요지</th><td>B 사건의 질의입니다.</td></tr>
    <tr><th>회답</th><td>B 사건의 회답입니다.</td></tr>
    <tr><th>이유</th><td>B 사건의 이유입니다.</td></tr>
  </tbody></table>
  <footer>최근 조회: 겸영업무 해당 여부</footer>
</div>
"""

# 실제 비조치의견서 페이지처럼, 상단 정식 제목과 회신영역에 '비슷하지만 다른' 문자열이
# 함께 존재하는 경우(외부에서 확인된 사실).
SIMILAR_TITLE_HTML = """
<div id="content">
  <h2>미등록 PG 계약체결 금지 관련 비조치의견서</h2>
  <table><tbody>
    <tr><th>회신일</th><td>2025-09-10</td></tr>
    <tr><th>질의요지</th><td>미등록 PG 계약체결 금지 관련 질의입니다.</td></tr>
    <tr><th>회답</th><td>비조치의견서를 직권발급합니다.</td></tr>
    <tr><th>이유</th><td>전자금융거래법 위반 소지가 있습니다.</td></tr>
  </tbody></table>
</div>
"""

# 정식 제목을 특정할 수 없는 페이지(제목 라벨 없음 + 후보 heading 이 여럿).
AMBIGUOUS_TITLE_HTML = """
<div id="content">
  <h2>금융규제·법령해석포털</h2>
  <h3>회신사례 상세</h3>
  <table><tbody>
    <tr><th>회신일</th><td>2026-08-20</td></tr>
    <tr><th>질의요지</th><td>겸영업무 신고 대상인지 여부를 질의함.</td></tr>
    <tr><th>회답</th><td>신고 대상에 해당하지 않습니다.</td></tr>
    <tr><th>이유</th><td>은행법 제28조에 따릅니다.</td></tr>
  </tbody></table>
</div>
"""


def test_title_scope_accepts_matching_dedicated_title():
    """A. 정식 제목과 회신일이 맞으면 본문·첨부를 정상 수집한다."""
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())
    sc.enrich(post)
    assert "[회답]" in post.body
    assert [a.filename for a in post.attachments] == ["회신문.hwp"]
    assert post.attachments[0].data == b"HWP"
    assert len(fetcher.downloaded) == 1


def test_title_in_navigation_does_not_pass_identity(caplog):
    """B. 정식 제목이 B 인데 이전글/다음글에 A 제목이 있어도 통과하면 안 된다."""
    fetcher = _Fetcher(html=NAV_CONTAINS_OTHER_TITLE_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())              # 제목 A, 회신일 2026-08-20 (상세와 동일)
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == ""
    assert post.attachments == []
    assert fetcher.downloaded == []      # 첨부 추출/다운로드 전에 return
    assert "상세 제목이 목록과 다름" in caplog.text
    assert "B 사건" not in post.body


def test_title_in_footer_does_not_pass_identity():
    """C. 푸터에 A 제목이 있어도 정식 제목이 다르면 거부."""
    fetcher = _Fetcher(html=FOOTER_CONTAINS_OTHER_TITLE_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())
    sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []


def test_similar_but_not_equal_title_is_rejected(caplog):
    """E. 부분 문자열이 아니라 정확히 같은 제목일 때만 통과한다."""
    fetcher = _Fetcher(html=SIMILAR_TITLE_HTML)
    sc = _scraper(fetcher)
    post = _post(
        _detail("OpinionDetail.do"),
        title="[비조치의견서] 미등록 PG 계약체결 금지 관련 비조치의견서 직권발급",
        date="2025-09-10",
    )
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert "상세 제목이 목록과 다름" in caplog.text


def test_ambiguous_detail_title_fails_closed(caplog):
    """정식 제목을 특정할 수 없으면 전체 페이지 검색으로 되돌아가지 않는다."""
    fetcher = _Fetcher(html=AMBIGUOUS_TITLE_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []
    assert "정식 제목을 확인할 수 없어" in caplog.text


def test_detail_title_from_labelled_value():
    """'제목' 라벨이 붙은 값이 있으면 그것을 정식 제목으로 쓴다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML_HEADING))
    post = _opinion_post()
    sc.enrich(post)
    assert "[회답]" in post.body


def test_title_whitespace_differences_are_tolerated():
    """D. 줄바꿈·중복 공백·NBSP 차이는 정상 허용(문장부호는 보존)."""
    html = DETAIL_HTML.replace(
        '<h2 class="title">겸영업무 해당 여부</h2>',
        '<h2 class="title">겸영업무\n   해당&nbsp;&nbsp;여부</h2>',
    )
    sc = _scraper(_Fetcher(html=html))
    post = _post(_detail())
    sc.enrich(post)
    assert "[회답]" in post.body


def test_identity_path_does_not_use_whole_page_substring():
    """회귀 방어: production identity 경로에 페이지 전체 substring 검색이 없어야 한다.

    (있으면 위 nav/footer 케이스가 조용히 다시 통과한다.)
    """
    import inspect

    from src.scrapers.better_fsc import BetterReplyScraper as _S

    source = "".join(
        inspect.getsource(fn)
        for fn in (_S._identity_ok, _S._detail_title, _S._heading_title,
                   _S._labelled_values, _S._reply_date)
    )
    assert "soup.get_text" not in source


# --- REVIEW 1: 한 자리 월/일 한국어 날짜 ---
def _with_reply_date(value):
    return DETAIL_HTML.replace("<td>2026-08-20</td>", f"<td>{value}</td>")


def test_korean_single_digit_detail_date_matches_list_date():
    """1. 목록 2026-08-20 ↔ 상세 '2026년 8월 20일' 은 같은 날이다."""
    sc = _scraper(_Fetcher(html=_with_reply_date("2026년 8월 20일")))
    post = _post(_detail(), date="2026-08-20")
    sc.enrich(post)
    assert "[회답]" in post.body


def test_korean_single_digit_list_date_matches_detail_date():
    """2. 반대 방향(목록이 한국어 표기)도 같다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML))          # 상세는 2026-08-20
    post = _post(_detail(), date="2026년 8월 20일")
    sc.enrich(post)
    assert "[회답]" in post.body


def test_single_digit_month_and_day_canonicalize_equally():
    """3. '2026-8-2' 와 '2026년 8월 2일' 은 같은 canonical date."""
    sc = _scraper(_Fetcher(html=_with_reply_date("2026년 8월 2일")))
    post = _post(_detail(), date="2026-8-2")
    sc.enrich(post)
    assert "[회답]" in post.body


def test_date_parser_canonical_forms():
    """지원 표기가 모두 같은 canonical 값이 되고, 달력에 없는 값은 거부된다."""
    from src.scrapers.better_fsc import _parse_date

    for text in (
        "2026-08-20", "2026-8-20", "2026.08.20", "2026.8.20",
        "2026/08/20", "2026/8/20", "2026년 08월 20일", "2026년 8월 20일", "20260820",
    ):
        assert _parse_date(text) == "20260820", text
    for bad in ("2026-02-29", "2026-13-01", "2026-00-10", "2026-04-31"):
        assert _parse_date(bad) == "", bad
    # 주변 숫자를 날짜로 오인하지 않는다.
    for bad in ("제2026-15호", "2026-08-2012", "20268 20", ""):
        assert _parse_date(bad) == "", bad


def test_invalid_detail_date_is_rejected_not_skipped(caplog):
    """4/5. 상세 날짜가 달력에 없으면 '검사 생략'이 아니라 reject."""
    fetcher = _Fetcher(html=_with_reply_date("2026-02-29"))
    sc = _scraper(fetcher)
    post = _post(_detail(), date="2026-08-20")
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []
    assert "동일 게시물 확인 불가" in caplog.text


def test_unparsable_list_date_is_rejected_not_skipped(caplog):
    """5. 목록 날짜가 해석 불가면 조용히 날짜 검사를 건너뛰지 않는다."""
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail(), date="언젠가")
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []
    assert "해석할 수 없어" in caplog.text


def test_empty_list_date_keeps_existing_skip_policy():
    """목록 날짜가 아예 빈 legacy 케이스는 기존대로 날짜 대조를 건너뛴다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    post = _post(_detail(), date="")
    sc.enrich(post)
    assert "[회답]" in post.body        # 제목 대조만으로 통과(기존 정책 유지)


# --- REVIEW 3: 번호 매김 라벨 ---
NUMBERED_HEADING_HTML = """
<div id="content">
  <h2>겸영업무 해당 여부</h2>
  <table><tbody><tr><th>회신일</th><td>2026-08-20</td></tr></tbody></table>
  <div class="view">
    <h4>(1) 질의요지</h4>
    <p>겸영업무 신고 대상인지 여부를 질의함.</p>
    <h4>2) 회답</h4>
    <p>신고 대상에 해당하지 않습니다.</p>
    <h4>3. 이유</h4>
    <p>은행법 제28조에 따릅니다.</p>
  </div>
</div>
"""


def test_norm_label_strips_numbering_prefixes():
    from src.scrapers.better_fsc import _norm_label

    assert _norm_label("(1) 질의요지") == "질의요지"
    assert _norm_label("1) 회답") == "회답"
    assert _norm_label("2. 이유") == "이유"
    assert _norm_label("1: 질의요지") == "질의요지"
    # 기존 장식 처리는 그대로
    assert _norm_label("□ 질의요지") == "질의요지"
    assert _norm_label("[회답]") == "회답"
    assert _norm_label("● 회답") == "회답"
    assert _norm_label("질의 요지") == "질의요지"
    # 정상 라벨의 글자를 삼키지 않는다
    assert _norm_label("1차 회답") == "1차회답"


def test_numbered_headings_are_parsed_end_to_end(caplog):
    """번호가 붙은 heading 세 개가 production 경로에서 모두 잡힌다."""
    sc = _scraper(_Fetcher(html=NUMBERED_HEADING_HTML))
    post = _post(_detail())
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert "[질의요지]\n겸영업무 신고 대상인지 여부를 질의함." in post.body
    assert "[회답]\n신고 대상에 해당하지 않습니다." in post.body
    assert "[이유]\n은행법 제28조에 따릅니다." in post.body
    assert "찾지 못해" not in caplog.text          # missing warning 없음


# --- 검증되지 않은 상세 후보 링크는 사용자에게 노출하지 않는다 ---
#
# 상세 URL 은 아직 라이브 확인되지 않은 dataIdx 매핑으로 만든 '후보' 다. identity 를
# 확인하기 전까지는 본문·첨부뿐 아니라 링크도 신뢰하지 않는다 — 본문만 막고 링크를
# 그대로 두면 제목만 보고 누른 사용자가 다른 사건의 상세로 간다.


def test_verified_detail_keeps_its_url():
    """A. identity 통과 시 상세 URL 을 그대로 유지한다."""
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    detail_url = _detail()
    post = _post(detail_url)
    sc.enrich(post)
    assert post.url == detail_url
    assert "[회답]" in post.body
    assert post.attachments[0].data == b"HWP"


def test_identity_mismatch_falls_back_to_list_url():
    """B. 다른 게시물이면 본문·첨부·다운로드는 물론 링크도 되돌린다."""
    fetcher = _Fetcher(html=NAV_CONTAINS_OTHER_TITLE_HTML)
    sc = _scraper(fetcher)
    post = _post(_detail())
    sc.enrich(post)
    assert post.url == LIST_URL
    assert post.body == ""
    assert post.attachments == []
    assert fetcher.downloaded == []


def test_wrong_reply_date_falls_back_to_list_url():
    """C. 제목은 같아도 회신일이 다르면 링크까지 되돌린다."""
    sc = _scraper(_Fetcher(html=WRONG_DATE_HTML))
    post = _post(_detail())
    sc.enrich(post)
    assert post.url == LIST_URL


def test_unverifiable_title_falls_back_to_list_url():
    """D. 정식 제목을 특정할 수 없으면 링크를 신뢰하지 않는다."""
    sc = _scraper(_Fetcher(html=AMBIGUOUS_TITLE_HTML))
    post = _post(_detail())
    sc.enrich(post)
    assert post.url == LIST_URL


def test_error_page_falls_back_to_list_url():
    """E. 포털 ERROR PAGE 는 그 후보 URL 이 살아 있다는 근거가 되지 못한다."""
    sc = _scraper(_Fetcher(html=ERROR_HTML))
    detail_url = _detail()
    post = _post(detail_url)
    assert post.url == detail_url          # enrich 전에는 후보 URL
    sc.enrich(post)
    assert post.url == LIST_URL
    assert post.body == "" and post.attachments == []


def test_detail_request_failure_falls_back_to_list_url(caplog):
    """F. GET 실패도 후보 URL 을 검증하지 못한 것이므로 되돌린다(예외는 전파 안 함)."""
    fetcher = _Fetcher(html=DETAIL_HTML)
    fetcher.get_error = RuntimeError("boom")
    sc = _scraper(fetcher)
    detail_url = _detail()
    post = _post(detail_url)
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert post.url == LIST_URL
    assert detail_url in caplog.text       # 어떤 후보가 실패했는지 로그에 남는다


def test_partial_body_keeps_verified_detail_url():
    """G. identity 는 통과했는데 본문만 못 읽은 경우 — 링크는 유지해야 한다.

    'enrich_succeeded == False' 를 근거로 링크까지 되돌리면, 사용자가 원문을 직접 볼
    길이 사라진다. 이 상세가 이 글의 것임은 이미 확인됐다.
    """
    sc = _scraper(_Fetcher(html=DETAIL_HTML_PARTIAL_WITH_FILE))
    detail_url = _detail()
    post = _post(detail_url)
    sc.enrich(post)
    assert post.body == ""
    assert sc.enrich_succeeded(post) is False
    assert post.url == detail_url          # 링크는 그대로


def test_attachment_download_failure_keeps_verified_detail_url():
    """H. identity 통과 후 첨부 다운로드만 실패한 경우도 링크를 유지한다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML, download_error=RuntimeError("net")))
    detail_url = _detail()
    post = _post(detail_url)
    sc.enrich(post)
    assert post.url == detail_url
    assert "[회답]" in post.body
    assert post.attachments[0].data is None


def test_attachment_too_large_keeps_verified_detail_url():
    """AttachmentTooLarge 도 identity 이후의 실패이므로 링크를 되돌리지 않는다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML, download_error=AttachmentTooLarge(999, 10)))
    detail_url = _detail()
    post = _post(detail_url)
    sc.enrich(post)
    assert post.url == detail_url


def test_rejected_candidate_url_never_reaches_the_mail():
    """I. notifier 는 production 코드 그대로 — post.url fallback 만으로 만족해야 한다."""
    fetcher = _Fetcher(html=NAV_CONTAINS_OTHER_TITLE_HTML)
    sc = _scraper(fetcher)
    candidate = _detail()
    post = _post(candidate)
    sc.enrich(post)
    assert post.url == LIST_URL

    html = build_html({post.source_name: [post]})
    text = build_text({post.source_name: [post]})
    from html import escape as _html_escape

    assert LIST_URL in text
    assert _html_escape(LIST_URL) in html       # HTML 은 & 가 &amp; 로 이스케이프된다
    assert "LawreqDetail.do" not in html        # 후보 URL 은 어떤 형태로도 나가지 않는다
    assert "LawreqDetail.do" not in text
    assert candidate not in text


def test_unsupported_type_url_is_untouched():
    """미지원 구분은 원래 목록 URL 이며 enrich 가 건드리지 않는다(기존 동작)."""
    fetcher = _Fetcher(html=DETAIL_HTML)
    sc = _scraper(fetcher)
    post = _post(LIST_URL)
    sc.enrich(post)
    assert post.url == LIST_URL
    assert fetcher.get_calls == []


# --- 상세 수집 성공 판정(enrich_succeeded) ---
def test_attachment_only_reply_is_not_a_detail_success():
    """A. 본문이 비고 첨부만 잡힌 상태는 이 소스의 계약상 실패다."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML_PARTIAL_WITH_FILE))
    post = _post(_detail())
    sc.enrich(post)
    assert post.body == "" and post.attachments != []
    assert sc.enrich_succeeded(post) is False
    # 기본 판정이었다면 성공으로 잡혔을 상태라는 것을 명시한다.
    assert bool(post.body or post.details or post.attachments) is True


def test_complete_body_with_attachment_is_a_detail_success():
    """B. 세 항목 + 첨부가 모두 있으면 성공."""
    sc = _scraper(_Fetcher(html=DETAIL_HTML))
    post = _post(_detail())
    sc.enrich(post)
    assert post.body != "" and post.attachments != []
    assert sc.enrich_succeeded(post) is True


def test_base_scraper_success_hook_keeps_existing_meaning():
    """C. 다른 스크래퍼는 기존 bool(body/details/attachments) 의미 그대로."""
    from src.config import SourceConfig as _SC
    from src.models import Attachment
    from src.scrapers.base import BaseScraper

    class _Plain(BaseScraper):
        pass

    plain = _Plain(_SC(key="k", name="n", type="t", list_url="https://x/"), fetcher=None)
    empty = _post("https://x/1")
    assert plain.enrich_succeeded(empty) is False
    empty.attachments.append(Attachment(filename="a.pdf", url="https://x/a.pdf"))
    assert plain.enrich_succeeded(empty) is True       # 첨부만 있어도 성공(기존 의미)
    only_details = _post("https://x/2")
    only_details.details = [("금융기관명", "A은행")]
    assert plain.enrich_succeeded(only_details) is True


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
