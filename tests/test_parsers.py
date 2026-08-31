"""라이브 HTML 구조를 압축한 파서 회귀 테스트.

실제 사이트(2026-07 캡처) 마크업의 핵심 패턴을 최소 HTML 로 재현한다.
"""
import logging
import os
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.models import Post
from src.scrapers.fsc import FscBoardScraper
from src.scrapers.fss import FssBoardScraper


def _fsc_scraper(list_url, fetcher=None):
    return FscBoardScraper(
        SourceConfig(key="k", name="k", type="x", list_url=list_url), fetcher=fetcher
    )


def _fsc(list_url, html):
    return _fsc_scraper(list_url)._parse_list(BeautifulSoup(html, "lxml"))


class _DetailFetcher:
    """enrich() 용 최소 fetcher — 상세 HTML 하나(+선택적 첨부 바이트)를 돌려준다."""

    def __init__(self, html, blob=None):
        self.html = html
        self.blob = blob
        self.downloaded = []

    def get(self, url, referer=None):
        return object()

    def text(self, resp):
        return self.html

    def download(self, url, referer=None):
        if self.blob is None:  # pragma: no cover - 첨부 없는 케이스
            raise AssertionError("이 테스트에는 첨부가 없어야 한다")
        self.downloaded.append(url)
        return self.blob


def _fss(list_url, html):
    sc = FssBoardScraper(SourceConfig(key="k", name="k", type="x", list_url=list_url), fetcher=None)
    return sc._parse_list(BeautifulSoup(html, "lxml"))


def _fss_scraper(key, list_url, fetcher):
    return FssBoardScraper(
        SourceConfig(key=key, name=key, type="fss_board", list_url=list_url), fetcher=fetcher
    )


def test_fsc_press_path_id_title_and_attachments():
    html = """
    <ul><li><div class="inner"><div class="cont">
      <div class="subject"><a class="new" href="/no010101/87401?curPage="
        title="외부감사 규정 개정안">외부감사 규정 개정안<span class="newbbs-span">. 금일</span></a></div>
      <div class="file"><div class="file-list">
        <a href="/comm/getFile?srvcId=BBSTY1&upperNo=87401&fileNo=1" title="보도자료.hwp"><span class="name">보도자료.hwp</span></a>
        <span class="name">(188 KB)</span>
        <span class="ico download"><a href="/comm/getFile?srvcId=BBSTY1&upperNo=87401&fileNo=1">down</a></span>
      </div></div>
    </div></div></li></ul>"""
    posts = _fsc("https://www.fsc.go.kr/no010101", html)
    assert len(posts) == 1
    p = posts[0]
    assert p.post_id == "path:87401"
    assert p.title == "외부감사 규정 개정안"          # span 잡텍스트 제외
    assert len(p.attachments) == 1                      # 중복 링크 dedup
    assert p.attachments[0].filename == "보도자료.hwp"


def test_fsc_legislation_noticeid_and_sibling_filename():
    html = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4159&curPage=" title="특별법 시행령 입법예고">특별법 시행령 입법예고</a></div>
      <div class="file"><div class="file-list">
        <span class="name">1. 공고문 입법예고.hwpx (42 KB)</span>
        <span class="ico download"><a href="/comm/getFile?srvcId=RULENOTICE&upperNo=4159&fileNo=1" title="파일 다운로드">down</a></span>
      </div></div>
    </div></li></ul>"""
    posts = _fsc("https://www.fsc.go.kr/po040301", html)
    assert len(posts) == 1
    assert posts[0].post_id == "noticeId:4159"
    assert posts[0].attachments[0].filename == "1. 공고문 입법예고.hwpx"  # 크기표기 제거, 앵커 title('파일 다운로드') 아님


def test_fsc_press_date_from_dedicated_element_not_first_date_in_text():
    """보도자료: 게시일 전용 요소(.day)의 값만 쓴다.

    항목 텍스트에는 제목의 공고번호와 첨부파일명이 게시일보다 앞에 나오므로,
    목록 전체를 정규식으로 훑으면 엉뚱한 숫자를 게시일로 잡는다.
    """
    html = """
    <ul><li><div class="inner"><div class="cont">
      <div class="subject"><a href="/no010101/87401?curPage="
        title="금융위원회 공고 제2026-15호 (2019-03-11 제정) 개정안">개정안</a></div>
      <div class="file"><div class="file-list">
        <a href="/comm/getFile?srvcId=BBSTY1&upperNo=87401&fileNo=1"><span class="name">2025-01-02_별첨.hwp</span></a>
      </div></div>
      <div class="info">
        <span class="division">자본시장과</span>
        <span class="day">2026-07-24</span>
        <span class="hit">1,234</span>
      </div>
    </div></div></li></ul>"""
    posts = _fsc("https://www.fsc.go.kr/no010101", html)
    assert len(posts) == 1
    p = posts[0]
    assert p.date == "2026-07-24"
    # 기존 동작 보존
    assert p.post_id == "path:87401"
    assert p.attachments[0].filename == "2025-01-02_별첨.hwp"


def test_fsc_press_date_from_label_and_time_element():
    """'등록일' 라벨 값과 <time datetime> 모두 게시일 전용 요소로 인정한다."""
    labelled = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87402" title="가계부채 점검회의">가계부채 점검회의</a></div>
      <dl class="info"><dt>등록일</dt><dd>2026.07.23</dd><dt>조회수</dt><dd>2026</dd></dl>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", labelled)[0].date == "2026-07-23"

    semantic = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87403" title="정례회의 결과">정례회의 결과</a></div>
      <p class="info"><time datetime="2026-07-22T14:00:00+09:00">7월 22일</time></p>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", semantic)[0].date == "2026-07-22"


def test_fsc_legislation_uses_posting_date_not_notice_period():
    """입법예고: 예고기간(시작일/종료일)이 아니라 등록일을 게시일로 쓴다."""
    html = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4159&curPage=" title="특별법 시행령 입법예고">특별법 시행령 입법예고</a></div>
      <div class="info">
        <span class="tit">예고기간</span><span class="day">2026-07-24 ~ 2026-08-13</span>
      </div>
      <dl class="info"><dt>등록일</dt><dd>2026-07-23</dd></dl>
      <div class="file"><div class="file-list">
        <span class="name">1. 공고문 입법예고.hwpx (42 KB)</span>
        <span class="ico download"><a href="/comm/getFile?srvcId=RULENOTICE&upperNo=4159&fileNo=1" title="파일 다운로드">down</a></span>
      </div></div>
    </div></li></ul>"""
    posts = _fsc("https://www.fsc.go.kr/po040301", html)
    assert len(posts) == 1
    p = posts[0]
    assert p.date == "2026-07-23"          # 예고기간 시작일(07-24)/종료일(08-13) 아님
    # 기존 동작 보존
    assert p.post_id == "noticeId:4159"
    assert p.attachments[0].filename == "1. 공고문 입법예고.hwpx"


