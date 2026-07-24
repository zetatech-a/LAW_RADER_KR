"""라이브 HTML 구조를 압축한 파서 회귀 테스트.

실제 사이트(2026-07 캡처) 마크업의 핵심 패턴을 최소 HTML 로 재현한다.
"""
import os
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.scrapers.fsc import FscBoardScraper
from src.scrapers.fss import FssBoardScraper


def _fsc(list_url, html):
    sc = FscBoardScraper(SourceConfig(key="k", name="k", type="x", list_url=list_url), fetcher=None)
    return sc._parse_list(BeautifulSoup(html, "lxml"))


def _fss(list_url, html):
    sc = FssBoardScraper(SourceConfig(key="k", name="k", type="x", list_url=list_url), fetcher=None)
    return sc._parse_list(BeautifulSoup(html, "lxml"))


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
