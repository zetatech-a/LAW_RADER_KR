"""Closing regressions for ongoing states and dated sales facts."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


@pytest.mark.parametrize("state", [
    "은행은 현재 온라인 판매가 금지되어 있다.",
    "현 제도에서는 해당 행위가 허용되어 있다.",
    "보험사는 현재 일부 의무가 면제되어 있다.",
    "현재 해당 행위는 허용되어 있다.",
])
def test_ongoing_state_does_not_displace_new_change(state):
    change = "보험회사의 보고 의무를 신설한다."
    body = f"{state} {change} 적용 대상은 은행이다. 2027년부터 시행한다."
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", change), ("대상·조건", "적용 대상은 은행이다."),
        ("시행·기한", "2027년부터 시행한다.")]
    assert _role_strength("변경 내용", state) == 0
    assert all(label != "변경 내용" for label, _ in build_rule_excerpt_rows(state))


@pytest.mark.parametrize("change", [
    "온라인 판매를 금지한다.", "온라인 판매가 금지된다.",
    "온라인 판매를 금지하였다.", "온라인 판매를 금지할 예정이다.",
    "보고 의무를 신설한다.", "기준을 강화한다.",
    "이번 개정안은 현재보다 기준을 강화한다.",
    "현재 온라인 판매는 금지되어 있으나 2027년부터 온라인 판매를 허용한다.",
])
def test_actual_changes_keep_original_text(change):
    assert _role_strength("변경 내용", change) > 0
    assert build_rule_excerpt_rows(change, "규제 개정") == [("변경 내용", change)]


@pytest.mark.parametrize("fact", [
    "9월 판매량은 100억원이었다.", "9월 판매량은 100억원이다.",
    "8월 판매액은 300억원이었다.", "8월 판매액은 전월보다 감소했다.",
    "2026년 판매 실적은 500억원이었다.", "9월 판매 현황은 다음과 같다.",
])
def test_dated_sales_facts_are_not_schedules(fact):
    assert _role_strength("후속 일정", fact) == 0
    assert _role_strength("시행·기한", fact) == 0
    rows = build_rule_excerpt_rows(fact + " 전월보다 10% 증가하였다.", "9월 온라인 판매 실적")
    assert fact in [text for _, text in rows]
    assert all(label not in ("후속 일정", "시행·기한") for label, _ in rows)


@pytest.mark.parametrize("schedule", [
    "10월 1일부터 판매한다.", "10월부터 판매를 시작한다.",
    "판매는 10월 31일까지 진행한다.", "2027년 1월부터 판매를 재개할 예정이다.",
    "10월 1일 판매 개시 예정이다.",
])
def test_actual_sales_schedules_remain_selected(schedule):
    assert _role_strength("후속 일정", schedule) == 6
    assert _role_strength("시행·기한", schedule) == 6
    rows = build_rule_excerpt_rows(
        "9월 판매량은 100억원이었다. 전월보다 10% 증가하였다. " + schedule,
        "9월 온라인 판매 실적")
    assert ("후속 일정", schedule) in rows
