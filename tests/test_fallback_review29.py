"""Final scoped regressions for negative applicability and date boundaries."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


CHANGE = "보고 의무를 신설한다."
TARGET = "적용 대상은 금융회사이다."
DATE = "2027년부터 시행한다."
FACT = "2026년 관련 거래 규모는 1,200억원이었다."


@pytest.mark.parametrize("target", [
    "은행에는 적용하지 않는다.", "금융회사는 적용되지 않는다.",
])
@pytest.mark.parametrize("title", ["", "관련 거래 규모 현황"])
def test_a_negative_applicability_survives_competing_numeric_background(target, title):
    rows = build_rule_excerpt_rows(f"{CHANGE}\n{DATE}\n{FACT}\n{target}", title)
    # DEFER: the target role is unsupported, but the complete exclusion survives.
    assert {text for _, text in rows} == {CHANGE, target, DATE}


@pytest.mark.parametrize("target", [
    "금융회사에 적용된다.", "금융회사에 적용됩니다.", "금융회사에 적용됨.",
])
def test_a_positive_applicability_is_preserved(target):
    assert build_rule_excerpt_rows(f"{CHANGE}\n{DATE}\n{FACT}\n{target}") == [
        ("변경 내용", CHANGE), ("대상·조건", target), ("시행·기한", DATE)]


@pytest.mark.parametrize("background", [
    "현행법은 은행에는 적용하지 않는다.",
    "과거 금융회사는 적용되지 않았다.",
    "은행에는 적용하지 않는 방안을 검토한다.",
])
def test_a_exclusion_does_not_promote_history_or_review(background):
    assert _role_strength("대상·조건", background) == 0
    assert build_rule_excerpt_rows(f"{CHANGE}\n{background}\n{TARGET}\n{DATE}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", DATE)]


@pytest.mark.parametrize("bound", ["이전에", "이후에", "부터", "이후", "후", "자로"])
def test_b_effective_boundary_survives_title_relevant_statistic(bound):
    date = f"2027년 1월 1일 {bound} 시행한다."
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{FACT}\n{date}", "관련 거래 규모 현황") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", date)]
    assert _role_strength("시행·기한", date) > 0


@pytest.mark.parametrize("date", ["2027년 1월 1일까지 제출한다.", "공포한 날부터 30일 이내 시행한다."])
def test_b_existing_deadlines_are_preserved(date):
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{FACT}\n{date}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", date)]


@pytest.mark.parametrize("bound", ["이전에", "이후에"])
@pytest.mark.parametrize("predicate", [
    "시행 여부를 검토한다.", "시행하지 않는다.", "시행했다.",
])
def test_b_date_boundary_does_not_bypass_existing_guards(bound, predicate):
    background = f"2027년 1월 1일 {bound} {predicate}"
    actual = "2028년부터 시행한다."
    assert _role_strength("시행·기한", background) == 0
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{background}\n{actual}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", actual)]
