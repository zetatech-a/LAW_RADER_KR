"""금감원 세칙 제·개정 예고(fss_rule_amendment) — 원문 4개 항목 추출·메일 렌더·AI 제외.

상세 fixture 는 tests/fixtures/fss_rule/ 의 **손으로 만든 구조 fixture**다(README 참고).
실제 #4405 응답은 live/ 에 캡처되면 맨 아래 라이브 테스트가 자동으로 돈다(지금은 skip).
"""
import logging
import os
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import AssemblyBatchConfig, LLMConfig, SourceConfig
from src.models import FSS_RULE_SECTION_TITLES, FSS_RULE_SOURCE_KEY, Attachment, Post
from src.notifier import _accent, _card, build_html, build_text
from src.scrapers.fss import FssBoardScraper
from src.scrapers.fss_rule import extract_rule_sections, is_complete
from src.summarizer import Summarizer, ai_target_count

PURPOSE, CONTENT, DEADLINE, CONTACT = FSS_RULE_SECTION_TITLES
FIXTURES = Path(__file__).parent / "fixtures" / "fss_rule"
LIVE_4405 = FIXTURES / "live" / "lrgSlno_4405.html"
LIST_URL = "https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/list.do?menuNo=200489"
VIEW_4405 = (
    "https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/view.do?lrgSlno=4405&menuNo=200489&pageIndex=1"
)
SOURCE_NAME = "금감원 · 세칙 제·개정 예고"

INLINE_EXPECTED = [
    (PURPOSE, "보험회사의 건전성 제고를 위해 게리가정 보고서를 신설하고 K-ICS 지급여력비율 제도 등을 정비"),
    (
        CONTENT,
        "1. '게리가정 보고서' 세부 기준 마련(안 제4-1조의5)\n"
        "2. 주택연금상품 K-ICS 요구자본 등 산출기준 마련(안 별표22)\n"
        "3. 업무보고서 정비(안 별지26)\n"
        "4. 용어 정비(안 별표22의1)",
    ),
    (DEADLINE, "2026-11-23"),
    (
        CONTACT,
        "- 접수부서 : 보험리스크제도실\n"
        "- 담당자 : 홍길동 선임 (☎ 02-0000-0001, gildong.hong@example.com)\n"
        "- 담당자 : 김예시 수석 <yesi.kim@example.com> (☎ 02-0000-0002)\n"
        "- 팩스번호 : 02-0000-0009",
    ),
]


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _extract(html: str, **kw):
    return extract_rule_sections(BeautifulSoup(html, "lxml"), **kw)


def _squash(s: str) -> str:
    return "".join(s.split())


def _page(body: str) -> str:
    """메뉴·꼬리말이 있는 최소 페이지에 본문 조각을 끼운다."""
    return (
        "<html><body><nav><a href='#'>세칙 제·개정 예고</a></nav>"
        f"<div id='content'><div class='n-dbdata'>{body}</div>"
        "<a href='/list.do'>목록</a></div><footer>금융감독원 대표전화 1332</footer>"
        "</body></html>"
    )


def _rule_body(**over) -> str:
    parts = {
        "1": "1. 규정의 명칭 : 예시세칙",
        "2": "2. 규정의 제·개정 취지 : 취지 원문",
        "3": "3. 규정의 제·개정 내용<br>1. 첫째 항목<br>2. 둘째 항목",
        "4": "4. 기타 필요사항 : 없음",
        "5": "5. 의견제출시한(도착기준) : 2026-11-23",
        "6": "6. 접수부서, 담당자, 전화번호, 팩스번호<br>예시국 홍길동 (02-0000-0000)",
    }
    parts.update(over)
    return "<br>".join(v for v in parts.values() if v is not None)


# ── 1. 원문 추출 ───────────────────────────────────────────────────────────────


def test_inline_fixture_extracts_four_sections_verbatim():
    """#4405 레이아웃(합성): 2·3·5·6번을 원문 그대로, 표시 순서대로 뽑는다."""
    result = _extract(_fixture("synthetic_inline.html"))
    assert result.sections == INLINE_EXPECTED
    assert result.missing == []
    assert result.reason == ""


def test_sections_1_and_4_are_not_extracted():
    result = _extract(_fixture("synthetic_inline.html"))
    joined = "\n".join(v for _, v in result.sections)
    assert "규정의 명칭" not in joined and "보험업감독업무시행세칙" not in joined   # 1번
    assert "기타 필요사항" not in joined and "붙임 참조" not in joined            # 4번
    assert [t for t, _ in result.sections] == list(FSS_RULE_SECTION_TITLES)


