"""LLM 요약 실패 시 쓰는 원문 발췌(src/snippet.py) 규칙 테스트.

네트워크·LLM 호출은 전혀 없다 — 순수 함수만 검증한다.
본문 예시는 저장소의 스크래퍼가 실제로 뽑아 오는 형태를 따른다:
FSC 는 `.board-view-wrap .body`, FSS 는 `.view-cont` 를 get_text("\\n") 으로 펼치므로
라벨(<dt>)과 값(<dd>)이 각각 한 줄씩 나온다.
"""
import html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import Post
from src.notifier import build_html, build_text
from src.snippet import (
    ASSEMBLY_FALLBACK_CHARS,
    ELLIPSIS,
    OMISSION_MARK,
    SNIPPET_LIMIT,
    build_assembly_fallback_lines,
    build_fallback_snippet,
    is_duplicate_title,
    is_label_line,
)

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


def test_title_comparison_keeps_numeric_punctuation():
    """소수점·부호·자릿점·날짜 구분자가 다르면 다른 제목이다.

    무시하면 "수익률 3.5% 증가" 제목에서 "수익률 35% 증가" 본문 줄이 중복으로
    지워져, 발췌에서 서로 다른 수치가 통째로 사라진다.
    """
    assert not is_duplicate_title("수익률 35% 증가", "수익률 3.5% 증가")
    assert not is_duplicate_title("수익률 3.5% 기록", "수익률 -3.5% 기록")
    assert not is_duplicate_title("실적 20260724 발표", "실적 2026-07-24 발표")
    assert not is_duplicate_title("한도 1234억원 상향", "한도 1,234억원 상향")


def test_title_comparison_keeps_signs_separated_from_numbers():
    """부호를 숫자와 띄어 쓴 형태("수익률 - 3.5%")도 부호로 본다.

    공백만 보고 형식 문자로 지우면 부호가 반대인 본문 줄이 제목 중복으로 사라진다.
    """
    assert not is_duplicate_title("수익률 3.5% 기록", "수익률 - 3.5% 기록")
    assert not is_duplicate_title("수익률 - 3.5% 기록", "수익률 3.5% 기록")
    assert not is_duplicate_title("실적 2026724 발표", "실적 2026. 7. 24. 발표")
    # 같은 표기끼리는 여전히 중복이다.
    assert is_duplicate_title("수익률 - 3.5% 기록", "수익률 - 3.5% 기록")


def test_title_comparison_still_ignores_formatting_only_differences():
    """반대로 수치와 무관한 구분자·괄호 차이는 계속 같은 제목으로 본다."""
    assert is_duplicate_title("외부감사 규정 개정안.", "외부감사 규정 개정안")
    assert is_duplicate_title("외부감사 규정 - 개정안", "외부감사 규정 개정안")
    assert is_duplicate_title("수익률 3.5% 증가", "수익률 3.5% 증가")


def test_title_comparison_preserves_ascii_comparison_operators():
    assert not is_duplicate_title("자산 < 10억원 적용", "자산 > 10억원 적용")
    assert is_duplicate_title("자산 < 10억원 적용", "자산 < 10억원 적용")


def test_keeps_first_body_line_with_opposite_comparison_operator():
    body = "자산 < 10억원 적용\n세부 기준은 다음과 같다."
    assert build_fallback_snippet(body, "자산 > 10억원 적용").startswith(
        "자산 < 10억원 적용"
    )


def test_keeps_body_line_whose_numbers_differ_from_the_title():
    title = "수익률 3.5% 증가"
    body = "수익률 35% 증가\n세부 내용은 붙임과 같다."
    assert build_fallback_snippet(body, title).startswith("수익률 35% 증가")


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


def test_keeps_body_sentence_that_mentions_javascript_as_a_rule():
    """'자바스크립트' 는 규제 내용일 수 있다 — 브라우저 안내문 형태만 지운다."""
    for first in (
        "자바스크립트 사용 여부를 점검하도록 규정하였다.",
        "금융회사의 홈페이지는 자바스크립트를 활성화하도록 규정하였다.",
        "홈페이지에 자바스크립트를 적용한 비율은 82%였다.",
    ):
        out = build_fallback_snippet(f"{first}\n세부 기준은 붙임과 같다.", "보안 점검 기준")
        assert out.startswith(first), first


