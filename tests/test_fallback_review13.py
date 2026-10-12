"""Closing regressions for active states, exception reviews and event schedules."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


@pytest.mark.parametrize("state", [
    "은행은 현재 온라인 판매를 금지하고 있다.",
    "현재 해당 행위를 허용하고 있다.",
    "현재 보고 의무를 면제하고 있다.",
    "은행은 현재 온라인 판매를 금지하고 있는 상태다.",
])
def test_active_state_does_not_displace_new_change(state):
    change = "보험회사의 보고 의무를 신설한다."
    assert build_rule_excerpt_rows(
        f"{state} {change} 적용 대상은 은행이다. 2027년부터 시행한다.") == [
            ("변경 내용", change), ("대상·조건", "적용 대상은 은행이다."),
            ("시행·기한", "2027년부터 시행한다.")]
    assert _role_strength("변경 내용", state) == 0


@pytest.mark.parametrize("change", [
    "온라인 판매를 금지한다.", "온라인 판매를 금지하였다.",
    "온라인 판매를 금지할 예정이다.", "보고 의무를 신설한다.", "기준을 강화한다.",
    "현재 온라인 판매를 금지하고 있으나 2027년부터 온라인 판매를 허용한다.",
])
def test_actual_change_is_preserved_verbatim(change):
    assert build_rule_excerpt_rows(change, "규제 개정") == [("변경 내용", change)]


@pytest.mark.parametrize("review", [
    "예외 적용 여부를 검토한다.", "예외 인정 여부를 논의한다.",
    "예외 확대 방안을 검토한다.", "대상 여부를 검토한다.",
])
def test_exception_review_does_not_displace_actual_target(review):
    assert build_rule_excerpt_rows(
        f"보고 의무를 신설한다. {review} 적용 대상은 은행이다. 2027년부터 시행한다.") == [
            ("변경 내용", "보고 의무를 신설한다."),
            ("대상·조건", "적용 대상은 은행이다."), ("시행·기한", "2027년부터 시행한다.")]
    assert _role_strength("대상·조건", review) == 0


@pytest.mark.parametrize("condition", [
    "예외 대상은 소규모 회사다.", "소규모 회사는 예외로 인정한다.",
    "다만 금융회사는 적용 대상에서 제외한다.", "예외 조건은 매출 10억원 미만이다.",
])
def test_actual_exception_conditions_are_preserved(condition):
    assert _role_strength("대상·조건", condition) > 0
    assert ("대상·조건", condition) in build_rule_excerpt_rows(
        f"보고 의무를 신설한다. {condition} 2027년부터 시행한다.")


@pytest.mark.parametrize("schedule", [
    "10월 모집 예정이다.", "10월 1일 모집 예정이다.", "10월부터 모집한다.",
    "10월 공표할 예정이다.", "결과는 10월 15일 공표 예정이다.",
])
def test_existing_event_actions_with_future_evidence_keep_schedule_slot(schedule):
    assert _role_strength("시행·기한", schedule) > 0
    assert build_rule_excerpt_rows(
        "보고 의무를 신설한다. 적용 대상은 금융회사다. "
        "은행의 공급 규모는 100억원이다. " + schedule) == [
            ("변경 내용", "보고 의무를 신설한다."),
            ("대상·조건", "적용 대상은 금융회사다."), ("시행·기한", schedule)]


@pytest.mark.parametrize("fact", [
    "10월 판매량은 100억원이었다.", "10월 모집 인원은 500명이었다.",
    "9월 공표 건수는 30건이었다.",
])
def test_event_statistics_are_not_schedules(fact):
    assert _role_strength("시행·기한", fact) == 0
    assert _role_strength("후속 일정", fact) == 0