def test_fsc_legislation_period_only_leaves_date_empty():
    """예고기간밖에 없으면 시작일을 게시일로 추측하지 않고 비워 둔다."""
    html = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4160" title="규정변경예고">규정변경예고</a></div>
      <div class="period"><span class="tit">예고기간</span>
        <span class="day">2026-07-24</span> ~ <span class="day">2026-08-13</span></div>
    </div></li></ul>"""
    posts = _fsc("https://www.fsc.go.kr/po040301", html)
    assert len(posts) == 1
    assert posts[0].date == ""


def test_fsc_date_absent_or_invalid_stays_empty():
    """전용 요소가 없거나 값이 날짜로 유효하지 않으면 빈 문자열."""
    no_date = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87404" title="공고 제2026-15호">공고 제2026-15호</a></div>
      <div class="file"><div class="file-list">
        <a href="/comm/getFile?srvcId=BBSTY1&upperNo=87404&fileNo=1"><span class="name">2026-07-24 보도자료.hwp</span></a>
      </div></div>
      <div class="info"><span class="division">감독정책과</span><span class="hit">20260724</span></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", no_date)[0].date == ""

    invalid = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87405" title="회의 결과">회의 결과</a></div>
      <div class="info"><span class="day">2026-13-45</span></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", invalid)[0].date == ""

    partial = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87406" title="회의 결과">회의 결과</a></div>
      <div class="info"><span class="day">2026-07</span></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", partial)[0].date == ""


def test_fsc_legislation_period_time_element_is_not_the_posting_date():
    """<time> 도 예외 없이 기간 가드를 받는다(코덱스 리뷰).

    예고기간을 시맨틱 마크업으로 적은 레이아웃에서 <time> 경로만 가드를 건너뛰면
    예고기간 시작일이 그대로 게시일로 올라온다.
    """
    period_only = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4161" title="규정변경예고">규정변경예고</a></div>
      <div class="info"><span class="tit">예고기간</span>
        <time datetime="2026-07-24">2026-07-24</time> ~ <time datetime="2026-08-13">2026-08-13</time></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/po040301", period_only)[0].date == ""

    # 예고기간과 등록일이 각각 별도 블록이면 등록일만 골라낸다.
    with_posted = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4162" title="시행령 입법예고">시행령 입법예고</a></div>
      <div class="period"><span class="tit">예고기간</span>
        <time datetime="2026-07-24">2026-07-24</time> ~ <time datetime="2026-08-13">2026-08-13</time></div>
      <div class="info"><span class="tit">등록일</span><time datetime="2026-07-23">2026-07-23</time></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/po040301", with_posted)[0].date == "2026-07-23"


def test_fsc_date_label_inside_dedicated_element_is_stripped():
    """전용 요소가 라벨과 값을 함께 담아도 인정한다(코덱스 리뷰).

    '날짜'는 게시일 라벨로 인정하면서 접두사 제거 대상에서 빠져 있으면
    '날짜: 2026-07-24' 가 통째로 버려진다. 인정 라벨 전부가 제거 대상이어야 한다.
    """
    for label in ("날짜", "등록일", "등록일자", "게시일자", "일자"):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="/no010101/87410" title="회의 결과">회의 결과</a></div>
          <div class="info"><span class="date">{label}: 2026-07-24</span></div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/no010101", html)[0].date == "2026-07-24", label


def test_fsc_enrich_fills_date_from_detail_header_when_list_has_none():
    """목록에 게시일 전용 요소가 없는 레이아웃이면 상세 머리말에서 보강한다(코덱스 리뷰).

    기존 목록 마크업(담당부서/조회수만 노출)에서는 목록 추출만으로는 날짜가 계속
    비어 있으므로, 상세를 읽는 enrich() 에서 한 번 더 시도해야 실제로 채워진다.
    """
    list_html = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87411" title="정례회의 결과">정례회의 결과</a></div>
      <div class="info"><span class="division">대변인실</span><span class="hit">1,234</span></div>
    </div></li></ul>"""
    post = _fsc("https://www.fsc.go.kr/no010101", list_html)[0]
    assert post.date == ""  # 목록 단계에서는 여전히 빈 문자열

    detail = """
    <div class="board-view-wrap">
      <div class="header">
        <h3 class="subject">정례회의 결과</h3>
        <dl class="info"><dt>담당부서</dt><dd>대변인실</dd><dt>등록일</dt><dd>2026-07-21</dd></dl>
      </div>
      <div class="body"><p>2026-07-15 회의에서 의결한 내용입니다.</p></div>
    </div>"""
    _fsc_scraper("https://www.fsc.go.kr/no010101", _DetailFetcher(detail)).enrich(post)
    assert post.date == "2026-07-21"                  # 본문의 2026-07-15 가 아님
    assert "의결한 내용입니다" in post.body            # 기존 본문 추출 보존


