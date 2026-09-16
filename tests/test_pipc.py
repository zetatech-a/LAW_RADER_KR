"""개인정보보호위원회(pipc.go.kr) 게시판 파서 회귀 테스트.

**중요 — 이 테스트가 통과한다고 라이브 계약이 검증된 것은 아니다.**
개발/CI 환경에서 `pipc.go.kr` 접속이 egress 정책으로 차단되어 실제 HTML 을 캡처하지
못했다(`likms.assembly.go.kr` 과 같은 상황 — tests/fixtures/README.md 참고).

그래서 여기서 검증하는 것은 두 가지로 나뉜다.

  (A) URL 계약에 근거한 동작 — 과제에서 사실로 주어진
      `selectBoardArticle.do?bbsId=…&mCode=…&nttId=…` 와 목록 URL 만으로 결정된다.
      목록 파싱·post_id·정규화 URL·페이지네이션 파라미터가 여기 속한다.
      마크업이 표든 리스트든 같은 결과가 나오는지 두 형태로 함께 확인한다.

  (B) 마크업 추정에 근거한 동작 — 상세 본문 컨테이너와 첨부 endpoint.
      tests/fixtures/synthetic/pipc_*_detail.html 은 **손으로 만든 구조 fixture**이며
      실제 응답이 아니다. 이 테스트는 "선언한 셀렉터 계약대로 동작하는가"만 보증한다.
"""
import logging
import os
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.models import Post
from src.scrapers import build_scraper
from src.scrapers.pipc import PipcBoardScraper

NOTICE_LIST = "https://pipc.go.kr/np/cop/bbs/selectBoardList.do?bbsId=BS061&mCode=C010010000"
PRESS_LIST = "https://pipc.go.kr/np/cop/bbs/selectBoardList.do?bbsId=BS074&mCode=C020010000"
_FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def _scraper(key="pipc_notice", list_url=NOTICE_LIST, fetcher=None, **extra):
    return PipcBoardScraper(
        SourceConfig(
            key=key, name=key, type="pipc_board", list_url=list_url, extra=extra
        ),
        fetcher=fetcher,
    )


def _parse(list_url, html, key="pipc_notice"):
    return _scraper(key=key, list_url=list_url)._parse_list(BeautifulSoup(html, "lxml"))


class _DetailFetcher:
    """enrich() 용 최소 fetcher — 상세 HTML 하나(+선택적 첨부 바이트)를 돌려준다."""

    def __init__(self, html, blob=b"PDF"):
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


class _ListFetcher:
    """collect() 용 — 페이지별 HTML 을 돌려주고 요청 URL 을 기록한다."""

    def __init__(self, pages: dict[int, str]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url, referer=None):
        self.requested.append(url)
        from urllib.parse import parse_qs, urlparse

        page = int(parse_qs(urlparse(url).query).get("pageIndex", ["1"])[0])
        return self.pages.get(page, "<html><body></body></html>")

    def text(self, resp):
        return resp


# ---------------------------------------------------------------- (A) 목록 파싱

# 공지사항(BS061): 표 기반 + 상단 고정(공지) 행 + 'N' 배지 + 첨부 아이콘 라벨.
NOTICE_LIST_HTML = """
<table class="board-list"><tbody>
  <tr class="notice">
    <td class="num"><span class="badge">공지</span></td>
    <td class="subject">
      <a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12490&amp;pageIndex=1">
        개인정보 보호 자율점검 상시 안내<span class="new">N</span>
        <span class="blind">첨부파일 있음</span>
      </a>
    </td>
    <td class="date">2026-08-01</td>
    <td class="hit">9,812</td>
  </tr>
  <tr>
    <td class="num">154</td>
    <td class="subject">
      <a title="개인정보 보호법 시행령 일부개정령안 입법예고 안내"
         href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12503&amp;pageIndex=1&amp;searchCnd=0">
        개인정보 보호법 시행령 일부개정령안 입법예고 안내
        <img src="/img/ico_file.png" alt="첨부파일"><span class="ico new">NEW</span>
      </a>
    </td>
    <td class="date">2026.09.11</td>
    <td class="hit">1,204</td>
  </tr>
  <tr>
    <td class="num">153</td>
    <td class="subject">
      <a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12488">
        2026년 개인정보 보호 교육 일정 공고
      </a>
    </td>
    <td class="date">20260828</td>
    <td class="hit">640</td>
  </tr>
  <!-- 다른 게시판(보도자료)을 가리키는 관련글 배너 — 이 소스의 글이 아니다 -->
  <tr class="banner">
    <td colspan="4">
      <a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS074&amp;mCode=C020010000&amp;nttId=99999">보도자료 바로가기</a>
    </td>
  </tr>
</tbody></table>
"""

