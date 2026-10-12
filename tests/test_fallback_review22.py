"""Existing relative dates require explicit schedule context, not new vocabulary."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


CHANGE = "보고 의무를 신설한다."
TARGET = "적용 대상은 금융회사이다."


@pytest.mark.parametrize("date", ["즉시", "내년", "오늘", "내일", "올해", "공포한 날"])
def test_existing_standalone_relative_date_requires_schedule_heading(date):
    assert _role_strength("시행·기한", date) == 0
    assert _role_strength("시행·기한", date, "시행·기한") > 0
    assert build_rule_excerpt_rows(f"시행 일정\n{date}") == [("시행·기한", date)]


@pytest.mark.parametrize("date", ["즉시", "내년", "공포한 날"])
def test_relative_date_is_not_lost_to_following_numeric_background(date):
    body = f"{CHANGE}\n{TARGET}\n시행 일정\n{date}\n2025년 판매 규모는 100억원이었다."
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", date)]


@pytest.mark.parametrize("sentence", [
    "즉시 보도", "내년 실적은 감소했다.", "2027년부터 시행하지 않는다.",
    "2027년부터 시행할 예정이었다.", "오늘 간담회를 개최했다.",
])
@pytest.mark.parametrize("section", ["", "시행·기한"])
def test_prose_and_existing_schedule_guards_remain_excluded(sentence, section):
    assert _role_strength("시행·기한", sentence, section) == 0


@pytest.mark.parametrize("date", [
    "2027년 1월 1일", "10.15.", "10. 15.부터 시행한다.",
    "발표일로부터 10영업일 이내 제출한다.", "공포한 날부터 6개월 이내 시행한다.",
])
def test_existing_calendar_month_day_and_relative_deadlines_remain(date):
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n시행 일정\n{date}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", date)]
