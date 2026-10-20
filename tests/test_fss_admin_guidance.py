"""금감원 행정지도 예고(fss_admin_guidance) — 원문 4개 항목 추출·메일 렌더·AI 제외.

실제 상세 fixture 는 tests/fixtures/fss_admin_guidance/live/seqno_280.html 이다(README 참고).
세칙 예고(dt/dd 짝)와 달리 div.n-dbdata 한 요소 안에 1~6번이 글자와 <br> 로만 이어진다.
합성 조각은 그 구조를 그대로 흉내 내 변형(표기·누락·하위 목록)을 확인한다.
"""
import logging
import os
import re
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import AssemblyBatchConfig, LLMConfig, SourceConfig
from src.models import (
    FSS_ADMIN_GUIDANCE_SECTION_TITLES,
    FSS_ADMIN_GUIDANCE_SOURCE_KEY,
    FSS_RULE_SECTION_TITLES,
    FSS_RULE_SOURCE_KEY,
    Attachment,
    Post,
)
from src.notifier import _accent, _card, build_html, build_text
from src.scrapers.fss import FssBoardScraper
from src.scrapers.fss_admin_guidance import extract_admin_guidance_sections, is_complete
from src.summarizer import Summarizer, ai_target_count

PURPOSE, CONTENT, DEADLINE, CONTACT = FSS_ADMIN_GUIDANCE_SECTION_TITLES
KEY = FSS_ADMIN_GUIDANCE_SOURCE_KEY
FIXTURES = Path(__file__).parent / "fixtures" / "fss_admin_guidance"
LIVE_280 = FIXTURES / "live" / "seqno_280.html"
LIST_URL = "https://www.fss.or.kr/fss/job/admnPrvntc/list.do?menuNo=200491"
VIEW_280 = "https://www.fss.or.kr/fss/job/admnPrvntc/view.do?seqno=280&menuNo=200491&pageIndex=1"
SOURCE_NAME = "금감원 · 행정지도 예고"
TITLE_280 = "「온투업권 스탁론 리스크 관리 방안」 연장 예고"
ATTACH_280 = "(261020) 사전예고_온투업 스탁론 리스크 관리 방안_F.pdf"

# 실제 seqno=280 원문(6번의 담당자 성명·직통·팩스·이메일만 fixture 에서 가짜 값으로 치환).
LIVE_280_EXPECTED = [
    (PURPOSE, "- 동 행정지도의 존속기한이 만료('26.12.31.)됨에 따라 이를 1년간 추가 연장"
              "('27.1.1.~'27.12.31.)"),
    (CONTENT, "- 첨부파일 참고"),
    (DEADLINE, "- 2026-11-09"),
    (CONTACT, "- 전자금융감독국 전자금융총괄팀 홍길동 조사역(전화 :02-0000-0001, "
              "FAX : 02-0000-0009, E-mail : contact1@example.com)"),
]

SYNTH_EXPECTED = [
    (PURPOSE, "- 취지 원문"),
    (CONTENT, "- 첨부파일 참고"),
    (DEADLINE, "- 2026-11-09"),
    (CONTACT, "- 예시국 예시팀 홍길동 (02-0000-0000)"),
]


def _live() -> str:
    return LIVE_280.read_text(encoding="utf-8")


def _extract(html: str, **kw):
    return extract_admin_guidance_sections(BeautifulSoup(html, "lxml"), **kw)


def _squash(s: str) -> str:
    return "".join(s.split())


def _page(dbdata: str) -> str:
    """실제 seqno=280 페이지의 뼈대: 안내문·제목·첨부 상자·n-dbdata·목록/의견제출 버튼·꼬리말."""
    return (
        "<html><body><nav><a href='#'>행정지도 예고</a></nav><div class='contents'>"
        "<div class='info-cmt'><p>금융감독원은 시행·변경·연장 예정인 행정지도 사항을 미리 알리고 "
        "이에 대한 의견을 제출 받고 있습니다.</p></div>"
        "<div class='krds-bd-view'><div class='sub-info'>"
        "<h3 class='subject'>「예시 행정지도」 연장 예고</h3></div>"
        "<dl class='attach-file-box'><dt>첨부파일</dt><dd><ul class='attach-wrap'><li>"
        "<div class='attach-box'><a href='/fss.hpdownload?path=/law/bgn/&amp;file=a.pdf' "
        "class='btn-attach-download'><span class='name'>예시 행정지도.pdf</span></a>"
        "<div class='right-btn'><a href='/fss.hpdownload?path=/law/bgn/&amp;file=a.pdf'>"
        "다운로드</a></div></div></li></ul></dd></dl>"
        f"<div class='n-dbdata'>\r\n\t\t{dbdata}\r\n\t</div></div>"
        "<div class='page-btn-wrap'><a href='./list.do'>목록</a>"
        "<a href='/fss/job/admnPrvntc/forInsert.do?seqno=1'>행정지도에 대한 의견제출</a></div>"
        "</div><footer>금융감독원 대표전화 1332</footer></body></html>"
    )


