"""원문 발췌 종료 계약: 짧은 본문 전문 보존, 추정 역할 라벨 제거, 장애 격리, 비용 상한.

실제 자료는 tests/fixtures/excerpts 와 better_fsc fixture 뿐이다. 그 밖의 문장은 합성
예시이며 운영 원문이 아니다.
"""
import html
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import notifier, snippet
from src.models import ASSEMBLY_SOURCE_KEY, Post
from src.notifier import build_html, build_text
from src.snippet import (
    BodyKind,
    build_fallback_snippet,
    build_full_excerpt_lines,
    build_key_excerpt_lines,
    build_rule_excerpt_rows,
    classify_body,
    clean_body_text,
    has_oversized_token,
    normalize_lines,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROLE_LABELS = ("변경 내용:", "대상·조건:", "시행·기한:", "주요 현황:", "세부 수치:", "후속 일정:", "주요 내용:")
NOTICE = "원문 문장을 자동으로 선별했습니다."
BG = "2025년 은행의 판매액은 100억원으로 20% 증가했다."


def _post(body, title="보고제도 안내", key="fsc_press", post_id="t1"):
    return Post(source_key=key, source_name="금융위 · 보도자료", post_id=post_id, title=title,
                url=f"https://example.com/{post_id}", date="2026-10-12", body=body)


def _render(*posts):
    grouped = {posts[0].source_name: list(posts)}
    return build_html(grouped), build_text(grouped)


def _text_rows(text_out):
    return [line[len("      · "):] for line in text_out.splitlines() if line.startswith("      · ")]


def _squash(text):
    return re.sub(r"\s+", "", text.replace("…", ""))


# ── S1. 짧은 본문은 선별·라벨 없이 원문 순서 그대로 ─────────────────────────────

# 합성 예시: 3줄 선별 때문에 실제 변경·부정·예외·일정이 빠지던 문형(진단 보고서 §4-3).
SHORT_BODIES = [
    [BG, "공시 기준을 마련한다.", "적용 대상은 은행이다.", "2027년부터 시행한다."],
    [BG, "보고 의무를 신설할 계획은 없다.", "보고 의무를 신설한다.", "적용 대상은 은행이다.", "2027년부터 시행한다."],
    ["보고 의무를 신설한다.", "적용 대상은 은행이다.", BG,
     "자산총액 5천억원 미만인 은행은 보고 대상에서 빠진다.", "2027년부터 시행한다."],
    [BG, "보고 의무를 강화하는 안은 보류되었다.", "검사 의무를 신설한다.", "2027년 시행 여부는 내년에 정한다."],
    ["보고 의무를 신설한다.", "다만, 자산 1조원 미만 은행은 제외한다.", "은행에는 적용하지 않는다.",
     "수익률은 - 3.5%였다.", "2027년 1월 1일 이전에 시행한다."],
]


@pytest.mark.parametrize("sentences", SHORT_BODIES)
def test_short_body_keeps_every_sentence_in_order_without_labels(sentences):
    body = " ".join(sentences)
    html_out, text_out = _render(_post(body))
    assert _text_rows(text_out) == sentences
    positions = [html_out.index(html.escape(s)) for s in sentences]
    assert positions == sorted(positions)
    for rendered in (html_out, text_out):
        assert "원문 발췌" in rendered and "문장 선별" not in rendered
        assert NOTICE not in rendered
        assert not any(label in rendered for label in ROLE_LABELS)


@pytest.mark.parametrize("sentences", SHORT_BODIES)
def test_short_body_contains_everything_the_main_220_excerpt_showed(sentences):
    body = "\n".join(sentences)
    shown = _squash(" ".join(_text_rows(_render(_post(body))[1])))
    assert _squash(build_fallback_snippet(body, "보고제도 안내")) in shown


# 연도 없는 "M. D." 날짜·번호 표식이 목록 기호로 지워지던 입력(검증 중 재현, 합성 예시).
DATED_BODIES = [
    ("□ 향후 일정\n○ 10. 15. 공청회를 개최한다.\n○ 11. 3. 입법예고를 실시한다.", ["10. 15.", "11. 3."]),
    ("보고 의무를 신설한다.\n12. 31. 종료된다.", ["12. 31."]),
    ("보고 의무를 신설한다. 9. 30. 기준 잔여물량은 2,140억원이다.", ["9. 30."]),
    ("7. 1.~12. 31. 접수한다.", ["7. 1.~12. 31."]),
    ("보고 의무를 신설한다.\n10. 1.(수) 공포 후 시행한다.", ["10. 1.(수)"]),
    ("1. 개정 내용\n가. 보고 의무를 신설한다.\n2. 시행 시기\n2027. 1. 1.",
     ["1. 개정 내용", "가. 보고 의무를 신설한다.", "2. 시행 시기", "2027. 1. 1."]),
]


@pytest.mark.parametrize(("body", "kept"), DATED_BODIES)
def test_short_body_keeps_numbered_markers_and_year_less_dates(body, kept):
    html_out, text_out = _render(_post(body))
    shown = " ".join(_text_rows(text_out))
    for token in kept:
        assert token in shown and html.escape(token) in html_out
    # 기호 불릿(□ ○)만 벗기고 나머지 글자는 공백을 빼면 원문과 같다.
    assert _squash(shown) == _squash(re.sub(r"[□○]", "", body))


def test_dot_or_comma_dividers_do_not_trip_the_cost_guard():
    body = "보고 의무를 신설한다.\n" + "." * 110 + "\n" + "," * 120 + "\n적용 대상은 은행이다."
    assert not has_oversized_token(normalize_lines(body))
    rows = _text_rows(_render(_post(body))[1])
    assert rows[0] == "보고 의무를 신설한다." and rows[-1].endswith("적용 대상은 은행이다.")


def test_short_body_keeps_source_headings_as_text():
    body = "적용 대상\n은행·보험회사\n시행 일정\n2027년 1월 1일"
    assert build_full_excerpt_lines(body) == ["적용 대상 은행·보험회사", "시행 일정 2027년 1월 1일"]


def _body_of_length(n):
    body, i = "", 0
    while len(body) + 40 < n:
        body += f"은행은 보고 의무 {i:03d}번을 이행한다. "
        i += 1
    body += "끝"
    return body + "가" * (n - len(body) - 2) + "다."


def test_length_is_measured_before_selection_and_600_is_inclusive():
    exact, over = _body_of_length(600), _body_of_length(601)
    assert (len(exact), len(over)) == (600, 601)
    assert build_full_excerpt_lines(exact) is not None
    assert build_full_excerpt_lines(over) is None
    html_out, text_out = _render(_post(over))
    selected = [text for _, text in build_rule_excerpt_rows(over, "보고제도 안내", document_order=True)]
    # 600자를 넘는 본문은 잘라서 전문 경로에 넣지 않고 기존 장문 선별을 쓴다.
    assert _text_rows(text_out) == selected
    assert f"원문 발췌 · {len(selected)}문장 선별" in text_out
    assert NOTICE in html_out and NOTICE in text_out


# ── S2. 장문 선별 이점 유지, 추정 라벨 제거, 원문 순서 ──────────────────────────

def _fixtures():
    items = [(x["title"], x["body"], "fsc_press")
             for x in json.loads((FIXTURES / "excerpts/fsc_posts.json").read_text(encoding="utf-8"))]
    items += [(x["title"], x["body"], x["source_key"])
              for x in json.loads((FIXTURES / "excerpts/press_audit.json").read_text(encoding="utf-8"))]
    return items


def test_long_fixture_keeps_previous_selection_in_document_order():
    title, body, _ = _fixtures()[1]  # 금융위 87831, 정제 후 5,000자 이상
    assert build_full_excerpt_lines(body, title) is None
    html_out, text_out = _render(_post(body, title))
    rows = _text_rows(text_out)
    assert set(rows) == set(build_key_excerpt_lines(body, title))  # 같은 문장 선택
    assert any("신설한다" in row for row in rows) and any("11.6조원" in row for row in rows)
    joined = " ".join(text for _, text in snippet._excerpt_paragraphs(body, title))
    positions = [joined.index(row[:30]) for row in rows]
    assert positions == sorted(positions)
    for rendered in (html_out, text_out):
        assert f"원문 발췌 · {len(rows)}문장 선별" in rendered and NOTICE in rendered
        assert not any(label in rendered for label in ROLE_LABELS)


def test_document_order_only_reorders_default_rows():
    body = f"2027년부터 시행한다. 적용 대상은 은행이다. 보고 의무를 신설한다. {BG}"
    default = build_rule_excerpt_rows(body, "보고 의무 신설")
    ordered = build_rule_excerpt_rows(body, "보고 의무 신설", document_order=True)
    # 기본 호출은 기존 역할 순서 그대로이고, document_order는 같은 선택을 원문 순서로만 바꾼다.
    assert [text for _, text in default] == [
        "보고 의무를 신설한다.", "적용 대상은 은행이다.", "2027년부터 시행한다."]
    assert sorted(default) == sorted(ordered)
    assert [text for _, text in ordered] == [
        "2027년부터 시행한다.", "적용 대상은 은행이다.", "보고 의무를 신설한다."]


# 실제 fixture 사실 포함(진단 보고서 §4-1). FSS/PIPC는 fixture의 expected_facts, 나머지는
# 분석자가 정한 핵심 사실이다. 단순화 전 PR 결과(20/21)보다 줄지 않아야 한다.
ANALYST_FACTS = {
    "제2차 국민참여성장펀드 판매 결과 잔여물량 현황(9.30일)": ["6,000억원", "35.7%", "2,140억원", "10.8일"],
    "대형 IB의 모험자본": ["11.6조원", "19.5%", "신설한다", "3년간 매년 2회", "커버리지 최소비율"],
}


def _reply_posts():
    from bs4 import BeautifulSoup
    from src.config import SourceConfig
    from src.scrapers.better_fsc import BetterReplyScraper
    scraper = BetterReplyScraper(SourceConfig(key="better_reply", name="금융규제포털", type="better_reply",
                                              list_url="https://better.fsc.go.kr/"), None)
    posts = []
    for name, conclusion in (("lawreq_5449_detail.html", "적용대상으로 보기 어려울 것으로 판단됩니다"),
                             ("lawreq_5450_detail.html", "업무에 해당한다고 볼 수 있습니다")):
        soup = BeautifulSoup((FIXTURES / "better_fsc" / name).read_text(encoding="utf-8"), "lxml")
        body = "\n\n".join(f"[{key}]\n{value}" for key, value in scraper._sections(soup).items())
        posts.append((_post(body, name, key="better_reply", post_id=name), conclusion))
    return posts


def test_real_fixture_fact_coverage_does_not_drop_below_previous_result():
    expected = {x["title"]: x["expected_facts"]
                for x in json.loads((FIXTURES / "excerpts/press_audit.json").read_text(encoding="utf-8"))}
    hits = total = 0
    for title, body, key in _fixtures():
        facts = expected.get(title) or next(v for k, v in ANALYST_FACTS.items() if title.startswith(k))
        text = " ".join(_text_rows(_render(_post(body, title, key=key))[1]))
        hits += sum(fact in text for fact in facts)
        total += len(facts)
    for post, conclusion in _reply_posts():
        hits += conclusion in " ".join(_text_rows(_render(post)[1]))
        total += 1
    assert total == 21 and hits >= 20


def test_portal_structure_labels_are_kept():
    for post, conclusion in _reply_posts():
        html_out, text_out = _render(post)
        rows = _text_rows(text_out)
        assert [row.split(":", 1)[0] for row in rows] == ["질의요지", "회답", "이유"]
        assert conclusion in rows[1]
        for rendered in (html_out, text_out):
            assert "원문 발췌" in rendered and "문장 선별" not in rendered and NOTICE not in rendered


def test_ai_summary_details_and_assembly_keep_their_priority_and_labels():
    summarized = _post(" ".join(SHORT_BODIES[0]), post_id="ai")
    summarized.summary = ["요약 1", "요약 2", "요약 3"]
    detailed = _post(" ".join(SHORT_BODIES[0]), post_id="details")
    detailed.details = [("조치", "과태료 100만원")]
    bill = _post("현행법은 신고를 규정하고 있음. 이에 보고 의무를 신설하려는 것임.",
                 key=ASSEMBLY_SOURCE_KEY, post_id="bill")
    short = _post(" ".join(SHORT_BODIES[1]), post_id="short")
    html_out, text_out = _render(summarized, detailed, bill, short)
    assert "    [AI 3줄 요약]\n      · 요약 1" in text_out
    assert "과태료 100만원" in text_out and "      조치: 과태료 100만원" in text_out
    assert "    [제안이유 및 주요내용 발췌]\n" in text_out
    assert text_out.count(NOTICE) == 1 and html_out.count(NOTICE) == 1  # 의안 카드에만
    assert "    [원문 발췌]\n" in text_out


@pytest.mark.parametrize("index", range(5))
def test_html_and_text_carry_the_same_label_rows_and_notice(index):
    title, body, key = _fixtures()[index]
    html_out, text_out = _render(_post(body, title, key=key))
    rows = _text_rows(text_out)
    assert rows
    positions = [html_out.index(html.escape(row)) for row in rows]
    assert positions == sorted(positions)
    label = re.search(r"    \[(원문 발췌[^\]]*)\]", text_out)[1]
    assert html.escape(label) in html_out
    assert (NOTICE in html_out) == (NOTICE in text_out) == ("문장 선별" in label)


# ── S3. 장애 격리 ─────────────────────────────────────────────────────────────

def _boom(*args, **kwargs):
    raise RuntimeError("injected")


def test_selector_failure_falls_back_without_blocking_other_posts(monkeypatch, caplog):
    title, body, _ = _fixtures()[1]
    long_post = _post(body, title, post_id="long")
    short_post = _post(" ".join(SHORT_BODIES[1]), post_id="short")
    monkeypatch.setattr(notifier, "build_rule_excerpt_rows", _boom)
    with caplog.at_level("WARNING", logger="src.notifier"):
        html_out, text_out = _render(long_post, short_post)
    safe = build_fallback_snippet(clean_body_text(body, title))
    for rendered in (html_out, text_out):
        assert html.escape(safe) in rendered or safe in rendered
        assert all(s in rendered for s in SHORT_BODIES[1])  # 다른 게시물은 정상
    assert "fsc_press:long" in caplog.text and "RuntimeError" in caplog.text
    assert body[:40] not in caplog.text and "injected" not in caplog.text


@pytest.mark.parametrize("target", ["build_full_excerpt_lines", "build_reply_excerpt_rows", "has_oversized_token"])
def test_any_excerpt_stage_failure_uses_safe_excerpt(monkeypatch, target):
    body = " ".join(SHORT_BODIES[2])
    monkeypatch.setattr(notifier, target, _boom)
    html_out, text_out = _render(_post(body))
    assert _text_rows(text_out) == [build_fallback_snippet(clean_body_text(body, "보고제도 안내"))]
    assert "    [원문 발췌]\n" in text_out and NOTICE not in html_out


def test_classification_failure_is_treated_as_content(monkeypatch):
    monkeypatch.setattr(notifier, "classify_body", _boom)
    html_out, text_out = _render(_post(" ".join(SHORT_BODIES[0])))
    assert _text_rows(text_out) == SHORT_BODIES[0]
    assert "본문 안내" not in html_out


def test_safe_excerpt_failure_uses_bounded_raw_text(monkeypatch):
    body = "보고 의무를 신설한다. " * 40
    monkeypatch.setattr(notifier, "build_rule_excerpt_rows", _boom)
    monkeypatch.setattr(notifier, "build_full_excerpt_lines", _boom)
    monkeypatch.setattr(notifier, "build_fallback_snippet", _boom)
    rows = _text_rows(_render(_post(body))[1])
    assert rows == [" ".join(body.split())[:snippet.SNIPPET_LIMIT] + " …"]


# ── S4. 비용 상한: 선별 정규식 전에 결정적으로 우회 ──────────────────────────────

@pytest.mark.parametrize("body", [
    "1" * 50000,
    "보고 의무를 신설한다. 계좌번호는 " + "1234567890" * 11 + "이다.",
    "가." * 20000,
])
def test_abnormal_tokens_never_reach_the_selection_regexes(monkeypatch, body):
    def forbidden(*args, **kwargs):
        pytest.fail("abnormal input reached a selection path")

    for name in ("build_rule_excerpt_rows", "build_full_excerpt_lines", "build_reply_excerpt_rows"):
        monkeypatch.setattr(notifier, name, forbidden)
    monkeypatch.setattr(snippet, "_key_sentences", forbidden)
    rows = _text_rows(_render(_post(body))[1])
    assert rows == [build_fallback_snippet(clean_body_text(body, "보고제도 안내"))]


def test_joined_digit_groups_are_checked_before_sentence_splitting(monkeypatch):
    body = "보고 의무를 신설한다.\n1,\n" + "\n".join(["234,"] * 60)
    assert not has_oversized_token(normalize_lines(body))  # 줄 단위로는 짧다

    def forbidden(text):
        pytest.fail("joined oversized token reached _key_sentences")

    monkeypatch.setattr(snippet, "_key_sentences", forbidden)
    assert build_rule_excerpt_rows(body, "보고 의무 신설") == []


@pytest.mark.parametrize(("text", "oversized"), [
    ("https://example.com/" + "document" * 45, False),   # 기존 긴 URL 회귀 입력
    ("1" * 100, False),
    ("1" * 101, True),
    ("1,234." * 17, True),
    ("1" + "." * 150, True),
    ("." * 150, False),                                   # 숫자 없는 구분선은 비용이 없다
    ("," * 150, False),
    ("가" * 1000, False),
    ("가" * 1001, True),
    (" ".join(["1" * 100] * 50), False),
])
def test_oversized_token_rule(text, oversized):
    assert has_oversized_token(text) is oversized


def test_real_fixtures_are_not_bypassed():
    for title, body, _ in _fixtures():
        assert not has_oversized_token(normalize_lines(body))
    for post, _ in _reply_posts():
        assert not has_oversized_token(normalize_lines(post.body))


# ── 공용 판정(Gemini 대상 판정)의 기존 영향이 더 바뀌지 않았는지 ──────────────────

def test_shared_classification_keeps_existing_results():
    from src.summarizer import _prepare_body
    cfg = SimpleNamespace(min_body_chars=80, max_input_chars=6000)
    for title, body, key in _fixtures():
        post = _post(body, title, key=key)
        assert classify_body(body, title) is BodyKind.CONTENT
        assert _prepare_body(cfg, post)
    # 이 PR의 공용 정제 변경(첨부 목록 라벨+파일명)으로 첨부 안내만 남는 본문은 요약 대상이 아니다.
    for body in ("첨부파일 목록\n보도자료.pdf\n자세한 내용은 첨부파일을 참고하시기 바랍니다.",
                 "첨부 파일 (2)\n보도자료.hwp\n보도자료.pdf\n자세한 내용은 첨부파일을 참고하시기 바랍니다."):
        assert classify_body(body, "제목") is BodyKind.ATTACHMENT_REFERENCE_ONLY
        assert _prepare_body(cfg, _post(body + " " * 80, "제목")) == ""
