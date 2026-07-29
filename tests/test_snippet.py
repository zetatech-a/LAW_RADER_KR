"""LLM 요약 실패 시 쓰는 원문 발췌(src/snippet.py) 규칙 테스트.

네트워크·LLM 호출은 전혀 없다 — 순수 함수만 검증한다.
본문 예시는 저장소의 스크래퍼가 실제로 뽑아 오는 형태를 따른다:
FSC 는 `.board-view-wrap .body`, FSS 는 `.view-cont` 를 get_text("\\n") 으로 펼치므로
라벨(<dt>)과 값(<dd>)이 각각 한 줄씩 나온다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import Post
from src.notifier import build_html, build_text
from src.snippet import build_fallback_snippet, is_duplicate_title

TITLE = "외부감사 규정 개정안 입법예고"
BODY_SENTENCE = "금융위원회는 외부감사 및 회계 등에 관한 규정 개정안을 입법예고한다고 밝혔다."


# --- 일반 본문 ---


def test_plain_body_is_shown_from_the_front():
    """군더더기가 없는 본문은 기존처럼 앞부분이 그대로 나온다."""
    body = f"{BODY_SENTENCE} 의견은 8월 13일까지 제출할 수 있다."
    assert build_fallback_snippet(body, TITLE) == body


def test_empty_body_keeps_current_behavior():
    """본문이 없거나 공백뿐이면 예전처럼 빈 문자열(카드에 발췌 없음)."""
    assert build_fallback_snippet("", TITLE) == ""
    assert build_fallback_snippet("   \n\n\t", TITLE) == ""
    assert build_fallback_snippet(None, TITLE) == ""


# --- 제목 중복 제거 ---


def test_drops_first_line_identical_to_title():
    body = f"{TITLE}\n{BODY_SENTENCE}"
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_drops_title_that_differs_only_in_whitespace():
    """앞뒤 공백·연속 공백 차이는 같은 제목으로 본다."""
    body = f"  외부감사   규정  개정안 입법예고 \n{BODY_SENTENCE}"
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_drops_title_that_differs_only_in_brackets():
    """괄호·구분자만 다른 경우도 같은 제목으로 본다."""
    body = f"[{TITLE}]\n{BODY_SENTENCE}"
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_keeps_normal_sentence_that_merely_starts_with_the_title():
    """제목으로 시작할 뿐인 정상 문장은 지우지 않는다(부분 일치 아님)."""
    first = f"{TITLE}에 따라 8월 13일까지 의견을 받는다."
    body = f"{first}\n{BODY_SENTENCE}"
    assert build_fallback_snippet(body, TITLE).startswith(first)


def test_keeps_sentence_that_only_shares_a_prefix_with_the_title():
    body = "외부감사 규정 개정안 입법예고안의 주요 내용은 다음과 같다."
    assert build_fallback_snippet(body, TITLE) == body


def test_is_duplicate_title_ignores_too_short_titles():
    """제목이 사실상 비어 있으면 우연 일치로 본문을 지우지 않는다."""
    assert not is_duplicate_title("가", "")
    assert not is_duplicate_title("본문 첫 줄", "  ")


# --- 상용구·메타데이터 제거 ---


def test_starts_from_real_body_after_metadata_block():
    """담당부서·등록일·조회수·첨부파일 안내를 지나 실제 본문부터 발췌한다."""
    body = "\n".join([
        "제목",
        TITLE,
        "담당부서",
        "기업회계팀",
        "등록일",
        "2026-07-24",
        "조회수",
        "1,234",
        "첨부파일",
        "보도자료.hwp (188 KB)",
        BODY_SENTENCE,
        "목록으로",
        "이전글",
        "다음글",
    ])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_strips_label_value_lines_and_press_notice():
    """'라벨 : 값' 한 줄 형태와 보도자료 배포 안내도 걷어낸다."""
    body = "\n".join([
        "이 자료는 배포 즉시 보도할 수 있습니다.",
        "담당부서 : 금융위원회 기업회계팀",
        "등록일 : 2026-07-24",
        BODY_SENTENCE,
        "※ 본 자료를 인용하여 보도할 경우 출처를 표기해 주시기 바랍니다.",
        "문의 : 02-2100-2600",
    ])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_strips_breadcrumb_and_javascript_notice():
    body = "\n".join([
        "본문 바로가기",
        "홈 > 알림마당 > 보도자료",
        "이 사이트는 자바스크립트를 지원하는 브라우저에서 최적화되어 있습니다.",
        BODY_SENTENCE,
        "이 페이지에서 제공하는 정보에 만족하십니까?",
    ])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_keeps_sentences_that_only_start_like_a_label():
    """구분자 없이 라벨로 시작하기만 하는 정상 문장은 지우지 않는다."""
    body = "\n".join([
        "담당자는 위반 사실을 즉시 보고해야 한다.",
        "문의사항이 많은 항목은 해설서로 별도 안내한다.",
    ])
    out = build_fallback_snippet(body, TITLE)
    assert out.startswith("담당자는 위반 사실을")
    assert "문의사항이 많은 항목은" in out


def test_does_not_touch_boilerplate_like_lines_in_the_middle():
    """가운데 줄은 상용구처럼 보여도 건드리지 않는다(본문 훼손 방지)."""
    body = "\n".join([
        BODY_SENTENCE,
        "등록일 : 2026-07-24",
        "개정안은 2027년 1월 1일부터 시행한다.",
    ])
    out = build_fallback_snippet(body, TITLE)
    assert "등록일 : 2026-07-24" in out
    assert out.endswith("2027년 1월 1일부터 시행한다.")


# --- HTML / 엔티티 ---


def test_strips_html_tags_and_entities():
    body = (
        "<div class='board-view-wrap'><dl><dt>담당부서</dt><dd>기업회계팀</dd></dl>"
        "<p>금융위원회는 &lt;주식회사 등의 외부감사에 관한 법률&gt; 개정안을 "
        "의결하였다.</p></div>"
    )
    out = build_fallback_snippet(body, TITLE)
    assert out == "금융위원회는 <주식회사 등의 외부감사에 관한 법률> 개정안을 의결하였다."


def test_keeps_korean_angle_notation_that_is_not_html():
    """<붙임> 같은 한국어 표기는 태그로 오인해 지우지 않는다."""
    body = f"{BODY_SENTENCE}\n<붙임> 개정안 주요 내용 1부."
    assert "<붙임>" in build_fallback_snippet(body, TITLE)


# --- 길이·말줄임표 ---


def test_no_ellipsis_at_exactly_the_limit():
    body = "가" * 220
    out = build_fallback_snippet(body, TITLE)
    assert out == body
    assert "…" not in out


def test_adds_ellipsis_over_the_limit():
    body = "가" * 221
    out = build_fallback_snippet(body, TITLE)
    assert out == "가" * 220 + " …"


def test_limit_applies_after_cleanup():
    """정제 뒤 길이를 기준으로 자른다 — 머리말이 발췌 분량을 먹지 않는다."""
    body = "담당부서 : 기업회계팀\n" + "나" * 300
    out = build_fallback_snippet(body, TITLE)
    assert out == "나" * 220 + " …"


# --- 안전한 폴백 ---


def test_falls_back_to_raw_excerpt_when_cleanup_empties_the_body():
    """전부 상용구라 남는 게 없으면 원본을 기존 방식으로 발췌한다."""
    body = "담당부서 : 기업회계팀\n등록일 : 2026-07-24\n목록으로"
    assert build_fallback_snippet(body, TITLE) == (
        "담당부서 : 기업회계팀 등록일 : 2026-07-24 목록으로"
    )


def test_falls_back_when_cleanup_leaves_only_symbols():
    body = "첨부파일\n보도자료.hwp\n○ ○ ○"
    out = build_fallback_snippet(body, TITLE)
    assert out == "첨부파일 보도자료.hwp ○ ○ ○"


# --- 수치 보존 ---


def test_numbers_dates_amounts_and_signs_are_preserved():
    """숫자·날짜·금액·음수·퍼센트가 접두사 제거 등으로 훼손되지 않아야 한다."""
    body = "\n".join([
        "담당부서 : 금융위원회 은행과",
        "- 3.5%p 인하",
        "-2.5%p 하락, △1,234억원 순손실, 2026-07-24 기준",
        "1. 시행일은 2027년 1월 1일이며 과징금 상한은 1,000억원이다.",
    ])
    out = build_fallback_snippet(body, TITLE)
    for token in (
        "- 3.5%p",
        "-2.5%p",
        "△1,234억원",
        "2026-07-24",
        "1. 시행일은",
        "2027년 1월 1일",
        "1,000억원",
    ):
        assert token in out, token


# --- notifier 연동 ---


def _post(body: str, summary=None) -> Post:
    return Post(
        source_key="fsc_press",
        source_name="금융위 · 보도자료",
        post_id="1",
        title=TITLE,
        url="https://www.fsc.go.kr/no010101/87401",
        date="2026-07-24",
        body=body,
        summary=list(summary or []),
    )


_NOISY_BODY = "\n".join([TITLE, "담당부서", "기업회계팀", BODY_SENTENCE, "목록으로"])


def test_email_uses_cleaned_snippet_when_summary_is_missing():
    p = _post(_NOISY_BODY)
    grouped = {p.source_name: [p]}

    html = build_html(grouped)
    text = build_text(grouped)
    for rendered in (html, text):
        assert BODY_SENTENCE in rendered
        assert "담당부서" not in rendered
        assert "목록으로" not in rendered
    # 제목은 카드에 따로 표시되므로 발췌에서는 빠진다(카드 제목으로 1회만 등장).
    assert text.count(TITLE) == 1


def test_email_ignores_fallback_snippet_when_summary_exists():
    """LLM 요약이 있으면 폴백 발췌 경로는 아예 쓰이지 않는다."""
    p = _post(_NOISY_BODY, summary=["첫째 요약함", "둘째 요약함", "셋째 요약함"])
    grouped = {p.source_name: [p]}

    html = build_html(grouped)
    text = build_text(grouped)
    for rendered in (html, text):
        assert "첫째 요약함" in rendered
        assert BODY_SENTENCE not in rendered
    assert "[원문 발췌]" not in text