# 보도자료(BS074): 리스트(ul/li) 기반 — 표가 아니어도 같은 계약으로 파싱돼야 한다.
PRESS_LIST_HTML = """
<ul class="board-list">
  <li class="item">
    <div class="tit">
      <a href="https://www.pipc.go.kr/np/cop/bbs/selectBoardArticle.do?bbsId=BS074&amp;mCode=C020010000&amp;nttId=12500">
        <span class="new">새글</span>개인정보위, 인공지능 학습데이터 처리 안내서 발간
      </a>
    </div>
    <div class="info"><span class="regdate">2026-09-15</span><span class="hit">873</span></div>
  </li>
  <li class="item">
    <div class="tit">
      <a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS074&amp;mCode=C020010000&amp;nttId=12497">
        가명정보 활용 우수사례 공모전 결과 발표
      </a>
    </div>
    <div class="info"><span class="regdate">2026년 9월 9일</span></div>
  </li>
</ul>
"""


def test_notice_list_title_nttid_url_and_date():
    posts = _parse(NOTICE_LIST, NOTICE_LIST_HTML)
    assert [p.post_id for p in posts] == ["nttId:12490", "nttId:12503", "nttId:12488"]

    pinned, main, old = posts
    # 상단 고정(공지) 글도 정상 파싱된다.
    assert pinned.title == "개인정보 보호 자율점검 상시 안내"
    assert pinned.date == "2026-08-01"

    assert main.title == "개인정보 보호법 시행령 일부개정령안 입법예고 안내"
    assert main.date == "2026-09-11"
    # 상세 URL 은 bbsId·mCode·nttId 만 남긴 정규 주소(휘발성 pageIndex/searchCnd 제거)
    assert main.url == (
        "https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do"
        "?bbsId=BS061&mCode=C010010000&nttId=12503"
    )
    assert old.date == "2026-08-28"     # YYYYMMDD 도 정규화


def test_press_list_title_nttid_url_and_date():
    posts = _parse(PRESS_LIST, PRESS_LIST_HTML, key="pipc_press")
    assert [p.post_id for p in posts] == ["nttId:12500", "nttId:12497"]

    first, second = posts
    assert first.title == "개인정보위, 인공지능 학습데이터 처리 안내서 발간"
    assert first.date == "2026-09-15"
    assert first.url == (
        "https://www.pipc.go.kr/np/cop/bbs/selectBoardArticle.do"
        "?bbsId=BS074&mCode=C020010000&nttId=12500"
    )
    assert second.date == "2026-09-09"  # '2026년 9월 9일' 표기


def test_ui_badges_and_attachment_icon_text_are_not_in_titles():
    """'N'/NEW/새글 배지, 첨부 아이콘 라벨, 스크린리더 전용 텍스트가 제목에 섞이지 않는다."""
    titles = [p.title for p in _parse(NOTICE_LIST, NOTICE_LIST_HTML)]
    titles += [p.title for p in _parse(PRESS_LIST, PRESS_LIST_HTML, key="pipc_press")]
    assert titles == [
        "개인정보 보호 자율점검 상시 안내",
        "개인정보 보호법 시행령 일부개정령안 입법예고 안내",
        "2026년 개인정보 보호 교육 일정 공고",
        "개인정보위, 인공지능 학습데이터 처리 안내서 발간",
        "가명정보 활용 우수사례 공모전 결과 발표",
    ]
    # 토큰(단어) 단위로도 한 번 더 못박는다 — 부분문자열 검사는 '인공지능' 안의
    # '공지' 처럼 멀쩡한 제목을 잡으므로 쓰지 않는다.
    for t in titles:
        assert t == t.strip()
        words = set(t.replace(",", " ").split())
        assert not (words & {"N", "NEW", "new", "새글", "신규", "첨부", "첨부파일", "공지"})