def _guidance_body(**over) -> str:
    """실제와 같은 '번호. 제목<br> - 값<br><br>' 의 이어진 원문. None 이면 그 항목을 뺀다."""
    parts = {
        "1": "1. 행정지도의 명칭\r\n\t\t<br>\r\n\t\t - 「예시 행정지도」",
        "2": "2. 행정지도의 신규·변경·연장의 취지\r\n\t\t<br>\r\n\t\t - 취지 원문",
        "3": "3. 행정지도의 내용\r\n\t\t<br>\r\n\t\t - 첨부파일 참고",
        "4": "4. 기타 필요사항\r\n\t\t<br>\r\n\t\t - 없음",
        "5": "5. 의견제출시한(도착기준)\r\n\t\t<br>\r\n\t\t - 2026-11-09",
        "6": "6. 접수부서, 담당자, 전화번호, 팩스번호\r\n\t\t<br>\r\n\t\t - 예시국 예시팀 홍길동 (02-0000-0000)",
    }
    parts.update(over)
    return "\r\n\t\t<br><br>\r\n\t\t".join(v for v in parts.values() if v is not None) + "\r\n\t\t<br>"


# ── 1. 실제 응답(seqno=280) ────────────────────────────────────────────────────


def test_live_280_extracts_four_sections_exactly():
    result = _extract(_live())
    assert result.missing == [], result.reason or result.missing
    assert result.sections == LIVE_280_EXPECTED
    assert result.reason == ""


def test_live_280_excludes_sections_1_and_4_and_numbers():
    joined = "\n".join(v for _, v in _extract(_live()).sections)
    assert "행정지도의 명칭" not in joined and "「온투업권 스탁론 리스크 관리 방안」" not in joined  # 1번
    assert "기타 필요사항" not in joined and "없음" not in joined                                # 4번
    for title, value in _extract(_live()).sections:
        assert value[:2] not in ("1.", "2.", "3.", "4.", "5.", "6."), (title, value)
    assert [t for t, _ in _extract(_live()).sections] == list(FSS_ADMIN_GUIDANCE_SECTION_TITLES)


def test_live_280_page_chrome_and_attachments_do_not_leak():
    """제목·첨부 상자·안내문·목록/의견제출 버튼·담당부서 안내는 값에 섞이지 않는다."""
    joined = "\n".join(v for _, v in _extract(_live()).sections)
    for noise in (TITLE_280, "사전예고_온투업", ".pdf", "다운로드", "목록", "행정지도에 대한 의견제출",
                  "미리 알리고", "의견접수부서 앞으로", "정보관리 담당부서", "법무국", "02-3145-5928"):
        assert noise not in joined, noise


def test_live_280_values_are_verbatim():
    """요약·의역이 없다: 추출한 모든 줄이 페이지 원문 글자에 그대로 있다."""
    page_text = _squash(BeautifulSoup(_live(), "lxml").get_text())
    for _, value in _extract(_live()).sections:
        for line in value.split("\n"):
            assert _squash(line) in page_text, line


def test_live_280_old_generic_selectors_matched_nothing():
    """기존 누락 원인: 개편 페이지는 div.contents 로 감싸여 공통 selector
    (.view-cont/.board-view/.bbs-view/.cont/#content)가 하나도 맞지 않아 본문이 비었다."""
    from src.scrapers.fss import _BODY_SELECTORS, _body_text

    soup = BeautifulSoup(_live(), "lxml")
    assert all(not soup.select(sel) for sel in _BODY_SELECTORS)
    assert _body_text(soup, _BODY_SELECTORS) == ""
    assert soup.select_one("div.contents .krds-bd-view > .n-dbdata") is not None


# ── 2. 원문 추출(합성 n-dbdata) ────────────────────────────────────────────────


def test_br_text_layout_extracts_four_sections_in_display_order():
    result = _extract(_page(_guidance_body()))
    assert result.sections == SYNTH_EXPECTED
    assert result.missing == []


def test_entities_and_whitespace_are_decoded_and_normalized():
    """HTML entity 는 글자로 풀고(이스케이프는 메일 렌더 단계), 줄 안의 공백·탭은 하나로."""
    html = _page(_guidance_body(**{
        "2": "2. 행정지도의&nbsp;신규·변경·연장의 취지<br>"
             " -&nbsp;&nbsp;만료(&#39;26.12.31.) &amp; 연장 &lt;안&gt;\t\t&quot;1년&quot;",
    }))
    assert dict(_extract(html).sections)[PURPOSE] == "- 만료('26.12.31.) & 연장 <안> \"1년\""


def test_multiline_values_and_paragraph_breaks_are_preserved():
    html = _page(_guidance_body(**{
        "6": "6. 접수부서, 담당자, 전화번호, 팩스번호<br> - 가국 가팀 홍길동(전화 : 02-0000-0001)"
             "<br> - 나국 나팀 김예시(전화 : 02-0000-0002)<br><br><br> * 팩스 : 02-0000-0009",
    }))
    assert dict(_extract(html).sections)[CONTACT] == (
        "- 가국 가팀 홍길동(전화 : 02-0000-0001)\n"
        "- 나국 나팀 김예시(전화 : 02-0000-0002)\n"
        "\n"
        "* 팩스 : 02-0000-0009"
    )


