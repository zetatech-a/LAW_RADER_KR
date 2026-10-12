"""Final stabilization: non-operative actions, tentative schedules, attachment prose."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


CHANGE = "검사 의무를 신설한다."
TARGET = "적용 대상은 금융회사이다."
DATE = "2028년부터 시행한다."
POLICY = [("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", DATE)]


@pytest.mark.parametrize("proposal", [
    "보고 의무를 강화할 필요가 있다.", "보고 의무를 신설할 필요가 있다.",
    "보고 의무를 강화할 수 있다.", "제도를 도입하는 방안을 검토한다.",
])
@pytest.mark.parametrize("heading", ["", "변경 내용\n"])
def test_a_nonoperative_action_does_not_displace_actual_change(proposal, heading):
    assert build_rule_excerpt_rows(f"{heading}{proposal}\n{CHANGE}\n{TARGET}\n{DATE}") == POLICY
    assert _role_strength("변경 내용", proposal, "변경 내용" if heading else "") == 0


@pytest.mark.parametrize("change", [
    "보고 의무를 강화한다.", "보고 의무를 신설한다.", "보고를 의무화한다.",
    "보고 의무를 강화할 예정이다.", "보고 의무 신설.",
    "보고 의무를 강화할 필요가 있다며 검사 의무를 신설한다.",
])
def test_a_actual_and_nominal_changes_remain(change):
    assert build_rule_excerpt_rows(f"변경 내용\n{change}\n{TARGET}\n{DATE}") == [
        ("변경 내용", change), *POLICY[1:]]


@pytest.mark.parametrize("proposal", [
    "2027년부터 시행 여부를 검토한다.",
    "2027년부터 시행하는 방안을 검토한다.",
    "2027년부터 시행할 필요가 있다.",
    "2027년부터 시행할 수 있다.",
])
@pytest.mark.parametrize("heading", ["", "시행 일정\n"])
def test_b_tentative_schedule_does_not_displace_committed_date(proposal, heading):
    assert build_rule_excerpt_rows(f"{heading}{proposal}\n{DATE}\n{CHANGE}\n{TARGET}") == POLICY
    assert _role_strength("시행·기한", proposal, "시행·기한" if heading else "") == 0


def test_b_two_schedule_sentences_prefer_committed_date():
    rows = build_rule_excerpt_rows("2027년부터 시행하는 방안을 검토한다.\n" + DATE, "규정 개정")
    assert ("시행·기한", DATE) in rows


@pytest.mark.parametrize("date", [
    DATE, "2028년 3월 1일 시행", "2028년 3월 1일부터 접수한다.",
    "발표일로부터 10영업일 이내 제출한다.",
    "검토를 마치고 2028년부터 시행한다.",
    "2027년부터 시행 여부를 검토한다며 2028년부터 시행한다.",
])
def test_b_valid_schedule_and_unrelated_review_word_remain(date):
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{date}") == [
        *POLICY[:2], ("시행·기한", date)]


def test_c_attachment_prefixed_prose_does_not_stop_scanning():
    body = ("기존 운영 절차를 개선한다.\n첨부 파일 목록 변경 사항은 다음과 같다.\n"
            "보고 의무를 신설한다.\n" + TARGET + "\n2027년부터 시행한다.")
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", "보고 의무를 신설한다."), ("대상·조건", TARGET),
        ("시행·기한", "2027년부터 시행한다.")]


@pytest.mark.parametrize("label", ["첨부파일", "첨부 파일:", "붙임", "첨부 파일 목록", "첨부파일 (2)\n첨부파일 목록"])
def test_c_actual_attachment_labels_and_files_still_end_content(label):
    body = "\n".join(text for _, text in POLICY)
    assert build_rule_excerpt_rows(body + f"\n{label}\n보도자료.pdf\n다른 의무를 강화한다.") == POLICY


def test_c_bare_list_heading_without_files_does_not_swallow_following_policy():
    assert build_rule_excerpt_rows("일반 운영 설명이다.\n첨부 파일 목록\n" +
                                   "\n".join(text for _, text in POLICY)) == POLICY