def test_fsc_enrich_keeps_list_date_and_tolerates_dateless_detail():
    """목록에서 얻은 게시일은 상세가 덮어쓰지 않고, 상세에도 없으면 빈 문자열 유지."""
    list_html = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87412" title="보도자료">보도자료</a></div>
      <div class="info"><span class="day">2026-07-24</span></div>
    </div></li></ul>"""
    post = _fsc("https://www.fsc.go.kr/no010101", list_html)[0]
    detail = """
    <div class="board-view-wrap">
      <div class="header"><dl><dt>등록일</dt><dd>2026-07-20</dd></dl></div>
      <div class="body"><p>본문</p></div>
    </div>"""
    _fsc_scraper("https://www.fsc.go.kr/no010101", _DetailFetcher(detail)).enrich(post)
    assert post.date == "2026-07-24"  # 목록 값 유지

    bare = _fsc(
        "https://www.fsc.go.kr/no010101",
        """<ul><li><div class="cont">
          <div class="subject"><a href="/no010101/87413" title="보도자료">보도자료</a></div>
        </div></li></ul>""",
    )[0]
    dateless = '<div class="board-view-wrap"><div class="header"><h3>보도자료</h3></div>' \
               '<div class="body"><p>공고 제2026-15호 관련</p></div></div>'
    _fsc_scraper("https://www.fsc.go.kr/no010101", _DetailFetcher(dateless)).enrich(bare)
    assert bare.date == ""


def test_fsc_period_guard_reaches_deeply_nested_wrappers():
    """기간 라벨과 값 사이에 래퍼가 여러 겹 끼어도 가드가 뚫리지 않는다(코덱스 리뷰).

    조상을 고정 단계(2단)만 보면 '예고기간'을 담은 블록에 닿기 전에 탐색이 끝나
    예고기간 시작일이 게시일로 올라온다.
    """
    nested_time = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4163" title="규정변경예고">규정변경예고</a></div>
      <div class="period"><span class="tit">예고기간</span>
        <div class="wrap"><div class="inner"><div class="row">
          <time datetime="2026-07-24">2026-07-24</time>
        </div></div></div></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/po040301", nested_time)[0].date == ""

    nested_class = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4164" title="규정변경예고">규정변경예고</a></div>
      <div class="period"><span class="tit">예고기간</span>
        <div class="wrap"><div class="inner"><div class="row">
          <span class="day">2026-07-24</span>
        </div></div></div></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/po040301", nested_class)[0].date == ""


def test_fsc_sibling_period_block_does_not_mask_unlabelled_posting_date():
    """예고기간 블록과 게시일 블록이 형제면, 공용 래퍼 때문에 게시일이 버려지면 안 된다(코덱스 리뷰).

    조상의 전체 텍스트를 보면 둘의 공용 래퍼(.cont)에 '예고기간'이 섞여 있어
    라벨 없는 게시일(.day / <time>)까지 함께 거부된다.
    """
    for value_el in (
        '<span class="day">2026-07-23</span>',
        '<time datetime="2026-07-23">2026-07-23</time>',
    ):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="./po040301/view?noticeId=4165" title="시행령 입법예고">시행령 입법예고</a></div>
          <div class="period"><span class="tit">예고기간</span><span class="day">2026-07-24 ~ 2026-08-13</span></div>
          <div class="info">{value_el}</div>
        </div></li></ul>"""
        posts = _fsc("https://www.fsc.go.kr/po040301", html)
        assert posts[0].date == "2026-07-23", value_el


def test_fsc_separator_between_label_and_value_is_skipped():
    """라벨과 값 사이의 구분자(':', '|', '-')를 라벨로 착각하면 안 된다(코덱스 리뷰).

    구분자에서 탐색이 멈추면 정작 '예고기간' 라벨을 못 보고 예고기간 시작일을
    게시일로 받아들인다.
    """
    for sep in (":", "：", "|", "-", "·"):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="./po040301/view?noticeId=4166" title="시행령 입법예고">시행령 입법예고</a></div>
          <div class="info"><span class="tit">예고기간</span> {sep} <time datetime="2026-07-24">2026-07-24</time></div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/po040301", html)[0].date == "", sep

    # 반대로 '~'는 구분자가 아니라 '이 값이 범위의 꼬리'라는 뜻이므로 건너뛰지 않는다.
    tail = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4167" title="시행령 입법예고">시행령 입법예고</a></div>
      <div class="period"><span class="tit">예고기간</span>
        <span class="day">2026-07-24</span> ~ <span class="day">2026-08-13</span></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/po040301", tail)[0].date == ""


