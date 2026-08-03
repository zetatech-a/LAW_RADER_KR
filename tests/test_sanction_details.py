"""검사결과 제재(fss_sanction) 상세의 구조화 추출 + 메일 렌더 회귀 테스트.

상세 페이지에서 실제로 보이는 정보는 금융기관명·제재조치일·관련부서 3개뿐이고
제재 내용은 첨부 PDF 에 있다. 넓은 컨테이너를 통째로 긁으면 라벨과 빈 행이 뒤섞인
잡음 발췌가 되므로, 이 소스만 라벨-값 3항목을 구조화해 표로 싣는다.

HTML 은 라이브 마크업의 핵심 패턴을 최소로 압축한 것이다.
"""
import os
import sys
from html import escape as html_escape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.models import Attachment, Post
from src.notifier import build_html, build_text
from src.scrapers.fss import FssBoardScraper

SANCTION_LIST = "https://www.fss.or.kr/fss/job/openInfo/list.do?menuNo=200476"
SANCTION_VIEW = (
    "https://www.fss.or.kr/fss/job/openInfo/view.do"
    "?menuNo=200476&examMgmtNo=202500123&emOpenSeq=1"
)

# 상세 표: th=라벨 / td=값. 제재조치내용은 비어 있고 실제 내용은 첨부 PDF 에 있다.
DETAIL_HTML = """
<div id="content">
  <div class="board-view">
    <table class="tbl-view">
      <caption>검사결과 제재내용 상세</caption>
      <tbody>
        <tr><th scope="row">금융기관명</th><td>쿠팡페이(주)</td></tr>
        <tr><th scope="row">제재조치일</th><td>20260723</td></tr>
        <tr><th scope="row">관련부서</th><td>전자금융검사국</td></tr>
        <tr><th scope="row">제재조치내용</th><td></td></tr>
        <tr><th scope="row">첨부파일</th>
          <td><a href="/fss/cmmn/file/fileDown.do?atchFileId=FILE_000000000123&fileSn=0">
            <span class="name">제재내용공개안.pdf</span></a></td></tr>
      </tbody>
    </table>
  </div>
</div>
"""

# dt/dd 로 같은 항목을 내보내는 변형(반응형 레이아웃).
DETAIL_HTML_DL = """
<div id="content"><div class="board-view"><dl>
  <dt>금융기관명</dt><dd>쿠팡페이(주)</dd>
  <dt>제재조치일</dt><dd>2026-07-23</dd>
  <dt>관련부서</dt><dd>전자금융검사국</dd>
</dl></div></div>
"""

# 라벨 구조가 깨진 경우(관련부서가 라벨 없이 문단으로만 있음).
DETAIL_HTML_BROKEN = """
<div id="content"><div class="board-view">
  <p>금융기관명 쿠팡페이(주)</p>
  <p>제재조치일 20260723</p>
  <p>전자금융검사국</p>
</div></div>
"""

PRESS_DETAIL_HTML = """
<div id="content"><div class="view-cont">
  <p>금융감독원은 오늘 보도자료를 배포하였다.</p>
</div></div>
"""


class _Fetcher:
    """상세 HTML 하나와 첨부 바이트를 돌려주는 최소 fetcher."""

    def __init__(self, html, blob=b"%PDF-1.4 dummy"):
        self.html = html
        self.blob = blob
        self.downloaded = []

    def get(self, url, referer=None):
        return object()

    def text(self, resp):
        return self.html

    def download(self, url, referer=None):
        self.downloaded.append(url)
        return self.blob


def _scraper(html, key="fss_sanction", list_url=SANCTION_LIST):
    src = SourceConfig(key=key, name="금감원 · 검사결과 제재", type="fss_board",
                       list_url=list_url)
    return FssBoardScraper(src, fetcher=_Fetcher(html))


