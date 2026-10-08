"""Assembly selection must reuse existing explicit-change polarity guards."""
import pytest

from src.snippet import build_assembly_fallback_lines


BACKGROUND = "제도 운영의 배경을 설명함."
NEGATED = "보고 의무를 신설하지 않는다."
CHANGE = "검사 의무를 강화하려는 것임."
CONCLUSION = "국민의 편의를 높이려는 것임."


def test_actual_assembly_amendment_displaces_negated_action():
    body = f"{BACKGROUND} {NEGATED} {CHANGE} 관계 기관과 절차를 협의함. {CONCLUSION}"
    assert build_assembly_fallback_lines(body) == [BACKGROUND, CHANGE, CONCLUSION]


@pytest.mark.parametrize("amendment", [
    CHANGE,
    "보고 의무를 신설하지 않고 검사 의무를 강화한다.",
    "신고절차 신설 및 과태료 상향임.",
])
def test_existing_affirmative_mixed_and_nominal_amendments_are_preserved(amendment):
    body = f"{BACKGROUND} 관련 현황을 설명함. {amendment} 관계 기관과 절차를 협의함. {CONCLUSION}"
    assert build_assembly_fallback_lines(body) == [BACKGROUND, amendment, CONCLUSION]


@pytest.mark.parametrize("first, second", [
    (CHANGE, "이에 신고 의무를 신설하려는 것임."),
    ("신고절차 신설 및 과태료 상향임.", CHANGE),
])
def test_same_priority_keeps_first_amendment(first, second):
    body = f"{BACKGROUND} {first} {second} 관계 기관과 절차를 협의함. {CONCLUSION}"
    assert build_assembly_fallback_lines(body) == [BACKGROUND, first, CONCLUSION]


@pytest.mark.parametrize("second, third, expected", [
    ("둘째 문장임.", "셋째 문장임.", "셋째 문장임."),
    ("현행 절차를 규정하고 있음.", "관련 현황을 설명함.", "현행 절차를 규정하고 있음."),
    ("이에 신고 절차를 조정하도록 하려는 것임.", "관련 현황을 설명함.",
     "이에 신고 절차를 조정하도록 하려는 것임."),
])
def test_existing_middle_weak_and_medium_fallbacks_are_unchanged(second, third, expected):
    body = f"{BACKGROUND} {second} {third} 관계 기관과 절차를 협의함. {CONCLUSION}"
    assert build_assembly_fallback_lines(body) == [BACKGROUND, expected, CONCLUSION]
