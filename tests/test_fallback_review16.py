"""Closing regressions for existing schedule, assembly priority and title contracts."""
import pytest

from src.snippet import (
    _excerpt_roles, _is_explicit_change, _role_strength,
    build_assembly_fallback_lines, build_rule_excerpt_rows,
)


CHANGE = "보고 의무를 신설한다."
TARGET = "적용 대상은 은행이다."
DATE = "2027년부터 시행한다."
BACKGROUND = "제도 운영 현황을 설명함."
CONCLUSION = "국민의 편의를 높이려는 것임."
AMENDMENT = "보험회사에 보고 의무를 신설하려는 것임."
STATUTE = "은행법은 은행의 온라인 판매를 금지한다."


def test_dated_future_change_displaces_historical_sales_fact():
    schedule = "새 제도는 2027년 도입할 예정이다."
    body = f"{CHANGE} 2025년 은행의 판매액은 100억원으로 증가했다. {TARGET} {schedule}"
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", schedule)]


@pytest.mark.parametrize("schedule", [
    "2027년 도입할 예정이다.", "2028년 폐지할 예정이다.",
    "2028년 폐지할 계획이다.", "2027년 적용 대상을 확대할 계획이다.",
])
def test_existing_future_regulatory_actions_get_schedule_role(schedule):
    assert _role_strength("시행·기한", schedule) > 0
    assert _role_strength("후속 일정", schedule) > 0
    # 별도 변경 문장이 없으면 일정이기도 한 유일한 변경을 버리지 않는다.
    assert build_rule_excerpt_rows(schedule) == [("변경 내용", schedule)]


@pytest.mark.parametrize("sentence", [
    "2025년 제도를 확대하였다.", "2025년 도입 실적은 30건이었다.",
    "2025년 폐지 사례를 조사했다.", "도입하는 방안을 검토한다.",
    "2025년 도입할 예정이었다.", "도입할 예정이었다.",
])
def test_regulatory_words_without_actual_future_change_are_not_schedules(sentence):
    assert _role_strength("시행·기한", sentence) == 0


@pytest.mark.parametrize("amendment", [AMENDMENT, "신고절차 신설 및 과태료 상향임."])
def test_assembly_amendment_intent_beats_named_statute_background(amendment):
    body = f"{BACKGROUND} {STATUTE} {amendment} 관련 절차를 함께 정비함. {CONCLUSION}"
    assert build_assembly_fallback_lines(body) == [BACKGROUND, amendment, CONCLUSION]


def test_assembly_keeps_statutory_background_when_it_is_the_first_line():
    body = f"{STATUTE} 관련 현황을 설명함. {AMENDMENT} 관련 절차를 함께 정비함. {CONCLUSION}"
    assert build_assembly_fallback_lines(body) == [STATUTE, AMENDMENT, CONCLUSION]


def test_assembly_history_guard_still_applies_to_high_priority_candidates():
    body = (f"{BACKGROUND} 현행법은 보고 의무를 신설함. "
            f"{AMENDMENT} 관련 절차를 함께 정비함. {CONCLUSION}")
    assert build_assembly_fallback_lines(body) == [BACKGROUND, AMENDMENT, CONCLUSION]


@pytest.mark.parametrize("first, second", [
    (AMENDMENT, "이에 기준을 강화하려는 것임."),
    ("신고절차 신설 및 과태료 상향임.", AMENDMENT),
    ("등록 제도를 개정함.", "별도 의무를 신설함."),
    ("온라인 판매를 금지한다.", "보고 기준을 강화한다."),
])
def test_assembly_same_priority_preserves_original_order(first, second):
    body = f"{BACKGROUND} {first} 관련 현황을 설명함. {second} {CONCLUSION}"
    assert build_assembly_fallback_lines(body) == [BACKGROUND, first, CONCLUSION]


def test_assembly_without_amendment_signal_keeps_middle_fallback():
    body = f"{BACKGROUND} 둘째 문장임. 셋째 문장임. 넷째 문장임. {CONCLUSION}"
    assert build_assembly_fallback_lines(body) == [BACKGROUND, "셋째 문장임.", CONCLUSION]


@pytest.mark.parametrize("title", ["은행법 개정안", "보고제도 변경안", "은행법 개정", "개정안 발표"])
def test_title_change_signal_keeps_nominal_change_over_statistic(title):
    nominal = "보고 의무 신설."
    body = f"2025년 금융회사 판매액은 100억원으로 20% 증가했다. {nominal} {TARGET} {DATE}"
    assert build_rule_excerpt_rows(body, title) == [
        ("변경 내용", nominal), ("대상·조건", TARGET), ("시행·기한", DATE)]


def test_hangul_title_boundary_is_preserved():
    assert _excerpt_roles("개정안내", ["관련 내용을 안내한다."], [""]) == ()


def test_title_suffix_does_not_change_body_action_classification():
    sentence = "개정안을 검토한다."
    assert not _is_explicit_change(sentence)
    assert _role_strength("변경 내용", sentence) == 0
    assert _excerpt_roles("기관 안내", [sentence], [""]) == ()
