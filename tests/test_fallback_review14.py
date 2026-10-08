"""Closing contract regressions: role selection, list context, attachments, plans."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


TARGET = "적용 대상은 은행이다."
DATE = "2027년부터 시행한다."
CHANGE = "보고 의무를 신설한다."
POLICY = [("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", DATE)]


@pytest.mark.parametrize("change", [
    "금융회사의 보고 의무를 신설하였다.", "적용 대상을 확대하였다.",
    "기준을 강화했다.", "일부 의무를 면제하였다.",
])
def test_completed_regulatory_action_activates_policy_roles(change):
    body = f"과거 통계는 100억원이었다. {change} {TARGET} {DATE}"
    assert build_rule_excerpt_rows(body, "기관 안내") == [
        ("변경 내용", change), ("대상·조건", TARGET), ("시행·기한", DATE)]
    assert _role_strength("변경 내용", change) > 0


@pytest.mark.parametrize("history", [
    "과거에는 보고 의무를 신설하였다.", "예전에는 해당 행위를 금지했다.",
    "지난 제도에서는 적용 대상을 제한하였다.", "당시 기준을 강화했다.",
])
def test_historical_completed_actions_stay_excluded(history):
    assert build_rule_excerpt_rows(f"{history} {CHANGE} {TARGET} {DATE}") == POLICY
    assert _role_strength("변경 내용", history) == 0


def test_enumerated_date_keeps_explicit_schedule_context():
    body = (f"{CHANGE}\n{TARGET}\n은행의 공급 규모는 100억원이다.\n"
            "시행 일정\n1. 2027년 1월 1일")
    assert build_rule_excerpt_rows(body) == [*POLICY[:2], ("시행·기한", "2027년 1월 1일")]


def test_enumerated_change_keeps_explicit_section():
    assert build_rule_excerpt_rows("변경 내용\n1. 보고 방식은 월별 보고다.") == [
        ("변경 내용", "보고 방식은 월별 보고다.")]


@pytest.mark.parametrize("target", ["은행", "보험회사"])
def test_enumerated_target_keeps_explicit_section(target):
    assert build_rule_excerpt_rows("적용 대상\n1. " + target) == [("대상·조건", target)]


def test_enumeration_still_clears_stale_background():
    assert build_rule_excerpt_rows("추진 배경\n현행 제도를 설명한다.\n1. " + CHANGE)[0] == POLICY[0]


def test_unheaded_list_does_not_invent_role():
    assert build_rule_excerpt_rows("1. 안내 사항입니다.\n2. 문의처입니다.") == [
        ("주요 내용", "안내 사항입니다."), ("주요 내용", "문의처입니다.")]


@pytest.mark.parametrize("label", ["첨부 파일 목록", "첨부파일"])
def test_leading_attachment_run_is_skipped(label):
    body = label + "\n보도자료.pdf\n참고자료.hwpx\n" + "\n".join(text for _, text in POLICY)
    assert build_rule_excerpt_rows(body) == POLICY


def test_trailing_attachment_block_still_stops_parsing():
    body = "\n".join(text for _, text in POLICY) + "\n첨부 파일 목록\n보도자료.pdf\n다른 의무를 강화한다."
    assert build_rule_excerpt_rows(body) == POLICY


def test_file_format_prose_is_preserved():
    sentence = "제출 파일은 report.pdf 형식이어야 한다."
    assert build_rule_excerpt_rows(sentence) == [("주요 내용", sentence)]


def test_attachment_marker_does_not_consume_unrecognized_prose():
    sentence = "제출 파일은 report.pdf 형식이어야 한다."
    rows = build_rule_excerpt_rows("첨부 파일 목록\n" + sentence)
    assert sentence in " ".join(text for _, text in rows)


@pytest.mark.parametrize("former", [
    "당초 2025년부터 시행할 예정이었다.", "2025년 적용할 계획이었다.",
    "당초 2025년 시행 예정이었으나 검토 중이다.",
    "당초 2025년 적용 계획이었으나 검토 중이다.",
])
def test_former_plan_does_not_displace_current_schedule(former):
    actual = "실제로는 2027년부터 시행한다."
    assert build_rule_excerpt_rows(f"{CHANGE} {TARGET} {former} {actual}") == [
        *POLICY[:2], ("시행·기한", actual)]
    assert _role_strength("시행·기한", former) == 0


@pytest.mark.parametrize("schedule", [
    "2027년부터 시행할 예정이다.", "2027년부터 시행할 예정임.",
    "2027년부터 적용할 계획이다.", DATE,
    "당초 2025년 시행 예정이었으나 실제로는 2027년부터 시행한다.",
    "당초 2025년 적용 계획이었으나 실제로는 2027년부터 시행한다.",
])
def test_current_and_mixed_schedules_keep_original_text(schedule):
    assert build_rule_excerpt_rows(f"{CHANGE} {TARGET} {schedule}") == [
        *POLICY[:2], ("시행·기한", schedule)]
