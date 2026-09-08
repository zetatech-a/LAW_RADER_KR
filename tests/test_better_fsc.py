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

import pytest
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


# =============================================================================
# 2026-09-07 운영 회귀 — lawreqIdx 5449 / 5450
#
# 이 두 건은 상세 수집까지 갔다가 identity 대조에서 전부 거부되었다. 상세 제목으로
# 페이지 유형 heading('법령해석')이 잡혀 목록 제목과 어긋났기 때문이다(본문 0/2,
# Gemini 대상 0건, 메일에 제목·링크만). 아래 fixture 는 실제 공개 상세페이지 DOM 에서
# parser 관련 부분만 축약한 것이며(tests/fixtures/better_fsc/ 의 주석 참고), 이 회귀를
# 구조로 잠근다.
# =============================================================================
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "better_fsc")

LIVE_CASES = {
    "5449": {
        "title": "여신전문금융회사가 신기술사업자에 투자하는 경우 금리인하요구권 안내의무 적용 여부",
        "attachment": "법령해석 회신문(260149)F.hwpx",
        "answer_head": "□ 신기술사업자에 대한 투자는 「여신전문금융업법」 제50조의13(금리인하 요구)",
    },
    "5450": {
        "title": "신기술사업금융업자의 글로벌펀드(외국법에 따른 외국펀드) 결성·운용 가능여부",
        "attachment": "법령해석 회신문(260150)F.hwpx",
        "answer_head": "□ 신기술사업금융업자가 외국법에 따른 외국펀드",
    },
}


def _live_html(idx):
    with open(os.path.join(_FIXTURES, f"lawreq_{idx}_detail.html"), encoding="utf-8") as f:
        return f.read()


def _live_post(idx):
    """목록 record 그대로의 Post(제목 접두어·회신일 포함)."""
    url = LIST_URL.replace("TotalReplyList.do", "LawreqDetail.do") + f"&lawreqIdx={idx}"
    return Post(
        source_key="better_reply",
        source_name="금융규제포털 · 법령해석·비조치의견서 회신사례",
        post_id=f"dataIdx:{idx}",
        title=f"[법령해석] {LIVE_CASES[idx]['title']}",
        url=url,
        date="2026-09-07",
    )


def _soup(idx):
    from bs4 import BeautifulSoup

    return BeautifulSoup(_live_html(idx), "lxml")


# --- A/B. 실제 사건 제목 추출 ---
def test_live_5449_detail_title_is_the_case_title():
    assert BetterReplyScraper._detail_title(_soup("5449")) == LIVE_CASES["5449"]["title"]


def test_live_5450_detail_title_is_the_case_title():
    assert BetterReplyScraper._detail_title(_soup("5450")) == LIVE_CASES["5450"]["title"]


# --- C. 페이지 유형 heading 은 제목이 아니다 ---
def test_live_page_type_heading_is_never_the_detail_title():
    """상세에 <h3>법령해석</h3> 이 있어도 그것을 제목으로 돌려주면 안 된다."""
    for idx in LIVE_CASES:
        soup = _soup(idx)
        assert soup.find("h3").get_text(strip=True) == "법령해석"      # fixture 전제 확인
        assert BetterReplyScraper._detail_title(soup) != "법령해석"
        # heading 폴백 자체도 유형 heading 을 후보로 삼지 않는다.
        assert BetterReplyScraper._heading_title(soup) == ""


# --- D/E. identity 통과 ---
def test_live_5449_identity_passes_and_keeps_detail_url():
    fetcher = _Fetcher(html=_live_html("5449"))
    sc = _scraper(fetcher)
    post = _live_post("5449")
    detail_url = post.url
    sc.enrich(post)
    assert post.url == detail_url          # 목록 URL 로 되돌아가지 않았다
    assert post.body != ""


def test_live_5450_identity_passes_and_keeps_detail_url():
    fetcher = _Fetcher(html=_live_html("5450"))
    sc = _scraper(fetcher)
    post = _live_post("5450")
    detail_url = post.url
    sc.enrich(post)
    assert post.url == detail_url
    assert post.body != ""