def test_fsc_preceding_sibling_is_label_or_block_by_content_not_tag():
    """앞 형제가 '라벨'인지 '자기 값을 품은 블록'인지는 태그가 아니라 내용으로 가른다.

    같은 <p>/<div> 가 한 곳에선 라벨('예고기간'), 다른 곳에선 값을 품은 블록
    ('예고기간 2026-07-24 ~ 2026-08-13')이다. 태그를 열거하면 한쪽을 고치는 순간
    다른 쪽이 뚫린다(코덱스 리뷰).
    """
    # 값을 품은 형제 블록 = 경계 → 뒤따르는 게시일은 살아 있어야 한다.
    for value_el in (
        '<time datetime="2026-07-23">2026-07-23</time>',
        '<span class="day">2026-07-23</span>',
    ):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="./po040301/view?noticeId=4168" title="시행령 입법예고">시행령 입법예고</a></div>
          <p class="period">예고기간 2026-07-24 ~ 2026-08-13</p>
          <p class="info">{value_el}</p>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/po040301", html)[0].date == "2026-07-23", value_el

    # 라벨만 든 형제(날짜 없음)는 태그가 <p>든 <div>든 이 값의 라벨 → 거부해야 한다.
    for tag in ("p", "div"):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="./po040301/view?noticeId=4169" title="시행령 입법예고">시행령 입법예고</a></div>
          <div class="period">
            <{tag} class="tit">예고기간</{tag}>
            <{tag} class="val"><time datetime="2026-07-24">2026-07-24</time></{tag}>
          </div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/po040301", html)[0].date == "", tag


