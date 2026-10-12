"""Closing regressions for contextual month-day dates and edge-only cleanup."""
import pytest

import src.snippet as snippet
from src.snippet import _role_strength, build_rule_excerpt_rows, strip_edge_noise


CHANGE = "보고 의무를 신설한다."
TARGET = "적용 대상은 은행이다."
DATE = "2027년부터 시행한다."
POLICY = [("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", DATE)]
INSTRUCTION = "온라인 광고에는 자료의 출처를 표기해 주시기 바랍니다."
NOTICES = [
    "보도 시 자료의 출처를 표기해 주시기 바랍니다.",
    "브라우저 설정에서 자바스크립트를 활성화해 주세요.",
    "이 페이지에서 제공하는 정보에 만족하십니까?",
]


@pytest.mark.parametrize("date", [
    "10.15.부터 시행한다.", "10.15.부터 적용한다.",
    "10/15부터 시행한다.", "10/15부터 적용한다.",
])
def test_month_day_schedule_wins_over_historical_amount(date):
    body = f"{CHANGE}\n{TARGET}\n과거 공급액은 100억원이었다.\n{date}"
    assert build_rule_excerpt_rows(body) == [*POLICY[:2], ("시행·기한", date)]
    assert _role_strength("시행·기한", date) > 0
    assert _role_strength("후속 일정", date) > 0


def test_explicit_effective_date_label_supplies_month_day_context():
    assert _role_strength("시행·기한", "시행일은 10.15.이다.") > 0


@pytest.mark.parametrize("date", ["10.15.", "10/15"])
def test_month_day_under_schedule_heading_is_selected(date):
    body = f"{CHANGE}\n{TARGET}\n과거 공급액은 100억원이었다.\n시행 일정\n{date}"
    assert build_rule_excerpt_rows(body) == [*POLICY[:2], ("시행·기한", date)]


@pytest.mark.parametrize("sentence", ["비율은 10.15였다.", "버전 1.25를 사용한다."])
@pytest.mark.parametrize("section", ["", "시행·기한"])
def test_decimal_and_version_prose_are_not_schedule_dates(sentence, section):
    assert _role_strength("시행·기한", sentence, section) == 0


@pytest.mark.parametrize("date", [
    "2026.10.15.부터 적용한다.", "2026-10-15부터 시행한다.",
    "10월 15일부터 시행한다.", "발표일로부터 10영업일 이내 제출한다.",
])
def test_existing_dates_and_relative_deadline_keep_schedule_role(date):
    assert build_rule_excerpt_rows(f"{CHANGE}\n{TARGET}\n{date}") == [
        *POLICY[:2], ("시행·기한", date)]


@pytest.mark.parametrize("sentence", [
    "10.15.부터 시행할 예정이었다.", "10/15부터 시행하였다.",
])
def test_month_day_context_does_not_bypass_past_guards(sentence):
    assert _role_strength("시행·기한", sentence) == 0


@pytest.mark.parametrize("instruction", [INSTRUCTION, *NOTICES[1:]])
def test_interior_sentence_reaches_candidate_ranking(monkeypatch, instruction):
    candidates_seen = []
    original = snippet._excerpt_roles

    def capture_candidates(title, candidates, sections):
        candidates_seen.extend(candidates)
        return original(title, candidates, sections)

    monkeypatch.setattr(snippet, "_excerpt_roles", capture_candidates)
    body = f"{CHANGE}\n{instruction}\n{TARGET}\n{DATE}"
    build_rule_excerpt_rows(body)
    assert instruction in candidates_seen


def test_compliance_instruction_is_available_for_output():
    body = f"{CHANGE}\n{INSTRUCTION}\n적용 대상은 금융회사다.\n{DATE}"
    rows = build_rule_excerpt_rows(body, max_lines=4)
    assert INSTRUCTION in [text for _, text in rows]


@pytest.mark.parametrize("notice", NOTICES)
@pytest.mark.parametrize("leading", [True, False])
def test_edge_notices_are_still_removed(notice, leading):
    body = "\n".join(text for _, text in POLICY)
    body = notice + "\n" + body if leading else body + "\n" + notice
    assert build_rule_excerpt_rows(body) == POLICY


@pytest.mark.parametrize("notice", NOTICES)
def test_classification_keeps_sentence_notices_but_removes_structural_noise(notice):
    assert strip_edge_noise(["본문 바로가기", notice, CHANGE, "목록으로"],
                            for_classification=True) == [notice, CHANGE]