def test_live_identity_uses_the_reply_date_row():
    """회신일(2026-09-07)이 다르면 여전히 거부한다 — 제목만으로 통과하지 않는다."""
    html = _live_html("5449").replace("2026-09-07\n", "2019-03-04\n")
    fetcher = _Fetcher(html=html)
    sc = _scraper(fetcher)
    post = _live_post("5449")
    sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert post.url == LIST_URL


# --- F. 본문 3/3 ---
def test_live_bodies_have_all_three_sections():
    for idx, case in LIVE_CASES.items():
        sc = _scraper(_Fetcher(html=_live_html(idx)))
        post = _live_post(idx)
        sc.enrich(post)
        for label in ("[질의요지]", "[회답]", "[이유]"):
            assert label in post.body, (idx, label)
        assert post.body.index("[질의요지]") < post.body.index("[회답]") < post.body.index("[이유]")
        assert case["answer_head"] in post.body
        assert sc.enrich_succeeded(post) is True
        # 본문에 조작·푸터 텍스트가 섞이지 않는다.
        for garbage in ("URL 복사", "COPYRIGHT", "대표전화", case["attachment"]):
            assert garbage not in post.body, (idx, garbage)


# --- G. 첨부 ---
def test_live_attachments_are_collected_with_the_original_filename():
    for idx, case in LIVE_CASES.items():
        fetcher = _Fetcher(html=_live_html(idx))
        sc = _scraper(fetcher)
        post = _live_post(idx)
        sc.enrich(post)
        assert [a.filename for a in post.attachments] == [case["attachment"]], idx
        att = post.attachments[0]
        assert att.url.startswith("https://better.fsc.go.kr/fsc_new/file/displayFile.do")
        assert att.data == b"HWP"
        assert len(fetcher.downloaded) == 1


# --- H. Gemini 요약 대상 ---
def test_live_enriched_posts_are_general_summary_targets():
    """실제 Summarizer 일반 경로에서 요약 대상이 되고, 3줄 요약이 붙는다."""
    for idx in LIVE_CASES:
        sc = _scraper(_Fetcher(html=_live_html(idx)))
        post = _live_post(idx)
        sc.enrich(post)
        assert len(_prepare_body(_llm_cfg(), post)) >= _llm_cfg().min_body_chars

        # 실제 일반 요약 경로(summarize_all)를 태운다 — Gemini 호출만 가짜 응답으로.
        summarizer = Summarizer(_llm_cfg())
        prompts: list[str] = []

        def _generate(prompt, deadline=None, **kw):
            prompts.append(prompt)
            return _envelope('{"summary": ["요지 1", "요지 2", "요지 3"]}')

        summarizer._generate = _generate
        assert summarizer.summarize_all({post.source_name: [post]}) == 1, idx
        assert len(prompts) == 1, idx           # 이 글로 실제 호출이 일어났다
        assert "질의요지" in prompts[0] and "회답" in prompts[0], idx
        assert post.summary == ["요지 1", "요지 2", "요지 3"], idx


# --- I. 메일 렌더 ---
def test_live_post_renders_summary_and_attachment_in_the_mail():
    sc = _scraper(_Fetcher(html=_live_html("5449")))
    post = _live_post("5449")
    sc.enrich(post)
    post.summary = ["첫째 줄", "둘째 줄", "셋째 줄"]

    html = build_html({post.source_name: [post]})
    text = build_text({post.source_name: [post]})
    for line in post.summary:
        assert line in html and line in text
    assert LIVE_CASES["5449"]["attachment"] in html
    assert LIVE_CASES["5449"]["attachment"] in text
    assert "lawreqIdx=5449" in text            # 검증된 상세 링크가 그대로 나간다


# --- 회귀 방어: 유형 heading 이 다시 제목으로 잡히면 안 된다 ---
def test_live_fixture_would_have_failed_before_the_fix():
    """이 fixture 로 예전 동작(유형 heading 채택)이 재현되지 않는지 못 박는다.

    예전 구현에서는 본문 라벨 앞의 유일한 heading 후보가 <h3>법령해석</h3> 이라
    _detail_title 이 '법령해석' 을 돌려주고 목록 제목과 어긋나 전부 거부됐다.
    """
    for idx, case in LIVE_CASES.items():
        soup = _soup(idx)
        headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3", "h4"])]
        assert "법령해석" in headings                      # 유형 heading 이 여전히 있다
        assert case["title"] not in headings               # 사건 제목은 heading 이 아니다
        assert BetterReplyScraper._detail_title(soup) == case["title"]