def test_internal_numbered_list_in_section_3_is_preserved():
    value = dict(_extract(_fixture("synthetic_inline.html")).sections)[CONTENT]
    assert [line.split(" ", 1)[0] for line in value.split("\n")] == ["1.", "2.", "3.", "4."]


def test_page_chrome_metadata_and_attachments_do_not_leak():
    """메뉴·공고문 머리말·메타(의견제출기한 2026.11.23(월))·첨부·목록·이전/다음글·꼬리말."""
    joined = "\n".join(v for _, v in _extract(_fixture("synthetic_inline.html")).sections)
    for noise in ("세칙 제·개정 예고", "공고합니다", "2026.11.23(월)", "담당부서",
                  ".hwp", "신구조문대비표", "목록", "이전글", "다음글", "대표전화"):
        assert noise not in joined, noise


@pytest.mark.parametrize("name", ["synthetic_inline.html", "synthetic_table.html"])
def test_every_extracted_line_appears_verbatim_in_page(name):
    """요약·의역이 없다: 추출한 모든 줄이 페이지 원문 글자에 그대로 있다."""
    html = _fixture(name)
    page_text = _squash(BeautifulSoup(html, "lxml").get_text())
    for title, value in _extract(html).sections:
        for line in value.split("\n"):
            if name == "synthetic_table.html" and title == CONTENT:
                line = line.split(". ", 1)[1]   # <ol> 번호는 브라우저가 그려 원문 글자에 없다
            assert _squash(line) in page_text, line


def test_table_layout_pairs_label_and_value_cells():
    """th/td 표: 갈라진 제목(<br>), 가운뎃점 변형, <ol> 번호, 빈 4번, 끝의 첨부 행."""
    result = _extract(_fixture("synthetic_table.html"))
    assert result.sections == [
        (PURPOSE, "전자금융업자의 이용자 보호를 강화하고\n보고서 서식을 정비"),
        (CONTENT, "1. 선불충전금 별도관리 기준 구체화(안 제4조)\n"
                  "2. 보고서 제출기한 정비(안 제5조)\n"
                  "3. 공시 서식 정비(안 별지3)"),
        (DEADLINE, "2026. 12. 1."),
        (CONTACT, "디지털금융감독국 전자금융팀\n"
                  "담당 : 박예시 팀장 (02-0000-0101, park@example.com)\n"
                  "FAX : 02-0000-0199"),
    ]


def test_flat_paragraph_layout_with_heading_elements():
    html = _page(
        "<p class='tit'><strong>2.</strong> 규정의 제·개정 취지</p><p>취지 원문</p>"
        "<p class='tit'>3. 규정의 제·개정 내용</p><ul><li>가. 첫째</li><li>나. 둘째</li></ul>"
        "<p class='tit'>5. 의견제출시한(도착기준)</p><p>2026. 11. 23.</p>"
        "<p class='tit'>6. 접수부서, 담당자, 전화번호, 팩스번호</p><p>예시국</p>"
    )
    assert _extract(html).sections == [
        (PURPOSE, "취지 원문"),
        (CONTENT, "가. 첫째\n나. 둘째"),
        (DEADLINE, "2026. 11. 23."),
        (CONTACT, "예시국"),
    ]


@pytest.mark.parametrize(
    "purpose_heading, content_heading",
    [
        ("2. 규정의 제·개정 취지", "3. 규정의 제·개정 내용"),
        ("2. 규정의 제∙개정 취지", "3. 규정의 제∙개정 내용"),
        ("2. 규정의 제ㆍ개정 취지", "3. 규정의 제ㆍ개정 내용"),
        ("2. 규정의 제・개정 취지", "3. 규정의 제・개정 내용"),
        ("2. 규정의 제개정 취지", "3. 규정의 제개정 내용"),
        ("2.규정의 제 · 개정 취지", "3)  규정의  제 · 개정  내용"),
        ("2. 규정의 제·\n개정 취지", "3. 규정의\n제·개정 내용"),       # 원문 개행으로 갈라진 제목
        ("2. 규정의<br>제·개정 취지", "3. 규정의 제·개정<br>내용"),    # <br> 로 갈라진 제목
        ("② 규정의 제·개정 취지", "③ 규정의 제·개정 내용"),
    ],
)
def test_heading_notation_and_whitespace_variants(purpose_heading, content_heading):
    html = _page(_rule_body(
        **{"2": f"{purpose_heading}<br>취지 원문",
           "3": f"{content_heading}<br>1. 첫째 항목<br>2. 둘째 항목"}
    ))
    got = dict(_extract(html).sections)
    assert got[PURPOSE] == "취지 원문"
    assert got[CONTENT] == "1. 첫째 항목\n2. 둘째 항목"
    assert set(got) == set(FSS_RULE_SECTION_TITLES)


