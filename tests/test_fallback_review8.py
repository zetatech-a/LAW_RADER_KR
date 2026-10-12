"""Closing regressions: purpose predicates, ordered dedup, passive targets."""
import pytest

from src.snippet import _role_strength, build_key_excerpt_lines, build_rule_excerpt_rows


@pytest.mark.parametrize("purpose", [
    "금융규제를 완화하기 위한 간담회를 열었다.",
    "금융규제를 완화하기 위해 의견을 수렴한다.",
    "금융규제를 완화하고자 의견을 수렴한다.",
    "기준을 강화하고자 검토를 시작한다.",
    "제도를 개선하기 위한 연구를 진행한다.",
])
def test_purpose_clause_does_not_displace_actual_policy(purpose):
    change = "보고 의무를 신설한다."
    body = f"{purpose} {change} 적용 대상은 은행이다. 2027년부터 시행한다."
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", change), ("대상·조건", "적용 대상은 은행이다."),
        ("시행·기한", "2027년부터 시행한다.")]
    assert _role_strength("변경 내용", purpose) == 0


@pytest.mark.parametrize("change", [
    "규제를 완화한다.", "규제를 완화하였다.", "보고 의무를 신설한다.",
    "기준을 강화한다.", "과징금을 부과한다.",
    "규제를 완화하기로 결정했다.", "규제를 완화할 예정이다.",
])
def test_actual_decisions_and_changes_keep_change_strength(change):
    assert _role_strength("변경 내용", change) > 0
    assert ("변경 내용", change) in build_rule_excerpt_rows(change, "규제 개정")


def test_future_change_keeps_policy_roles_without_title_hint():
    sentence = "금융규제를 완화할 예정이다."
    assert build_rule_excerpt_rows(sentence) == [("변경 내용", sentence)]


def test_purpose_clause_does_not_hide_actual_change_in_same_sentence():
    sentence = "규제를 완화하기 위해 보고 의무를 신설한다."
    assert build_rule_excerpt_rows(sentence) == [("변경 내용", sentence)]


def test_opposite_object_action_policies_survive_dedup():
    first = "은행은 대출을 허용하고 예금을 금지한다."
    second = "은행은 예금을 허용하고 대출을 금지한다."
    date = "2027년부터 시행한다."
    rows = build_rule_excerpt_rows(f"{first} {second} {date}")
    assert {text for _, text in rows} == {first, second, date}


@pytest.mark.parametrize("second", [
    "금융위는 보고 의무를 강화한다.",
    "금융위가 해당 보고 의무를 강화할 예정이다.",
])
def test_normalized_announcements_still_deduplicate(second):
    first = "금융위원회는 보고 의무를 강화한다고 밝혔다."
    assert len(build_key_excerpt_lines(first + " " + second)) == 1


@pytest.mark.parametrize("target", [
    "이 규정은 금융회사에 적용된다.",
    "해당 기준은 은행에 적용된다.",
    "이 제도는 사업자에게 적용된다.",
])
def test_passive_applicability_wins_target_slot(target):
    assert _role_strength("대상·조건", target) > 0
    body = f"보고 의무를 신설한다. 과거 예외 사례를 조사했다. {target} 2027년부터 시행한다."
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", "보고 의무를 신설한다."), ("대상·조건", target),
        ("시행·기한", "2027년부터 시행한다.")]


def test_passive_target_guard_does_not_expand_to_arbitrary_nouns():
    assert _role_strength("대상·조건", "이 규정은 시스템에 적용된다.") == 0
    # '기준은'은 기존 조건 어휘이며 새 수동형 대상의 우선 점수를 받지 않는다.
    assert _role_strength("대상·조건", "이 기준은 시스템에 적용된다.") == 4
    assert _role_strength("대상·조건", "적용 대상은 금융회사다.") > 0