def test_fsc_period_endpoint_labels_are_not_posting_dates():
    """예고기간의 양 끝(시작일/종료일)을 게시일로 삼지 않는다(코덱스 리뷰).

    바로 앞 라벨이 '시작일'이면 기간 라벨이 아니라 통과해 버린다. 감싼 블록의
    선두 라벨('예고기간')까지 봐야 막힌다.
    """
    for label in ("시작일", "종료일", "개시일", "만료일"):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="./po040301/view?noticeId=4170" title="시행령 입법예고">시행령 입법예고</a></div>
          <div class="period"><span class="tit">예고기간</span>
            <span class="tit">{label}</span><time datetime="2026-07-24">2026-07-24</time></div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/po040301", html)[0].date == "", label

    # 감싼 '예고기간' 블록이 없어도 시작일 자체는 게시일이 아니다.
    bare = """
    <ul><li><div class="cont">
      <div class="subject"><a href="./po040301/view?noticeId=4171" title="시행령 입법예고">시행령 입법예고</a></div>
      <div class="info"><span class="tit">시작일</span><time datetime="2026-07-24">2026-07-24</time></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/po040301", bare)[0].date == ""


def test_fsc_period_context_survives_a_preceding_date_value():
    """시작·종료가 물결 없이 두 값으로 나뉘어도 둘 다 게시일이 아니다(코덱스 리뷰).

    두 번째 값 입장에서 바로 앞은 '첫 번째 날짜 값'이라 경계로 보이고, 그 순간
    바깥 '예고기간' 라벨을 잃는다.
    """
    for sep in ("", " ", "부터 ", "<span>부터</span>"):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="./po040301/view?noticeId=4172" title="시행령 입법예고">시행령 입법예고</a></div>
          <div class="period"><span class="tit">예고기간</span>
            <span class="day">2026-07-24</span>{sep}<span class="day">2026-08-13</span></div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/po040301", html)[0].date == "", repr(sep)


def test_fsc_excluded_class_matches_tokens_not_substrings():
    """제외 영역은 클래스 '토큰'으로 맞춘다 — 부분문자열이면 안 된다(코덱스 리뷰).

    'profile'에 'file'이, 'subject-info'에 'subject'가 걸리면 그 안의 등록일까지
    통째로 사라진다.
    """
    for wrapper in ("profile", "subject-info", "subject-wrap", "filed-info", "info"):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="/no010101/87420" title="회의 결과">회의 결과</a></div>
          <div class="{wrapper}"><span class="tit">등록일</span><span class="day">2026-07-23</span></div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/no010101", html)[0].date == "2026-07-23", wrapper

    # 진짜 제외 대상(.subject/.file-list)은 여전히 후보가 되지 않아야 한다.
    still_excluded = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87421" title="회의 결과">회의 결과</a>
        <span class="day">2019-01-01</span></div>
      <div class="file"><div class="file-list"><span class="name">2020-02-02 별첨.hwp</span></div></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", still_excluded)[0].date == ""


def test_fsc_generic_date_label_does_not_override_period_or_event_context():
    """기간·행사 블록 안의 일반 필드 라벨('일자'/'날짜')은 게시일이 아니다(코덱스 리뷰).

    라벨 경로가 가드 없이 먼저 값을 내면, 뒤에 있는 진짜 등록일보다 예고기간 날짜가
    이긴다. '일자'는 값이 날짜라는 것만 알려줄 뿐 게시일이라는 역할은 말해주지 않는다.
    """
    for generic in ("일자", "날짜"):
        # 기간 블록 안의 '일자' 대신, 뒤에 있는 진짜 등록일이 이겨야 한다.
        with_posted = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="./po040301/view?noticeId=4173" title="시행령 입법예고">시행령 입법예고</a></div>
          <div class="period"><span class="tit">예고기간</span>
            <span class="tit">{generic}</span><time datetime="2026-08-01">2026-08-01</time></div>
          <dl class="info"><dt>등록일</dt><dd>2026-07-23</dd></dl>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/po040301", with_posted)[0].date == "2026-07-23", generic

        # 행사 블록 안이고 게시일이 따로 없으면 비워 둔다(행사일을 올리지 않는다).
        event_only = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="/no010101/87422" title="설명회 안내">설명회 안내</a></div>
          <div class="event"><span class="tit">행사일</span>
            <span class="tit">{generic}</span><time datetime="2026-08-05">2026-08-05</time></div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/no010101", event_only)[0].date == "", generic

        # 반대로 중립적인 맥락이면 '일자'/'날짜'도 그대로 게시일로 쓴다.
        neutral = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="/no010101/87423" title="회의 결과">회의 결과</a></div>
          <div class="info"><span class="tit">{generic}</span><span class="day">2026-07-24</span></div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/no010101", neutral)[0].date == "2026-07-24", generic


def test_fsc_posting_labels_ending_in_ilsi_are_kept():
    """'등록일시' 같은 게시 시각 라벨이 일반 '일시'(행사 일시)에 걸리면 안 된다(코덱스 리뷰)."""
    for label in ("등록일시", "게시일시", "작성일시"):
        pair = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="/no010101/87424" title="회의 결과">회의 결과</a></div>
          <dl class="info"><dt>{label}</dt><dd>2026-07-24</dd></dl>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/no010101", pair)[0].date == "2026-07-24", label

        semantic = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="/no010101/87425" title="회의 결과">회의 결과</a></div>
          <div class="info"><span class="tit">{label}</span>
            <time datetime="2026-07-24 14:00:00+09:00">7월 24일</time></div>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/no010101", semantic)[0].date == "2026-07-24", label

    # 라벨과 값이 한 전용 요소에 같이 있어도 접두사로 떨어져야 한다.
    inline = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87426" title="회의 결과">회의 결과</a></div>
      <div class="info"><span class="date">등록일시: 2026-07-24</span></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", inline)[0].date == "2026-07-24"

    # 단독 '일시'는 여전히 행사 맥락으로 보고 받지 않는다.
    bare = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87427" title="설명회 안내">설명회 안내</a></div>
      <div class="info"><span class="tit">일시</span><time datetime="2026-08-05">8월 5일</time></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", bare)[0].date == ""


def test_fsc_period_words_in_title_or_filename_do_not_drop_the_date():
    """기간 가드는 라벨에만 걸린다 — 제목·첨부파일명의 낱말로 게시일을 버리지 않는다.

    조상 텍스트를 통째로 보면 '제출 기한' 같은 흔한 제목 하나로 그 항목의 게시일이
    사라진다(기간 가드를 조상 전체로 넓히면서 생기는 반대쪽 실패).
    """
    html = """
    <ul><li><div class="inner"><div class="cont">
      <div class="subject"><a href="/no010101/87417"
        title="사업보고서 제출 기한 연장 및 의견제출 기간 안내">사업보고서 제출 기한 연장</a></div>
      <div class="file"><div class="file-list">
        <a href="/comm/getFile?srvcId=BBSTY1&upperNo=87417&fileNo=1"><span class="name">기간별 현황.hwp</span></a>
      </div></div>
      <div class="info"><span class="division">공시제도과</span><span class="day">2026-07-24</span></div>
    </div></div></li></ul>"""
    posts = _fsc("https://www.fsc.go.kr/no010101", html)
    assert posts[0].date == "2026-07-24"
    assert posts[0].attachments[0].filename == "기간별 현황.hwp"


def test_fsc_explicit_posting_label_beats_unrelated_time_element():
    """무관한 <time>(회의일/행사일)보다 '등록일' 라벨 값이 우선한다(코덱스 리뷰).

    <time> 은 '이 값이 날짜'라는 것만 알려줄 뿐 게시일이라는 역할까지 보장하지 않는다.
    """
    meeting = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87414" title="정례회의 개최">정례회의 개최</a></div>
      <div class="meeting"><span class="tit">회의일</span><time datetime="2026-08-01">2026-08-01</time></div>
      <dl class="info"><dt>등록일</dt><dd>2026-07-25</dd></dl>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", meeting)[0].date == "2026-07-25"

    # 행사일만 있고 게시일 라벨이 없으면 행사일을 게시일로 삼지 않는다.
    event_only = """
    <ul><li><div class="cont">
      <div class="subject"><a href="/no010101/87415" title="설명회 안내">설명회 안내</a></div>
      <div class="event"><span class="tit">행사일</span><time datetime="2026-08-05">2026-08-05</time></div>
    </div></li></ul>"""
    assert _fsc("https://www.fsc.go.kr/no010101", event_only)[0].date == ""


def test_fsc_time_datetime_accepts_space_separated_value():
    """<time datetime> 은 날짜와 시각을 'T' 또는 공백으로 가른다 — 둘 다 처리(코덱스 리뷰)."""
    for value in ("2026-07-22T14:00:00+09:00", "2026-07-22 14:00:00+09:00", "2026-07-22"):
        html = f"""
        <ul><li><div class="cont">
          <div class="subject"><a href="/no010101/87416" title="정례회의 결과">정례회의 결과</a></div>
          <p class="info"><time datetime="{value}">7월 22일</time></p>
        </div></li></ul>"""
        assert _fsc("https://www.fsc.go.kr/no010101", html)[0].date == "2026-07-22", value


def test_fss_title_link_board_nttid():
    html = """
    <table><tbody><tr>
      <td class="num">20749</td>
      <td class="title"><a href="/fss/bbs/B0000188/view.do?nttId=219329&menuNo=200218&pageIndex=1">기본예탁금 강화 안내</a></td>
      <td>부서</td><td>2026-07-24</td>
      <td><a class="file-single" href="/fss/cmmn/file/fileDown.do?atchFileId=abc&fileSn=1"><span class="name">보도참고.hwp</span></a></td>
    </tr></tbody></table>"""
    posts = _fss("https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218", html)
    assert len(posts) == 1
    p = posts[0]
    assert p.post_id == "nttId:219329"
    assert p.title == "기본예탁금 강화 안내"
    assert p.date == "2026-07-24"
    assert p.attachments and p.attachments[0].filename == "보도참고.hwp"


def test_fss_sanction_title_from_cell_and_examno_and_yyyymmdd():
    html = """
    <table><tbody><tr>
      <td>215</td>
      <td>주식회사 제이엠씨자산운용</td>
      <td>20260722</td>
      <td><a class="b-default xs" href="/fss/job/openInfo/view.do?menuNo=200476&sdate=2026-01-01&edate=2026-07-24&examMgmtNo=202400649&emOpenSeq=1">내용보기</a></td>
      <td>금융투자검사3국</td><td>593</td>
    </tr></tbody></table>"""
    posts = _fss("https://www.fss.or.kr/fss/job/openInfo/list.do?menuNo=200476", html)
    assert len(posts) == 1
    p = posts[0]
    assert p.title == "주식회사 제이엠씨자산운용"       # '내용보기' 앵커 텍스트가 아니라 회사명
    assert p.post_id == "exam:202400649_1"
    assert p.date == "2026-07-22"                        # YYYYMMDD 처리


def test_fss_mgmt_notice_file_download_detail():
    html = """
    <table><tbody><tr>
      <td class="no">71</td>
      <td>KB증권주식회사</td>
      <td>20260714</td>
      <td><a class="b-default xs" href="/fss.hpdownload?path=/dtm/opn/&file=202500516_12_KB%EC%A6%9D%EA%B6%8C.pdf">내용보기</a></td>
      <td>금융투자검사1국</td>
    </tr></tbody></table>"""
    posts = _fss("https://www.fss.or.kr/fss/job/openInfoImpr/list.do?menuNo=200483", html)
    assert len(posts) == 1
    p = posts[0]
    assert p.title == "KB증권주식회사"
    assert p.post_id.startswith("file:202500516_12_KB")  # 파일명 기반 안정 ID
    assert "hpdownload" in p.url                          # 상세=파일 URL(enrich 에서 첨부 처리)


# --- 금감원 보도자료 상세 본문(2026-08 개편) ---
PRESS_LIST = "https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218"
PRESS_VIEW = (
    "https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=225832&menuNo=200218&pageIndex=1"
)

# 개편 후 상세 마크업의 핵심 패턴을 최소로 압축한 것. 실제 본문은 .n-dbdata 안에만
# 있고, 같은 .krds-bd-view 안에 제목·담당부서·문의전화·등록일·첨부파일명이 함께 있다.
PRESS_DETAIL_NEW = """
<div id="content"><div class="krds-bd-view">
  <div class="sub-info">
    <h3 class="subject">26.6월말 국내은행 BIS기준 자본비율 현황(잠정)</h3>
    <dl class="bd-info">
      <div class="info-wrap"><dt>담당부서</dt><dd>은행리스크감독국</dd></div>
      <div class="info-wrap"><dt>문의</dt><dd>3145-8331</dd></div>
      <div class="info-wrap"><dt>등록일</dt><dd>2026-08-31</dd></div>
    </dl>
  </div>

  <dl class="attach-file-box">
    <dt>첨부파일</dt>
    <dd><a href="/fss/cmmn/file/fileDown.do?atchFileId=FILE_0001&fileSn=0">
      <span class="name">보도자료_자본비율.hwp</span></a></dd>
  </dl>

  <div class="n-dbdata">
    ㅁ '26.6월말 국내은행의 자본비율은 전분기말 대비 상승하고,
    모든 은행의 자본비율이 규제비율을 상회하는 등 양호한 수준을 유지
    <br><br>
    ㅁ 대외 불확실성이 지속되는 가운데 신용위험 확대 가능성이 상존
    <br><br>
    ㅇ 손실흡수능력 확충 및 자본적정성 관리를 강화하도록 유도할 예정
    <br><br>
    ※ 자세한 내용은 첨부파일을 참고하시기 바랍니다.
  </div>
