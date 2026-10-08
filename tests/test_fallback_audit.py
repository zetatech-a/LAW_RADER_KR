"""Source-aware fallback regressions; examples are synthetic unless a fixture is cited."""
import html
import json
from pathlib import Path

import pytest

from src.notifier import build_html, build_text
from src.snippet import build_assembly_fallback_lines, build_key_excerpt_lines, build_rule_excerpt_rows
from test_summarizer import _post

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("number", ["3.5%", "120억원", "0.7%p"])
@pytest.mark.parametrize("sign", ["-", "–", "—"])
def test_spaced_negative_sign_survives(sign, number):
    sentence = f"{sign} {number}의 손실을 기록했다."
    assert build_key_excerpt_lines(sentence) == [sentence]


@pytest.mark.parametrize("marker", ["- ", "• "])
def test_real_bullet_is_removed(marker):
    assert build_key_excerpt_lines(marker + "일반 항목 설명") == ["일반 항목 설명"]


@pytest.mark.parametrize("lead", ["현행법에서는", "현행 「은행법」은", "현행 은행법은", "현재 제도는", "종전에는", "기존 제도에서는"])
def test_current_law_variants_do_not_take_change_slot(lead):
    body = f"{lead} 온라인 판매를 금지한다. 보고 대상은 은행이다. 보고 의무를 신설한다. 2027년부터 시행한다."
    assert build_rule_excerpt_rows(body, "보고 의무 신설")[0] == ("변경 내용", "보고 의무를 신설한다.")


@pytest.mark.parametrize("predicate", ["시행한다", "시행된다", "적용한다", "적용된다", "발효된다", "효력이 발생한다", "효력을 가진다", "시행 예정"])
def test_effective_date_is_reserved(predicate):
    date = f"규정은 2027년 1월 1일부터 {predicate}."
    body = "보고 의무를 신설한다. 적용 대상은 은행이다. " + date + " 과징금 100억원을 부과한다."
    assert ("시행·기한", date) in build_rule_excerpt_rows(body, "보고 의무 신설")


@pytest.mark.parametrize("action", ["허용", "면제", "부과", "유예", "연장", "단축", "의무화", "제한"])
def test_regulatory_predicate_is_selected_by_both_extractors(action):
    amendment = f"이에 은행의 보고 절차를 {action}하려는 것임."
    body = "제도 개선의 배경을 설명함. 현행법에서는 절차를 금지하고 있음. 개선의 필요성이 있음. " + amendment + " 부칙은 2027년부터 시행함."
    assert ("변경 내용", amendment) in build_rule_excerpt_rows(body, f"보고 절차 {action}")
    assert amendment in build_assembly_fallback_lines(body)


def test_regulatory_nouns_do_not_displace_actual_amendment():
    amendment = "보고 의무를 면제함."
    body = "제도 개선의 배경임. 제한속도와 연장근로 관련 통계를 조사함. 관계 기관의 허용 여부를 검토함. " + amendment + " 부칙에서 시행일을 정함."
    assert amendment in build_assembly_fallback_lines(body)
    assert build_rule_excerpt_rows(body, "보고 의무 면제")[0] == ("변경 내용", amendment)


@pytest.mark.parametrize("acronym", ["IPO", "IMA", "P-CBO"])
def test_latin_sentence_start_without_space(acronym):
    first = "제도를 개선한다."
    second = f"{acronym} 대상 기업은 은행이다."
    assert build_key_excerpt_lines(first + second) == [first, second]


def test_urls_decimals_and_abbreviations_remain_intact():
    sentence = "U.S. GAAP 기준은 https://example.org/rule.v2 에서 확인하며 손실률은 -3.5%이다."
    assert build_key_excerpt_lines(sentence) == [sentence]


def test_duplicate_announcement_does_not_fill_two_rows():
    rows = build_key_excerpt_lines("금융위는 제도를 확대한다고 밝혔다. 금융위원회는 해당 제도를 확대할 예정이다. 제도 적용 대상은 10월부터 중소기업으로 확대된다.")
    assert len(rows) == 2
    assert any("중소기업" in row for row in rows)


def test_empty_selection_recovers_a_valid_body(monkeypatch):
    monkeypatch.setattr("src.notifier.build_rule_excerpt_rows", lambda *args: [])
    post = _post(body="은행의 보고 의무를 신설한다.")
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert post.body in rendered
        assert "원문 발췌" in rendered


@pytest.mark.parametrize("body", ["", "   ", "---", "담당부서: 금융정책과\n목록", "자세한 내용은 첨부파일을 확인하세요."])
def test_empty_or_attachment_only_body_is_not_resurrected(body):
    post = _post(body=body)
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert "원문 발췌" not in rendered


def test_portal_answer_is_preserved_and_html_text_agree():
    answer = "제3조에 따라 100억원 미만이면 2027년 1월 1일부터 허용되지 않습니다."
    post = _post(body="[질의요지]\n보고 의무를 면제할 수 있는지?\n\n[회답]\n" + answer + "\n\n[이유]\n" + "은행의 보고 의무를 신설한다. " * 30)
    post.source_key = "better_reply"
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert f"회답: {answer}" in rendered
        assert "질의요지:" in rendered and "이유:" in rendered
        assert "AI 3줄 요약" not in rendered


