"""Closing regressions for exception evidence, modality dedup, and dated events."""
import pytest

from src.snippet import _duplicate_signature, _role_strength, build_rule_excerpt_rows


CHANGE = "보고 의무를 신설한다."
TARGET = "적용 대상은 은행이다."
DATE = "2027년부터 시행한다."


@pytest.mark.parametrize("prefix", ["다만", "단,", "예외"])
def test_exception_prefix_alone_does_not_displace_actual_target(prefix):
    announcement = f"{prefix} 구체적인 내용은 추후 발표할 예정이다."
    assert build_rule_excerpt_rows(f"{CHANGE}\n{announcement}\n{TARGET}\n{DATE}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", DATE)]
    assert _role_strength("대상·조건", announcement) == 0


@pytest.mark.parametrize("exception", [
    "다만 금융회사는 제외한다.", "단, 금융회사는 제외한다.",
    "예외 조건은 매출 10억원 미만이다.",
    "다만 자산 100억원 미만인 회사는 적용 대상에서 제외한다.",
])
def test_existing_substantive_exceptions_keep_priority(exception):
    assert _role_strength("대상·조건", exception) == 8
    assert build_rule_excerpt_rows(f"{CHANGE}\n{exception}\n{TARGET}\n{DATE}")[1] == (
        "대상·조건", exception)


def test_discretionary_and_mandatory_actions_have_distinct_signatures():
    optional = "은행은 보고 의무를 신설할 수 있다."
    mandatory = "은행은 보고 의무를 신설한다."
    assert _duplicate_signature(optional) != _duplicate_signature(mandatory)


def test_discretionary_action_does_not_remove_mandatory_candidate():
    sentences = ["은행은 보고 의무를 신설할 수 있다.", "은행은 보고 의무를 신설한다."]
    assert {text for _, text in build_rule_excerpt_rows("\n".join(sentences))} == set(sentences)


def test_repeated_mandatory_action_is_still_deduplicated():
    sentence = "은행은 보고 의무를 신설한다."
    assert build_rule_excerpt_rows(sentence + "\n" + sentence) == [("변경 내용", sentence)]


@pytest.mark.parametrize("section", ["", "시행·기한"])
def test_generic_month_day_event_is_not_effective_schedule(section):
    sentence = "10.15.에 간담회 개최."
    assert _role_strength("시행·기한", sentence, section) == 0
    heading = "시행 일정\n" if section else ""
    rows = build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{heading}{sentence}")
    assert not any(label == "시행·기한" for label, _ in rows)
    assert sentence in [text for _, text in rows]


@pytest.mark.parametrize("sentence", [
    "10.15.부터 시행한다.", "10.15.에 적용한다.",
])
def test_existing_month_day_schedule_evidence_is_preserved(sentence):
    assert _role_strength("시행·기한", sentence) > 0
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{sentence}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", sentence)]


def test_existing_effective_date_label_still_supplies_schedule_evidence():
    assert _role_strength("시행·기한", "시행일은 10.15.이다.") > 0
