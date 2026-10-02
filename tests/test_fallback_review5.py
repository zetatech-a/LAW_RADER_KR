"""PR #35: section context, sentence boundaries, and assembly nominal clauses."""
import json
from pathlib import Path

import pytest

from src.snippet import (
    _key_sentences, build_assembly_fallback_lines, build_key_excerpt_lines,
    build_rule_excerpt_rows,
)


@pytest.mark.parametrize("marker", ["①", "②", "❶", "➊", "1.", "2)", "Ⅰ.", "Ⅱ.", "가.", "나)"])
def test_enumerated_section_does_not_inherit_background(marker):
    body = f"추진 배경\n현행 제도를 설명한다.\n{marker} 보고 의무를 신설한다."
    assert build_rule_excerpt_rows(body)[0] == ("변경 내용", "보고 의무를 신설한다.")


@pytest.mark.parametrize("marker", ["□", "○", "ㅇ"])
def test_decorative_bullets_keep_heading_context(marker):
    body = f"변경 내용\n{marker} 적용 대상은 금융회사다.\n{marker} 시행일은 2027년 1월 1일이다."
    rows = build_rule_excerpt_rows(body)
    assert rows == [("변경 내용", "적용 대상은 금융회사다."),
                    ("시행·기한", "시행일은 2027년 1월 1일이다.")]


@pytest.mark.parametrize("first,second", [
    ("The rule applies.", "Banks must report."),
    ("The policy changed.", "Firms must comply."),
])
def test_unspaced_english_sentences_are_separate_facts(first, second):
    assert _key_sentences(first + second) == [first, second]
    assert build_key_excerpt_lines(first + second) == [first, second]


@pytest.mark.parametrize("token", [
    "report.pdf", "report.final.pdf", "신청서.hwpx", "user@example.com",
    "first.last+tag@example.co.kr", "example.com", "www.example.com",
    "https://example.org/rule.v2", "3.5", "U.S.A.",
])
def test_known_identifiers_and_numeric_abbreviation_boundaries_stay_protected(token):
    sentence = f"자료는 {token} 에서 확인한다."
    assert _key_sentences(sentence + " 접수 후 검토한다.") == [sentence, "접수 후 검토한다."]


@pytest.mark.parametrize("sentence", [
    "증권선물위원회는 제17차 정례회의('26.9.30.)에서, 안건을 의결하였다.",
    "(2026.9.30.)부터 시행한다.", "「2026.9.30.」에 시행한다.",
    "'26.9.30.'에 시행한다.",
])
def test_enclosed_date_keeps_following_particle(sentence):
    assert _key_sentences(sentence) == [sentence]
    assert build_key_excerpt_lines(sentence) == [sentence]


def test_real_fss_fixture_keeps_meeting_date_with_its_decision():
    corpus = json.loads((Path(__file__).parent / "fixtures/excerpts/press_audit.json").read_text(encoding="utf-8"))
    item = next(p for p in corpus if "nttId=232881" in p["url"])
    first, rest = item["body"].split(" 또한,", 1)
    assert _key_sentences(item["body"])[0] == first
    rows = build_key_excerpt_lines(item["body"], item["title"])
    assert len(rows) == 2
    assert rows[0] == first
    assert rows[1] == "또한," + rest.split("\n", 1)[0]
    assert "('26.9.30.)에서" in rows[0] and "제174조" in rows[0]


@pytest.mark.parametrize("first", [
    "(회의를 마쳤다.)", "「안건을 의결하였다.」", "'검토를 마쳤다.'",
])
def test_actual_quoted_sentence_endings_still_split(first):
    assert _key_sentences(first + " 다음 문장이다.") == [first, "다음 문장이다."]


def test_assembly_nominal_amendment_beats_weak_background():
    amendment = "신고절차 신설 및 과태료 상향임."
    body = f"제도의 배경을 설명함. {amendment} 현장의 상황을 설명함. 추가 정비가 필요함. 부칙에서 시행일을 정함."
    assert build_assembly_fallback_lines(body) == [
        "제도의 배경을 설명함.", amendment, "부칙에서 시행일을 정함."]


@pytest.mark.parametrize("background", [
    "규제 완화를 위한 논의가 필요함.", "규제 강화를 위해 검토함.",
    "대상 확대를 위한 검토가 필요함.", "현행법은 신고 의무를 규정하고 있음.",
    "현행법은 신고절차 신설 및 과태료 상향임.",
])
def test_assembly_nominal_selection_excludes_purpose_and_history(background):
    amendment = "신고절차 신설 및 과태료 상향임."
    body = f"제도의 배경을 설명함. {background} {amendment} 추가 정비가 필요함. 부칙에서 시행일을 정함."
    assert build_assembly_fallback_lines(body) == [
        "제도의 배경을 설명함.", amendment, "부칙에서 시행일을 정함."]


def test_assembly_verbal_extension_still_beats_nominal_background():
    amendment = "적용기한을 2029년까지 연장하려는 것임."
    body = f"제도의 배경을 설명함. 규제 완화를 위한 논의가 필요함. {amendment} 추가 정비가 필요함. 부칙에서 시행일을 정함."
    assert amendment in build_assembly_fallback_lines(body)


def test_assembly_nominal_rule_does_not_activate_generic_policy_roles():
    sentence = "신고절차 신설 및 과태료 상향임."
    assert build_rule_excerpt_rows(sentence) == [("주요 내용", sentence)]