</div></div>
"""


def _press_post(url=PRESS_VIEW):
    return Post(
        source_key="fss_press",
        source_name="금감원 · 보도자료",
        post_id="nttId:225832",
        title="26.6월말 국내은행 BIS기준 자본비율 현황(잠정)",
        url=url,
        date="2026-08-31",
    )


def _enrich_press(html, blob=b"%HWP dummy", key="fss_press"):
    sc = _fss_scraper(key, PRESS_LIST, _DetailFetcher(html, blob=blob))
    post = _press_post()
    post.source_key = key
    sc.enrich(post)
    return sc, post


def test_fss_press_body_comes_from_n_dbdata_only():
    """개편 후 본문은 .n-dbdata 에서 오고, 주변 메타데이터는 섞이지 않는다."""
    _, p = _enrich_press(PRESS_DETAIL_NEW)
    assert p.body
    assert "국내은행의 자본비율" in p.body
    assert "손실흡수능력" in p.body
    # .krds-bd-view 전체를 긁었다면 함께 들어왔을 값들
    assert "은행리스크감독국" not in p.body
    assert "3145-8331" not in p.body
    assert "담당부서" not in p.body
    # 첨부 블록(파일명)은 본문에 섞이지 않는다. 본문 마지막의 '첨부파일을 참고하시기'는
    # 금감원이 직접 쓴 문장이므로 그대로 남아야 한다 — 그래서 파일명으로 검사한다.
    assert "보도자료_자본비율.hwp" not in p.body
    assert "BIS기준 자본비율 현황" not in p.body   # 제목 h3 도 아님


def test_fss_press_body_keeps_bullet_markers_and_numbers():
    """ㅁ/ㅇ 표식과 날짜·비율 같은 수치 표현이 손상되지 않는다."""
    _, p = _enrich_press(PRESS_DETAIL_NEW)
    assert "ㅁ" in p.body and "ㅇ" in p.body
    assert "'26.6월말" in p.body
    assert "※ 자세한 내용은 첨부파일을 참고하시기 바랍니다." in p.body


def test_fss_press_new_markup_still_collects_and_downloads_attachments():
    """본문 selector 를 바꿔도 첨부 수집·다운로드는 그대로다."""
    sc, p = _enrich_press(PRESS_DETAIL_NEW)
    assert [a.filename for a in p.attachments] == ["보도자료_자본비율.hwp"]
    assert p.attachments[0].data == b"%HWP dummy"
    assert sc.fetcher.downloaded == [p.attachments[0].url]


def test_fss_press_legacy_view_cont_still_works():
    """구 홈페이지 구조(.view-cont)도 fallback 으로 그대로 동작한다."""
    legacy = """
    <div id="content"><div class="view-cont">
      <p>금융감독원은 오늘 보도자료를 배포하였다.</p>
    </div></div>"""
    _, p = _enrich_press(legacy, blob=None)
    assert "보도자료를 배포" in p.body


def test_fss_press_empty_n_dbdata_falls_through_to_legacy():
    """신형 컨테이너가 빈 껍데기로만 있어도 본문을 잃지 않는다."""
    mixed = """
    <div id="content">
      <div class="n-dbdata"> </div>
      <div class="view-cont"><p>구 구조에 남아 있는 본문</p></div>
    </div>"""
    _, p = _enrich_press(mixed, blob=None)
    assert "구 구조에 남아 있는 본문" in p.body


def test_fss_press_second_n_dbdata_beats_lower_priority_selector():
    """같은 selector 에 여러 요소가 있으면 전부 훑는다 — 첫 요소가 비었다고 해서
    우선순위가 낮은 selector 로 넘어가면 안 된다(빈 반응형/템플릿 컨테이너 대비)."""
    duplicated = """
    <div id="content">
      <div class="n-dbdata"> </div>

      <div class="n-dbdata">
        ㅁ 실제 금감원 공식 본문
        <br>
        ㅇ 두 번째 n-dbdata의 내용
      </div>

      <div class="view-cont">이 legacy fallback은 선택되면 안 됨</div>
    </div>"""
    _, p = _enrich_press(duplicated, blob=None)
    assert "실제 금감원 공식 본문" in p.body
    assert "두 번째 n-dbdata의 내용" in p.body
    assert "legacy fallback은 선택되면 안 됨" not in p.body


def test_fss_press_legacy_cont_still_works():
    """구 홈페이지의 .cont 만 있는 상세도 그대로 읽는다.

    이번 변경에서 보도자료 목록에서 빼는 것은 넓은 #content 하나뿐이다 — 기존
    production chain 이 지원하던 .cont 까지 함께 빠지면 구형 페이지가 본문을 잃는다.
    """
    legacy_cont = """
    <div id="content"><div class="cont">
      <p>구형 금감원 보도자료 본문</p>
    </div></div>"""
    _, p = _enrich_press(legacy_cont, blob=None)
    assert p.body
    assert "구형 금감원 보도자료 본문" in p.body


def test_fss_press_does_not_fall_back_to_whole_content(caplog):
    """본문 컨테이너가 없으면 #content 를 긁지 않고, 대신 경고를 남긴다."""
    chrome_only = """
    <div id="content">
      <ul class="lnb"><li><a href="/fss/main">금융감독원 홈</a></li></ul>
      <p>페이지 껍데기만 있는 상세</p>
    </div>"""
    with caplog.at_level(logging.WARNING, logger="src.scrapers.fss"):
        _, p = _enrich_press(chrome_only, blob=None)
    assert p.body == ""                     # 메뉴가 본문으로 들어오지 않는다
    assert "상세 본문 selector를 찾지 못함" in caplog.text
    assert "fss_press" in caplog.text
    assert "nttId:225832" in caplog.text
    assert PRESS_VIEW in caplog.text