# --- identity mismatch 시 HTML 스냅샷(진단용) ---
#
# 이번 운영 버그에서는 로그에 "상세 '법령해석' ↔ 목록 '<제목>'" 까지만 남아 실제 DOM 을
# 볼 수 없었다. 정상 수집에서는 남기지 않고, 확인 실패에서만 응답 본문을 남긴다.
def test_identity_mismatch_dumps_the_response_html(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sc = _scraper(_Fetcher(html=NAV_CONTAINS_OTHER_TITLE_HTML))
    sc.enrich(_post(_detail() + "&lawreqIdx=5449"))

    dumped = list((tmp_path / "debug").glob("*.html"))
    assert len(dumped) == 1
    assert dumped[0].name == "better_reply_identity_mismatch_5449.html"
    body = dumped[0].read_text(encoding="utf-8")
    # 응답 본문만 남는다(요청 헤더·쿠키 없음) + 진단에 필요한 구조·공개 텍스트는 보존.
    assert "B 사건에 대한 질의" in body
    assert 'class="subject"' in body or "B 사건의 회답" in body
    assert "Cookie" not in body and "User-Agent" not in body


def test_error_page_dumps_the_response_html(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sc = _scraper(_Fetcher(html=ERROR_HTML))
    sc.enrich(_post(_detail() + "&lawreqIdx=5449"))
    assert (tmp_path / "debug" / "better_reply_error_5449.html").exists()


def test_successful_enrich_does_not_dump_anything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sc = _scraper(_Fetcher(html=_live_html("5450")))
    post = _live_post("5450")
    sc.enrich(post)
    assert post.body != ""
    assert not (tmp_path / "debug").exists()


def test_detail_idx_is_read_from_the_url():
    from src.scrapers.better_fsc import BetterReplyScraper as S

    assert S._detail_idx("https://x/LawreqDetail.do?stNo=11&lawreqIdx=5449") == "5449"
    assert S._detail_idx("https://x/OpinionDetail.do?opinionIdx=2285") == "2285"
    # 파일명이 되므로 숫자가 아니면 쓰지 않는다(경로 조각 유입 방지).
    assert S._detail_idx("https://x/LawreqDetail.do?lawreqIdx=../../etc/passwd") == "unknown"
    assert S._detail_idx("https://x/TotalReplyList.do") == "unknown"


# =============================================================================
# Codex 리뷰 1 — 아티팩트로 나가는 debug HTML 은 정화본이어야 한다
#
# 요청 헤더·쿠키를 저장하지 않는 것만으로는 부족하다. 응답 HTML 자체가 살아 있는
# CSRF 토큰·세션 값·인라인 토큰을 싣고 오고, debug/ 는 verify 워크플로가 아티팩트로
# 올린다. 캡처 스크립트는 같은 내용을 Actions 로그로도 찍으므로 로그도 같은 경계다.
# =============================================================================
SECRET_DETAIL_HTML = """
<div id="content">
  <head><meta name="_csrf" content="csrf-secret-123"></head>
  <h2>B 사건에 대한 질의</h2>
  <input type="hidden" name="sessionId" value="session-secret">
  <input type="hidden" name="lawreqIdx" value="5449">
  <table><tbody>
    <tr><th>회신일</th><td>2026-08-20</td></tr>
    <tr><th>질의요지</th><td>B 사건의 질의입니다.</td></tr>
    <tr><th>회답</th><td>B 사건의 회답입니다.</td></tr>
    <tr><th>이유</th><td>B 사건의 이유입니다.</td></tr>
  </tbody></table>
  <a href="/fsc_new/replyCase/LawreqDetail.do?lawreqIdx=5449&amp;token=xyz">관련</a>
  <a href="/callback#access_token=fragment-secret">callback</a>
  <a href="#session=fragment-session">프래그먼트만</a>
  <a href="#">top</a>
  <script>window.token = "super-secret";</script>
</div>
"""


def _dumped_html(tmp_path):
    files = list((tmp_path / "debug").glob("*.html"))
    assert len(files) == 1, files
    return files[0].read_text(encoding="utf-8")


def test_identity_mismatch_dump_is_sanitized(tmp_path, monkeypatch):
    """A. _dump_identity_debug 가 저장한 HTML 에 비밀이 남지 않는다."""
    monkeypatch.chdir(tmp_path)
    sc = _scraper(_Fetcher(html=SECRET_DETAIL_HTML))
    post = _post(_detail() + "&lawreqIdx=5449")
    sc.enrich(post)
    assert post.body == "" and post.attachments == []        # identity 는 여전히 거부

    body = _dumped_html(tmp_path)
    for secret in (
        "csrf-secret-123", "session-secret", "super-secret", "token=xyz",
        "fragment-secret", "fragment-session",      # URL 프래그먼트에 실린 credential
    ):
        assert secret not in body, secret
    # 진단 가치는 남는다 — 필드 이름·구조·공개 식별자·본문.
    assert 'name="_csrf"' in body and 'name="sessionId"' in body
    assert "lawreqIdx=5449" in body
    assert "B 사건에 대한 질의" in body
    assert 'href="/callback#REDACTED"' in body       # 프래그먼트 값만 사라진다
    assert 'href="#"' in body                        # 값 없는 로컬 앵커는 그대로


def test_error_page_dump_is_sanitized(tmp_path, monkeypatch):
    """ERROR PAGE 덤프도 같은 정화를 거친다."""
    monkeypatch.chdir(tmp_path)
    html = ERROR_HTML.replace(
        "<div class=\"error\">",
        '<div class="error"><meta name="_csrf" content="csrf-secret-123">',
    )
    sc = _scraper(_Fetcher(html=html))
    sc.enrich(_post(_detail() + "&lawreqIdx=5449"))
    assert "csrf-secret-123" not in _dumped_html(tmp_path)


def test_production_parsing_uses_raw_html_not_the_sanitized_copy():
    """정화는 덤프 전용이다 — 본문·첨부는 원본에서 뽑아야 한다.

    정화본으로 파싱하면 32자 이상 16진수를 값 패턴으로 지우므로 실제 첨부 URL 의
    sysFileName 이 REDACTED 가 되어 다운로드가 깨진다.
    """
    fetcher = _Fetcher(html=_live_html("5450"))
    sc = _scraper(fetcher)
    post = _live_post("5450")
    sc.enrich(post)
    assert post.attachments[0].url.endswith("0180752ab85844dd91c87fe7eb5d681c.hwpx")
    assert "REDACTED" not in post.attachments[0].url
    assert "REDACTED" not in post.body


# --- B/C. 캡처 스크립트: 파일도 stdout 도 정화본에서만 만든다 ---
def test_capture_script_persists_and_logs_only_sanitized_html(tmp_path, monkeypatch, capsys):
    """저장 파일과 Actions stdout 어디에도 비밀이 평문으로 남지 않는다."""
    import scripts.capture_better_reply_detail as cap

    monkeypatch.chdir(tmp_path)

    class _Resp:
        status_code = 200
        encoding = "utf-8"

    class _CapFetcher:
        def get(self, url, *, referer=None, **kw):
            return _Resp()

        @staticmethod
        def text(resp):
            return SECRET_DETAIL_HTML

    sc = cap._scraper()
    sc.fetcher = _CapFetcher()
    assert cap.capture(sc, "5449", "법령해석", "B 사건에 대한 질의", 50, 1) == 0

    stdout = capsys.readouterr().out
    saved = (tmp_path / "debug" / "better_reply_detail_5449.html").read_text(encoding="utf-8")
    for secret in (
        "csrf-secret-123", "session-secret", "super-secret", "token=xyz",
        "fragment-secret", "fragment-session",
    ):
        assert secret not in saved, ("file", secret)
        assert secret not in stdout, ("stdout", secret)
    # 진단 출력은 살아 있다(제목 위치·구조를 계속 볼 수 있어야 한다).
    assert "B 사건에 대한 질의" in stdout
    assert "정화된 HTML 저장" in stdout


# =============================================================================
# Codex 리뷰 2 — 제목 칸 충돌에서 heading 폴백을 타면 안 된다
#
# 제목 칸이 '없음'과 '있는데 값이 갈림'은 다른 상태다. 후자에서 heading 으로 내려가면
# 페이지 어딘가의 heading 이 목록 제목과 우연히 같을 때 identity 가 통과해, 다른
# 사건의 회답·첨부가 그 제목 밑에 실린다.
# =============================================================================
def _subject_page(subjects, heading="A 사건", date="2026-08-20"):
    cells = "".join(f'<tr><td class="subject" colspan="2">{s}</td></tr>' for s in subjects)
    return f"""
<div id="content">
  <h2>{heading}</h2>
  <table class="tbl-view two"><tbody>{cells}</tbody></table>
  <table class="tbl-write"><tbody>
    <tr><th>회신일</th><td>{date}</td></tr>
    <tr><th>질의요지</th><td>질의 본문입니다.</td></tr>
    <tr><th>회답</th><td>회답 본문입니다.</td></tr>
    <tr><th>이유</th><td>이유 본문입니다.</td></tr>
    <tr><th>첨부파일</th><td>
      <a href="/fsc_new/file/displayFile.do?filePath=%2Fx&amp;orgFileName=a.hwp&amp;sysFileName=1.hwp">첨부.hwp</a>
    </td></tr>
  </tbody></table>
</div>
"""


def _title_of(html):
    from bs4 import BeautifulSoup

    return BetterReplyScraper._detail_title(BeautifulSoup(html, "lxml"))


def test_subject_cell_contract_is_three_state():
    """None=칸 없음(폴백 가능) / 값=확정 / ""=충돌(폴백 금지)."""
    from bs4 import BeautifulSoup

    def _subject(html):
        return BetterReplyScraper._subject_cell_title(BeautifulSoup(html, "lxml"))

    assert _subject(_subject_page([])) is None
    assert _subject(_subject_page(["A 사건", "A 사건"])) == "A 사건"
    assert _subject(_subject_page(["B 사건", "C 사건"])) == ""


def test_1_no_subject_cell_falls_back_to_heading():
    """1. 제목 칸이 없으면 기존 heading 폴백 호환이 유지된다."""
    assert _title_of(_subject_page([], heading="A 사건")) == "A 사건"


def test_2_identical_subject_cells_give_the_canonical_title():
    """2. 같은 값이 두 번이면 그 값이 정식 제목이다."""
    assert _title_of(_subject_page(["A 사건", "A 사건"], heading="다른 heading")) == "A 사건"


def test_3_conflicting_subjects_never_fall_back_to_heading():
    """3. Codex repro — 제목 칸이 갈리는데 heading 이 목록 제목과 같은 경우."""
    html = _subject_page(["B 사건", "C 사건"], heading="A 사건")
    assert _title_of(html) == ""                       # heading 'A 사건' 을 쓰지 않는다

    fetcher = _Fetcher(html=html)
    sc = _scraper(fetcher)
    post = _post(_detail(), title="[법령해석] A 사건", date="2026-08-20")
    detail_url = post.url
    from bs4 import BeautifulSoup

    # 회신일은 목록과 같다 — 거부 사유가 날짜가 아니라 제목임을 못 박는다.
    assert BetterReplyScraper._reply_date(BeautifulSoup(html, "lxml")) == "20260820"

    sc.enrich(post)
    assert post.body == ""
    assert post.attachments == []
    assert fetcher.downloaded == []                    # 다운로드 호출 0회
    assert post.url == LIST_URL                        # 후보 상세 링크도 되돌린다
    assert post.url != detail_url
    assert sc.enrich_succeeded(post) is False


def test_4_conflict_is_rejected_even_when_one_subject_matches_the_list():
    """4. 후보 중 하나가 목록 제목과 같다는 이유로 고르면 안 된다."""
    html = _subject_page(["A 사건", "B 사건"], heading="A 사건")
    assert _title_of(html) == ""

    fetcher = _Fetcher(html=html)
    sc = _scraper(fetcher)
    post = _post(_detail(), title="[법령해석] A 사건", date="2026-08-20")
    sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []
    assert post.url == LIST_URL


def test_conflict_identity_is_reported_as_unverifiable(caplog):
    """충돌은 '제목이 다름'이 아니라 '정식 제목 확인 불가'로 남는다."""
    sc = _scraper(_Fetcher(html=_subject_page(["B 사건", "C 사건"], heading="A 사건")))
    post = _post(_detail(), title="[법령해석] A 사건", date="2026-08-20")
    with caplog.at_level("WARNING"):
        sc.enrich(post)
    assert "정식 제목을 확인할 수 없어" in caplog.text


def test_5_live_fixtures_still_pass_with_identical_subject_cells():
    """5. 5449/5450 은 제목 칸 두 개가 같으므로 기존 동작 그대로다."""
    for idx, case in LIVE_CASES.items():
        fetcher = _Fetcher(html=_live_html(idx))
        sc = _scraper(fetcher)
        post = _live_post(idx)
        detail_url = post.url
        sc.enrich(post)
        assert _title_of(_live_html(idx)) == case["title"], idx
        assert post.url == detail_url, idx
        for label in ("[질의요지]", "[회답]", "[이유]"):
            assert label in post.body, (idx, label)
        assert [a.filename for a in post.attachments] == [case["attachment"]], idx
        assert sc.enrich_succeeded(post) is True, idx


# =============================================================================
# Codex 리뷰 — 요청 예외 문자열이 sanitizer 를 우회해 Actions 로그에 남으면 안 된다
#
# 응답 HTML·href·프래그먼트는 모두 정화하는데, 요청 실패 경로만 str(e) 를 그대로
# 찍고 있었다. requests 계열 예외는 실패한 URL(최종 리다이렉트 주소·쿼리·
# ;jsessionid·프래그먼트 포함)을 메시지에 담으므로 같은 credential 이 그 길로 샌다.
# 정책: 예외 '종류'까지만 남기고 메시지는 버린다.
# =============================================================================
_EXC_SECRETS = ("JS_SECRET", "TOKEN_SECRET", "FRAGMENT_SECRET", "SESSION_SECRET")


def _leaky_error(cls=RuntimeError):
    """실패한 URL 을 메시지에 담는, requests 계열과 같은 모양의 예외."""
    return cls(
        "failed at https://better.fsc.go.kr/x"
        ";jsessionid=JS_SECRET"
        "?token=TOKEN_SECRET"
        "#access_token=FRAGMENT_SECRET"
    )


def _assert_no_exception_secrets(capsys):
    captured = capsys.readouterr()
    for stream, text in (("stdout", captured.out), ("stderr", captured.err)):
        for secret in _EXC_SECRETS:
            assert secret not in text, (stream, secret)
    return captured


def test_retry_logs_only_the_exception_type(capsys, monkeypatch):
    """1. _retry 의 시도별 실패 로그에 예외 메시지가 실리지 않는다."""
    import scripts.capture_better_reply_detail as cap

    monkeypatch.setattr(cap.time, "sleep", lambda *_a, **_k: None)

    def _boom():
        raise _leaky_error()

    with pytest.raises(RuntimeError):
        cap._retry(_boom, 2, "상세 GET")

    out = _assert_no_exception_secrets(capsys).out
    assert "RuntimeError" in out                 # 예외 종류는 남는다
    assert "상세 GET 시도 1/2" in out            # 동작 이름·시도 횟수도 남는다
    assert "상세 GET 시도 2/2" in out


def test_warm_up_failure_logs_only_the_exception_type(capsys, monkeypatch):
    """2. 목록 수집 실패 outer except 도 메시지를 찍지 않는다."""
    import scripts.capture_better_reply_detail as cap

    monkeypatch.setattr(cap.time, "sleep", lambda *_a, **_k: None)
    sc = cap._scraper()
    monkeypatch.setattr(
        sc, "fetch_list",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("https://host/list?sessionId=SESSION_SECRET")
        ),
    )

    cap.warm_up(sc, {"5449"}, attempts=1)

    out = _assert_no_exception_secrets(capsys).out
    assert "RuntimeError" in out
    assert "목록 수집 실패" in out


def test_capture_http_failure_logs_only_the_exception_type(capsys, monkeypatch):
    """3. 상세 GET 실패 시 rc=2 를 돌려주되 메시지는 남기지 않는다."""
    import scripts.capture_better_reply_detail as cap

    monkeypatch.setattr(cap.time, "sleep", lambda *_a, **_k: None)

    class _BoomFetcher:
        def get(self, url, *, referer=None, **kw):
            raise _leaky_error()

    sc = cap._scraper()
    sc.fetcher = _BoomFetcher()
    assert cap.capture(sc, "5449", "법령해석", "", 50, 1) == 2

    out = _assert_no_exception_secrets(capsys).out
    assert "HTTP 실패" in out and "RuntimeError" in out


def test_capture_still_prints_the_sanitized_target_url(capsys, monkeypatch):
    """4. 이번 수정이 URL 진단 자체를 없애면 안 된다 — 공개 식별자는 계속 보인다."""
    import scripts.capture_better_reply_detail as cap

    monkeypatch.setattr(cap.time, "sleep", lambda *_a, **_k: None)

    class _BoomFetcher:
        def get(self, url, *, referer=None, **kw):
            raise _leaky_error()

    sc = cap._scraper()
    sc.fetcher = _BoomFetcher()
    cap.capture(sc, "5449", "법령해석", "", 50, 1)

    out = capsys.readouterr().out
    assert "lawreqIdx=5449" in out               # 요청 전에 찍는 정화된 URL
    assert "LawreqDetail.do" in out


def test_capture_failure_leaves_no_traceback_on_stderr(capsys, monkeypatch):
    """5. 실패 경로가 트레이스백을 흘리지 않는다(마지막 줄이 곧 예외 메시지다)."""
    import scripts.capture_better_reply_detail as cap

    monkeypatch.setattr(cap.time, "sleep", lambda *_a, **_k: None)

    class _BoomFetcher:
        def get(self, url, *, referer=None, **kw):
            raise _leaky_error()

    sc = cap._scraper()
    sc.fetcher = _BoomFetcher()
    cap.capture(sc, "5449", "법령해석", "", 50, 1)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Traceback" not in captured.out and "Traceback" not in captured.err


def test_main_catches_unexpected_errors_without_printing_the_message(capsys, monkeypatch):
    """main 의 마지막 안전망도 종류까지만 남긴다(트레이스백 유출 방지)."""
    import scripts.capture_better_reply_detail as cap

    monkeypatch.setattr(cap, "warm_up", lambda *a, **kw: None)

    def _boom(*_a, **_kw):
        raise _leaky_error()

    monkeypatch.setattr(cap, "capture", _boom)
    assert cap.main(["--idx", "5449"]) == 2

    captured = _assert_no_exception_secrets(capsys)
    assert "캡처 중단(idx=5449)" in captured.out
    assert "RuntimeError" in captured.out
    assert "Traceback" not in captured.err


# =============================================================================
# Codex 리뷰 — 내비게이션 링크를 담은 제목 칸은 canonical 후보가 아니다
#
# _inside_boundary 는 셀의 **조상**만 본다. 이전글/다음글 목록은 링크를 셀 **안에**
# 두므로(<td class="subject"><a href="/previous">A 사건</a></td>) 조상에는 걸리는 것이
# 없고, 옆 글 제목이 canonical 후보로 섞인다. 그 제목이 마침 목록 제목과 같으면
# identity 가 통과해 다른 사건의 회답·첨부가 실린다.
# =============================================================================
_NAV_SUBJECT = '<td class="subject"><a href="/previous">A 사건</a></td>'


def _subject_page_cells(cells, heading="제목 없음", date="2026-08-20"):
    return f"""
<div id="content">
  <h2>{heading}</h2>
  <table class="tbl-view two"><tbody>{cells}</tbody></table>
  <table class="tbl-write"><tbody>
    <tr><th>회신일</th><td>{date}</td></tr>
    <tr><th>질의요지</th><td>질의 본문입니다.</td></tr>
    <tr><th>회답</th><td>회답 본문입니다.</td></tr>
    <tr><th>이유</th><td>이유 본문입니다.</td></tr>
    <tr><th>첨부파일</th><td>
      <a href="/fsc_new/file/displayFile.do?filePath=%2Fx&amp;orgFileName=a.hwp&amp;sysFileName=1.hwp">첨부.hwp</a>
    </td></tr>
  </tbody></table>
</div>
"""


def _subject_of(html):
    from bs4 import BeautifulSoup

    return BetterReplyScraper._subject_cell_title(BeautifulSoup(html, "lxml"))


def test_A_navigation_subject_does_not_conflict_with_the_canonical_title():
    """A. 이전글 제목이 섞여도 canonical 두 칸이 같으면 그 값이 제목이다."""
    cells = '<td class="subject">B 사건</td><td class="subject">B 사건</td>' + _NAV_SUBJECT
    assert _subject_of(_subject_page_cells(cells)) == "B 사건"


def test_B_navigation_only_subject_counts_as_absence():
    """B. 링크 전용 제목 칸만 있으면 canonical 근거가 '없는' 것이다(3-state 의 None)."""
    assert _subject_of(_subject_page_cells(_NAV_SUBJECT)) is None
    # 근거가 없으므로 heading 폴백이 살아 있다(기존 호환 경로).
    assert _title_of(_subject_page_cells(_NAV_SUBJECT, heading="H 사건")) == "H 사건"


def test_C_previous_post_title_matching_the_list_never_passes_identity():
    """C. 공격 재현 — 이전글 제목이 목록 제목과 같고 회신일도 같은 경우."""
    cells = '<td class="subject">B 사건</td><td class="subject">B 사건</td>' + _NAV_SUBJECT
    html = _subject_page_cells(cells)
    fetcher = _Fetcher(html=html)
    sc = _scraper(fetcher)
    post = _post(_detail(), title="[법령해석] A 사건", date="2026-08-20")
    sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == []
    assert post.url == LIST_URL
    assert sc.enrich_succeeded(post) is False


def test_D_canonical_conflict_with_navigation_still_forbids_the_heading_fallback():
    """D. canonical 이 갈리면 내비게이션 제목이 있어도 폴백 없이 거부한다."""
    cells = ('<td class="subject">B 사건</td><td class="subject">C 사건</td>' + _NAV_SUBJECT)
    html = _subject_page_cells(cells, heading="A 사건")
    assert _subject_of(html) == ""
    assert _title_of(html) == ""

    fetcher = _Fetcher(html=html)
    sc = _scraper(fetcher)
    post = _post(_detail(), title="[법령해석] A 사건", date="2026-08-20")
    sc.enrich(post)
    assert post.body == "" and post.attachments == []
    assert fetcher.downloaded == [] and post.url == LIST_URL


def test_inline_link_inside_a_subject_cell_is_not_navigation():
    """회귀 방어 — 앵커 밖에 실질 텍스트가 있으면 본문 문단과 같은 취급이다."""
    cell = '<td class="subject">사건 제목 <a href="/law">관련 법령</a></td>'
    assert _subject_of(_subject_page_cells(cell)) == "사건 제목 관련 법령"


def test_E_live_fixtures_are_unaffected_by_the_boundary_check():
    """E. 5449/5450 의 제목 칸은 링크가 없는 평범한 셀이라 그대로 통과한다."""
    for idx, case in LIVE_CASES.items():
        fetcher = _Fetcher(html=_live_html(idx))
        sc = _scraper(fetcher)
        post = _live_post(idx)
        detail_url = post.url
        sc.enrich(post)
        assert _subject_of(_live_html(idx)) == case["title"], idx
        assert post.url == detail_url, idx
        for label in ("[질의요지]", "[회답]", "[이유]"):
            assert label in post.body, (idx, label)
        assert [a.filename for a in post.attachments] == [case["attachment"]], idx
        assert sc.enrich_succeeded(post) is True, idx