def test_links_to_other_boards_are_ignored():
    """다른 bbsId 를 가리키는 링크(관련글 배너 등)는 이 소스의 글이 아니다."""
    ids = {p.post_id for p in _parse(NOTICE_LIST, NOTICE_LIST_HTML)}
    assert "nttId:99999" not in ids


def test_non_article_and_malformed_links_are_ignored():
    html = """
    <ul>
      <li><a href="/np/cop/bbs/selectBoardList.do?bbsId=BS061&amp;mCode=C010010000">목록</a></li>
      <li><a href="javascript:void(0);">정렬</a></li>
      <li><a href="#content">본문 바로가기</a></li>
      <li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000">nttId 없음</a></li>
      <li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=abc">숫자 아님</a></li>
    </ul>"""
    assert _parse(NOTICE_LIST, html) == []


def test_same_article_linked_twice_in_a_row_is_one_post():
    """제목 링크와 썸네일 링크가 한 행에 둘 달려도 글은 하나다."""
    html = """
    <ul><li>
      <a class="thumb" href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12503">
        <img src="/t.png" alt="썸네일"></a>
      <a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12503">제목입니다</a>
      <span class="date">2026-09-11</span>
    </li></ul>"""
    posts = _parse(NOTICE_LIST, html)
    assert len(posts) == 1 and posts[0].title == "제목입니다"


# ------------------------------------------------------- (A) 페이지네이션 / 중복

def test_page2_url_keeps_bbsid_mcode_and_adds_pageindex():
    for list_url, bbs in ((NOTICE_LIST, "BS061"), (PRESS_LIST, "BS074")):
        url = _scraper(list_url=list_url)._list_page_url(2)
        assert f"bbsId={bbs}" in url
        assert "mCode=" in url
        assert "pageIndex=2" in url
        assert url.startswith("https://pipc.go.kr/np/cop/bbs/selectBoardList.do?")
    # 1페이지는 원래 URL 그대로(불필요한 파라미터를 붙이지 않는다)
    assert _scraper()._list_page_url(1) == NOTICE_LIST


def test_collect_paginates_and_pinned_notices_are_not_duplicated():
    """상단 고정 공지가 1·2페이지에 모두 나와도 신규는 한 번만 잡힌다."""
    pinned = """
      <tr><td class="subject"><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12490">
        상시 공지</a></td><td class="date">2026-08-01</td></tr>"""
    page1 = f"""<table><tbody>{pinned}
      <tr><td class="subject"><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12503">
        1페이지 글</a></td><td class="date">2026-09-11</td></tr>
    </tbody></table>"""
    page2 = f"""<table><tbody>{pinned}
      <tr><td class="subject"><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12470">
        2페이지 글</a></td><td class="date">2026-07-20</td></tr>
    </tbody></table>"""
    fetcher = _ListFetcher({1: page1, 2: page2})
    result = _scraper(fetcher=fetcher).collect(30, seen_ids=set(), max_pages=3)

    ids = [p.post_id for p in result.posts]
    assert ids == ["nttId:12490", "nttId:12503", "nttId:12470"]
    assert len(ids) == len(set(ids))            # 고정 공지 중복 없음
    assert any("pageIndex=2" in u for u in fetcher.requested)