def test_strips_complete_javascript_browser_notices():
    """브라우저·설정 문맥을 갖추고 안내 종결로 끝나는 줄만 걷어낸다."""
    for notice in (
        "이 사이트는 자바스크립트를 지원하는 브라우저에서 최적화되어 있습니다.",
        "자바스크립트를 지원하지 않는 브라우저에서는 정상적으로 동작하지 않습니다.",
        "브라우저 설정에서 자바스크립트를 활성화해 주세요.",
    ):
        assert build_fallback_snippet(f"{notice}\n{BODY_SENTENCE}", TITLE) == (
            BODY_SENTENCE
        ), notice


def test_zero_width_characters_do_not_split_numbers():
    """제로폭 공백은 공백으로 바꾸지 않고 지운다(숫자가 갈라지면 안 된다)."""
    body = "과징금은 1,​234억원이다."
    assert build_fallback_snippet(body, "과징금 부과") == "과징금은 1,234억원이다."


def test_strips_breadcrumb_and_javascript_notice():
    body = "\n".join([
        "본문 바로가기",
        "홈 > 알림마당 > 보도자료",
        "이 사이트는 자바스크립트를 지원하는 브라우저에서 최적화되어 있습니다.",
        BODY_SENTENCE,
        "이 페이지에서 제공하는 정보에 만족하십니까?",
    ])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_keeps_body_paragraph_that_states_a_source_labeling_rule():
    """'출처를 표기' 는 실제 규제 내용이기도 하다 — 서술형 문장은 지우지 않는다."""
    body = "\n".join([
        "온라인 광고에는 자료의 출처를 표기해야 한다.",
        "위반 시 과태료 1,000만원을 부과한다.",
    ])
    out = build_fallback_snippet(body, "광고규제 개선방안")
    assert out.startswith("온라인 광고에는 자료의 출처를 표기해야 한다.")


def test_keeps_body_paragraph_that_mentions_an_embargo():
    body = "엠바고를 위반한 언론사에는 제재를 부과한다.\n세부 기준은 붙임과 같다."
    out = build_fallback_snippet(body, "보도 관련 제재 기준")
    assert out.startswith("엠바고를 위반한 언론사에는 제재를 부과한다.")


def test_strips_press_notice_only_in_request_form():
    """요청·안내 종결로 끝나는 배포 안내문만 걷어낸다."""
    body = "\n".join([
        "이 자료는 배포 즉시 보도할 수 있습니다.",
        BODY_SENTENCE,
        "※ 본 자료를 인용하여 보도할 경우 출처를 표기해 주시기 바랍니다.",
    ])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_keeps_body_paragraph_that_mentions_a_satisfaction_survey():
    """'만족도 조사' 는 실제 보도자료 본문에도 나오는 말 — 문장을 지우면 안 된다."""
    body = "\n".join([
        "금융감독원은 금융소비자 만족도 조사를 실시했다.",
        "조사 대상은 1,234명이며 만족 응답은 82%였다.",
    ])
    out = build_fallback_snippet(body, "금융소비자 만족도 조사 결과")
    assert out.startswith("금융감독원은 금융소비자 만족도 조사를 실시했다.")


