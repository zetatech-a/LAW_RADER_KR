"""Closing patch: URL sentence punctuation and announcement-relative deadlines."""
import pytest

from src.snippet import (
    _key_sentences, _role_strength, build_key_excerpt_lines, build_rule_excerpt_rows,
)


@pytest.mark.parametrize("first,second", [
    ("자세한 내용은 https://example.com.", "은행은 의무를 신설한다."),
    ("홈페이지는 https://example.com/path.", "신청은 내일까지 가능하다."),
    ("홈페이지는 https://example.com/path).", "다음 절차를 진행한다."),
    ('홈페이지는 "https://example.com/path."', "다음 절차를 진행한다."),
    ("홈페이지는 www.example.com.", "다음 절차를 진행한다."),
])
def test_url_terminal_punctuation_keeps_sentences_separate(first, second):
    body = first + " " + second
    assert _key_sentences(body) == [first, second]
    assert set(build_key_excerpt_lines(body)) == {first, second}


def test_long_url_does_not_truncate_the_following_policy_change():
    first = "자세한 내용은 https://example.com/" + "document" * 45 + "."
    change = "은행은 의무를 신설한다."
    body = first + " " + change
    assert _key_sentences(body) == [first, change]
    assert ("변경 내용", change) in build_rule_excerpt_rows(body)


@pytest.mark.parametrize("token", [
    "https://example.com/path?q=1.2", "https://example.com/a.b/file.pdf",
    "www.example.com", "report.pdf", "신청서.hwpx", "user@example.com",
    "example.com", "3.5%", "U.S.A.",
])
def test_identifier_contents_are_preserved_without_changing_source(token):
    first = f"자료는 {token} 에서 확인한다."
    second = "IPO 대상 기업은 별도 기준을 적용한다."
    body = first + second
    assert _key_sentences(body) == [first, second]
    assert set(build_key_excerpt_lines(body)) == {first, second}
    assert body == first + second


@pytest.mark.parametrize("label", ["시행·기한", "후속 일정"])
@pytest.mark.parametrize("reference", ["발표일", "배포일", "등록일", "게시일"])
def test_bounded_deadline_from_metadata_date_is_a_schedule(label, reference):
    sentence = f"결과 {reference}로부터 5영업일까지 서류를 제출한다."
    assert _role_strength(label, sentence) > 0


@pytest.mark.parametrize("title,prefix,label", [
    ("보고 의무 신설", "보고 의무를 신설한다. 적용 대상은 은행이다.", "시행·기한"),
    ("공급 실적", "총 공급 규모는 100억원이다. 판매 실적은 20억원이다.", "후속 일정"),
])
def test_announcement_deadline_is_not_displaced_by_numeric_fact(title, prefix, label):
    deadline = "결과 발표일로부터 5영업일까지 서류를 제출한다."
    rows = build_rule_excerpt_rows(prefix + " " + deadline + " 총 목표 금액은 500억원이다.", title)
    assert (label, deadline) in rows


@pytest.mark.parametrize("label", ["시행·기한", "후속 일정"])
@pytest.mark.parametrize("sentence", [
    "결과 발표일은 2026년 9월 30일이었다.",
    "보도자료 배포일은 2026년 9월 30일이다.",
    "발표일인 2026년 9월 30일에 간담회를 개최하였다.",
    "발표일은 2026년 9월 30일이며 당시 결과를 공개했다.",
    "결과 발표일로부터 5영업일까지 서류를 제출했다.",
])
def test_metadata_and_completed_deadline_are_not_future_schedules(label, sentence):
    assert _role_strength(label, sentence) == 0
    assert _role_strength(label, sentence, "시행·기한") == 0


def test_unsupported_contract_deadline_remains_available_as_generic_content():
    # '이내' + 계약 체결은 기존 일정 규칙 밖이다. 이번 수정으로 어휘를 늘리지 않는다.
    sentence = "선정 결과 발표일 이후 10일 이내 계약을 체결할 예정이다."
    assert build_rule_excerpt_rows(sentence) == [("주요 내용", sentence)]
