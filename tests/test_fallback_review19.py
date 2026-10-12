"""Closing regressions for existing date/list and history/change contracts."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


CHANGE = "보고 의무를 신설한다."
TARGET = "적용 대상은 은행이다."
DATE = "2027년부터 시행한다."
REPEAL = "기존 규정을 폐지하고 새 보고 의무를 신설한다."


@pytest.mark.parametrize("date", [
    "10. 15.부터 시행한다.", "10.15.부터 시행한다.", "10/15부터 적용한다.",
])
def test_supported_month_day_survives_both_bullet_stripping_stages(date):
    assert _role_strength("시행·기한", date) > 0
    rows = build_rule_excerpt_rows(date, "시행 일정 변경")
    assert rows == [("시행·기한", date)]
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{date}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", date)]


def test_spaced_date_in_paragraph_keeps_month():
    date = "10. 15.부터 시행한다."
    assert build_rule_excerpt_rows(f"{CHANGE} {TARGET} {date}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", date)]


@pytest.mark.parametrize("marker, content", [
    ("1. ", "안내 사항입니다."), ("2. ", "신청 방법입니다."),
    ("10. ", "15개 기관이 참여한다."),
])
def test_normal_enumerations_still_strip_only_the_marker(marker, content):
    assert build_rule_excerpt_rows(marker + content) == [("주요 내용", content)]


@pytest.mark.parametrize("change", [
    REPEAL, "기존 규정을 폐지한다.", "기존 규정을 폐지하고 새로운 의무를 신설한다.",
])
def test_explicit_change_to_existing_rule_is_not_history(change):
    assert _role_strength("변경 내용", change) > 0
    assert build_rule_excerpt_rows(f"{change} {TARGET} {DATE}") == [
        ("변경 내용", change), ("대상·조건", TARGET), ("시행·기한", DATE)]


@pytest.mark.parametrize("background", [
    "기존 규정은 온라인 판매를 금지한다.", "규제 강화.",
])
def test_actual_repeal_wins_over_background(background):
    target = "적용 대상은 금융회사다."
    body = f"{background}\n{REPEAL}\n{target}\n{DATE}"
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", REPEAL), ("대상·조건", target), ("시행·기한", DATE)]


@pytest.mark.parametrize("sentence", [
    "기존 규정은 온라인 판매를 금지한다.", "기존 제도에서는 해당 행위를 제한한다.",
    "과거에는 보고 의무를 신설하였다.", "현행법은 온라인 판매를 금지한다.",
    "다만, 현행법은 소규모 회사에 예외를 인정한다.",
    "보고 의무를 신설하지 않는다.", "은행은 온라인 판매를 금지하고 있다.",
    "금융규제를 완화하는 방안을 검토한다.", "규제를 완화하기 위한 간담회를 개최한다.",
    "기존 규정을 폐지하지 않는다.", "기존 규정을 폐지하는 방안을 검토한다.",
])
def test_history_and_non_change_guards_remain_effective(sentence):
    assert _role_strength("변경 내용", sentence) == 0