def _post(key="fss_sanction", title="쿠팡페이(주)", date="2026-07-23", url=SANCTION_VIEW):
    return Post(source_key=key, source_name="금감원 · 검사결과 제재",
                post_id="exam:202500123_1", title=title, url=url, date=date)


# --- 구조화 추출 ---
def test_sanction_detail_yields_three_ordered_fields():
    sc = _scraper(DETAIL_HTML)
    p = _post()
    sc.enrich(p)
    assert p.details == [
        ("금융기관명", "쿠팡페이(주)"),
        ("제재조치일", "2026-07-23"),   # YYYYMMDD → YYYY-MM-DD
        ("관련부서", "전자금융검사국"),
    ]


def test_sanction_detail_leaves_body_empty():
    """구조화에 성공하면 body 는 비어 있어야 한다(= LLM 요약 대상에서 제외)."""
    sc = _scraper(DETAIL_HTML)
    p = _post()
    sc.enrich(p)
    assert p.body == ""


def test_sanction_detail_still_collects_and_downloads_attachments():
    sc = _scraper(DETAIL_HTML)
    p = _post()
    sc.enrich(p)
    assert [a.filename for a in p.attachments] == ["제재내용공개안.pdf"]
    assert p.attachments[0].data == b"%PDF-1.4 dummy"
    assert sc.fetcher.downloaded == [p.attachments[0].url]


def test_sanction_detail_dl_variant():
    sc = _scraper(DETAIL_HTML_DL)
    p = _post()
    sc.enrich(p)
    assert p.details == [
        ("금융기관명", "쿠팡페이(주)"),
        ("제재조치일", "2026-07-23"),
        ("관련부서", "전자금융검사국"),
    ]


def test_sanction_detail_uses_list_values_when_labels_missing():
    """금융기관명·제재조치일 라벨이 없어도 목록에서 확인한 값으로 채운다."""
    html = """
    <div id="content"><table><tbody>
      <tr><th>관련부서</th><td>전자금융검사국</td></tr>
    </tbody></table></div>"""
    sc = _scraper(html)
    p = _post()
    sc.enrich(p)
    assert p.details == [
        ("금융기관명", "쿠팡페이(주)"),      # post.title 폴백
        ("제재조치일", "2026-07-23"),        # post.date 폴백
        ("관련부서", "전자금융검사국"),
    ]


def test_sanction_broken_structure_falls_back_to_body():
    """라벨 구조가 깨지면 details 를 비우고 기존 본문 추출을 그대로 유지한다."""
    sc = _scraper(DETAIL_HTML_BROKEN)
    p = _post()
    sc.enrich(p)
    assert p.details == []
    assert "쿠팡페이(주)" in p.body       # 정보를 잃지 않음


def test_sanction_without_department_falls_back_to_body():
    """관련부서는 라벨이 붙은 값에서만 온다 — 없으면 폴백."""
    html = """
    <div id="content"><table><tbody>
      <tr><th>금융기관명</th><td>쿠팡페이(주)</td></tr>
      <tr><th>제재조치일</th><td>20260723</td></tr>
    </tbody></table></div>"""
    sc = _scraper(html)
    p = _post()
    sc.enrich(p)
    assert p.details == []
    assert p.body


def test_other_fss_sources_keep_body_extraction():
    """다른 FSS 소스는 구조화 경로를 타지 않는다."""
    sc = _scraper(PRESS_DETAIL_HTML, key="fss_press",
                  list_url="https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218")
    p = _post(key="fss_press", title="보도자료 제목")
    sc.enrich(p)
    assert p.details == []
    assert "보도자료를 배포" in p.body


def test_other_fss_source_with_sanction_like_table_keeps_body():
    """같은 라벨이 있는 표라도 fss_sanction 이 아니면 본문 추출 그대로."""
    sc = _scraper(DETAIL_HTML, key="fss_press")
    p = _post(key="fss_press")
    sc.enrich(p)
    assert p.details == []
    assert "금융기관명" in p.body