def test_sub_lists_in_section_3_do_not_split_it():
    """3번 하위 번호(1. 2. …, 가. 나.)와 최상위 제목 문구를 다시 쓰는 하위 항목은 경계가 아니다."""
    content = (
        "3. 행정지도의 내용<br> - 주요 내용<br>  1. 적용 대상 : 온투업자<br>"
        "  2. 리스크 관리 기준<br>   가. 한도 관리<br>   나. 담보 관리<br>"
        "  3. 행정지도의 내용 변경 없음<br>  4. 기타 필요사항은 별도 안내<br>"
        "  5. 의견제출기한 단축 사유 명확화<br>  6. 접수부서 변경 안내<br>"
        "  1. 행정지도의 명칭<br>  2. 행정지도의 신규·변경·연장의 취지 : 이전 문구"
    )
    got = dict(_extract(_page(_guidance_body(**{"3": content}))).sections)
    assert got[CONTENT] == (
        "- 주요 내용\n1. 적용 대상 : 온투업자\n2. 리스크 관리 기준\n가. 한도 관리\n나. 담보 관리\n"
        "3. 행정지도의 내용 변경 없음\n4. 기타 필요사항은 별도 안내\n"
        "5. 의견제출기한 단축 사유 명확화\n6. 접수부서 변경 안내\n"
        "1. 행정지도의 명칭\n2. 행정지도의 신규·변경·연장의 취지 : 이전 문구"
    )
    assert got[PURPOSE] == "- 취지 원문"
    assert got[DEADLINE] == "- 2026-11-09"
    assert got[CONTACT] == "- 예시국 예시팀 홍길동 (02-0000-0000)"


def test_ol_sub_list_keeps_browser_numbers():
    html = _page(_guidance_body(**{
        "3": "3. 행정지도의 내용<ol><li>대상 확대</li><li>기간 연장</li></ol>",
    }))
    assert dict(_extract(html).sections)[CONTENT] == "1. 대상 확대\n2. 기간 연장"


@pytest.mark.parametrize(
    "purpose_heading, content_heading",
    [
        ("2. 행정지도의 신규·변경·연장의 취지", "3. 행정지도의 내용"),
        ("2. 행정지도의 신규ㆍ변경ㆍ연장의 취지", "3. 행정지도의 내용"),
        ("2. 행정지도의 신규∙변경∙연장의 취지", "3. 행정지도의 내용"),
        ("2. 행정지도의 신규・변경・연장의 취지", "3. 행정지도의 내용"),
        ("2. 행정지도의 신규, 변경, 연장의 취지", "3. 행정지도의 내용"),
        ("2. 행정지도의 신규·변경·연장 취지", "3. 행정지도 내용"),
        ("2. 행정지도의 연장의 취지", "3. 행정지도의 주요 내용"),
        ("2.행정지도의  신규 · 변경 · 연장의  취지", "3)  행정 지도의 내용"),
        ("2. 행정지도의 신규·변경·\n연장의 취지", "3. 행정지도의\n내용"),     # 원문 개행으로 갈라진 제목
        ("2. 행정지도의<br>신규·변경·연장의 취지", "3. 행정지도의<br>내용"),  # <br> 로 갈라진 제목
        ("② 행정지도의 신규·변경·연장의 취지", "③ 행정지도의 내용"),
    ],
)
def test_heading_notation_and_whitespace_variants(purpose_heading, content_heading):
    html = _page(_guidance_body(**{
        "2": f"{purpose_heading}<br> - 취지 원문",
        "3": f"{content_heading}<br> - 1. 첫째<br> - 2. 둘째",
    }))
    got = dict(_extract(html).sections)
    assert got[PURPOSE] == "- 취지 원문"
    assert got[CONTENT] == "- 1. 첫째\n- 2. 둘째"
    assert set(got) == set(FSS_ADMIN_GUIDANCE_SECTION_TITLES)


@pytest.mark.parametrize(
    "deadline_heading",
    ["5. 의견제출시한(도착기준) :", "5. 의견 제출 시한 ( 도착 기준 ) ：", "5. 의견제출기한", "5 의견제출시한(도착기준)"],
)
def test_deadline_heading_variants_with_value_on_same_line(deadline_heading):
    html = _page(_guidance_body(**{"5": f"{deadline_heading} 2026-11-09"}))
    assert dict(_extract(html).sections)[DEADLINE] == "2026-11-09"


@pytest.mark.parametrize("missing_item, missing_title", [
    ("2", PURPOSE), ("3", CONTENT), ("5", DEADLINE), ("6", CONTACT),
])
def test_missing_item_is_reported_and_others_are_not_contaminated(missing_item, missing_title):
    result = _extract(_page(_guidance_body(**{missing_item: None})))
    assert result.missing == [missing_title]
    assert result.sections == [(t, v) for t, v in SYNTH_EXPECTED if t != missing_title]


