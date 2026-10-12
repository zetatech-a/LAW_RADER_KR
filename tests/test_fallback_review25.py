"""Final rendering must retain source tails and avoid partial numeric evidence."""
import pytest

from src.snippet import build_rule_excerpt_rows


PREFIX = "제도 운영에 관한 설명과 관계 기관의 의견을 함께 살펴보면 " * 12


@pytest.mark.parametrize("role, ending", [
    ("변경 내용", "은행의 보고 의무를 신설한다."),
    ("시행·기한", "2027년 1월 1일부터 시행한다."),
    ("대상·조건", "적용 대상은 은행이다."),
])
def test_long_row_keeps_operative_tail_and_source_order(role, ending):
    sentence = PREFIX + ending
    rows = build_rule_excerpt_rows(sentence, "보고 의무 신설")
    assert len(rows) == 1
    label, output = rows[0]
    assert label == role
    assert output.endswith(ending)
    head, tail = output.split(" … ")
    assert sentence.startswith(head) and sentence.endswith(tail)
    assert len(output) <= 300


@pytest.mark.parametrize("limit", [80, 300])
@pytest.mark.parametrize("offset", [-1, 0])
def test_untruncated_rows_are_byte_identical_at_limit(limit, offset):
    ending = " 보고 의무를 신설한다."
    sentence = "설" * (limit + offset - len(ending)) + ending
    assert build_rule_excerpt_rows(sentence, max_line_chars=limit) == [("변경 내용", sentence)]


@pytest.mark.parametrize("limit", [1, 2, 3, 20, 80, 299, 300])
def test_custom_length_limit_is_always_respected(limit):
    rows = build_rule_excerpt_rows(PREFIX + "보고 의무를 신설한다.", max_line_chars=limit)
    assert len(rows) == 1
    assert len(rows[0][1]) <= limit
    assert "…" in rows[0][1]


@pytest.mark.parametrize("evidence", ["- 3.5%", "−3.5%", "2027년 1월 1일", "1,234억원"])
def test_numeric_evidence_at_cut_boundaries_is_whole_or_omitted(evidence):
    # Move both cut points across each part of a value, including a spaced minus/date.
    for shift in range(len(evidence) + 2):
        sentence = "설명 " * (40 + shift) + evidence + " 설명" * 80 + evidence + " 설명" * (40 + shift) + " 보고 의무를 신설한다."
        for limit in (260, 300, 340):
            output = build_rule_excerpt_rows(sentence, max_line_chars=limit)[0][1]
            head, tail = output.split(" … ")
            assert sentence.startswith(head) and sentence.endswith(tail)
            for start in (sentence.index(evidence), sentence.rindex(evidence)):
                end = start + len(evidence)
                assert not start < len(head) < end
                assert not start < len(sentence) - len(tail) < end