def test_collect_stops_when_page_param_is_ignored():
    """PAGE_PARAM 이 무시되어 2페이지가 1페이지와 같아도 중복을 만들지 않는다."""
    page = """<table><tbody>
      <tr><td class="subject"><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000&amp;nttId=12503">
        같은 글</a></td><td class="date">2026-09-11</td></tr>
    </tbody></table>"""
    result = _scraper(fetcher=_ListFetcher({1: page, 2: page, 3: page})).collect(
        30, seen_ids=set(), max_pages=3
    )
    assert [p.post_id for p in result.posts] == ["nttId:12503"]
    assert result.reached_boundary is True


# --------------------------------------------------------- (B) 상세 본문 / 첨부

def _enrich(fixture, key, list_url, url_id="12503"):
    fetcher = _DetailFetcher((_FIXTURES / fixture).read_text(encoding="utf-8"))
    scraper = _scraper(key=key, list_url=list_url, fetcher=fetcher)
    post = Post(
        source_key=key,
        source_name=key,
        post_id=f"nttId:{url_id}",
        title="제목",
        url=f"https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId={url_id}",
    )
    scraper.enrich(post)
    return scraper, post, fetcher


def test_notice_detail_body_excludes_metadata_navigation_and_attachments():
    scraper, post, _ = _enrich("pipc_notice_detail.html", "pipc_notice", NOTICE_LIST)
    assert len(post.body) > 100
    assert "입법예고한다고 밝혔습니다" in post.body
    assert "의견을 제출할 수 있습니다" in post.body

    for junk in (
        "개인정보 보호법 시행령 일부개정령안 입법예고 안내",  # 제목 중복
        "담당부서", "개인정보정책과", "등록일", "조회수", "1,204",  # 머리말 메타
        "시행령 개정안 입법예고문.hwp", "신구조문대비표.pdf",       # 첨부 파일명
        "이전글", "다음글", "자율점검 안내", "교육 일정 공고",       # 이전/다음글
        "만족도", "이 페이지에서 제공하는 정보",                    # 만족도 조사
        "본문 바로가기", "홈 >", "정부서울청사", "Copyright",       # 네비·breadcrumb·푸터
        "목록",
    ):
        assert junk not in post.body, f"본문에 {junk!r} 가 섞였다"

    assert scraper.enrich_succeeded(post) is True


def test_press_detail_body_uses_a_different_container_and_stays_clean():
    scraper, post, _ = _enrich("pipc_press_detail.html", "pipc_press", PRESS_LIST, "12500")
    assert len(post.body) > 100
    assert "안내서」를 발간했다고 밝혔다" in post.body
    assert "지속적으로" in post.body

    for junk in (
        "개인정보위, 인공지능 학습데이터 처리 안내서 발간",   # 제목 중복
        "담당부서", "신기술개인정보과", "등록일", "조회수", "873",
        ".hwpx", ".pdf", "첨부파일",
        "이전글", "다음글", "공모전", "분쟁조정",
        "만족하십니까", "Copyright", "홈 >",
    ):
        assert junk not in post.body, f"본문에 {junk!r} 가 섞였다"

    assert scraper.enrich_succeeded(post) is True


def test_notice_attachments_preserve_filenames_and_deduplicate_urls():
    _, post, fetcher = _enrich("pipc_notice_detail.html", "pipc_notice", NOTICE_LIST)
    names = [a.filename for a in post.attachments]
    urls = [a.url for a in post.attachments]

    assert names == ["시행령 개정안 입법예고문.hwp", "신구조문대비표.pdf"]  # HWP 와 PDF 모두
    assert len(urls) == len(set(urls))               # '바로보기' 중복 링크 제거
    assert all(u.startswith("https://pipc.go.kr/cmm/fms/FileDown.do?") for u in urls)
    assert fetcher.downloaded == urls                # 기존 fetcher.download 재사용
    assert all(a.data == b"PDF" for a in post.attachments)


def test_press_attachments_from_javascript_downloader_include_hwpx():
    _, post, _ = _enrich("pipc_press_detail.html", "pipc_press", PRESS_LIST, "12500")
    names = [a.filename for a in post.attachments]
    assert len(names) == 2
    assert names[0].endswith(".hwpx")     # .pdf 만 인식하면 안 된다
    assert names[1].endswith(".pdf")
    assert "(3.1 MB)" not in names[1]     # 크기 표기는 파일명이 아니다
    assert post.attachments[0].url == (
        "https://pipc.go.kr/cmm/fms/FileDown.do"
        "?atchFileId=FILE_000000000012500&fileSn=0"
    )