@pytest.mark.parametrize("boundary_item", ["1", "4"])
def test_missing_boundary_items_do_not_change_neighbours(boundary_item):
    result = _extract(_page(_guidance_body(**{boundary_item: None})))
    assert result.sections == SYNTH_EXPECTED and result.missing == []


@pytest.mark.parametrize("orphan_item, orphan_value, missing_title", [
    ("2", " - 취지 원문", PURPOSE),
    ("5", " - 2026-11-09", DEADLINE),
])
def test_orphan_value_without_heading_goes_to_the_hidden_boundary_item(
    orphan_item, orphan_value, missing_title
):
    """제목이 빠지고 값만 남으면 그 값은 앞의 경계 항목(1·4번, 미표시)에 붙는다 — 표시 항목에
    다른 항목의 내용이 섞이지 않고, 빠진 항목은 누락으로 드러난다."""
    result = _extract(_page(_guidance_body(**{orphan_item: orphan_value})))
    assert result.missing == [missing_title]
    assert result.sections == [(t, v) for t, v in SYNTH_EXPECTED if t != missing_title]


def test_empty_item_counts_as_missing():
    result = _extract(_page(_guidance_body(**{"2": "2. 행정지도의 신규·변경·연장의 취지"})))
    assert result.missing == [PURPOSE]
    assert dict(result.sections)[CONTENT] == "- 첨부파일 참고"


def test_structure_change_without_titles_is_a_total_failure():
    result = _extract(_page("본 게시물은 JavaScript 를 사용해야 볼 수 있습니다."))
    assert result.sections == []
    assert result.missing == list(FSS_ADMIN_GUIDANCE_SECTION_TITLES)
    assert "찾지 못함" in result.reason


def test_titles_only_without_values_is_a_total_failure():
    body = "<br>".join(["1. 행정지도의 명칭", "2. 행정지도의 신규·변경·연장의 취지", "3. 행정지도의 내용",
                        "4. 기타 필요사항", "5. 의견제출시한(도착기준)",
                        "6. 접수부서, 담당자, 전화번호, 팩스번호"])
    result = _extract(_page(body))
    assert result.sections == []
    assert "비어 있음" in result.reason


def _live_titles_only() -> str:
    """실제 seqno=280 에서 값 줄(' - …')만 모두 비운 페이지."""
    html, n = re.subn(r"(\n\t\t) - [^\n]*", r"\1", _live())
    assert n == 6
    return html


def test_live_titles_only_does_not_pick_up_buttons_or_page_notice():
    """값이 모두 빈 실제 구조: 6번 값으로 '목록 행정지도에 대한 의견제출'·'정보관리 담당부서
    안내' 같은 페이지 문구를 싣지 않고 전체 실패로 드러낸다."""
    result = _extract(_live_titles_only())
    assert result.sections == []
    assert result.missing == list(FSS_ADMIN_GUIDANCE_SECTION_TITLES)
    assert "비어 있음" in result.reason


def test_last_section_stops_at_attachment_names():
    html = _page(_guidance_body() + "<br>예시 행정지도.pdf<br>목록")
    got = dict(_extract(html, stop_lines=("예시 행정지도.pdf",)).sections)
    assert got[CONTACT] == "- 예시국 예시팀 홍길동 (02-0000-0000)"


def test_rule_amendment_headings_are_not_admin_guidance_headings():
    """세칙 예고의 2·3번 제목(규정의 제·개정 …)은 행정지도 예고의 제목이 아니다."""
    html = _page(_guidance_body(**{"2": "2. 규정의 제·개정 취지<br> - 취지",
                                   "3": "3. 규정의 제·개정 내용<br> - 내용"}))
    result = _extract(html)
    assert result.missing == [PURPOSE, CONTENT]


def test_is_complete_requires_all_four_values():
    assert is_complete(LIVE_280_EXPECTED)
    assert not is_complete(LIVE_280_EXPECTED[:3])
    assert not is_complete([(t, "") for t in FSS_ADMIN_GUIDANCE_SECTION_TITLES])
    assert not is_complete(list(zip(FSS_RULE_SECTION_TITLES, ["a", "b", "c", "d"])))


# ── 3. 스크래퍼 연동 ───────────────────────────────────────────────────────────


class _Fetcher:
    def __init__(self, html="", fail=False):
        self.html = html
        self.fail = fail
        self.downloaded = []

    def get(self, url, referer=None):
        if self.fail:
            raise ConnectionError("상세 503")
        return object()

    def text(self, resp):
        return self.html

    def download(self, url, referer=None):
        self.downloaded.append(url)
        return b"%PDF dummy"


def _scraper(html="", key=KEY, fail=False):
    src = SourceConfig(key=key, name=SOURCE_NAME, type="fss_board", list_url=LIST_URL)
    return FssBoardScraper(src, fetcher=_Fetcher(html, fail=fail))