@pytest.mark.parametrize(
    "deadline_heading",
    ["5. 의견제출시한(도착기준) :", "5. 의견 제출 시한 ( 도착 기준 ) ：", "5. 의견제출기한", "5 의견제출시한(도착기준)"],
)
def test_deadline_heading_variants(deadline_heading):
    html = _page(_rule_body(**{"5": f"{deadline_heading} 2026-11-23"}))
    assert dict(_extract(html).sections)[DEADLINE] == "2026-11-23"


def test_sub_items_that_reuse_top_level_titles_do_not_split_section_3():
    """3번 하위 항목이 최상위 제목 문구를 다시 써도 경계가 아니다.

    - 제목 뒤에 구분자 없이 설명이 이어지는 줄('5. 의견제출기한 단축 …')은 제목이 아니다.
    - 원문 순서를 거슬러 다시 나오는 제목('1. 규정의 명칭', '2. … 취지 :')은 경계가 아니다.
    """
    html = _page(_rule_body(**{
        "3": "3. 규정의 제·개정 내용<br>1. 규정의 명칭 변경(안 제1조)"
             "<br>5. 의견제출기한 단축 사유 명확화(안 제5조)<br>6. 부칙 정비"
             "<br>1. 규정의 명칭<br>2. 규정의 제·개정 취지 : 이전 문구 삭제",
    }))
    got = dict(_extract(html).sections)
    assert got[CONTENT] == (
        "1. 규정의 명칭 변경(안 제1조)\n5. 의견제출기한 단축 사유 명확화(안 제5조)\n6. 부칙 정비"
        "\n1. 규정의 명칭\n2. 규정의 제·개정 취지 : 이전 문구 삭제"
    )
    assert got[PURPOSE] == "취지 원문"
    assert got[DEADLINE] == "2026-11-23"


def test_long_content_is_not_truncated():
    items = "<br>".join(f"{n}. 개정 항목 {n} — " + "가나다라마바사 " * 20 for n in range(1, 151))
    html = _page(_rule_body(**{"3": f"3. 규정의 제·개정 내용<br>{items}"}))
    value = dict(_extract(html).sections)[CONTENT]
    assert len(value.split("\n")) == 150
    assert value.split("\n")[-1].startswith("150. 개정 항목 150 — ")


def test_paragraph_breaks_are_kept_but_not_multiplied():
    html = _page(_rule_body(**{"6": "6. 접수부서, 담당자, 전화번호, 팩스번호<br><br>"
                                     "가국 홍길동<br><br><br>나국 김예시<br>"}))
    assert dict(_extract(html).sections)[CONTACT] == "가국 홍길동\n\n나국 김예시"


def test_plain_newlines_inside_db_text_are_kept():
    """DB 원문을 pre-line 으로 내보내는 경우: 글자 사이 개행이 줄바꿈으로 남는다."""
    html = _page("2. 규정의 제·개정 취지\n취지 원문\n3. 규정의 제·개정 내용\n1. 첫째\n2. 둘째\n"
                 "5. 의견제출시한(도착기준) : 2026-11-23\n6. 접수부서, 담당자, 전화번호, 팩스번호 : 예시국")
    got = dict(_extract(html).sections)
    assert got[CONTENT] == "1. 첫째\n2. 둘째"
    assert got[CONTACT] == "예시국"


def test_contact_details_keep_every_character():
    contact = "보험리스크제도실 홍길동 선임 (☎ 02-0000-0001 / FAX 02-0000-0009, a.b-c_d@example.co.kr)"
    html = _page(_rule_body(**{"6": f"6. 접수부서, 담당자, 전화번호, 팩스번호 : {contact}"}))
    assert dict(_extract(html).sections)[CONTACT] == contact


