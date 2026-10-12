"""Local threshold grammar regression; semantic/statute/token coverage is deferred."""
import pytest

from src.snippet import _RULE_THRESHOLD, _role_strength, build_rule_excerpt_rows


@pytest.mark.parametrize("sentence", [
    "자산 100억원을 초과하는 경우에만 보고 의무가 적용된다.",
    "매출액 10억원을 초과하는 회사는 신고해야 한다.",
    "자산 50억원 이하인 기업이 대상이다.",
    "이용자 100명 이상인 사업자",
])
def test_declared_threshold_grammar_gets_condition_role(sentence):
    assert _RULE_THRESHOLD.search(sentence)
    assert _role_strength("대상·조건", sentence) > 0


def test_actual_threshold_displaces_unrelated_limit_background():
    threshold = "자산 100억원을 초과하는 경우에만 보고 의무가 적용된다."
    body = ("보고 의무를 신설한다. 대출한도 변동 현황을 조사했다. "
            f"{threshold} 2027년부터 시행한다.")
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", "보고 의무를 신설한다."),
        ("대상·조건", threshold),
        ("시행·기한", "2027년부터 시행한다."),
    ]


@pytest.mark.parametrize("comparator", ["이상", "이하", "초과", "미만"])
@pytest.mark.parametrize("connector", ["", "인", "의", "에 해당하는"])
def test_existing_comparators_and_connectors_remain_supported(comparator, connector):
    sentence = f"자산 100억원 {comparator}{connector} 회사"
    assert _RULE_THRESHOLD.search(sentence)
    assert _role_strength("대상·조건", sentence) > 0


def test_comparator_mention_without_eligibility_is_not_condition():
    sentence = "100억원 초과 여부를 검토한다."
    assert not _RULE_THRESHOLD.search(sentence)
    assert _role_strength("대상·조건", sentence) == 0