def test_strips_satisfaction_footer_when_it_is_the_whole_line():
    body = "\n".join([
        BODY_SENTENCE,
        "이 페이지에서 제공하는 정보에 만족하십니까?",
        "만족도 조사",
    ])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_accepts_period_terminated_date_as_a_metadata_value():
    """"2026. 7. 24." 처럼 마침표로 끝나는 날짜도 라벨 값으로 인정해 함께 걷어낸다."""
    body = "\n".join(["등록일", "2026. 7. 24.", "조회수", "1,234", BODY_SENTENCE])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE

    body = "\n".join(["게시일", "2026년 7월 24일", BODY_SENTENCE])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_keeps_prose_that_merely_ends_with_a_filename():
    """산문이 파일명으로 끝나도 첨부 목록 행으로 오인하지 않는다."""
    body = "제출 파일명은 report.pdf\n서식은 홈페이지에서 받을 수 있다."
    out = build_fallback_snippet(body, "서식 안내")
    assert out.startswith("제출 파일명은 report.pdf")

    body = "신청 서식: 금융지원신청서.hwp\n접수는 8월 1일부터 시작한다."
    out = build_fallback_snippet(body, "지원 신청 안내")
    assert out.startswith("신청 서식: 금융지원신청서.hwp")


def test_strips_attachment_rows_in_attachment_list_context():
    """'첨부파일' 라벨 뒤라면 크기 표기가 없는 파일명도 여러 줄 걷어낸다."""
    body = "\n".join([
        "첨부파일", "보도자료.hwp", "1. 공고문 입법예고.hwpx", BODY_SENTENCE,
    ])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE

    # 꼬리말 쪽도 같다.
    body = "\n".join([BODY_SENTENCE, "첨부파일", "보도자료.hwp", "별첨.pdf"])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_keeps_filename_with_size_outside_attachment_context():
    sentence = "제출 파일은 report.pdf (10 MB)"
    assert build_fallback_snippet(f"{sentence}\n{BODY_SENTENCE}", TITLE).startswith(sentence)
    assert build_fallback_snippet(f"{BODY_SENTENCE}\n{sentence}", TITLE).endswith(sentence)


def test_strips_filename_with_size_in_attachment_context():
    body = f"첨부파일\nreport.pdf (10 MB)\n{BODY_SENTENCE}"
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE

    body = f"첨부파일: report.pdf (10 MB)\n{BODY_SENTENCE}"
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_label_without_a_value_does_not_swallow_the_next_label():
    """값이 비어 라벨만 잇달아 나와도 메타데이터가 발췌 앞에 남지 않아야 한다."""
    body = "\n".join(["첨부파일", "등록일", "2026-07-24", BODY_SENTENCE])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE

    body = "\n".join([
        "담당부서", "담당자", "연락처", "02-2100-2600", "조회수", "1,234", BODY_SENTENCE,
    ])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE


def test_keeps_short_heading_after_a_label_without_a_value():
    """값이 빈 라벨 뒤에 오는 짧은 소제목을 값으로 먹지 않는다.

    소제목은 사람 이름·부서명과 생김새가 같으므로(추진배경/홍길동, 기대효과/은행과)
    직급·전화 같은 사람 고유의 증거나 머리말 블록 문맥이 없으면 건드리지 않는다.
    """
    body = "담당부서\n대출한도 1억원 상향\n금융위원회는 서민금융 지원을 확대한다고 밝혔다."
    out = build_fallback_snippet(body, "서민금융 지원 확대")
    assert out.startswith("대출한도 1억원 상향")

    for label in ("담당자", "작성자", "담당", "담당부서", "부서"):
        for heading in ("추진배경", "주요내용", "검토의견", "기대효과", "추진 성과"):
            body = f"{label}\n{heading}\n금융위원회는 규정 개정을 추진한다고 밝혔다."
            out = build_fallback_snippet(body, "규정 개정 추진")
            assert out.startswith(heading), f"{label}/{heading} → {out}"


def test_keeps_heading_even_when_a_repeated_title_precedes_the_label():
    """제목 반복을 걷어낸 뒤라도 소제목을 부서명으로 보지 않는다.

    "주요성과"·"검토결과" 는 한 음절 부서 접미사('과')로 끝나 "은행과" 와 모양이 같다.
    그래서 모양이 아니라 구조를 본다 — 값 다음 줄이 또 다른 라벨일 때만 머리말로 본다.
    """
    for heading in ("주요성과", "검토결과", "기대효과", "추진경과", "신청", "출처"):
        for label in ("담당부서", "담당자", "부서", "작성자"):
            body = "\n".join([TITLE, label, heading, BODY_SENTENCE])
            out = build_fallback_snippet(body, TITLE)
            assert out.startswith(heading), f"{label}/{heading} → {out}"