@pytest.mark.parametrize("body, expected", [
    ("[회답]\n허용되지 않습니다.", "회답: 허용되지 않습니다."),
    ("[질의요지]\n허용되는지?\n[이유]\n제3조에 따른다.", "질의요지: 허용되는지?"),
    ("구조가 유실된 본문입니다.", "구조가 유실된 본문입니다."),
])
def test_portal_partial_body_preserves_only_present_sections(body, expected):
    from src.reply_excerpt import build_reply_excerpt_rows
    # 전용 helper의 부분 데이터 지원은 보존한다. 자동 메일 판별은 세 섹션이
    # 모두 있는 수집 계약만 신뢰하므로 partial에는 일반 발췌를 적용한다.
    rows = build_reply_excerpt_rows(body)
    if rows:
        assert expected in [f"{label}: {text}" for label, text in rows]
    post = _post(body=body)
    post.source_key = "better_reply"
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert expected.split(": ", 1)[-1] in rendered
        if "[회답]" not in body:
            assert "회답:" not in rendered


def test_portal_escaping_and_ai_priority():
    post = _post(body='[회답]\n<상품>은 허용되지 않습니다.\n[이유]\nA & B 규정에 따른다.')
    post.source_key = "better_reply"
    assert html.escape("<상품>은 허용되지 않습니다.") in build_html({post.source_name: [post]})
    post.summary = ["AI 첫째", "AI 둘째", "AI 셋째"]
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert "AI 3줄 요약" in rendered
        assert "회답:" not in rendered


def test_mixed_current_law_and_new_clause_retains_proposal():
    sentence = "현행 「은행법」은 판매를 금지하고 있으나, 개정안에서는 온라인 판매를 허용한다."
    assert build_rule_excerpt_rows(sentence, "온라인 판매 허용") == [("변경 내용", sentence)]


@pytest.mark.parametrize("ending", ["적용했다", "시행되었다", "발효되었다", "효력이 발생했다"])
def test_past_effective_date_does_not_become_a_future_schedule(ending):
    sentence = f"2025년 1월 1일부터 {ending}."
    assert all(role != "시행·기한" for role, _ in build_rule_excerpt_rows(sentence, "규정 개정"))


def test_near_duplicate_negation_and_different_numbers_are_preserved():
    for body in [
        "금융위원회는 중소기업 대상 제도를 확대한다. 금융위는 중소기업 대상 제도를 확대하지 않는다.",
        "펀드 수익률은 - 3.5%이다. 펀드 수익률은 3.5%이다.",
        "펀드 수익률은 3.5%이다. 펀드 수익률은 4.5%이다.",
    ]:
        assert len(build_key_excerpt_lines(body)) == 2


def test_long_portal_answer_preserves_the_negative_tail_and_all_sections():
    from src.reply_excerpt import build_reply_excerpt_rows
    answer = "해당 사업자가 " + "제3조에 따른 조건을 충족하더라도 " * 60 + "허용되지 않습니다."
    rows = build_reply_excerpt_rows(f"[질의요지]\n허용 여부\n[회답]\n{answer}\n[이유]\n제3조에 따른다.")
    assert [role for role, _ in rows] == ["질의요지", "회답", "이유"]
    assert rows[1][1].startswith("해당 사업자가")
    assert rows[1][1].endswith("허용되지 않습니다.")
    assert " … " in rows[1][1] and len(rows[1][1]) <= 600


@pytest.mark.parametrize("filename", ["lawreq_5449_detail.html", "lawreq_5450_detail.html"])
def test_real_portal_sections_retain_verbatim_answer(filename):
    from bs4 import BeautifulSoup
    from src.config import SourceConfig
    from src.scrapers.better_fsc import BetterReplyScraper
    from src.reply_excerpt import build_reply_excerpt_rows
    scraper = BetterReplyScraper(SourceConfig(key="better_reply", name="금융규제포털", type="better_reply", list_url="https://better.fsc.go.kr/"), None)
    sections = scraper._sections(BeautifulSoup((FIXTURES / "better_fsc" / filename).read_text(encoding="utf-8"), "lxml"))
    assert set(sections) == {"질의요지", "회답", "이유"}
    body = "\n\n".join(f"[{key}]\n{value}" for key, value in sections.items())
    rows = build_reply_excerpt_rows(body)
    assert [role for role, _ in rows] == ["질의요지", "회답", "이유"]
    assert rows[1][1] == " ".join(sections["회답"].split())
    assert len(rows[2][1]) <= 300


def test_real_assembly_fixture_keeps_tax_deadline_extension():
    from bs4 import BeautifulSoup
    from src.scrapers.assembly import _probe_summary
    _, body = _probe_summary(BeautifulSoup((FIXTURES / "assembly/available/billinfo.html").read_text(encoding="utf-8"), "lxml"))
    lines = build_assembly_fallback_lines(body)
    assert len(lines) == 3
    assert "2026년 12월 31일" in lines[0]
    assert "2029년 12월 31일까지 3년 연장" in lines[-1]
    assert "제121조의30제1항" in lines[-1]


@pytest.mark.parametrize("source", ["fss_press", "pipc_press"])
def test_real_press_corpus_preserves_sanctions_statistics_and_policy(source):
    corpus = json.loads((FIXTURES / "excerpts/press_audit.json").read_text(encoding="utf-8"))
    for item in (p for p in corpus if p["source_key"] == source):
        post = _post(body=item["body"], title=item["title"])
        post.source_key = source
        raw = post.body
        for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
            for fact in item["expected_facts"]:
                assert fact in rendered
            assert "AI 3줄 요약" not in rendered
        assert post.body == raw