def test_wide_container_still_drops_noise_subtrees():
    """본문 컨테이너가 넓게 잡히는 레이아웃에서도 메타·첨부·이전다음글은 빠진다."""
    html = """
    <div class="view-cont">
      <ul class="view-info"><li>담당부서 개인정보정책과</li><li>조회수 12</li></ul>
      <p>실제 본문 문단입니다. 이 문장만 요약 입력이 되어야 합니다.</p>
      <div class="file-list"><a href="/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0">보도자료.hwp</a></div>
      <div class="prev-next">이전글 : 앞 글 제목</div>
      <div class="satisfaction">만족도 조사입니다</div>
    </div>"""
    scraper = _scraper(fetcher=_DetailFetcher(html))
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1",
                title="t", url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    scraper.enrich(post)
    assert post.body == "실제 본문 문단입니다. 이 문장만 요약 입력이 되어야 합니다."
    # 첨부는 원본 문서에서 그대로 수집된다(본문 정리가 원본을 훼손하지 않는다).
    assert [a.filename for a in post.attachments] == ["보도자료.hwp"]


def test_navigation_heavy_container_is_rejected_as_body():
    """링크가 대부분인 컨테이너(메뉴)는 본문으로 채택하지 않는다."""
    html = """
    <div class="view-cont">
      <a href="/a">개인정보 보호법</a> <a href="/b">시행령</a> <a href="/c">고시</a>
      <a href="/d">해설서</a> <a href="/e">자료실</a>
    </div>"""
    scraper = _scraper(fetcher=_DetailFetcher(html))
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1",
                title="t", url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    scraper._dump_debug = lambda *a, **k: None
    scraper.enrich(post)
    assert post.body == ""


def test_body_selectors_can_be_overridden_from_config():
    html = '<div class="wholly-custom"><p>운영자가 지정한 컨테이너의 본문입니다.</p></div>'
    scraper = _scraper(fetcher=_DetailFetcher(html), body_selectors=[".wholly-custom"])
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1",
                title="t", url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    scraper.enrich(post)
    assert post.body == "운영자가 지정한 컨테이너의 본문입니다."


# ------------------------------------------------- (B) 상세 성공 판정 / 실패 격리

def test_attachments_without_body_is_not_a_successful_enrich():
    """첨부만 남고 본문 파서가 깨진 상태를 '성공'으로 세면 고장이 통계에 묻힌다."""
    html = """
    <div id="content">
      <div class="unknown-new-layout"><p>개편으로 셀렉터가 바뀐 본문</p></div>
      <div class="file-list"><a href="/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0">보도자료.pdf</a></div>
    </div>"""
    scraper = _scraper(fetcher=_DetailFetcher(html))
    scraper._dump_debug = lambda *a, **k: None
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:12503",
                title="t", url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=12503")
    scraper.enrich(post)

    assert post.body == ""
    assert len(post.attachments) == 1                       # 첨부는 잡혔지만
    assert scraper.enrich_succeeded(post) is False          # 성공이 아니다
    # 기본 판정(BaseScraper)이었다면 성공으로 셌을 상황임을 함께 못박는다.
    assert bool(post.body or post.details or post.attachments) is True


def test_missing_body_logs_source_key_post_id_and_url(caplog):
    scraper = _scraper(fetcher=_DetailFetcher("<div>구조 미상</div>"))
    scraper._dump_debug = lambda *a, **k: None
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:12503",
                title="t", url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=12503")
    with caplog.at_level(logging.WARNING):
        scraper.enrich(post)
    msg = caplog.text
    assert "pipc_notice" in msg and "nttId:12503" in msg and post.url in msg