def test_contact_label_on_next_line_is_not_eaten_as_heading():
    html = _page(_rule_body(**{"6": "6. 접수부서<br>담당자 : 홍길동<br>팩스번호 : 02-0000-0009"}))
    assert dict(_extract(html).sections)[CONTACT] == "담당자 : 홍길동\n팩스번호 : 02-0000-0009"


def test_missing_section_is_reported_and_others_kept():
    result = _extract(_page(_rule_body(**{"5": None})))
    assert result.missing == [DEADLINE]
    assert [t for t, _ in result.sections] == [PURPOSE, CONTENT, CONTACT]


def test_empty_section_counts_as_missing():
    result = _extract(_page(_rule_body(**{"2": "2. 규정의 제·개정 취지 :"})))
    assert result.missing == [PURPOSE]
    assert dict(result.sections)[CONTENT] == "1. 첫째 항목\n2. 둘째 항목"


def test_structure_change_without_titles_is_a_total_failure():
    result = _extract(_page("<p>본 게시물은 JavaScript 를 사용해야 볼 수 있습니다.</p>"))
    assert result.sections == []
    assert result.missing == list(FSS_RULE_SECTION_TITLES)
    assert "찾지 못함" in result.reason


def test_titles_only_without_values_is_a_total_failure():
    body = "<br>".join(["2. 규정의 제·개정 취지", "3. 규정의 제·개정 내용",
                        "5. 의견제출시한(도착기준)", "6. 접수부서, 담당자, 전화번호, 팩스번호"])
    result = _extract(_page(body))
    assert result.sections == []
    assert "비어 있음" in result.reason


def test_duplicate_print_copy_does_not_leak_into_last_section():
    body = _rule_body()
    html = _page(f"{body}<br>{body}")
    assert dict(_extract(html).sections)[CONTACT] == "예시국 홍길동 (02-0000-0000)"


def test_last_section_stops_at_attachment_names():
    html = _page(_rule_body() + "<br>개정안.hwp<br>신구조문.pdf")
    got = dict(_extract(html, stop_lines=("개정안.hwp", "신구조문.pdf")).sections)
    assert got[CONTACT] == "예시국 홍길동 (02-0000-0000)"


def test_numberless_titles_are_accepted_only_when_no_numbered_titles_exist():
    html = _page("<dl><dt>규정의 제·개정 취지</dt><dd>취지 원문</dd>"
                 "<dt>규정의 제·개정 내용</dt><dd>1. 첫째</dd>"
                 "<dt>의견제출시한(도착기준)</dt><dd>2026-11-23</dd>"
                 "<dt>접수부서, 담당자, 전화번호, 팩스번호</dt><dd>예시국</dd></dl>")
    assert dict(_extract(html).sections) == {
        PURPOSE: "취지 원문", CONTENT: "1. 첫째", DEADLINE: "2026-11-23", CONTACT: "예시국",
    }


def test_special_characters_are_kept_raw_in_extracted_values():
    """추출값은 원문 글자 그대로(이스케이프는 메일 렌더 단계에서 한다)."""
    html = _page(_rule_body(**{"2": "2. 규정의 제·개정 취지 : A &amp; B &lt;script&gt;x&lt;/script&gt;"}))
    assert dict(_extract(html).sections)[PURPOSE] == "A & B <script>x</script>"


# ── 2. 스크래퍼 연동 ───────────────────────────────────────────────────────────


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
        return b"%HWP dummy"


def _scraper(html="", key=FSS_RULE_SOURCE_KEY, fail=False):
    src = SourceConfig(key=key, name=SOURCE_NAME, type="fss_board", list_url=LIST_URL)
    return FssBoardScraper(src, fetcher=_Fetcher(html, fail=fail))