def test_fss_press_missing_body_does_not_raise():
    """본문을 못 찾아도 예외 없이(=발송을 막지 않고) 넘어간다 — fail-soft 유지."""
    _, p = _enrich_press("<div id='content'></div>", blob=None)
    assert p.body == ""
    assert p.title                          # 나머지 필드는 그대로


def test_other_fss_sources_keep_wide_content_fallback():
    """보도자료가 아닌 게시판은 기존 #content fallback 을 그대로 쓴다."""
    html = """
    <div id="content"><p>행정지도 예고 본문</p></div>"""
    sc = _fss_scraper(
        "fss_admin_guidance",
        "https://www.fss.or.kr/fss/job/admnPrvntc/list.do?menuNo=200491",
        _DetailFetcher(html),
    )
    post = Post(
        source_key="fss_admin_guidance",
        source_name="금감원 · 행정지도 예고",
        post_id="seqno:123",
        title="행정지도 예고",
        url="https://www.fss.or.kr/fss/job/admnPrvntc/view.do?seqno=123",
    )
    sc.enrich(post)
    assert "행정지도 예고 본문" in post.body


def test_other_fss_sources_can_also_read_new_markup():
    """다른 게시판이 같은 개편 구조를 쓰더라도 #content 폴백으로 본문은 살아 있다."""
    sc = _fss_scraper(
        "fss_rule_amendment",
        "https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/list.do?menuNo=200489",
        _DetailFetcher(PRESS_DETAIL_NEW, blob=b"%HWP dummy"),
    )
    post = Post(
        source_key="fss_rule_amendment",
        source_name="금감원 · 세칙 예고",
        post_id="lrgSlno:9",
        title="세칙 예고",
        url="https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/view.do?lrgSlno=9",
    )
    sc.enrich(post)
    assert "손실흡수능력" in post.body


