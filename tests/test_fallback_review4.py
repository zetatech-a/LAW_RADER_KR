"""PR #35: four reproduced review findings, using synthetic source text."""
import pytest

from src.snippet import _key_sentences, build_key_excerpt_lines, build_rule_excerpt_rows


@pytest.mark.parametrize("purpose", ["완화를 위한", "강화를 위한", "확대를 위해"])
def test_purpose_noun_does_not_displace_actual_change(purpose):
    background = f"금융규제 {purpose} 간담회를 열었다."
    change = "보고 의무를 신설한다."
    rows = build_rule_excerpt_rows(
        f"{background} 적용 대상은 은행이다. {change} 2027년부터 시행한다.")
    assert rows == [("변경 내용", change), ("대상·조건", "적용 대상은 은행이다."),
                    ("시행·기한", "2027년부터 시행한다.")]
    assert build_rule_excerpt_rows(background) == [("주요 내용", background)]


@pytest.mark.parametrize("change", [
    "보고 의무를 신설한다.", "규제를 완화한다.", "대상을 확대한다.",
    "기준을 강화한다.", "과징금을 부과한다.", "적용기한을 연장한다.",
])
def test_actual_verbal_change_still_activates_policy_roles(change):
    assert build_rule_excerpt_rows(change) == [("변경 내용", change)]


@pytest.mark.parametrize("heading,files", [
    ("붙임", ["보도자료.pdf", "신청서.hwpx"]),
    ("참고", ["보도자료.pdf"]),
])
def test_attachment_list_stops_after_flushed_heading(heading, files):
    sentence = "금융위는 새 제도를 시행한다."
    body = "\n".join([sentence, heading, f"첨부파일 ({len(files)})", *files])
    assert build_key_excerpt_lines(body) == [sentence]


@pytest.mark.parametrize("sentences", [
    ["첨부파일 제출은 의무이다.", "접수는 내일까지 가능하다."],
    ["신청은 내일까지 가능하다.", "첨부파일 형식은 자유이다.", "접수 후 검토한다."],
])
def test_attachment_word_in_prose_does_not_stop_collection(sentences):
    assert set(build_key_excerpt_lines("\n".join(sentences))) == set(sentences)


@pytest.mark.parametrize("sentence", [
    "신청서.hwpx를 제출한다.", "보도자료.pdf를 확인한다.",
    "신청서.final.pdf를 제출한다.", "자료는 신청서.hwp.",
])
def test_korean_filename_does_not_split_or_displace_facts(sentence):
    sentences = [sentence, "신청은 내일까지 가능하다.", "접수 후 검토한다."]
    body = " ".join(sentences)
    assert _key_sentences(body) == sentences
    assert set(build_key_excerpt_lines(body)) == set(sentences)


@pytest.mark.parametrize("first,second", [
    ("은행이 보험사를 인수한다.", "보험사가 은행을 인수한다."),
    ("은행은 보험사를 인수한다.", "보험사는 은행을 인수한다."),
])
def test_reversed_actor_roles_remain_distinct_facts(first, second):
    assert build_key_excerpt_lines(first + " " + second) == [first, second]


def test_true_duplicate_with_subject_particle_and_alias_is_still_removed():
    first = "금융위원회는 제도를 확대한다고 밝혔다."
    second = "금융위가 제도를 확대한다."
    rows = build_key_excerpt_lines(first + " " + second)
    assert len(rows) == 1
    assert rows[0] in (first, second)