def test_still_strips_label_values_that_match_the_label_shape():
    """반대로 라벨에 맞는 값이면 계속 두 줄 다 걷어낸다.

    부서명·사람 이름처럼 소제목과 생김새가 같은 값은 머리말 블록이라는 문맥 증거가
    있을 때만(여기서는 뒤따르는 '등록일' 라벨) 걷어낸다.
    """
    for value in (
        "기업회계팀",
        "금융위원회 기업회계팀",
        "제재심의국",
        "은행과",
        "금융투자검사3국",
    ):
        body = f"담당부서\n{value}\n등록일\n2026-07-24\n{BODY_SENTENCE}"
        assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE, value

    # 날짜·전화·조회수·직급은 형식만으로 메타데이터임이 드러나 문맥 증거가 필요 없다.
    for label, value in (
        ("담당자", "홍길동 사무관"),
        ("연락처", "02-2100-2600"),
        ("등록일", "2026. 7. 24."),
        ("조회수", "1,234"),
    ):
        body = f"{label}\n{value}\n{BODY_SENTENCE}"
        assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE, label


def test_lone_free_text_value_is_kept_without_block_evidence():
    """머리말 블록이라는 증거가 없으면 자유 텍스트 값은 건드리지 않는다.

    "은행과"(부서)와 "기대효과"(소제목), "홍길동"(이름)과 "추진배경"(소제목)은 생김새가
    같아 정규식으로 가를 수 없다. 홀로 선 라벨 뒤에서는 본문 소제목일 가능성을 우선해
    남겨 둔다 — 부서명이 발췌 앞에 한 조각 남는 쪽이, 사실이 통째로 사라지는 것보다 낫다.
    """
    out = build_fallback_snippet(f"담당부서\n기업회계팀\n{BODY_SENTENCE}", TITLE)
    assert out == f"기업회계팀 {BODY_SENTENCE}"


def test_inline_label_value_must_match_the_label_shape():
    """'라벨 : 값' 한 줄도 값 모양을 검증한다 — 라벨을 머리말로 쓴 문장을 지우지 않는다."""
    body = "문의: 대출 상한은 어떻게 해야 합니까?\n상한은 연소득의 3배로 제한된다."
    out = build_fallback_snippet(body, "대출 상한 안내")
    assert out.startswith("문의: 대출 상한은 어떻게 해야 합니까?")

    assert not is_label_line("담당부서 : 대출한도를 1억원으로 상향한다")
    assert not is_label_line("등록일 : 접수일로부터 30일 이내")
    # 라벨에 맞는 값이면 그대로 군더더기다.
    assert is_label_line("문의 : 02-2100-2600")
    assert is_label_line("문의 : 금융위원회 은행과 (02-2100-2000)")
    assert is_label_line("담당부서 : 금융위원회 기업회계팀")
    assert is_label_line("등록일 : 2026-07-24")
    assert is_label_line("조회수 : 1,234")
    # 값이 임의의 글 제목인 내비게이션 라벨은 값을 보지 않는다.
    assert is_label_line("이전글 : 은행법 시행령 개정안")


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
        "<div class='board-view-wrap'><dl><dt>담당부서</dt><dd>기업회계팀</dd>"
        "<dt>등록일</dt><dd>2026-07-24</dd></dl>"
        "<p>금융위원회는 &lt;주식회사 등의 외부감사에 관한 법률&gt; 개정안을 "
        "의결하였다.</p></div>"
    )
    out = build_fallback_snippet(body, TITLE)
    assert out == "금융위원회는 <주식회사 등의 외부감사에 관한 법률> 개정안을 의결하였다."


