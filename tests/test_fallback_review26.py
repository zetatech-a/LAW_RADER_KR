"""Prefer amendment evidence without bypassing existing section/history guards."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


CHANGE = "보고 의무를 신설한다."
EXCEPTION = "다만 소규모 회사는 제외한다."
DATE = "2027년부터 시행한다."


@pytest.mark.parametrize("target", [
    "적용 대상은 금융회사이다.", "적용 대상은 자산 100억원 이상인 금융회사이다.",
])
def test_change_heading_does_not_let_conditions_displace_amendment(target):
    body = f"변경 내용\n{CHANGE}\n{target}\n{EXCEPTION}\n{DATE}"
    rows = build_rule_excerpt_rows(body)
    assert rows[0] == ("변경 내용", CHANGE)
    assert rows[1][0] == "대상·조건" and rows[1][1] in (target, EXCEPTION)
    assert rows[2] == ("시행·기한", DATE)


@pytest.mark.parametrize("sentence", ["보고 의무 신설.", "보고 절차 안내."])
def test_change_heading_keeps_nominal_and_no_evidence_fallback(sentence):
    assert build_rule_excerpt_rows("변경 내용\n" + sentence) == [("변경 내용", sentence)]


@pytest.mark.parametrize("sentence", [
    "금융규제를 완화하는 방안을 검토한다.",
    "현행법은 보고 의무를 부과한다.",
    "보고 의무를 신설하지 않는다.",
    "은행은 현재 온라인 판매가 금지되어 있다.",
])
def test_change_heading_does_not_bypass_existing_guards(sentence):
    assert _role_strength("변경 내용", sentence, "변경 내용") == 0
    rows = build_rule_excerpt_rows(f"변경 내용\n{sentence}\n{CHANGE}\n{EXCEPTION}\n{DATE}")
    assert rows[0] == ("변경 내용", CHANGE)


@pytest.mark.parametrize("history", [
    "지난 1년간 판매액은 100억원이었다.",
    "지난 1년간 지원 대상 100명을 조사했다.",
    "당시 고객 100명에게 지원금을 지급했다.",
])
def test_historical_facts_remain_outside_current_eligibility(history):
    assert _role_strength("대상·조건", history) == 0
    rows = build_rule_excerpt_rows(f"{CHANGE}\n{history}\n적용 대상은 은행이다.\n{DATE}")
    assert rows == [("변경 내용", CHANGE), ("대상·조건", "적용 대상은 은행이다."), ("시행·기한", DATE)]
