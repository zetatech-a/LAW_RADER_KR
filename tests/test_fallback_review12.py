"""Closing regressions for literal changes, bounds, history and section metadata."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


@pytest.mark.parametrize("change", [
    "보고 주기를 월별로 변경한다.", "보고 주기를 월별로 변경하였다.",
    "보고 주기를 월별로 변경할 예정이다.",
])
def test_literal_change_is_preserved(change):
    body = f"과거 수치는 100억원이었다. {change} 적용 대상은 금융회사다. 2027년부터 시행한다."
    assert build_rule_excerpt_rows(body, "보고 체계 개정") == [
        ("변경 내용", change), ("대상·조건", "적용 대상은 금융회사다."),
        ("시행·기한", "2027년부터 시행한다.")]
    assert _role_strength("변경 내용", change) == 6


def test_literal_change_selects_policy_without_title_hint():
    body = ("과거 수치는 100억원이었다. 보고 주기를 월별로 변경한다. "
            "적용 대상은 금융회사다. 2027년부터 시행한다.")
    assert build_rule_excerpt_rows(body)[0] == ("변경 내용", "보고 주기를 월별로 변경한다.")


@pytest.mark.parametrize("proposal", [
    "보고 주기 변경 여부를 검토한다.", "보고 주기를 변경하는 방안을 검토한다.",
    "보고 주기를 변경하기 위한 간담회를 열었다.", "보고 주기가 변경되어 있다.",
])
def test_literal_change_does_not_promote_proposals_or_states(proposal):
    assert _role_strength("변경 내용", proposal) < 6
    rows = build_rule_excerpt_rows(
        proposal + " 보고 의무를 신설한다. 적용 대상은 은행이다. 2027년부터 시행한다.")
    assert rows[0] == ("변경 내용", "보고 의무를 신설한다.")


@pytest.mark.parametrize("deadline", [
    "신청일로부터 30일 이내에 서류를 제출한다.",
    "계약일로부터 3개월 이내 보고서를 제출한다.",
])
def test_within_deadline_wins_over_numeric_background(deadline):
    body = ("보고 의무를 신설한다. 적용 대상은 금융회사다. "
            "은행의 공급 규모는 100억원이다. " + deadline)
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", "보고 의무를 신설한다."),
        ("대상·조건", "적용 대상은 금융회사다."), ("시행·기한", deadline)]
    assert _role_strength("시행·기한", deadline) == 6


def test_statistical_period_is_not_a_deadline():
    assert _role_strength("시행·기한", "최근 30일 이내 발생 건수는 100건이었다.") == 0


@pytest.mark.parametrize("history", [
    "과거에는 은행의 온라인 판매를 금지했다.",
    "예전에는 은행의 온라인 판매를 허용했다.",
    "과거 은행의 온라인 판매를 금지했다.",
    "예전 은행의 온라인 판매를 허용했다.",
])
def test_explicit_history_does_not_displace_new_change(history):
    change = "보험회사의 보고 의무를 신설한다."
    assert build_rule_excerpt_rows(
        f"{history} {change} 적용 대상은 금융회사다. 2027년부터 시행한다.",
        "보고 의무 신설") == [
            ("변경 내용", change), ("대상·조건", "적용 대상은 금융회사다."),
            ("시행·기한", "2027년부터 시행한다.")]


def test_history_word_inside_actual_change_is_preserved():
    sentence = "이번 개정안은 과거보다 기준을 강화한다."
    assert build_rule_excerpt_rows(sentence) == [("변경 내용", sentence)]


@pytest.mark.parametrize("headings", [
    ("추진 배경", "변경 내용"), ("변경 내용", "추진 배경"),
    ("변경 내용", "변경 내용"), ("", "변경 내용"),
])
def test_exact_repeat_keeps_explicit_section_without_duplicate_text(headings):
    change = "보고 의무를 신설한다."
    body = (f"{headings[0]}\n{change}\n{headings[1]}\n{change}\n"
            "추진 배경\n은행의 공급 규모는 100억원이다.\n"
            "적용 대상\n금융회사다.\n시행 일정\n2027년부터 시행한다.")
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", change), ("대상·조건", "금융회사다."),
        ("시행·기한", "2027년부터 시행한다.")]
