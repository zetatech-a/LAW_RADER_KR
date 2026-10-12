"""Bounded fixes for as-of dates, existing monetary thresholds, and unlimited service."""
import pytest

from src.snippet import (
    _KEY_ACTION, _RULE_CHANGE, _RULE_CHANGE_TITLE, _role_strength, build_rule_excerpt_rows,
)


CHANGE = "보고 의무를 신설한다."
TARGET = "적용 대상은 금융회사이다."
DATE = "2027년부터 시행한다."


@pytest.mark.parametrize("date", ["2027년 1월 1일자로 시행한다.", "2027년 1월 1일 자로 시행한다."])
def test_a_effective_date_is_not_displaced_by_title_relevant_statistic(date):
    body = f"{CHANGE}\n{TARGET}\n2026년 기준 관련 거래 규모는 1,200억원이다.\n{date}"
    assert build_rule_excerpt_rows(body, "관련 거래 규모 현황") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", date)]


@pytest.mark.parametrize("date", [
    "2027년 1월 1일자로 시행한다.", "2027년 1월 1일 자로 시행한다.",
    "2027년 1월 1일부터 시행한다.", "2027년 1월 1일에 시행한다.",
    "2027년 1월 1일 이후 시행한다.", "2027년 1월 1일 후 시행한다.",
])
def test_a_effective_date_survives_competing_historical_statistic(date):
    assert _role_strength("시행·기한", date) > 0
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n2026년 기준 관련 거래 규모는 1,200억원이다.\n{date}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", date)]


@pytest.mark.parametrize("sentence", [
    "2027년 1월 1일자로 시행 여부를 검토한다.",
    "2027년 1월 1일 자로 시행하는 방안을 검토한다.",
    "2027년 1월 1일자로 시행할 필요가 있다.",
    "2027년 1월 1일자로 시행할 수 있다.",
    "2027년 1월 1일자로 시행했다.",
    "2027년 1월 1일자로 시행하지 않는다.",
])
def test_a_as_of_date_does_not_bypass_existing_guards(sentence):
    assert _role_strength("시행·기한", sentence) == 0
    actual = "2028년부터 시행한다."
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{sentence}\n{actual}")[-1] == ("시행·기한", actual)


@pytest.mark.parametrize("condition", [
    "매출 500만원 이하인 사업자는 적용에서 제외한다.",
    "매출 500만원 이하인 사업자.", "5천만원 미만인 회사.",
])
def test_b_specific_monetary_condition_is_not_lost_to_broad_target(condition):
    rows = build_rule_excerpt_rows(f"{CHANGE}\n적용 대상은 금융회사 및 관련 사업자이다.\n{condition}\n{DATE}")
    assert rows == [("변경 내용", CHANGE), ("대상·조건", condition), ("시행·기한", DATE)]


def test_c_unlimited_service_does_not_displace_sales_fact_as_a_change():
    title = "무제한 데이터 제공 안내"
    sentence = "데이터를 무제한 제공한다."
    fact = "관련 서비스 판매 실적은 100억원 증가했다."
    rows = build_rule_excerpt_rows(f"{sentence}\n서비스 이용 대상은 금융회사이다.\n2027년부터 판매한다.\n{fact}", title)
    assert fact in [text for _, text in rows]
    assert not any(role == "변경 내용" for role, _ in rows)
    assert _role_strength("변경 내용", sentence) == 0
    assert not _RULE_CHANGE_TITLE.search(title)
    assert not _RULE_CHANGE.search(sentence)
    assert not _KEY_ACTION.search(sentence)


@pytest.mark.parametrize("title", ["법령개정", "법령개정안", "규제완화", "정책변경", "보고 의무 신설"])
def test_c_existing_regulatory_compounds_keep_title_evidence(title):
    assert _RULE_CHANGE_TITLE.search(title)
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{DATE}", title) == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", DATE)]


def test_c_actual_restriction_keeps_change_role():
    sentence = "규정을 제한한다."
    assert _role_strength("변경 내용", sentence) > 0
    assert build_rule_excerpt_rows(sentence) == [("변경 내용", sentence)]
