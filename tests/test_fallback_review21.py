"""Structural regressions: generic heading boundaries and lossy case dedup."""
import pytest

import src.snippet as snippet
from src.snippet import _duplicate_signature, build_rule_excerpt_rows


DATE = "2027년부터 시행한다."
FACT = "2025년 판매 규모는 100억원이다."


@pytest.mark.parametrize("heading", ["참고", "붙임"])
def test_generic_heading_ends_inherited_schedule_role(heading, monkeypatch):
    contexts = {}
    original = snippet._excerpt_roles

    def capture_context(title, candidates, sections):
        contexts.update(zip(candidates, sections))
        return original(title, candidates, sections)

    monkeypatch.setattr(snippet, "_excerpt_roles", capture_context)
    body = f"시행 일정\n{DATE}\n{heading}\n{FACT}"
    rows = build_rule_excerpt_rows(body)
    assert contexts[FACT] == ""
    assert ("시행·기한", DATE) in rows
    assert ("주요 내용", FACT) in rows
    assert ("시행·기한", FACT) not in rows


@pytest.mark.parametrize("child", ["2027년 1월 1일", "1. 2027년 1월 1일"])
def test_explicit_heading_and_numbered_child_keep_role(child):
    body = f"시행 일정\n{child}"
    assert ("시행·기한", "2027년 1월 1일") in build_rule_excerpt_rows(body)


def test_explicit_role_heading_after_generic_heading_still_applies():
    body = f"시행 일정\n{DATE}\n참고\n적용 대상\n1. 은행"
    assert build_rule_excerpt_rows(body) == [("대상·조건", "은행"), ("시행·기한", DATE)]


def test_existing_case_markers_have_distinct_ordered_signatures():
    signatures = [_duplicate_signature(f"은행{case} 보고 의무를 면제한다.")
                  for case in ("에서는", "에게", "으로")]
    assert len(set(signatures)) == 3


def test_location_and_recipient_facts_are_not_deduplicated():
    first = "은행에서는 보고 의무를 면제한다."
    second = "은행에게 보고 의무를 면제한다."
    rows = build_rule_excerpt_rows(f"{first} {second}")
    assert {text for _, text in rows} == {first, second}


@pytest.mark.parametrize("case", ["에서는", "에게", "으로"])
def test_true_duplicates_with_same_case_marker_still_collapse(case):
    sentence = f"은행{case} 보고 의무를 면제한다."
    assert len(build_rule_excerpt_rows(f"{sentence} {sentence}")) == 1


def test_subject_object_and_word_order_stay_distinct():
    assert _duplicate_signature("은행이 보험사를 감독한다.") != _duplicate_signature("은행을 보험사가 감독한다.")
    assert _duplicate_signature("보고 의무를 폐지하고 검사 의무를 신설한다.") != _duplicate_signature(
        "검사 의무를 폐지하고 보고 의무를 신설한다.")


@pytest.mark.parametrize("first, second", [
    ("은행에게 보고 의무를 면제한다.", "은행에게 보고 의무를 면제하지 않는다."),
    ("은행에게 100억원을 부과한다.", "은행에게 200억원을 부과한다."),
])
def test_negation_and_numeric_differences_remain_visible(first, second):
    assert {text for _, text in build_rule_excerpt_rows(f"{first} {second}")} == {first, second}