def test_detail_fetch_failure_does_not_raise():
    """한 글의 상세 실패가 나머지 글·소스 처리를 끊지 않는다(fail-soft 유지)."""

    class _Boom:
        def get(self, url, referer=None):
            raise RuntimeError("503")

    scraper = _scraper(fetcher=_Boom())
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1",
                title="t", url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    scraper.enrich(post)                       # 예외가 새어 나오지 않는다
    assert scraper.enrich_succeeded(post) is False


def test_attachment_download_failure_keeps_the_post_and_the_link():
    class _PartialFetcher(_DetailFetcher):
        def download(self, url, referer=None):
            raise RuntimeError("연결 실패")

    html = '<div class="view-cont"><p>본문이 있습니다. 첨부만 실패합니다.</p></div>' \
           '<a href="/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0">자료.pdf</a>'
    scraper = _scraper(fetcher=_PartialFetcher(html))
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1",
                title="t", url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    scraper.enrich(post)
    assert post.body                                        # 글은 남는다
    assert post.attachments[0].data is None                 # 바이트만 없다
    assert post.attachments[0].filename == "자료.pdf"       # 링크는 메일에 실린다
    assert scraper.enrich_succeeded(post) is True


# --------------------------------------------------------------- 레지스트리

def test_registry_resolves_pipc_board():
    for key, list_url in (("pipc_notice", NOTICE_LIST), ("pipc_press", PRESS_LIST)):
        scraper = build_scraper(
            SourceConfig(key=key, name=key, type="pipc_board", list_url=list_url),
            fetcher=None,
        )
        assert isinstance(scraper, PipcBoardScraper)
        assert scraper.PAGE_PARAM == "pageIndex"
        assert scraper.paginates is True
        assert scraper.SUPPORTS_ENRICH is True


def test_config_yaml_declares_both_pipc_sources():
    from src.config import load_config

    sources = {s.key: s for s in load_config("config.yaml").sources}
    notice, press = sources["pipc_notice"], sources["pipc_press"]
    assert (notice.name, notice.type, notice.enabled) == ("개인정보위 · 공지사항", "pipc_board", True)
    assert (press.name, press.type, press.enabled) == ("개인정보위 · 보도자료", "pipc_board", True)
    assert notice.list_url == NOTICE_LIST
    assert press.list_url == PRESS_LIST


# ------------------------------------------------------------- 경계 조건

def test_missing_mcode_in_href_is_filled_from_the_list_url():
    html = ('<li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;nttId=11199">제목</a>'
            '<span class="date">2026-01-02</span></li>')
    post = _parse(NOTICE_LIST, html)[0]
    assert post.url.endswith("?bbsId=BS061&mCode=C010010000&nttId=11199")


def test_impossible_date_and_numeric_noise_leave_the_date_empty():
    """달력에 없는 날짜·공고번호·조회수를 게시일로 읽지 않는다(빈 값을 유지)."""
    bad_day = ('<tr><td class="subject"><a href="/np/cop/bbs/selectBoardArticle.do'
               '?bbsId=BS061&amp;mCode=C010010000&amp;nttId=2">t</a></td>'
               '<td class="date">2026-13-45</td></tr>')
    noise = ('<li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000'
             '&amp;nttId=1">제목 제2026-15호 조회 12345</a></li>')
    assert _parse(NOTICE_LIST, bad_day)[0].date == ""
    assert _parse(NOTICE_LIST, noise)[0].date == ""


def test_badge_only_anchor_does_not_become_a_post():
    html = ('<li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000'
            '&amp;nttId=3"><span class="new">N</span></a></li>')
    assert _parse(NOTICE_LIST, html) == []


def test_invalid_body_selector_in_config_is_skipped_not_fatal(caplog):
    """config 오타 한 줄이 상세 수집을 통째로 죽이지 않는다."""
    scraper = _scraper(
        fetcher=_DetailFetcher('<div class="ok"><p>본문입니다.</p></div>'),
        body_selectors=["((((", ".ok"],
    )
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1",
                title="t", url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    with caplog.at_level(logging.WARNING):
        scraper.enrich(post)
    assert post.body == "본문입니다."
    assert "body_selectors" in caplog.text
