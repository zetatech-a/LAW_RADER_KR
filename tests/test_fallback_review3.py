"""PR #35: dotted tokens, reply conclusions, and configurable source keys."""
import pytest

from src.config import SourceConfig
from src.notifier import build_html, build_text
from src.reply_excerpt import _bounded_section, build_reply_excerpt_rows
from src.scrapers import build_scraper
from src.snippet import _key_sentences, build_key_excerpt_lines
from test_better_fsc import DETAIL_HTML, LIST_URL, _Fetcher, _record
from test_summarizer import _post


@pytest.mark.parametrize("sentence", [
    "첨부자료 report.pdf를 제출한다.",
    "문의는 user@example.com으로 보낸다.",
    "자세한 사항은 example.com에서 확인한다.",
    "자료는 report.final.pdf를 제출한다.",
    "문의는 first.last+tag@example.co.kr로 보낸다.",
])
def test_dotted_identifier_does_not_displace_substantive_sentences(sentence):
    sentences = [sentence, "신청은 내일까지 가능하다.", "접수 후 검토한다."]
    body = " ".join(sentences)
    assert _key_sentences(body) == sentences
    assert set(build_key_excerpt_lines(body)) == set(sentences)


@pytest.mark.parametrize("first,second", [
    ("제도를 개선한다.", "IPO 대상 기업은 별도 기준을 적용한다."),
    ("변경하였다.", "IMA 관련 사항은 별도로 적용한다."),
    ("적용한다.", "P-CBO 지원은 계속된다."),
])
def test_unspaced_latin_sentence_still_splits(first, second):
    assert _key_sentences(first + second) == [first, second]


def test_identifiers_keep_their_final_sentence_boundary():
    assert _key_sentences("자료는 report.pdf. 다음 문장이다.") == ["자료는 report.pdf.", "다음 문장이다."]
    assert _key_sentences("문의는 user@example.com. 접수 후 검토한다.") == ["문의는 user@example.com.", "접수 후 검토한다."]


def test_existing_protected_tokens_and_decimals():
    first = "U.S. GAAP 자료는 https://example.com/report.pdf에서 확인하며 3.5%, 1.25배이다."
    assert _key_sentences(first + "IPO 기업도 같다.") == [first, "IPO 기업도 같다."]


@pytest.mark.parametrize("limit", [300, 600])
@pytest.mark.parametrize("separate_conclusion", [False, True])
def test_short_reply_preamble_keeps_overflowing_conclusion(limit, separate_conclusion):
    preamble = "검토 결과는 다음과 같습니다."
    conclusion = "따라서 해당 거래에는 금리인하요구권이 적용되지 않습니다."
    middle = "해당 계약의 조건 및 관련 규정에 따라 " * 50
    text = preamble + " " + middle + ("검토하였습니다. " if separate_conclusion else "") + conclusion
    out = _bounded_section(text, limit)
    assert out.startswith(preamble)
    assert out.endswith(conclusion)
    assert len(out) <= limit
    head, tail = out.split(" … ")
    assert text.startswith(head) and text.endswith(tail)
    rows = build_reply_excerpt_rows("[질의요지]\n의무가 있는지?\n[회답]\n" + text + "\n[이유]\n법령상 근거가 없습니다.")
    assert rows[1][1].endswith(conclusion) and len(rows[1][1]) <= 600


def test_single_long_sentence_still_preserves_both_ends():
    text = "이 거래는 " + "제3조 및 관련 규정에 따라 " * 70 + "허용되지 않습니다."
    out = _bounded_section(text, 600)
    assert len(out) <= 600
    assert out.startswith("이 거래는 ") and out.endswith("허용되지 않습니다.")


def test_short_multisentence_reply_is_not_truncated():
    text = "검토 결과입니다. 해당 의무는 없습니다. 예외적으로 별도 신고가 필요합니다."
    assert _bounded_section(text, 600) == text


@pytest.mark.parametrize("key", ["better_reply", "financial_interpretation"])
def test_configured_reply_scraper_keeps_reply_with_any_key(key):
    source = SourceConfig(key=key, name="금융규제포털", type="better_reply", list_url=LIST_URL)
    scraper = build_scraper(source, _Fetcher(records=[_record("법령해석")], html=DETAIL_HTML))
    post = scraper.fetch_list(1)[0]
    scraper.enrich(post)
    assert post.source_key == key
    original_body = post.body
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert "질의요지:" in rendered
        assert "회답: 신고 대상에 해당하지 않습니다." in rendered
        assert "이유:" in rendered
    assert post.body == original_body


def test_reply_body_contract_does_not_depend_on_key_or_display_name():
    post = _post(body="[질의요지]\n해당 의무가 있는지?\n[회답]\n해당 의무는 없습니다.\n[이유]\n관련 법령상 적용대상이 아니기 때문입니다.")
    post.source_key = "financial_interpretation"
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert "회답: 해당 의무는 없습니다." in rendered
        assert "질의요지:" in rendered and "이유:" in rendered


@pytest.mark.parametrize("body", [
    "[이유]\n은행의 보고 의무를 신설한다.",
    "[질의요지]라는 표현과 [회답], [이유]를 안내한다.",
    "주요 변경을 안내한다.\n[질의요지]\n설명이다.\n[회답]\n설명이다.\n[이유]\n설명이다.",
    "[회답]\n설명이다.\n[질의요지]\n설명이다.\n[이유]\n설명이다.",
    "[질의요지]\n설명이다.\n[회답]\n[이유]\n설명이다.",
])
def test_generic_body_is_not_mistaken_for_reply_contract(body):
    post = _post(body=body)
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert "회답:" not in rendered and "질의요지:" not in rendered and "이유:" not in rendered
