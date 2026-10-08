"""Closing polarity regressions and reuse of each renderer's excerpt count."""
import pytest

from src import notifier
from src.models import Post
from src.snippet import _is_explicit_change, _role_strength, build_rule_excerpt_rows


CHANGE = "검사 의무를 강화한다."
TARGET = "적용 대상은 은행이다."
DATE = "2028년부터 시행한다."


@pytest.mark.parametrize("sentence", [
    "보고 의무를 신설하지 않는다.", "보고 의무를 신설하지 않기로 했다.",
    "보고 의무가 신설되지 않는다.", "온라인 판매를 허용하지 않는다.",
    "해당 의무를 면제하지 않는다.",
])
def test_negated_change_is_not_explicit_or_promoted_by_heading(sentence):
    assert not _is_explicit_change(sentence)
    assert _role_strength("변경 내용", sentence) == 0
    assert _role_strength("변경 내용", sentence, "변경 내용") == 0


def test_actual_change_displaces_earlier_negated_action():
    body = f"보고 의무를 신설하지 않는다. {CHANGE} {TARGET} 2027년부터 시행한다."
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", "2027년부터 시행한다.")]


@pytest.mark.parametrize("sentence", [
    "보고 의무를 신설한다.", "보고 의무를 신설하였다.", CHANGE,
    "온라인 판매를 허용한다.", "기준을 완화할 예정이다.", "온라인 판매를 금지한다.",
    "보고 의무를 신설하지 않고 검사 의무를 강화한다.",
    "기존 기준을 유지하지 않고 적용 대상을 확대한다.",
])
def test_affirmative_changes_survive_local_negation_filter(sentence):
    assert _is_explicit_change(sentence)


def test_mixed_change_keeps_original_sentence():
    sentence = "보고 의무를 신설하지 않고 검사 의무를 강화한다."
    assert build_rule_excerpt_rows(f"{sentence} {TARGET} {DATE}")[0] == ("변경 내용", sentence)


@pytest.mark.parametrize("sentence", [
    "2027년부터 시행하지 않는다.", "2027년부터 적용하지 않는다.",
    "2027년에는 시행하지 않기로 했다.", "2027년부터 발효하지 않는다.",
    "2027년부터 시행되지 않는다.", "10.15.부터 시행하지 않는다.",
])
@pytest.mark.parametrize("section", ["", "시행·기한"])
def test_negated_effective_dates_cannot_use_bounded_or_section_fallback(sentence, section):
    assert _role_strength("시행·기한", sentence, section) == 0
    assert _role_strength("후속 일정", sentence, section) == 0


@pytest.mark.parametrize("heading", ["", "시행 일정\n"])
def test_actual_schedule_displaces_rejected_date(heading):
    body = f"{CHANGE}\n{TARGET}\n{heading}2027년부터 시행하지 않는다.\n{DATE}"
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", DATE)]


def test_schedule_only_document_uses_affirmative_date():
    rows = build_rule_excerpt_rows(f"2027년부터 시행하지 않는다. {DATE}", "시행 일정 변경")
    assert ("시행·기한", DATE) in rows
    assert ("시행·기한", "2027년부터 시행하지 않는다.") not in rows


@pytest.mark.parametrize("sentence", [
    DATE, "2028년부터 적용한다.", "2028년부터 시행할 예정이다.", "10.15.부터 시행한다.",
    "2027년부터 시행하지 않고 2028년부터 시행한다.",
    "접수기간은 변경하지 않고 2028년부터 시행한다.",
])
def test_affirmative_and_mixed_schedules_keep_original_text(sentence):
    assert _role_strength("시행·기한", sentence) > 0
    assert build_rule_excerpt_rows(f"{CHANGE} {TARGET} {sentence}") == [
        ("변경 내용", CHANGE), ("대상·조건", TARGET), ("시행·기한", sentence)]


def _post(source_key="fsc_press"):
    return Post(source_key=source_key, source_name="금융위원회", post_id="test",
                title="보고 제도 변경", url="https://example.com/1", date="2026-10-08",
                body=f"{CHANGE} {TARGET} {DATE}")


@pytest.mark.parametrize("renderer", [notifier.build_html, notifier.build_text])
@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_renderer_extracts_once_and_keeps_identical_output(monkeypatch, renderer, count):
    post = _post()
    rows = [f"원문 {i + 1} <표기> & 조건" for i in range(count)]
    calls = []

    def extract(p):
        calls.append(p)
        return rows

    monkeypatch.setattr(notifier, "_general_excerpt", extract)
    grouped = {post.source_name: [post]}
    actual = renderer(grouped)
    extraction_count = len(calls)

    # 이전 라벨 구현을 사용한 렌더링과 문자열 전체를 비교한다.
    def legacy_label(p, *args):
        n = len(notifier._general_excerpt(p))
        return f"원문 발췌 · 핵심 {n}줄" if n else "원문 발췌"

    monkeypatch.setattr(notifier, "_body_label", legacy_label)
    assert actual == renderer(grouped)
    assert extraction_count == 1


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_supplied_count_keeps_general_label_without_extraction(monkeypatch, count):
    def unexpected_extraction(p):
        pytest.fail("An already supplied row count must not trigger extraction")

    monkeypatch.setattr(notifier, "_general_excerpt", unexpected_extraction)
    expected = f"원문 발췌 · 핵심 {count}줄" if count else "원문 발췌"
    assert notifier._body_label(_post(), count) == expected
    assert notifier._body_label(_post("assembly_bill"), count) == "제안이유 및 주요내용 발췌"


def test_legacy_body_label_call_remains_compatible():
    assert notifier._body_label(_post()) == "원문 발췌 · 핵심 3줄"