def _post(key=KEY, **over):
    kw = dict(source_key=key, source_name=SOURCE_NAME, post_id="seqno:280", title=TITLE_280,
              url=VIEW_280, date="2026-10-20")
    kw.update(over)
    return Post(**kw)


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """실패 시 debug/ 덤프가 저장소가 아니라 임시 디렉터리에 남게 한다."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_enrich_live_280_fills_details_only_and_keeps_attachments(in_tmp):
    sc = _scraper(_live())
    p = _post()
    sc.enrich(p)
    assert p.details == LIVE_280_EXPECTED
    assert p.body == ""                       # 일반 본문·요약 경로를 타지 않는다
    assert [a.filename for a in p.attachments] == [ATTACH_280]
    assert p.attachments[0].url.startswith("https://www.fss.or.kr/fss.hpdownload?path=/law/bgn/&file=")
    assert all(a.data == b"%PDF dummy" for a in p.attachments)
    assert sc.enrich_succeeded(p) is True
    assert not (in_tmp / "debug").exists()    # 성공하면 덤프하지 않는다


def test_partial_extraction_logs_missing_items_and_counts_as_failure(in_tmp, caplog):
    sc = _scraper(_live().replace("5. 의견제출시한(도착기준)", "5. 회신 기한"))
    p = _post()
    with caplog.at_level(logging.WARNING):
        sc.enrich(p)
    assert [t for t, _ in p.details] == [PURPOSE, CONTENT, CONTACT]
    assert p.body == ""
    assert sc.enrich_succeeded(p) is False
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "행정지도 예고 원문 항목 일부 누락" in msg and DEADLINE in msg and VIEW_280 in msg
    assert (in_tmp / "debug" / "fss_admin_guidance_detail_seqno_280.txt").exists()


def test_total_failure_logs_reason_and_url_without_body_fallback(in_tmp, caplog):
    """구조가 바뀌어도 일반 본문으로 되돌아가지 않는다(= AI 요약 경로로 새지 않는다)."""
    changed = _live()
    for old, new in (("행정지도의", "지도의"), ("의견제출시한", "회신기한"), ("접수부서", "부서"),
                     ("기타 필요", "그밖")):
        changed = changed.replace(old, new)            # 6개 항목 제목이 모두 바뀐 페이지
    sc = _scraper(changed)
    p = _post()
    with caplog.at_level(logging.WARNING):
        sc.enrich(p)
    assert p.details == [] and p.body == ""
    assert [a.filename for a in p.attachments] == [ATTACH_280]   # 첨부는 그대로 유지
    assert sc.enrich_succeeded(p) is False                       # 첨부만으로 성공 처리하지 않는다
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "행정지도 예고 원문 항목 추출 실패" in msg and "찾지 못함" in msg and VIEW_280 in msg
    assert (in_tmp / "debug" / "fss_admin_guidance_detail_seqno_280.txt").exists()


def test_http_failure_is_logged_with_url_and_not_counted_as_success(in_tmp, caplog):
    sc = _scraper(fail=True)
    p = _post()
    with caplog.at_level(logging.WARNING):
        sc.enrich(p)
    assert p.details == [] and p.body == ""
    assert sc.enrich_succeeded(p) is False
    assert any(VIEW_280 in r.getMessage() and "상세 로드 실패" in r.getMessage()
               for r in caplog.records)


def test_rule_amendment_board_still_uses_its_own_titles(in_tmp):
    """같은 실제 행정지도 HTML 이라도 세칙 예고 소스에는 세칙 제목표만 쓰인다(소스별 분기)."""
    sc = _scraper(_live(), key=FSS_RULE_SOURCE_KEY)
    p = _post(key=FSS_RULE_SOURCE_KEY, source_name="금감원 · 세칙 제·개정 예고")
    sc.enrich(p)
    assert {t for t, _ in p.details} <= set(FSS_RULE_SECTION_TITLES)
    assert sc.enrich_succeeded(p) is False


# ── 4. AI 요약 제외 ─────────────────────────────────────────────────────────────


def _llm(**over) -> LLMConfig:
    base = dict(enabled=True, model="m", lines=3, max_line_chars=90, min_body_chars=10,
                max_input_chars=8000, max_posts=10, rpm=0, timeout_sec=5, max_retries=0,
                retry_backoff_sec=0, api_key="k", assembly_batch=AssemblyBatchConfig())
    base.update(over)
    return LLMConfig(**base)


def _no_gemini(summarizer):
    summarizer._generate = lambda *a, **k: pytest.fail("행정지도 예고는 Gemini 를 호출하면 안 된다")
    return summarizer


@pytest.mark.parametrize("details", [LIVE_280_EXPECTED, LIVE_280_EXPECTED[:2], []],
                         ids=["complete", "partial", "failed"])
def test_admin_guidance_posts_never_reach_gemini(details):
    p = _post(details=list(details))
    s = _no_gemini(Summarizer(_llm()))
    assert s.summarize_all({SOURCE_NAME: [p]}) == 0
    assert p.summary == []
    assert ai_target_count(_llm(), {SOURCE_NAME: [p]}) == 0


def test_admin_guidance_post_is_excluded_even_if_a_body_were_present():
    """어떤 경로로 body 가 채워져도(추출 실패 우회 등) AI 로 새지 않는다."""
    p = _post(body="행정지도의 신규·변경·연장의 취지 원문이 아주 길게 이어지는 본문 " * 10)
    s = _no_gemini(Summarizer(_llm()))
    assert s.summarize_all({SOURCE_NAME: [p]}) == 0
    assert s.summarize(p) == []
    assert ai_target_count(_llm(), {SOURCE_NAME: [p]}) == 0


def test_other_sources_are_still_summarized_alongside_admin_guidance_posts():
    guidance = _post(body="행정지도 예고 본문 " * 20)
    press = Post(source_key="fss_press", source_name="금감원 · 보도자료", post_id="nttId:1",
                 title="보도자료", url="https://www.fss.or.kr/x", body="금융감독원 보도자료 본문 " * 20)
    calls = []
    s = Summarizer(_llm())
    s._generate = lambda prompt, deadline=None: calls.append(prompt) or {"ok": 1}
    s._parse = lambda data: ["요약 1", "요약 2", "요약 3"]
    grouped = {SOURCE_NAME: [guidance], "금감원 · 보도자료": [press]}
    assert ai_target_count(_llm(), grouped) == 1
    assert s.summarize_all(grouped) == 1
    assert len(calls) == 1 and "보도자료" in calls[0]
    assert press.summary == ["요약 1", "요약 2", "요약 3"] and guidance.summary == []


def _run_pipeline(tmp_path, monkeypatch, caplog, html):
    """main.run 전체 경로: 실제 FSS 스크래퍼 enrich → 요약 단계 → 발송 직전 메일 내용.

    SMTP·state 는 임시 대역, Gemini 는 호출되면 실패하도록 막는다.
    """
    from src import main as main_mod
    from src.scrapers.base import CollectResult
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    st.mark_seen(KEY, ["seqno:279"], baselined=True)
    st.save()
    monkeypatch.chdir(tmp_path)
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"

    def _build(src, fetcher):
        sc = FssBoardScraper(src, fetcher=_Fetcher(html))
        sc.collect = lambda limit, seen, pages: CollectResult(
            posts=[_post(source_name=src.name)], reached_boundary=True, scanned=2)
        return sc

    sent = []
    monkeypatch.setenv("SMTP_USER", "s@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main_mod, "verify_smtp_login", lambda cfg: None)
    monkeypatch.setattr(main_mod, "build_scraper", _build)
    monkeypatch.setattr(main_mod, "send_digest", lambda cfg, posts: sent.append(posts))
    monkeypatch.setattr(Summarizer, "_generate",
                        lambda *a, **k: pytest.fail("Gemini 가 호출되었다"))

    with caplog.at_level(logging.INFO):
        rc = main_mod.run(["--config", str(cfg_path), "--state", str(state_path),
                           "--only", KEY])
    return rc, sent, State(state_path).seen_ids(KEY)


def test_run_pipeline_mails_admin_guidance_sections_without_calling_gemini(
    tmp_path, monkeypatch, caplog
):
    rc, sent, seen = _run_pipeline(tmp_path, monkeypatch, caplog, _live())
    assert rc == 0 and len(sent) == 1
    (post,) = next(iter(sent[0].values()))
    assert post.details == LIVE_280_EXPECTED and post.summary == []
    assert "AI 요약 집계 — 대상 0건 / 요약 0건 / 발췌 폴백 0건" in caplog.text
    assert "상세 수집 집계(전체) — 시도 1건 / 성공 1건 / 등록대기 0건 / 실패 0건" in caplog.text
    html = build_html(sent[0])
    assert html.count("class='lr-rule-title'") == 4
    assert "seqno:280" in seen                     # 기존 정책 그대로: 발송 성공 후 seen


def test_run_pipeline_counts_attachment_only_detail_as_failure(tmp_path, monkeypatch, caplog):
    rc, sent, seen = _run_pipeline(tmp_path, monkeypatch, caplog,
                                   _live().replace("행정지도의", "지도의"))
    assert rc == 0 and len(sent) == 1
    (post,) = next(iter(sent[0].values()))
    assert post.attachments and [t for t, _ in post.details] == [DEADLINE, CONTACT]
    assert "AI 요약 집계 — 대상 0건 / 요약 0건 / 발췌 폴백 0건" in caplog.text
    assert "상세 수집 집계(전체) — 시도 1건 / 성공 0건 / 등록대기 0건 / 실패 1건" in caplog.text
    assert "seqno:280" in seen


# ── 5. 메일 렌더 ───────────────────────────────────────────────────────────────

_ATTACH = Attachment(ATTACH_280, "https://www.fss.or.kr/fss.hpdownload?path=/law/bgn/&file=a.pdf",
                     data=b"x")


def _render(details):
    p = _post(details=list(details), attachments=[_ATTACH])
    grouped = {SOURCE_NAME: [p]}
    return p, build_html(grouped), build_text(grouped)


def test_html_shows_four_unnumbered_titles_in_fss_theme_color():
    _, html, _ = _render(LIVE_280_EXPECTED)
    accent = _accent(SOURCE_NAME)
    assert accent == "#0f766e"                       # 기존 금감원 강조색을 그대로 쓴다
    assert html.count("class='lr-rule-title'") == 4
    for title in FSS_ADMIN_GUIDANCE_SECTION_TITLES:
        assert f"<span style='padding-right:6px'>•</span>{title}</div>" in html
    titles = html.split("class='lr-rule-title'")[1:]
    assert all(f"color:{accent}" in t.split(">", 1)[0] and "font-weight:700" in t.split(">", 1)[0]
               and "font-size:13px" in t.split(">", 1)[0] for t in titles)
    for numbered in ("2. 행정지도", "3. 행정지도", "5. 의견", "6. 접수", "1. 행정지도", "4. 기타",
                     "행정지도의 명칭", "기타 필요사항"):
        assert numbered not in html


def test_html_wraps_each_section_in_the_same_light_box_as_rule_amendment():
    _, html, _ = _render(LIVE_280_EXPECTED)
    boxes = html.split("class='lr-rule-body' style='")[1:]
    assert len(boxes) == 4
    for box in boxes:
        style = box.split("'", 1)[0]
        assert "background:#f8fafc" in style and "border:1px solid #e2e8f0" in style
        assert "border-radius:6px" in style and "padding:10px 12px" in style
        assert "color:#334155" in style and "font-size:13px" in style


def test_html_keeps_line_breaks_and_escapes_special_characters():
    _, html, _ = _render([
        (PURPOSE, LIVE_280_EXPECTED[0][1]),
        (CONTENT, "- 1. 대상 <온투업자> & 연계\n- 2. 기간 연장"),
        *LIVE_280_EXPECTED[2:],
    ])
    assert "만료(&#x27;26.12.31.)됨에 따라 이를 1년간 추가 연장(&#x27;27.1.1.~&#x27;27.12.31.)" in html
    assert "- 1. 대상 &lt;온투업자&gt; &amp; 연계<br>- 2. 기간 연장" in html
    assert "<온투업자>" not in html


def test_html_escapes_markup_injected_through_the_source():
    _, html, _ = _render([(PURPOSE, "<script>alert(1)</script> & <b>굵게</b>"),
                          *LIVE_280_EXPECTED[1:]])
    assert "<script>" not in html and "<b>굵게" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;굵게&lt;/b&gt;" in html


def test_card_keeps_title_date_attachments_and_original_link():
    p, html, _ = _render(LIVE_280_EXPECTED)
    assert f">{p.title}</a>" in html
    assert ">2026-10-20</div>" in html
    assert f"href='{_ATTACH.url.replace('&', '&amp;')}'" in html and f"📎 {ATTACH_280}" in html
    assert html.count(f"href='{p.url.replace('&', '&amp;')}'") == 2      # 제목 + 원문 보기
    assert "원문 보기 &rsaquo;" in html


def test_admin_guidance_only_digest_has_no_ai_notice():
    _, html, text = _render(LIVE_280_EXPECTED)
    assert "AI 요약은 생성형 AI" not in html and "AI 요약은 생성형 AI" not in text


def test_text_part_carries_the_same_original_content():
    p, _, text = _render(LIVE_280_EXPECTED)
    lines = text.splitlines()
    start = lines.index(f"  - {p.title}  {p.date}")
    assert lines[start + 1:start + 11] == [
        f"    • {PURPOSE}",
        "      - 동 행정지도의 존속기한이 만료('26.12.31.)됨에 따라 이를 1년간 추가 연장"
        "('27.1.1.~'27.12.31.)",
        f"    • {CONTENT}",
        "      - 첨부파일 참고",
        f"    • {DEADLINE}",
        "      - 2026-11-09",
        f"    • {CONTACT}",
        "      - 전자금융감독국 전자금융총괄팀 홍길동 조사역(전화 :02-0000-0001, FAX : 02-0000-0009, "
        "E-mail : contact1@example.com)",
        f"    {p.url}",
        f"      첨부: {_ATTACH.filename} ({_ATTACH.url})",
    ]


def test_text_part_keeps_multiline_values():
    _, _, text = _render([LIVE_280_EXPECTED[0], (CONTENT, "- 가\n\n- 나"), *LIVE_280_EXPECTED[2:]])
    lines = text.splitlines()
    i = lines.index(f"    • {CONTENT}")
    assert lines[i + 1:i + 4] == ["      - 가", "", "      - 나"]


def test_partial_extraction_marks_missing_sections_in_both_parts():
    details = [LIVE_280_EXPECTED[0], LIVE_280_EXPECTED[1], LIVE_280_EXPECTED[3]]
    _, html, text = _render(details)
    assert html.count("class='lr-rule-title'") == 4          # 4개 제목은 모두 자리를 지킨다
    assert html.count("class='lr-rule-body'") == 3
    missing_block = html.split(f"•</span>{DEADLINE}</div>", 1)[1].split("</div>", 1)[0]
    assert "lr-rule-missing" in missing_block and "추출하지 못했습니다" in missing_block
    i = text.splitlines().index(f"    • {DEADLINE}")
    assert "추출하지 못했습니다" in text.splitlines()[i + 1]


def test_total_failure_shows_notice_and_keeps_title_attachments_link():
    p, html, text = _render([])
    notice = "상세 내용 추출 실패 — 원문 페이지에서 확인해 주세요."
    assert notice in html and f"    {notice}" in text.splitlines()
    assert "lr-rule-title" not in html
    assert f">{p.title}</a>" in html and f"📎 {ATTACH_280}" in html
    assert "원문 보기 &rsaquo;" in html
    assert p.url in text and _ATTACH.url in text


def test_other_cards_render_identically_when_an_admin_guidance_post_is_in_the_digest():
    sanction = Post("fss_sanction", "금감원 · 검사결과 제재", "exam:1_1", "쿠팡페이(주)",
                    "https://www.fss.or.kr/s", date="2026-07-23",
                    details=[("금융기관명", "쿠팡페이(주)"), ("제재조치일", "2026-07-23"),
                             ("관련부서", "전자금융검사국")])
    press = Post("fss_press", "금감원 · 보도자료", "nttId:1", "보도", "https://www.fss.or.kr/p",
                 summary=["a", "b", "c"])
    rule = Post(FSS_RULE_SOURCE_KEY, "금감원 · 세칙 제·개정 예고", "lrgSlno:1", "세칙 예고",
                "https://www.fss.or.kr/r", details=list(zip(FSS_RULE_SECTION_TITLES, "abcd")))
    guidance = _post(details=list(LIVE_280_EXPECTED))
    alone = build_html({sanction.source_name: [sanction], rule.source_name: [rule],
                        press.source_name: [press]})
    mixed = build_html({sanction.source_name: [sanction], rule.source_name: [rule],
                        SOURCE_NAME: [guidance], press.source_name: [press]})
    for p in (sanction, rule, press):
        card = _card(p, _accent(p.source_name))
        assert card in alone and card in mixed
    rule_card = _card(rule, _accent(rule.source_name))
    for title in FSS_RULE_SECTION_TITLES:            # 세칙 예고 카드는 세칙 제목 그대로
        assert f"•</span>{title}</div>" in rule_card
    assert mixed.count("<tr class='lr-detail'>") == 3  # 제재 표 렌더 그대로


# ── 6. 단건 미리보기 스크립트 ──────────────────────────────────────────────────


def _preview():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import preview_fss_rule

    return preview_fss_rule


def test_preview_script_renders_live_280_offline(in_tmp, capsys):
    """로컬 HTML 로만 동작: 메일 발송·state·Gemini·네트워크 없음."""
    rc = _preview().main(["--source", KEY, "--html-file", str(LIVE_280), "--out-dir", "out",
                          "--date", "2026-10-20"])
    assert rc == 0
    out = capsys.readouterr().out
    for title, value in LIVE_280_EXPECTED:
        assert f"• {title}" in out and value in out
    assert f"[seqno:280] {TITLE_280}" in out and ATTACH_280 in out
    html = (in_tmp / "out" / "fss_admin_guidance_seqno_280.html").read_text(encoding="utf-8")
    assert html.count("class='lr-rule-title'") == 4
    assert (in_tmp / "out" / "fss_admin_guidance_seqno_280.txt").exists()
    assert not (in_tmp / "state").exists()


def test_preview_script_infers_source_from_url_and_reports_partial(in_tmp, capsys):
    page = in_tmp / "partial.html"
    page.write_text(_live().replace("5. 의견제출시한(도착기준)", "5. 회신 기한"), encoding="utf-8")
    rc = _preview().main(["--url", VIEW_280, "--html-file", str(page), "--out-dir", "out"])
    assert rc == 2
    assert f"• {DEADLINE}  — 추출 실패(누락)" in capsys.readouterr().out
    assert (in_tmp / "out" / "fss_admin_guidance_seqno_280.html").exists()


def test_preview_script_reports_total_failure(in_tmp):
    page = in_tmp / "failed.html"
    page.write_text(_page("본 게시물은 JavaScript 를 사용해야 볼 수 있습니다."), encoding="utf-8")
    rc = _preview().main(["--source", KEY, "--html-file", str(page), "--out-dir", "out"])
    assert rc == 3
    html = (in_tmp / "out" / "fss_admin_guidance_seqno_280.html").read_text(encoding="utf-8")
    assert "상세 내용 추출 실패 — 원문 페이지에서 확인해 주세요." in html