def _post(key=FSS_RULE_SOURCE_KEY, **over):
    kw = dict(source_key=key, source_name=SOURCE_NAME, post_id="lrgSlno:4405",
              title="[합성 예시] 보험업감독업무시행세칙 일부개정 예고", url=VIEW_4405, date="2026-10-12")
    kw.update(over)
    return Post(**kw)


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """실패 시 debug/ 덤프가 저장소가 아니라 임시 디렉터리에 남게 한다."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_enrich_fills_details_only_and_keeps_attachments(in_tmp):
    sc = _scraper(_fixture("synthetic_inline.html"))
    p = _post()
    sc.enrich(p)
    assert p.details == INLINE_EXPECTED
    assert p.body == ""                       # 일반 본문·요약 경로를 타지 않는다
    assert [a.filename for a in p.attachments] == [
        "보험업감독업무시행세칙 일부개정(안).hwp", "신구조문대비표.pdf"]
    assert all(a.data == b"%HWP dummy" for a in p.attachments)
    assert sc.enrich_succeeded(p) is True
    assert not (in_tmp / "debug").exists()    # 성공하면 덤프하지 않는다


def test_partial_extraction_logs_missing_items_and_counts_as_failure(in_tmp, caplog):
    sc = _scraper(_page(_rule_body(**{"5": None})))
    p = _post()
    with caplog.at_level(logging.WARNING):
        sc.enrich(p)
    assert [t for t, _ in p.details] == [PURPOSE, CONTENT, CONTACT]
    assert sc.enrich_succeeded(p) is False
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "일부 누락" in msg and DEADLINE in msg and VIEW_4405 in msg
    assert (in_tmp / "debug" / "fss_rule_amendment_detail_lrgSlno_4405.txt").exists()


def test_total_failure_logs_reason_and_url_without_body_fallback(in_tmp, caplog):
    """구조가 바뀌어도 일반 본문으로 되돌아가지 않는다(= AI 요약 경로로 새지 않는다)."""
    changed = _fixture("synthetic_inline.html")
    for old, new in (("규정의", "규칙의"), ("의견제출", "의견"), ("접수부서", "부서"), ("기타 필요", "그밖")):
        changed = changed.replace(old, new)              # 6개 항목 제목이 모두 바뀐 페이지
    sc = _scraper(changed)
    p = _post(attachments=[Attachment("안.hwp", "https://www.fss.or.kr/fss/cmmn/file/fileDown.do?x=1")])
    with caplog.at_level(logging.WARNING):
        sc.enrich(p)
    assert p.details == [] and p.body == ""
    assert p.attachments                       # 첨부는 그대로 유지
    assert sc.enrich_succeeded(p) is False      # 첨부만으로 성공 처리하지 않는다
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "추출 실패" in msg and "찾지 못함" in msg and VIEW_4405 in msg


def test_http_failure_is_logged_with_url_and_not_counted_as_success(in_tmp, caplog):
    sc = _scraper(fail=True)
    p = _post()
    with caplog.at_level(logging.WARNING):
        sc.enrich(p)
    assert p.details == [] and p.body == ""
    assert sc.enrich_succeeded(p) is False
    assert any(VIEW_4405 in r.getMessage() and "상세 로드 실패" in r.getMessage()
               for r in caplog.records)


def test_other_fss_board_with_same_markup_keeps_generic_body(in_tmp):
    """다른 금감원 게시판은 같은 마크업이어도 기존 본문 추출 그대로(구조화 항목 없음)."""
    sc = _scraper(_fixture("synthetic_inline.html"), key="fss_admin_guidance")
    p = _post(key="fss_admin_guidance", source_name="금감원 · 행정지도 예고", post_id="seqno:1")
    sc.enrich(p)
    assert p.details == []
    assert "규정의 제·개정 취지" in p.body
    assert sc.enrich_succeeded(p) is True


def test_is_complete_requires_all_four_values():
    assert is_complete(INLINE_EXPECTED)
    assert not is_complete(INLINE_EXPECTED[:3])
    assert not is_complete([(t, "") for t in FSS_RULE_SECTION_TITLES])


# ── 3. AI 요약 제외 ─────────────────────────────────────────────────────────────


def _llm(**over) -> LLMConfig:
    base = dict(enabled=True, model="m", lines=3, max_line_chars=90, min_body_chars=10,
                max_input_chars=8000, max_posts=10, rpm=0, timeout_sec=5, max_retries=0,
                retry_backoff_sec=0, api_key="k", assembly_batch=AssemblyBatchConfig())
    base.update(over)
    return LLMConfig(**base)


def _no_gemini(summarizer):
    summarizer._generate = lambda *a, **k: pytest.fail("세칙 예고는 Gemini 를 호출하면 안 된다")
    return summarizer


@pytest.mark.parametrize("details", [INLINE_EXPECTED, INLINE_EXPECTED[:2], []],
                         ids=["complete", "partial", "failed"])
def test_rule_posts_never_reach_gemini(details):
    p = _post(details=list(details))
    s = _no_gemini(Summarizer(_llm()))
    assert s.summarize_all({SOURCE_NAME: [p]}) == 0
    assert p.summary == []


def test_rule_post_is_excluded_even_if_a_body_were_present():
    """어떤 경로로 body 가 채워져도(추출 실패 우회 등) AI 로 새지 않는다."""
    p = _post(body="규정의 제·개정 취지 원문이 아주 길게 이어지는 본문 " * 10)
    s = _no_gemini(Summarizer(_llm()))
    assert s.summarize_all({SOURCE_NAME: [p]}) == 0
    assert s.summarize(p) == []
    assert ai_target_count(_llm(), {SOURCE_NAME: [p]}) == 0


def test_other_sources_are_still_summarized_alongside_rule_posts():
    rule = _post(body="세칙 예고 본문 " * 20)
    press = Post(source_key="fss_press", source_name="금감원 · 보도자료", post_id="nttId:1",
                 title="보도자료", url="https://www.fss.or.kr/x", body="금융감독원 보도자료 본문 " * 20)
    calls = []
    s = Summarizer(_llm())
    s._generate = lambda prompt, deadline=None: calls.append(prompt) or {"ok": 1}
    s._parse = lambda data: ["요약 1", "요약 2", "요약 3"]
    grouped = {SOURCE_NAME: [rule], "금감원 · 보도자료": [press]}
    assert ai_target_count(_llm(), grouped) == 1
    assert s.summarize_all(grouped) == 1
    assert len(calls) == 1 and "보도자료" in calls[0]
    assert press.summary == ["요약 1", "요약 2", "요약 3"] and rule.summary == []


def test_run_pipeline_mails_rule_sections_without_calling_gemini(tmp_path, monkeypatch, caplog):
    """main.run 전체 경로: 실제 FSS 스크래퍼 enrich → 요약 단계 → 발송 직전 메일 내용.

    SMTP·state 는 임시 대역, Gemini 는 호출되면 실패하도록 막는다.
    """
    from src import main as main_mod
    from src.scrapers.base import CollectResult
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    st.mark_seen(FSS_RULE_SOURCE_KEY, ["lrgSlno:4385"], baselined=True)
    st.save()
    monkeypatch.chdir(tmp_path)
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"

    def _build(src, fetcher):
        sc = FssBoardScraper(src, fetcher=_Fetcher(_fixture("synthetic_inline.html")))
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
                           "--only", FSS_RULE_SOURCE_KEY])

    assert rc == 0 and len(sent) == 1
    (post,) = next(iter(sent[0].values()))
    assert post.details == INLINE_EXPECTED and post.summary == []
    assert "AI 요약 집계 — 대상 0건 / 요약 0건 / 발췌 폴백 0건" in caplog.text
    assert "상세 수집 집계(전체) — 시도 1건 / 성공 1건" in caplog.text
    html = build_html(sent[0])
    assert html.count("class='lr-rule-title'") == 4
    assert "lrgSlno:4405" in State(state_path).seen_ids(FSS_RULE_SOURCE_KEY)


# ── 4. 메일 렌더 ───────────────────────────────────────────────────────────────

_ATTACH = Attachment("보험업감독업무시행세칙 일부개정(안).hwp",
                     "https://www.fss.or.kr/fss/cmmn/file/fileDown.do?atchFileId=A&fileSn=0", data=b"x")


def _render(details):
    p = _post(details=list(details), attachments=[_ATTACH])
    grouped = {SOURCE_NAME: [p]}
    return p, build_html(grouped), build_text(grouped)


def test_html_shows_four_unnumbered_titles_in_fss_theme_color():
    _, html, _ = _render(INLINE_EXPECTED)
    accent = _accent(SOURCE_NAME)
    assert accent == "#0f766e"                       # 기존 금감원 강조색을 그대로 쓴다
    assert html.count("class='lr-rule-title'") == 4
    for title in FSS_RULE_SECTION_TITLES:
        assert f"•</span>{title}</div>" in html
    titles = html.split("class='lr-rule-title'")[1:]
    assert all(f"color:{accent}" in t.split(">", 1)[0] and "font-weight:700" in t.split(">", 1)[0]
               for t in titles)
    for numbered in ("2. 규정의", "3. 규정의", "5. 의견", "6. 접수"):
        assert numbered not in html


def test_html_wraps_each_section_in_a_light_box():
    _, html, _ = _render(INLINE_EXPECTED)
    boxes = html.split("class='lr-rule-body' style='")[1:]
    assert len(boxes) == 4
    for box in boxes:
        style = box.split("'", 1)[0]
        assert "background:#f8fafc" in style and "border:1px solid #e2e8f0" in style
        assert "border-radius:6px" in style and "padding:10px 12px" in style
        assert "color:#334155" in style


def test_html_keeps_line_breaks_and_escapes_special_characters():
    _, html, _ = _render(INLINE_EXPECTED)
    assert ("1. &#x27;게리가정 보고서&#x27; 세부 기준 마련(안 제4-1조의5)<br>"
            "2. 주택연금상품 K-ICS 요구자본 등 산출기준 마련(안 별표22)<br>") in html
    assert "&lt;yesi.kim@example.com&gt;" in html
    assert "<yesi.kim" not in html


def test_html_escapes_markup_injected_through_the_source():
    _, html, _ = _render([(PURPOSE, "<script>alert(1)</script> & <b>굵게</b>"),
                          *INLINE_EXPECTED[1:]])
    assert "<script>" not in html and "<b>굵게" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;굵게&lt;/b&gt;" in html


def test_card_keeps_title_date_attachments_and_original_link():
    p, html, _ = _render(INLINE_EXPECTED)
    assert f">{p.title}</a>" in html
    assert ">2026-10-12</div>" in html
    assert f"href='{_ATTACH.url.replace('&', '&amp;')}'" in html and "📎 보험업감독업무시행세칙" in html
    assert html.count(f"href='{p.url.replace('&', '&amp;')}'") == 2      # 제목 + 원문 보기
    assert "원문 보기 &rsaquo;" in html


def test_rule_only_digest_has_no_ai_notice():
    _, html, text = _render(INLINE_EXPECTED)
    assert "AI 요약은 생성형 AI" not in html and "AI 요약은 생성형 AI" not in text


def test_text_part_carries_the_same_original_content():
    p, _, text = _render(INLINE_EXPECTED)
    lines = text.splitlines()
    start = lines.index(f"  - {p.title}  {p.date}")
    assert lines[start + 1:start + 17] == [
        f"    • {PURPOSE}",
        "      보험회사의 건전성 제고를 위해 게리가정 보고서를 신설하고 K-ICS 지급여력비율 제도 등을 정비",
        f"    • {CONTENT}",
        "      1. '게리가정 보고서' 세부 기준 마련(안 제4-1조의5)",
        "      2. 주택연금상품 K-ICS 요구자본 등 산출기준 마련(안 별표22)",
        "      3. 업무보고서 정비(안 별지26)",
        "      4. 용어 정비(안 별표22의1)",
        f"    • {DEADLINE}",
        "      2026-11-23",
        f"    • {CONTACT}",
        "      - 접수부서 : 보험리스크제도실",
        "      - 담당자 : 홍길동 선임 (☎ 02-0000-0001, gildong.hong@example.com)",
        "      - 담당자 : 김예시 수석 <yesi.kim@example.com> (☎ 02-0000-0002)",
        "      - 팩스번호 : 02-0000-0009",
        f"    {p.url}",
        f"      첨부: {_ATTACH.filename} ({_ATTACH.url})",
    ]


def test_partial_extraction_marks_missing_sections_in_both_parts():
    details = [INLINE_EXPECTED[0], INLINE_EXPECTED[1], INLINE_EXPECTED[3]]
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
    assert f">{p.title}</a>" in html and "📎 보험업감독업무시행세칙" in html
    assert "원문 보기 &rsaquo;" in html
    assert p.url in text and _ATTACH.url in text


def test_other_cards_render_identically_when_a_rule_post_is_in_the_digest():
    sanction = Post("fss_sanction", "금감원 · 검사결과 제재", "exam:1_1", "쿠팡페이(주)",
                    "https://www.fss.or.kr/s", date="2026-07-23",
                    details=[("금융기관명", "쿠팡페이(주)"), ("제재조치일", "2026-07-23"),
                             ("관련부서", "전자금융검사국")])
    press = Post("fss_press", "금감원 · 보도자료", "nttId:1", "보도", "https://www.fss.or.kr/p",
                 summary=["a", "b", "c"])
    rule = _post(details=list(INLINE_EXPECTED))
    alone = build_html({sanction.source_name: [sanction], press.source_name: [press]})
    mixed = build_html({sanction.source_name: [sanction], SOURCE_NAME: [rule],
                        press.source_name: [press]})
    for p in (sanction, press):
        card = _card(p, _accent(p.source_name))
        assert card in alone and card in mixed
    assert "lr-rule" not in alone
    # 다른 소스의 구조화 항목 표(제재)는 기존 표 렌더 그대로다.
    assert mixed.count("<tr class='lr-detail'>") == 3


# ── 5. 실제 응답(#4405) ─────────────────────────────────────────────────────────
#
# tests/fixtures/fss_rule/live/lrgSlno_4405.html 은 실제 금감원 상세 페이지 원본이다
# (2026-10-13 사용자 제공, 서버 소스 저장본). 담당자 성명·이메일·직통/팩스 번호만 같은
# 형태의 가짜 값으로 바꿨고 나머지는 바이트 그대로다(fixture README 참고).
# 실제 구조: div.krds-reg > dl.form-group > dt.form-tit(번호+제목) / dd.form-conts(값).

LIVE_4405_EXPECTED = [
    (PURPOSE, "보험회사의 건전성 제고를 위해 계리가정 보고서를 신설하고 K-ICS 지급여력비율 제도 등을 정비"),
    (CONTENT, "1. \u2018계리가정 보고서\u2019 세부 기준 마련(안 제4-1조의5)\n"
              "2. 주택연금상품 K-ICS 요구자본 등 산출기준 마련(안 별표22)\n"
              "3. 업무보고서 정비(안 별지26)\n"
              "4. 용어 정비(안 별표22의1)"),
    (DEADLINE, "2026-11-23"),
    (CONTACT, "계리리스크감독국 홍길동 수석조사역(총괄) 02-0000-0001, contact1@example.com\n"
              "계리리스크감독국 김예시 선임조사역(계리가정보고서) 02-0000-0002 contact2@example.com\n"
              "계리리스크감독국 박예시 수석조사역(K-ICS 관련) 02-0000-0003 contact3@example.com\n"
              "(팩스번호 : 02-0000-0009)"),
]

_SKIP_LIVE = pytest.mark.skipif(
    not LIVE_4405.exists(),
    reason="실제 응답 fixture 없음 — tests/fixtures/fss_rule/README.md 의 캡처 명령 참고",
)


@_SKIP_LIVE
def test_live_4405_extracts_four_sections_exactly():
    result = _extract(LIVE_4405.read_text(encoding="utf-8"))
    assert result.missing == [], result.reason or result.missing
    assert result.sections == LIVE_4405_EXPECTED


@_SKIP_LIVE
def test_live_4405_values_are_verbatim_and_exclude_sections_1_and_4():
    html = LIVE_4405.read_text(encoding="utf-8")
    page_text = _squash(BeautifulSoup(html, "lxml").get_text())
    for _, value in _extract(html).sections:
        assert "보험업감독업무시행세칙" not in value          # 1번(규정의 명칭) 값
        for noise in ("규정의 명칭", "기타 필요사항", "첨부파일", "다운로드", "목록", "의견제출"):
            assert noise not in value, noise
        for line in value.split("\n"):
            assert _squash(line) in page_text, line


@_SKIP_LIVE
def test_live_4405_enrich_keeps_attachments_and_counts_as_success(in_tmp):
    sc = _scraper(LIVE_4405.read_text(encoding="utf-8"))
    p = _post()
    sc.enrich(p)
    assert p.details == LIVE_4405_EXPECTED and p.body == ""
    assert sc.enrich_succeeded(p) is True
    assert [a.filename for a in p.attachments] == [
        "2610_보험업감독업무시행세칙 개정 사전예고안_최종_187132806250143.hwp",
        "2610_(별첨)_ 업무보고서 개정안_최종_187132842774673.xlsx",
    ]
    assert not (in_tmp / "debug").exists()


@_SKIP_LIVE
def test_live_4405_old_generic_selectors_matched_nothing():
    """운영 #4405 본문이 빈 원인: 개편 페이지는 div.contents 로 감싸여 기존 공통 selector
    (.view-cont/.board-view/.bbs-view/.cont/#content)가 하나도 맞지 않는다."""
    from src.scrapers.fss import _BODY_SELECTORS, _body_text

    soup = BeautifulSoup(LIVE_4405.read_text(encoding="utf-8"), "lxml")
    assert all(not soup.select(sel) for sel in _BODY_SELECTORS)
    assert _body_text(soup, _BODY_SELECTORS) == ""
