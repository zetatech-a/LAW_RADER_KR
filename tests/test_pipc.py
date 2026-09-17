"""개인정보보호위원회(pipc.go.kr) 게시판 파서 회귀 테스트.

이 에이전트 환경에서는 `pipc.go.kr` 접속이 egress 정책으로 차단되어 있다. 다만
**GitHub Actions 러너는 실제 응답을 받아냈고**(2026-09-16 verify-results 아티팩트),
그 원본 HTML 로 두 가지 계약이 확인됐다. 그래서 여기서 검증하는 것은 셋으로 나뉜다.

  (A) URL 계약에 근거한 동작 — 사실로 주어진
      `selectBoardArticle.do?bbsId=…&mCode=…&nttId=…` 와 목록 URL 만으로 결정된다.
      목록 파싱·post_id·정규화 URL·페이지네이션 파라미터가 여기 속한다.
      마크업이 표든 리스트든 같은 결과가 나오는지 두 형태로 함께 확인한다.

  (B) **라이브 확인된 상세 계약** — 본문 컨테이너 `td.tbl_cnts` 와 첨부 원본 파일명
      위치(앵커 `alt`). 파일 맨 아래 '라이브 HTML 정합' 절이 이것을 다룬다.

  (C) 아직 추정인 부분 — `_BODY_SELECTORS` 의 나머지 후보와 첨부 endpoint 형태.
      tests/fixtures/synthetic/pipc_*_detail.html 은 **손으로 만든 구조 fixture**이며
      실제 응답이 아니다. 그 부분은 "선언한 계약대로 동작하는가"만 보증한다.
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
    # /np 애플리케이션 컨텍스트가 보존돼야 한다(루트 '/cmm/...' 는 깨진 링크다).
    assert post.attachments[0].url == (
        "https://pipc.go.kr/np/cmm/fms/FileDown.do"
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


# ------------------------------------------- 하드닝 회귀 (2026-09-16 사전점검)

def test_attachment_anchor_with_hash_href_and_onclick_handler():
    """`href="#" + onclick=fn_egov_downFile(...)` 형태의 첨부가 누락되지 않는다.

    예전 순서에서는 href 가 '#' 이면 즉시 버려서 onclick 분기에 **도달하지 못했다**
    (그 분기는 href 가 javascript: 인 경우에만 실행되는 사실상 죽은 코드였다).
    국내 정부 게시판에 흔한 형태라 순서를 바로잡은 것에 대한 회귀다.
    """
    html = (
        '<div class="view-cont"><p>본문입니다. 첨부는 onclick 으로 내려받습니다.</p></div>'
        '<div class="file-list">'
        '  <a href="#" onclick="fn_egov_downFile(\'FILE_00000000012503\',\'0\'); return false;">'
        '    규제영향분석서.pdf</a>'
        '</div>'
    )
    scraper = _scraper(fetcher=_DetailFetcher(html))
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:12503", title="t",
                url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=12503")
    scraper.enrich(post)
    assert [a.filename for a in post.attachments] == ["규제영향분석서.pdf"]
    assert post.attachments[0].url == (
        "https://pipc.go.kr/np/cmm/fms/FileDown.do?atchFileId=FILE_00000000012503&fileSn=0"
    )


def test_document_viewer_links_are_not_counted_as_attachments():
    """'바로보기'(문서뷰어)는 같은 파일의 다른 표현이라 첨부 개수를 부풀리면 안 된다."""
    html = (
        '<div class="view-cont"><p>본문입니다. 뷰어 링크가 함께 있습니다.</p></div>'
        '<div class="file-list">'
        '  <a href="/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0">보도자료.pdf</a>'
        '  <a href="/cmm/fms/FileViewer.do?atchFileId=A&amp;fileSn=0">바로보기</a>'
        '  <a href="/synap/skin/doc.html?fn=A&amp;fileSn=0">미리보기</a>'
        '</div>'
    )
    scraper = _scraper(fetcher=_DetailFetcher(html))
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1", title="t",
                url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    scraper.enrich(post)
    assert [a.filename for a in post.attachments] == ["보도자료.pdf"]


def test_titles_keep_legitimate_leading_words_that_look_like_badges():
    """'New'·'N'·'첨부파일'·'공지' 로 **시작하는 진짜 제목**의 첫 단어를 지우지 않는다.

    라이브 목록에서 새 글 표식 N 은 제목 **뒤**에 붙는다. 앞쪽까지 넓게 지우면
    멀쩡한 제목이 잘린다.
    """
    cases = {
        12801: "New Deal 정책 관련 개인정보 처리 안내",
        12802: "N번째 개인정보 보호주간 행사 안내",
        12803: "첨부파일 양식 개정 안내",
        12804: "공지 운영 기준 개정 안내",
        12805: "신규 서비스 개인정보 처리방침 안내",
        12806: "제출 서식 첨부파일",
    }
    html = "<ul>" + "".join(
        f'<li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000'
        f'&amp;nttId={n}">{t}</a></li>'
        for n, t in cases.items()
    ) + "</ul>"
    parsed = {int(p.post_id.split(":")[1]): p.title for p in _parse(NOTICE_LIST, html)}
    assert parsed == cases


def test_trailing_new_badge_text_is_stripped_even_without_its_own_element():
    """배지가 별도 요소가 아니어도 제목 뒤의 'N'/'NEW' 는 떨어진다."""
    html = "<ul>" + "".join(
        f'<li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000'
        f'&amp;nttId={n}">{t}</a></li>'
        for n, t in {12811: "개인정보 보호법 시행령 입법예고 N",
                     12812: "ISMS-P 심사 개선 방안 NEW"}.items()
    ) + "</ul>"
    assert [p.title for p in _parse(NOTICE_LIST, html)] == [
        "개인정보 보호법 시행령 입법예고",
        "ISMS-P 심사 개선 방안",
    ]


def test_ui_words_inside_a_filename_are_preserved():
    """'바로보기'·'다운로드' 를 파일명 **안에서** 지우지 않는다(양 끝에서만 뗀다)."""
    html = (
        '<div class="view-cont"><p>본문입니다.</p></div>'
        '<a href="/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0" '
        '   title="자료 미리보기 안내서.pdf 다운로드">자료 미리보기 안내서.pdf 다운로드</a>'
    )
    scraper = _scraper(fetcher=_DetailFetcher(html))
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1", title="t",
                url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    scraper.enrich(post)
    assert [a.filename for a in post.attachments] == ["자료 미리보기 안내서.pdf"]


def test_page2_query_is_exactly_board_context_plus_pageindex():
    """2페이지 요청이 bbsId·mCode 를 잃지 않는다(결정적 검사).

    `selectBoardList.do?pageIndex=2` 처럼 게시판 맥락이 빠지면 엉뚱한 목록을 받는다.
    """
    from urllib.parse import parse_qs, urlparse

    for list_url, bbs, mcode in (
        (NOTICE_LIST, "BS061", "C010010000"),
        (PRESS_LIST, "BS074", "C020010000"),
    ):
        parsed = urlparse(_scraper(list_url=list_url)._list_page_url(2))
        assert parsed.netloc == "pipc.go.kr"
        assert parsed.path == "/np/cop/bbs/selectBoardList.do"
        assert parse_qs(parsed.query) == {
            "bbsId": [bbs], "mCode": [mcode], "pageIndex": ["2"],
        }


# --- 라이브 수용 표본의 '기대 형태' (2026-09-16 사용자 관측치 기준) --------------
#
# **아래 HTML 은 합성이다.** 개발 환경에서 pipc.go.kr 접속이 차단되어 실제 DOM 을
# 받지 못했다. 그래서 이 테스트가 증명하는 것은 "라이브가 이런 형태라면 파서가 관측된
# 파일명·개수를 그대로 낸다"까지이며, **라이브 검증이 아니다.**
# 관측치: nttId=12503 → PDF 4개 / nttId=12500 → PDF 1 + HWPX 1.

_NOTICE_12503_FILES = [
    "개인정보 보호법 시행령 일부개정령안 입법예고 공고문.pdf",
    "개인정보 보호법 시행령 일부개정령안.pdf",
    "개인정보 보호법 시행령 일부개정령안 조문별 제개정이유서.pdf",
    "규제영향분석서.pdf",
]
_PRESS_12500_FILES = [
    "[260916 10시보도] ISMS-P 심사 개선을 위한 보호법 시행령 개정안 입법예고(자율보호정책과).pdf",
    "[260916 10시보도] ISMS-P 심사 개선을 위한 보호법 시행령 개정안 입법예고(자율보호정책과).hwpx",
]


def _acceptance_detail(files, body):
    items = "".join(
        f'<li><a href="/cmm/fms/FileDown.do?atchFileId=FILE_ACCEPT&amp;fileSn={i}" '
        f'title="{name} 다운로드"><span class="name">{name}</span>'
        f'<span class="blind">다운로드</span></a>'
        f'<a href="/cmm/fms/FileViewer.do?atchFileId=FILE_ACCEPT&amp;fileSn={i}" '
        f'class="btn">바로보기</a></li>'
        for i, name in enumerate(files)
    )
    return (
        f'<div class="bbs-view-cont"><p>{body}</p></div>'
        f'<div class="file-list"><span class="tit">첨부파일</span><ul>{items}</ul></div>'
    )


def test_acceptance_shape_notice_12503_yields_four_pdfs():
    html = _acceptance_detail(
        _NOTICE_12503_FILES,
        "개인정보보호위원회 공고 제2026-00호 「개인정보 보호법 시행령」 일부개정령(안) 입법예고",
    )
    scraper = _scraper(fetcher=_DetailFetcher(html))
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:12503", title="t",
                url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=12503")
    scraper.enrich(post)
    assert [a.filename for a in post.attachments] == _NOTICE_12503_FILES
    assert len(post.attachments) == 4
    assert all(a.filename.lower().endswith(".pdf") for a in post.attachments)
    assert len({a.url for a in post.attachments}) == 4    # 뷰어 링크가 섞이지 않음
    assert scraper.enrich_succeeded(post) is True


def test_acceptance_shape_press_12500_yields_pdf_and_hwpx():
    html = _acceptance_detail(
        _PRESS_12500_FILES,
        "개인정보보호위원회는 개인정보 보호 인증(ISMS-P) 심사 개선을 위한 시행령 개정안을 입법예고한다고 밝혔다.",
    )
    scraper = _scraper(key="pipc_press", list_url=PRESS_LIST, fetcher=_DetailFetcher(html))
    post = Post(source_key="pipc_press", source_name="p", post_id="nttId:12500", title="t",
                url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=12500")
    scraper.enrich(post)
    names = [a.filename for a in post.attachments]
    assert names == _PRESS_12500_FILES                    # 표시 파일명 그대로 보존
    assert len(names) == 2
    assert {n.rsplit(".", 1)[1].lower() for n in names} == {"pdf", "hwpx"}
    assert "ISMS-P" in post.body
    assert scraper.enrich_succeeded(post) is True


def test_viewer_exclusion_looks_at_the_path_not_the_filename_query():
    """파일명에 '미리보기'/'preview' 가 들어간 **진짜** 다운로드 링크를 버리지 않는다."""
    html = (
        '<div class="view-cont"><p>본문입니다.</p></div>'
        '<a href="/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0&amp;orignFileNm=preview.pdf">'
        'preview.pdf</a>'
    )
    scraper = _scraper(fetcher=_DetailFetcher(html))
    post = Post(source_key="pipc_notice", source_name="n", post_id="nttId:1", title="t",
                url="https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1")
    scraper.enrich(post)
    assert [a.filename for a in post.attachments] == ["preview.pdf"]


# ================================================================================
# Codex 리뷰 대응 회귀 (PR #32)
# ================================================================================

def _detail(html, key="pipc_notice", list_url=NOTICE_LIST, nttid="12503"):
    """상세 HTML 하나로 enrich 를 돌리고 (scraper, post) 를 돌려준다."""
    scraper = _scraper(key=key, list_url=list_url, fetcher=_DetailFetcher(html))
    post = Post(
        source_key=key, source_name=key, post_id=f"nttId:{nttid}", title="t",
        url=f"https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do"
            f"?bbsId=BS061&mCode=C010010000&nttId={nttid}",
    )
    scraper._dump_debug = lambda *a, **k: None
    scraper.enrich(post)
    return scraper, post


_BODY = '<div class="view-cont"><p>기사 본문입니다. 요약 입력으로 쓰기에 충분히 깁니다.</p></div>'


# --- Finding 1: href 없이 onclick 만 가진 앵커 ---------------------------------

def test_f1_anchor_with_only_onclick_is_discovered():
    """`<a onclick="fn_egov_downFile(...)">` (href 없음)도 첨부로 잡힌다.

    예전 후보 선정은 `find_all("a", href=True)` 라서 `_file_url()` 이 onclick 을 볼 줄
    알면서도 **호출되지 않았다**(내부 계약 불일치).
    """
    _, post = _detail(
        _BODY + '<a onclick="fn_egov_downFile(\'FILE_ONLY_ONCLICK\',\'2\')">규제영향분석서.pdf</a>'
    )
    assert [a.filename for a in post.attachments] == ["규제영향분석서.pdf"]
    assert post.attachments[0].url == (
        "https://pipc.go.kr/np/cmm/fms/FileDown.do?atchFileId=FILE_ONLY_ONCLICK&fileSn=2"
    )


def test_f1_anchor_without_href_and_onclick_is_ignored():
    _, post = _detail(_BODY + "<a>앵커 메커니즘 없음</a>")
    assert post.attachments == []


def test_f1_unrelated_onclick_anchor_is_ignored():
    """다운로드와 무관한 onclick(레이어 열기 등)은 첨부가 아니다."""
    _, post = _detail(
        _BODY
        + '<a href="#" onclick="openLayer(\'help\'); return false;">도움말</a>'
        + '<a onclick="goPage(2)">다음 페이지</a>'
    )
    assert post.attachments == []


# --- Finding 2: /np 애플리케이션 컨텍스트 보존 ---------------------------------

def test_f2_js_download_url_preserves_np_application_context():
    """`/cmm/...` 로 시작하는 경로를 urljoin 하면 컨텍스트가 날아가 링크가 깨진다."""
    _, post = _detail(_BODY + '<a href="#" onclick="fn_egov_downFile(\'FILE_123\',\'4\')">붙임.pdf</a>')
    assert post.attachments[0].url == (
        "https://pipc.go.kr/np/cmm/fms/FileDown.do?atchFileId=FILE_123&fileSn=4"
    )
    # 루트 형태는 명시적으로 거부한다(회귀 방지).
    assert not post.attachments[0].url.startswith("https://pipc.go.kr/cmm/")
    assert "/np/np/" not in post.attachments[0].url        # 컨텍스트 중복 없음


def test_f2_context_is_derived_not_hardcoded():
    """컨텍스트는 현재 URL 에서 유도한다 — 호스트도 '/np' 도 박아넣지 않는다."""
    from src.scrapers.pipc import PipcBoardScraper as P

    cases = {
        # 상세 URL                                                  기대 다운로드 URL
        "https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1":
            "https://pipc.go.kr/np/cmm/fms/FileDown.do?atchFileId=F&fileSn=0",
        # 컨텍스트가 없는 배치(루트 애플리케이션)
        "https://pipc.go.kr/cop/bbs/selectBoardArticle.do?nttId=1":
            "https://pipc.go.kr/cmm/fms/FileDown.do?atchFileId=F&fileSn=0",
        # 다른 호스트여도 그 호스트를 그대로 쓴다
        "https://www.pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=1":
            "https://www.pipc.go.kr/np/cmm/fms/FileDown.do?atchFileId=F&fileSn=0",
    }
    for base, expected in cases.items():
        assert P._egov_download_url(base, "F", "0") == expected


# --- Finding 3: 행 텍스트에만 있는 한국어 표기 날짜 -----------------------------

def test_f3_korean_date_in_bare_row_text_is_parsed():
    """전용 요소 없이 행 텍스트로만 적힌 '2026년 9월 9일' 도 정규화된다."""
    html = ('<li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;mCode=C010010000'
            '&amp;nttId=1">제목</a> 2026년 9월 9일</li>')
    assert _parse(NOTICE_LIST, html)[0].date == "2026-09-09"


def test_f3_iso_date_behaviour_is_unchanged():
    """현행 라이브 표기(YYYY-MM-DD)와 기존 동작은 그대로다."""
    for raw, expected in (
        ("2026-09-16", "2026-09-16"),
        ("2026.09.16", "2026-09-16"),
        ("2026/9/16", "2026-09-16"),
    ):
        html = (f'<li><a href="/np/cop/bbs/selectBoardArticle.do?bbsId=BS061&amp;'
                f'mCode=C010010000&amp;nttId=1">제목</a> {raw}</li>')
        assert _parse(NOTICE_LIST, html)[0].date == expected


# --- Finding 4: 크기 표기 / UI 안내말 정규화 순서 -------------------------------

def test_f4_size_suffix_is_removed_even_when_a_ui_word_follows_it():
    from src.scrapers.pipc import _normalize_filename

    assert _normalize_filename("보고서.pdf (3.1 MB) 다운로드") == "보고서.pdf"
    assert _normalize_filename("보고서.hwpx [850 KB] 새창열림") == "보고서.hwpx"
    assert _normalize_filename("보고서.pdf 다운로드 새창열림") == "보고서.pdf"
    assert _normalize_filename("보고서.pdf") == "보고서.pdf"


def test_f4_legitimate_filename_words_are_not_mangled():
    """'다운로드'·'미리보기' 가 진짜 파일명의 일부일 때 잘라내지 않는다."""
    from src.scrapers.pipc import _normalize_filename

    for name in (
        "다운로드 서비스 개선안.pdf",          # 앞
        "자료 미리보기 안내서.pdf",            # 가운데
        "2026 내려받기 통계.hwpx",
    ):
        assert _normalize_filename(name) == name


def test_f4_attachment_filename_keeps_extension_through_enrich():
    """정규화 순서 버그의 실제 영향(확장자 잃은 파일명)이 재발하지 않는다."""
    _, post = _detail(
        _BODY
        + '<a href="/np/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0">'
          '보고서.pdf (3.1 MB) 다운로드</a>'
    )
    assert [a.filename for a in post.attachments] == ["보고서.pdf"]


# --- Finding 5: 페이지 전체의 다운로드 링크를 첨부로 보지 않는다 -----------------

def test_f5_site_wide_download_links_are_not_article_attachments():
    """머리말·꼬리말·네비게이션의 공통 다운로드가 글 첨부로 붙지 않는다.

    이것이 실측으로 재현되던 문제다 — 꼬리말의 '프로그램 다운로드'가 모든 글의
    첨부가 되어 메일에 실리고 실제로 내려받기까지 했다.
    """
    html = (
        _BODY
        + '<header><a href="/np/common/download?file=guide">이용안내 다운로드</a></header>'
        + '<nav><a href="/np/cop/bbs/download.do?menu=1">서식 다운로드</a></nav>'
        + '<footer><a href="/software/download">프로그램 다운로드</a>'
          '<a href="/np/getFile?name=viewer.exe">문서뷰어 내려받기</a></footer>'
    )
    _, post = _detail(html)
    assert post.attachments == []


def test_f5_real_egov_attachment_is_still_included():
    _, post = _detail(
        _BODY + '<a href="/np/cmm/fms/FileDown.do?atchFileId=FILE_REAL&amp;fileSn=0">붙임.pdf</a>'
    )
    assert [a.filename for a in post.attachments] == ["붙임.pdf"]
    assert post.attachments[0].url.endswith("atchFileId=FILE_REAL&fileSn=0")


def test_f5_four_article_attachments_and_site_noise_together():
    """사이트 공통 링크가 섞여 있어도 글 첨부 4건만 정확히 잡는다."""
    files = [f"붙임{i}.pdf" for i in range(1, 5)]
    html = (
        _BODY
        + '<header><a href="/np/common/download?file=guide">이용안내 다운로드</a></header>'
        + "".join(
            f'<a href="/np/cmm/fms/FileDown.do?atchFileId=FILE_A&amp;fileSn={i}">{n}</a>'
            for i, n in enumerate(files)
        )
        + '<footer><a href="/software/download">프로그램 다운로드</a></footer>'
    )
    _, post = _detail(html)
    assert [a.filename for a in post.attachments] == files
    assert len({a.url for a in post.attachments}) == 4


def test_f5_same_file_exposed_by_two_ui_links_is_one_attachment():
    """같은 파일을 가리키는 두 UI 링크는 URL 중복 제거로 1건이 된다."""
    html = (
        _BODY
        + '<a href="/np/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0">붙임.pdf</a>'
        + '<a href="/np/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0" class="btn">다운로드</a>'
        + '<a href="#" onclick="fn_egov_downFile(\'A\',\'0\')">같은 파일</a>'
    )
    _, post = _detail(html)
    assert len(post.attachments) == 1
    assert post.attachments[0].filename == "붙임.pdf"


def test_f5_viewer_link_is_not_an_attachment_even_with_file_identifiers():
    """뷰어는 같은 파일의 다른 표현이다 — 식별자를 달고 와도 첨부가 아니다."""
    html = (
        _BODY
        + '<a href="/np/cmm/fms/FileDown.do?atchFileId=A&amp;fileSn=0">붙임.pdf</a>'
        + '<a href="/np/cmm/fms/FileViewer.do?atchFileId=A&amp;fileSn=0">바로보기</a>'
    )
    _, post = _detail(html)
    assert len(post.attachments) == 1
    assert post.attachments[0].filename == "붙임.pdf"


# ================================================================================
# 라이브 HTML 정합 (2026-09-16 GitHub Actions verify-results 아티팩트 기준)
#
# 아래 두 계약은 **추정이 아니라 실제 PIPC 응답에서 관측된 것**이다.
#   본문   : <tr><td colspan="4" class="tbl_cnts"><div><p>…</p></div></td></tr>
#   첨부명 : <a alt="원본파일명.pdf" class="downBtn" href="#LINK"
#              onclick="javascript:fn_egov_downFile('FILE_…','0','pdf')"
#              title="첨부파일 다운로드">다운로드</a>
# 공지사항·보도자료 양쪽 모두 같은 형태였다.
# ================================================================================

def test_live_body_container_td_tbl_cnts_is_used():
    """실제 본문 컨테이너 `td.tbl_cnts` 에서 본문을 뽑는다."""
    html = """
    <table>
      <tr>
        <td colspan="4" class="tbl_cnts">
          <div>
            <p>실제 게시글 본문입니다.</p>
          </div>
        </td>
      </tr>
    </table>"""
    scraper, post = _detail(html)
    assert post.body
    assert "실제 게시글 본문입니다." in post.body
    assert scraper.enrich_succeeded(post) is True


def test_live_body_container_excludes_metadata_outside_it():
    """`td.tbl_cnts` 바깥의 제목·담당부서·등록일·조회수·첨부·이전다음글은 본문이 아니다."""
    html = """
    <table class="board_view">
      <tr><th>제목</th><td colspan="3">「개인정보 보호법 시행령」 일부개정령(안) 입법예고</td></tr>
      <tr><th>담당부서</th><td>자율보호정책과</td><th>등록일</th><td>2026-09-16</td></tr>
      <tr><th>조회수</th><td colspan="3">1204</td></tr>
      <tr>
        <td colspan="4" class="tbl_cnts">
          <div>
            <p>개인정보보호위원회는 「개인정보 보호법 시행령」 일부개정령안을 입법예고합니다.</p>
            <p>('26.1월 기준)</p>
          </div>
        </td>
      </tr>
      <tr><th>첨부파일</th><td colspan="3">
        <a alt="공고문.pdf" class="downBtn" href="#LINK"
           onclick="javascript:fn_egov_downFile('FILE_1','0','pdf')"
           title="첨부파일 다운로드">다운로드</a></td></tr>
      <tr><th>이전글</th><td colspan="3">앞 글 제목</td></tr>
      <tr><th>다음글</th><td colspan="3">뒤 글 제목</td></tr>
    </table>
    <div class="satisfaction">만족도 조사</div>
    <footer>Copyright 개인정보보호위원회</footer>"""
    _, post = _detail(html)
    assert "개인정보보호위원회는" in post.body
    assert "('26.1월 기준)" in post.body
    for junk in (
        "일부개정령(안) 입법예고",   # 제목
        "자율보호정책과", "등록일", "2026-09-16", "조회수", "1204",
        "공고문.pdf", "첨부파일", "다운로드",
        "앞 글 제목", "뒤 글 제목", "만족도", "Copyright",
    ):
        assert junk not in post.body, f"본문에 {junk!r} 가 섞였다"


def test_live_body_container_is_tried_before_the_unverified_candidates():
    """검증된 `td.tbl_cnts` 가 뒤쪽 미검증 후보보다 먼저 쓰인다."""
    from src.scrapers.pipc import _BODY_SELECTORS

    assert _BODY_SELECTORS[0] == "td.tbl_cnts"
    html = """
    <div class="view-cont"><p>옛 후보 셀렉터에 걸린 내용</p></div>
    <table><tr><td colspan="4" class="tbl_cnts"><div><p>진짜 본문입니다.</p></div></td></tr></table>"""
    _, post = _detail(html)
    assert post.body == "진짜 본문입니다."


def test_live_attachment_original_filename_comes_from_alt_pdf():
    """원본 파일명은 앵커 `alt` 에 있다 — title·텍스트는 공통 안내문이다."""
    html = _BODY + (
        '<a alt="[260916 10시보도] 테스트 보도자료(자율보호정책과).pdf"'
        '   class="downBtn" href="#LINK"'
        '   onclick="javascript:fn_egov_downFile(\'FILE_123\',\'0\',\'pdf\')"'
        '   title="첨부파일 다운로드">다운로드</a>'
    )
    _, post = _detail(html, key="pipc_press", list_url=PRESS_LIST, nttid="12500")
    assert len(post.attachments) == 1
    att = post.attachments[0]
    assert att.filename == "[260916 10시보도] 테스트 보도자료(자율보호정책과).pdf"
    assert att.filename not in ("첨부파일", "다운로드")
    # href="#LINK" 여도 onclick 핸들러로 URL 이 만들어지고 /np 컨텍스트가 보존된다.
    assert att.url == (
        "https://pipc.go.kr/np/cmm/fms/FileDown.do?atchFileId=FILE_123&fileSn=0"
    )


def test_live_attachment_original_filename_comes_from_alt_hwpx():
    """HWPX 확장자도 그대로 보존된다(.pdf 만 다루지 않는다)."""
    html = _BODY + (
        '<a alt="[260916 10시보도] 테스트 보도자료(자율보호정책과).hwpx"'
        '   class="downBtn" href="#LINK"'
        '   onclick="javascript:fn_egov_downFile(\'FILE_123\',\'1\',\'hwpx\')"'
        '   title="첨부파일 다운로드">다운로드</a>'
    )
    _, post = _detail(html, key="pipc_press", list_url=PRESS_LIST, nttid="12500")
    assert post.attachments[0].filename == (
        "[260916 10시보도] 테스트 보도자료(자율보호정책과).hwpx"
    )
    assert post.attachments[0].url.endswith("atchFileId=FILE_123&fileSn=1")


def test_live_press_shape_yields_body_plus_pdf_and_hwpx():
    """보도자료 수용표본 형태(nttId=12500): 본문 + PDF 1 + HWPX 1."""
    base = "[260916 10시보도] ISMS-P 심사 개선을 위한 보호법 시행령 개정안 입법예고(자율보호정책과)"
    html = f"""
    <table>
      <tr><th>제목</th><td colspan="3">ISMS-P 심사 개선 … 입법예고</td></tr>
      <tr><td colspan="4" class="tbl_cnts"><div>
        <p>- 인증심사시 서면·현장심사 병행 등 개선 추진</p>
      </div></td></tr>
      <tr><th>첨부파일</th><td colspan="3">
        <a alt="{base}.pdf" class="downBtn" href="#LINK"
           onclick="javascript:fn_egov_downFile('FILE_000000000561491','0','pdf')"
           title="첨부파일 다운로드">다운로드</a>
        <a alt="{base}.hwpx" class="downBtn" href="#LINK"
           onclick="javascript:fn_egov_downFile('FILE_000000000561491','1','hwpx')"
           title="첨부파일 다운로드">다운로드</a>
      </td></tr>
    </table>"""
    scraper, post = _detail(html, key="pipc_press", list_url=PRESS_LIST, nttid="12500")
    assert "서면·현장심사 병행" in post.body
    assert scraper.enrich_succeeded(post) is True
    names = [a.filename for a in post.attachments]
    assert names == [f"{base}.pdf", f"{base}.hwpx"]
    assert {n.rsplit(".", 1)[1] for n in names} == {"pdf", "hwpx"}
    assert len({a.url for a in post.attachments}) == 2


def test_live_notice_shape_yields_body_plus_four_pdfs():
    """공지사항 수용표본 형태(nttId=12503): 본문 + PDF 4건, 원본 파일명 보존."""
    files = [
        "개인정보 보호법 시행령 일부개정령안 입법예고 공고문.pdf",
        "개인정보 보호법 시행령 일부개정령안.pdf",
        "개인정보 보호법 시행령 일부개정령안 조문별 제개정이유서.pdf",
        "규제영향분석서.pdf",
    ]
    links = "".join(
        f'<a alt="{n}" class="downBtn" href="#LINK"'
        f'   onclick="javascript:fn_egov_downFile(\'FILE_000000000561490\',\'{i}\',\'pdf\')"'
        f'   title="첨부파일 다운로드">다운로드</a>'
        for i, n in enumerate(files)
    )
    html = f"""
    <table>
      <tr><td colspan="4" class="tbl_cnts"><div>
        <p>개인정보보호위원회는 「개인정보 보호법 시행령」 일부개정령(안)을 입법예고합니다.</p>
      </div></td></tr>
      <tr><th>첨부파일</th><td colspan="3">{links}</td></tr>
    </table>"""
    scraper, post = _detail(html)
    assert "입법예고합니다" in post.body
    assert scraper.enrich_succeeded(post) is True
    assert [a.filename for a in post.attachments] == files
    assert len(post.attachments) == 4
    assert all(a.filename.endswith(".pdf") for a in post.attachments)
    assert len({a.url for a in post.attachments}) == 4
