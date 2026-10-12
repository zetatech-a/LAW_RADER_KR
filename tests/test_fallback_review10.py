"""Final closing regressions: review-only proposals and target predicates."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


PROPOSALS = [
    "금융규제를 완화하는 방안을 검토한다.",
    "기준을 강화하는 방안을 논의한다.",
    "의무를 폐지하는 계획을 연구한다.",
]


@pytest.mark.parametrize("proposal", PROPOSALS)
def test_review_only_proposal_does_not_displace_actual_change(proposal):
    body = proposal + " 보고 의무를 신설한다. 적용 대상은 은행이다. 2027년부터 시행한다."
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", "보고 의무를 신설한다."),
        ("대상·조건", "적용 대상은 은행이다."),
        ("시행·기한", "2027년부터 시행한다.")]
    assert _role_strength("변경 내용", proposal) < 6


@pytest.mark.parametrize("proposal", PROPOSALS)
def test_review_only_proposal_does_not_turn_statistics_into_policy(proposal):
    body = (proposal + " 온라인 판매액은 120억원으로 증가했다. "
            "판매 규모는 전월 대비 20억원 증가했다. 온라인 판매는 10월 8일부터 재개할 예정이다.")
    rows = build_rule_excerpt_rows(body, "8월 온라인 판매 실적")
    assert [label for label, _ in rows] == ["주요 현황", "세부 수치", "후속 일정"]


@pytest.mark.parametrize("change", [
    "금융규제를 완화한다.", "금융규제를 완화하였다.",
    "금융규제를 완화할 예정이다.", "보고 의무를 신설한다.",
    "기준을 강화하기로 결정했다.", "과징금을 부과한다.",
])
def test_actual_change_contract_is_preserved(change):
    assert _role_strength("변경 내용", change) == 6
    assert build_rule_excerpt_rows(change, "규제 개정") == [("변경 내용", change)]


def test_proposal_does_not_hide_separate_actual_change_in_same_sentence():
    sentence = "보고 의무를 신설하고 금융규제를 완화하는 방안을 검토한다."
    assert build_rule_excerpt_rows(sentence) == [("변경 내용", sentence)]


@pytest.mark.parametrize("target", [
    "은행만 대상이다.", "금융회사가 대상이다.", "사업자도 대상입니다.",
    "사업자는 대상입니다.", "보험사도 대상임.",
    "이 규정은 금융회사에 적용된다.",
])
def test_explicit_target_wins_over_unrelated_exception_history(target):
    body = f"보고 의무를 신설한다. 과거 예외 사례를 조사했다. {target} 2027년부터 시행한다."
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", "보고 의무를 신설한다."),
        ("대상·조건", target), ("시행·기한", "2027년부터 시행한다.")]
    assert _role_strength("대상·조건", target) > _role_strength(
        "대상·조건", "과거 예외 사례를 조사했다.")


def test_existing_target_prefix_keeps_its_role():
    sentence = "적용 대상은 금융회사다."
    assert _role_strength("대상·조건", sentence) > 0
    assert ("대상·조건", sentence) in build_rule_excerpt_rows(
        "보고 의무를 신설한다. " + sentence + " 2027년부터 시행한다.")


@pytest.mark.parametrize("sentence", [
    "대상 여부를 검토한다.", "대상 확대 방안을 논의한다.",
    "대상에 관한 연구를 진행했다.",
])
def test_target_mentions_are_not_explicit_applicability(sentence):
    assert _role_strength("대상·조건", sentence) == 0