def test_keeps_decoded_literal_tag_examples():
    """스크래퍼가 이미 풀어 놓은 '글자 그대로의' 태그 표기를 파싱하지 않는다.

    "&lt;br&gt;" 는 get_text() 를 거치며 본문 글자 "<br>" 로 바뀐다. 태그처럼
    보인다고 파싱하면 "공시서식에는 <br> 태그를 사용할 수 없다" 가 주어를 잃는다.
    근거가 없으면 표기 그대로 두는 편이 낫다.
    """
    for raw in (
        "공시서식에는 &lt;br&gt; 태그를 사용할 수 없다.",
        '공시서식에는 &lt;img src="x"&gt; 태그를 넣을 수 없다.',
        "&lt;script&gt;alert(1)&lt;/script&gt; 태그는 공시서식에서 사용할 수 없다.",
        "공시서식에는 &lt;style&gt;body{color:red}&lt;/style&gt; 을 넣을 수 없다.",
    ):
        body = html.unescape(raw)
        assert build_fallback_snippet(body, "공시서식 안내") == body, raw


def test_keeps_multiple_literal_tag_examples_in_one_sentence():
    body = "허용되는 표기는 <br>, <img>, <span> 세 가지다."
    assert build_fallback_snippet(body, "허용 표기 안내") == body


def test_still_drops_script_and_style_in_real_markup():
    """반대로 속성·문서 골격이 있는 진짜 마크업이면 script/style 내용을 버린다."""
    body = (
        '<div class="board-view-wrap"><script type="text/javascript">var a=1;</script>'
        "<p>금융위원회는 규정을 의결하였다.</p></div>"
    )
    assert build_fallback_snippet(body, "규정 의결") == "금융위원회는 규정을 의결하였다."

    body = (
        "<html><body><script>var a=1;</script>"
        "<p>금융위원회는 규정을 의결하였다.</p></body></html>"
    )
    assert build_fallback_snippet(body, "규정 의결") == "금융위원회는 규정을 의결하였다."


def test_keeps_label_word_split_out_by_inline_formatting():
    """인라인 강조로 라벨 낱말이 떨어져 나온 줄은 지우지 않는다.

    스크래퍼의 get_text("\\n") 은 "<strong>등록일</strong>부터 30일 이내 …" 를
    "등록일" / "부터 30일 이내 …" 로 나눈다. 뒷줄이 조사로 시작하면 낱말이 잘린
    것이므로 앞줄을 라벨로 보면 안 된다.
    """
    for label, rest in (
        ("등록일", "부터 30일 이내 신고해야 한다."),
        ("담당자", "가 위반 사실을 보고해야 한다."),
        ("조회수", "는 공시 대상에서 제외한다."),
        ("첨부", "와 함께 제출해야 한다."),
    ):
        body = f"{label}\n{rest}"
        assert build_fallback_snippet(body, "무관한 제목입니다") == f"{label} {rest}"


def test_still_strips_bare_label_before_a_real_block():
    """반대로 뒷줄이 조사로 시작하지 않으면 값 없는 라벨은 그대로 걷어낸다."""
    body = "\n".join(["담당부서", "기업회계팀", "등록일", "2026-07-24", BODY_SENTENCE])
    assert build_fallback_snippet(body, TITLE) == BODY_SENTENCE

    body = "\n".join(["담당부서", "추진배경", BODY_SENTENCE])
    assert build_fallback_snippet(body, TITLE).startswith("추진배경")


def test_does_not_decode_already_decoded_body_text():
    """세미콜론 없는 옛 엔티티 표기까지 풀면 URL 이 깨진다.

    스크래퍼의 get_text() 가 이미 &amp; 를 풀어 놓았으므로, "?a=1&reg=2" 를 한 번 더
    풀면 "?a=1®=2" 가 된다(&copy·&sect·&times 도 같다).
    """
    body = "신청은 https://example.test/?a=1&reg=2 에서 가능하다."
    assert build_fallback_snippet(body, "신청 안내") == body

    body = "조회는 https://example.test/?x=1&copy=2&sect=3&times=4 이다."
    assert build_fallback_snippet(body, "조회 안내") == body


def test_still_decodes_unambiguous_entities():
    """세미콜론으로 끝나는 분명한 엔티티는 계속 해제한다."""
    body = "금융위원회는 &lt;은행법&gt; 개정안을 &amp; 시행령을 의결하였다."
    assert build_fallback_snippet(body, "은행법 개정") == (
        "금융위원회는 <은행법> 개정안을 & 시행령을 의결하였다."
    )