def test_assembly_openapi_envelope_parsing(monkeypatch):
    """열린국회 Open API 표준 봉투에서 row 추출 + 필드 폴백 확인."""
    import os
    from src.scrapers.assembly import AssemblyBillScraper

    src = SourceConfig(
        key="assembly_bill", name="a", type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/",
        extra={"api_service": "svc", "age": "22"},
    )
    monkeypatch.setenv("ASSEMBLY_API_KEY", "dummy")
    sc = AssemblyBillScraper(src, fetcher=None)

    envelope = {
        "svc": [
            {"head": [{"list_total_count": 2}, {"RESULT": {"CODE": "INFO-000"}}]},
            {"row": [
                {"BILL_ID": "PRC_A1", "BILL_NAME": "테스트법률안", "PROPOSE_DT": "2026-07-01", "RST_PROPOSER": "홍길동"},
                {"BILL_ID": "PRC_A2", "BILL_NM": "두번째안", "PPSL_DT": "2026-07-02"},
            ]},
        ]
    }

    class _R:
        def json(self):
            return envelope

    class _F:
        def get(self, *a, **k):
            return _R()
    sc.fetcher = _F()

    posts = sc.fetch_list(30, page=1)
    assert len(posts) == 2
    assert posts[0].post_id == "PRC_A1"
    assert posts[0].title == "테스트법률안 (홍길동)"
    assert posts[0].url.endswith("billId=PRC_A1")
    assert posts[0].date == "2026-07-01"
    assert posts[1].title == "두번째안"          # 제안자 없으면 이름만


def test_assembly_error_envelope_raises(monkeypatch, tmp_path):
    """RESULT 가 에러 코드(인증/쿼터 등)면 [] 로 삼키지 말고 예외를 던져야 한다."""
    import pytest
    from src.scrapers.assembly import AssemblyBillScraper

    src = SourceConfig(
        key="assembly_bill", name="a", type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/",
        extra={"api_service": "svc"},
    )
    monkeypatch.setenv("ASSEMBLY_API_KEY", "dummy")
    monkeypatch.chdir(tmp_path)  # _dump_debug 가 debug/ 를 tmp 에 쓰도록
    sc = AssemblyBillScraper(src, fetcher=None)

    err = {"RESULT": {"CODE": "INFO-300", "MESSAGE": "인증키 사용 제한"}}

    class _R:
        def json(self):
            return err

    class _F:
        def get(self, *a, **k):
            return _R()
    sc.fetcher = _F()

    with pytest.raises(RuntimeError):
        sc.fetch_list(30, page=1)


def test_assembly_no_data_returns_empty(monkeypatch, tmp_path):
    """INFO-200(데이터 없음)은 정상적인 빈 목록(예외 아님)."""
    from src.scrapers.assembly import AssemblyBillScraper

    src = SourceConfig(
        key="assembly_bill", name="a", type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/", extra={"api_service": "svc"},
    )
    monkeypatch.setenv("ASSEMBLY_API_KEY", "dummy")
    monkeypatch.chdir(tmp_path)
    sc = AssemblyBillScraper(src, fetcher=None)

    class _R:
        def json(self):
            return {"RESULT": {"CODE": "INFO-200", "MESSAGE": "데이터 없음"}}

    class _F:
        def get(self, *a, **k):
            return _R()
    sc.fetcher = _F()

    assert sc.fetch_list(30, page=1) == []