def test_structured_post_is_not_sent_to_the_llm():
    """구조화에 성공한 글은 body 가 비어 요약 대상에서 빠진다(무료 할당량 보호)."""
    import pytest

    from src.config import LLMConfig
    from src.summarizer import Summarizer

    sc = _scraper(DETAIL_HTML)
    p = _post()
    sc.enrich(p)

    s = Summarizer(LLMConfig(
        enabled=True, model="m", lines=3, max_line_chars=90, min_body_chars=10,
        max_input_chars=1000, max_posts=10, rpm=0, timeout_sec=5, max_retries=0,
        retry_backoff_sec=0, api_key="k",
    ))
    s._generate = lambda *a, **k: pytest.fail("구조화된 제재 글은 LLM 을 호출하면 안 된다")
    assert s.summarize_all({p.source_name: [p]}) == 0
    assert p.summary == []


# --- 메일 렌더 ---
def _rendered(details, attachments=None):
    p = _post()
    p.details = details
    p.attachments = attachments or [
        Attachment(filename="제재내용공개안.pdf",
                   url="https://www.fss.or.kr/fss/cmmn/file/fileDown.do?atchFileId=A&fileSn=0",
                   data=b"x")
    ]
    grouped = {p.source_name: [p]}
    return p, build_html(grouped), build_text(grouped)


DETAILS = [
    ("금융기관명", "쿠팡페이(주)"),
    ("제재조치일", "2026-07-23"),
    ("관련부서", "전자금융검사국"),
]


def test_html_renders_three_detail_rows():
    _, html, _ = _rendered(DETAILS)
    assert html.count("<tr class='lr-detail'>") == 3
    for label, value in DETAILS:
        assert f">{label}</td>" in html
        assert f">{value}</td>" in html


def test_text_renders_three_detail_lines():
    _, _, text = _rendered(DETAILS)
    lines = [ln.strip() for ln in text.splitlines()]
    assert "[주요 정보]" in lines
    assert "금융기관명: 쿠팡페이(주)" in lines
    assert "제재조치일: 2026-07-23" in lines
    assert "관련부서: 전자금융검사국" in lines


def test_structured_details_have_no_noise_or_ai_labels():
    _, html, text = _rendered(DETAILS)
    for out in (html, text):
        assert "제재조치내용" not in out       # 상세 표의 잡음 라벨
        assert "[원문 발췌]" not in out
        assert "AI 3줄 요약" not in out
        assert "생성형 AI" not in out          # AI 유의사항 문구


def test_details_are_html_escaped():
    _, html, _ = _rendered([("<b>금융기관명</b>", "<script>alert(1)</script>")])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;금융기관명&lt;/b&gt;" in html


def test_details_keep_attachment_chips_and_original_link():
    p, html, text = _rendered(DETAILS)
    assert "제재내용공개안.pdf" in html
    assert "원문 보기" in html
    assert html_escape(p.url) in html     # 원문 링크(& 이스케이프)
    assert p.url in text
    assert "제재내용공개안.pdf" in text


def test_details_take_priority_over_summary_and_body():
    p = _post()
    p.details = DETAILS
    p.summary = ["요약 문장"]
    p.body = "본문 잡음 제재조치내용"
    html = build_html({p.source_name: [p]})
    assert "요약 문장" not in html
    assert "본문 잡음" not in html
    assert "전자금융검사국" in html


def test_ai_notice_still_shown_when_a_real_summary_exists():
    """구조화 항목만 있는 메일에는 AI 유의사항이 없지만, 실제 요약이 섞이면 남는다."""
    d = _post()
    d.details = DETAILS
    s = Post(source_key="fss_press", source_name="금감원 · 보도자료", post_id="1",
             title="보도자료", url="https://www.fss.or.kr/x", date="2026-07-23",
             summary=["요약 문장"])
    grouped = {d.source_name: [d], s.source_name: [s]}
    assert "생성형 AI" in build_html(grouped)
    assert "생성형 AI" in build_text(grouped)