def test_inline_tags_do_not_break_numbers_or_word_boundaries():
    """인라인 태그 경계에 줄바꿈을 넣으면 수치와 어절이 훼손된다."""
    body = "<p>과징금은 1,<strong>234</strong>억원이다.</p>"
    assert build_fallback_snippet(body, "과징금 부과") == "과징금은 1,234억원이다."

    body = "<p><strong>금융위원회</strong>는 <em>3.5</em>% 인하를 의결하였다.</p>"
    assert build_fallback_snippet(body, "금리 인하 의결") == (
        "금융위원회는 3.5% 인하를 의결하였다."
    )


def test_block_nested_after_text_gets_a_boundary():
    """텍스트 뒤에 중첩된 블록 앞에도 경계가 있어야 어절이 붙지 않는다."""
    body = "<div><strong>주의</strong><p>금융위원회는 규정을 의결하였다.</p></div>"
    assert build_fallback_snippet(body, "규정 의결") == (
        "주의 금융위원회는 규정을 의결하였다."
    )

    body = "<div>머리말<ul><li>첫 항목</li><li>둘째 항목</li></ul></div>"
    assert build_fallback_snippet(body, "안내") == "머리말 첫 항목 둘째 항목"


def test_block_tags_still_separate_lines():
    """반대로 블록 요소·<br> 자리에서는 줄이 갈려야 한다(라벨/값 분리)."""
    body = (
        "<div><dl><dt>담당부서</dt><dd>제재심의국</dd>"
        "<dt>등록일</dt><dd>2026-07-24</dd></dl>"
        "<p>금융위원회는 1,<b>234</b>억원을 부과하였다.</p></div>"
    )
    assert build_fallback_snippet(body, "과징금 부과") == (
        "금융위원회는 1,234억원을 부과하였다."
    )
    assert build_fallback_snippet("<p>첫째 줄이다.<br/>둘째 줄이다.</p>", "무제") == (
        "첫째 줄이다. 둘째 줄이다."
    )


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


def test_keeps_short_substantive_body_after_edge_cleanup():
    body = "담당부서 : 금융위원회 기업회계팀\n등록일 : 2026-07-24\n접수 마감"
    assert build_fallback_snippet(body, TITLE) == "접수 마감"


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


# ==========================================================================
# 의안 전용 발췌 (build_assembly_fallback_lines)
#
# 의안 본문은 '제안이유 및 주요내용' 한 덩어리라 앞 220자에는 거의 항상 현행 제도
# 설명만 실린다. AI 요약이 실패한 날 그 메일이 쓸모없어지지 않도록, 의안만 원문에서
# 서로 다른 의미 구간을 골라 여러 줄로 싣는다. **원문 그대로의 결정적 발췌**이며
# 새 문장을 만들지 않는다.
# ==========================================================================
_BACKGROUND = "현행법은 가상자산사업자의 이용자 예치금 보호 의무를 명확히 규정하고 있지 아니함."
_MIDDLE = "그 결과 사업자 도산 시 이용자가 예치금을 회수하지 못하는 사례가 발생하고 있음."
_AMENDMENT = "이에 예치금을 은행에 별도 예치하도록 의무화하고 위반 시 과태료를 신설하려는 것임."


def _assembly_body(*sentences: str) -> str:
    return " ".join(sentences)


def test_assembly_fallback_empty_text_gives_no_lines():
    for empty in ("", "   \n\t", None, "···---···"):
        assert build_assembly_fallback_lines(empty) == []


def test_assembly_fallback_keeps_short_bodies_as_is():
    """한두 문장뿐인 본문은 그대로 싣는다(억지로 채우지 않는다)."""
    assert build_assembly_fallback_lines(_BACKGROUND) == [_BACKGROUND]
    assert build_assembly_fallback_lines(
        _assembly_body(_BACKGROUND, _AMENDMENT)
    ) == [_BACKGROUND, _AMENDMENT]


