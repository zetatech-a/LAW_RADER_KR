"""Closing regressions for history-prefix guards and compact Korean lists."""
import pytest

from src.snippet import build_key_excerpt_lines, build_rule_excerpt_rows


POLICY_ROWS = [
    ("변경 내용", "보고 의무를 신설한다."),
    ("대상·조건", "적용 대상은 금융회사이다."),
    ("시행·기한", "2027년부터 시행한다."),
]


@pytest.mark.parametrize("prefix", ["", "다만, ", "다만 ", "단, ", "단 "])
@pytest.mark.parametrize("history", [
    "현행법은 소규모 회사에 예외를 인정한다.",
    "현행 제도는 소규모 사업자를 제외한다.",
    "현재 제도는 소규모 사업자를 제외한다.",
    "종전에는 소규모 사업자를 제외한다.",
])
def test_historical_exception_does_not_displace_actual_target(prefix, history):
    body = prefix + history + " " + " ".join(text for _, text in POLICY_ROWS)
    assert build_rule_excerpt_rows(body) == POLICY_ROWS


def test_current_policy_exception_keeps_original_text_and_target_priority():
    exception = "다만, 소규모 회사는 적용 대상에서 제외한다."
    body = " ".join(text for _, text in POLICY_ROWS) + " " + exception
    assert build_rule_excerpt_rows(body) == [
        POLICY_ROWS[0], ("대상·조건", exception), POLICY_ROWS[2]]


def test_current_law_mention_inside_new_clause_is_not_history():
    change = "다만, 개정안은 현행법과 달리 금융회사에 의무를 신설한다."
    body = change + " " + " ".join(text for _, text in POLICY_ROWS[1:])
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", change), *POLICY_ROWS[1:]]


@pytest.mark.parametrize("markers", [
    ("가.", "나.", "다."), ("가)", "나)", "다)"),
    ("가. ", "나. ", "다. "), ("가) ", "나) ", "다) "),
    ("Ⅰ. ", "Ⅱ. ", "Ⅲ. "), ("①", "②", "③"),
])
def test_list_markers_reset_background_and_preserve_substantive_rows(markers):
    body = "추진 배경\n현행 제도를 설명한다.\n" + "\n".join(
        marker + text for marker, (_, text) in zip(markers, POLICY_ROWS))
    assert build_rule_excerpt_rows(body) == POLICY_ROWS


@pytest.mark.parametrize("marker", ["{n}.", "{n})", "{n}. "])
def test_existing_arabic_textual_items_keep_all_contents(marker):
    contents = ["안내 사항입니다.", "신청 방법입니다.", "문의처입니다."]
    body = "\n".join(marker.format(n=i) + text for i, text in enumerate(contents, 1))
    assert build_key_excerpt_lines(body) == contents


@pytest.mark.parametrize("sentence", [
    "1.5% 증가했다.", "3.14를 기준으로 계산한다.",
    "2026.10.2.부터 시행한다.",
])
def test_numeric_facts_remain_intact(sentence):
    assert build_key_excerpt_lines(sentence) == [sentence]