def test_assembly_fallback_returns_at_most_three_lines():
    body = _assembly_body(*([_MIDDLE] * 8), _AMENDMENT)
    lines = build_assembly_fallback_lines(body)
    assert 1 <= len(lines) <= 3


def test_assembly_fallback_picks_background_amendment_and_conclusion():
    filler = "관련 통계에 따르면 예치금 규모는 매년 증가하고 있음."
    body = _assembly_body(_BACKGROUND, filler, filler, _MIDDLE, filler, _AMENDMENT)
    lines = build_assembly_fallback_lines(body)
    assert lines[0] == _BACKGROUND          # 배경 / 현행 제도
    assert lines[-1] == _AMENDMENT          # "이에 … 하려는 것임" 계열 결론
    assert len(lines) == 3
    # 원문 순서를 보존한다
    assert [body.index(line) for line in lines] == sorted(body.index(l) for l in lines)


def test_assembly_fallback_dedupes_when_roles_collide():
    """첫 문장이 곧 입법행위 문장인 본문에서 같은 줄이 두 번 실리지 않는다."""
    tail = "관련 통계에 따르면 예치금 규모는 매년 증가하고 있음."
    body = _assembly_body(_AMENDMENT, tail, tail, tail)
    lines = build_assembly_fallback_lines(body)
    assert len(lines) == len(set(lines))
    assert lines[0] == _AMENDMENT


def test_assembly_fallback_respects_the_total_char_cap():
    body = _assembly_body(*[f"{i}번 문장으로서 " + "가" * 400 + "임." for i in range(5)])
    lines = build_assembly_fallback_lines(body)
    assert sum(len(line) for line in lines) <= ASSEMBLY_FALLBACK_CHARS
    # 상한을 직접 지정해도 지켜진다
    small = build_assembly_fallback_lines(body, max_total_chars=200)
    assert sum(len(line) for line in small) <= 200


def test_assembly_fallback_splits_a_single_giant_sentence():
    """문장 분리가 안 되는 한 덩어리는 머리 + 중략 표시 + 꼬리로 보여준다."""
    body = "머리부분 " * 60 + "중간내용 " * 200 + "결론부분 " * 40
    lines = build_assembly_fallback_lines(body)
    assert len(lines) == 2
    assert lines[0].startswith("머리부분")
    assert lines[0].endswith(ELLIPSIS)
    assert lines[1].startswith(OMISSION_MARK)
    assert lines[1].endswith("결론부분")
    assert sum(len(line) for line in lines) <= ASSEMBLY_FALLBACK_CHARS


def test_assembly_fallback_avoids_cutting_mid_word():
    body = "가나다라마바사아자차 " * 300
    lines = build_assembly_fallback_lines(body)
    head = lines[0][: -len(ELLIPSIS)].rstrip()
    assert head.endswith("가나다라마바사아자차")     # 어절 중간에서 끊기지 않는다
    tail = lines[1][len(OMISSION_MARK) :].strip()
    assert tail.startswith("가나다라마바사아자차")


def test_assembly_fallback_does_not_emit_markup():
    """원문에 태그가 섞여 있어도 헬퍼가 마크업을 만들어 내지 않는다(이스케이프는 notifier)."""
    body = "<p>현행법은 규정하지 아니함.</p><script>alert(1)</script><p>이에 개정하려는 것임.</p>"
    lines = build_assembly_fallback_lines(body)
    joined = " ".join(lines)
    assert "<" not in joined and ">" not in joined
    assert "현행법은 규정하지 아니함." in joined


def test_generic_fallback_snippet_behavior_is_unchanged():
    """일반 소스의 220자 한 줄 발췌 계약은 그대로다."""
    body = "가나다라마바사아자차 " * 60
    out = build_fallback_snippet(body, TITLE)
    assert out.endswith(f" {ELLIPSIS}")
    assert len(out) == SNIPPET_LIMIT + 2
    assert build_fallback_snippet(BODY_SENTENCE, TITLE) == BODY_SENTENCE
