"""본문이 첨부 참조 안내뿐인 일반 게시물 처리 테스트.

"자세한 내용은 첨부파일 참고 부탁드립니다." 만 있는 글은 요약할 내용이 없으므로
  - AI 요약 대상·집계에서 빠지고 Gemini 를 부르지 않으며,
  - 메일에는 그 안내문 대신 짧은 '본문 안내'를 싣는다.
정상 게시물(요약·발췌·구조화 항목·의안)의 경로는 그대로여야 한다.

네트워크 호출은 하지 않는다 — Summarizer._generate 를 가짜로 대체한다.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import main as main_mod
from src.config import LLMConfig
from src.models import ASSEMBLY_SOURCE_KEY, Attachment, Post
from src.notifier import build_html, build_text
from src.snippet import BodyKind, build_fallback_snippet, classify_body
from src.summarizer import Summarizer, ai_target_count

SOURCE = "금융위 · 보도자료"
TITLE = "게시물 제목"
NOTICE = "자세한 내용은 첨부파일 참고 부탁드립니다."
# 스크래퍼가 상세 페이지를 get_text("\n") 으로 펼치면 제목·머리말·첨부 목록이 함께 온다.
RAW_NOTICE_BODY = (
    f"{TITLE}\n"
    "등록일 2026-09-23\n"
    "담당부서 금융정책과\n"
    "\n"
    f"{NOTICE}\n"
    "\n"
    "첨부파일\n"
    "보도자료.hwpx\n"
    "보도자료.pdf\n"
)
# 금융위 보도자료 형태의 정상 본문(요약 대상 길이).
NORMAL_BODY = (
    "금융위원회는 9월 23일 정례회의에서 「대부업 등의 등록 및 금융이용자 보호에 관한 "
    "법률 시행령」 일부개정안을 의결하였다고 밝혔다. 개정안은 대부업자의 등록요건을 "
    "강화하고 불법사금융 피해 구제 절차를 정비하는 내용을 담고 있다. 개정 시행령은 "
    "공포 후 3개월이 경과한 날부터 시행된다."
)
ATTACHMENT_ONLY_TEXT = "웹 본문에 상세 내용이 없어 첨부파일을 확인해 주세요."
NO_FILES_TEXT = "웹 본문에는 첨부자료 참조 안내만 있습니다."
GENERAL_FALLBACK_DIV = (
    "<div style='margin:8px 0 0;font-size:13px;line-height:1.6;color:#475569'>"
)


def _cfg(**over) -> LLMConfig:
    base = dict(
        enabled=True,
        model="gemini-flash-latest",
        lines=3,
        max_line_chars=90,
        min_body_chars=80,
        max_input_chars=6000,
        max_posts=10,
        rpm=0,
        timeout_sec=5,
        max_retries=0,
        retry_backoff_sec=0,
        api_key="test-key",
    )
    base.update(over)
    return LLMConfig(**base)


def _attachments() -> list[Attachment]:
    return [
        Attachment(filename="보도자료.hwpx", url="https://example.com/a.hwpx"),
        Attachment(filename="보도자료.pdf", url="https://example.com/a.pdf"),
    ]


def _post(body=RAW_NOTICE_BODY, **over) -> Post:
    kw = dict(
        source_key="fsc_press",
        source_name=SOURCE,
        post_id="1",
        title=TITLE,
        url="https://example.com/1",
        date="2026-09-23",
        body=body,
    )
    kw.update(over)
    return Post(**kw)


def _envelope(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


# ── A. classifier ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "자세한 내용은 첨부파일 참고 부탁드립니다.",                   # 1
        "세부사항은 붙임을 참고하시기 바랍니다.",                       # 2
        "상세 내용은 첨부자료를 확인하여 주시기 바랍니다.",             # 3
        "자세한 사항은 첨부 파일을 참고해 주세요.",
        "자세한 내용은 붙임자료를 참조하시기 바랍니다.",
        "※ 붙임 참조",
    ],
)
def test_attachment_reference_sentence_is_classified(body):
    assert classify_body(body) is BodyKind.ATTACHMENT_REFERENCE_ONLY


def test_raw_body_with_metadata_is_classified_after_edge_cleaning():
    """4 — 원본 전체에 fullmatch 하면 놓치는 실제형 본문(머리말·첨부 목록 포함)."""
    assert len(RAW_NOTICE_BODY) > 80
    assert classify_body(RAW_NOTICE_BODY, TITLE) is BodyKind.ATTACHMENT_REFERENCE_ONLY


def test_label_and_value_on_separate_lines_are_cleaned_for_classification():
    """<dt>/<dd> 가 한 줄씩 펼쳐진 머리말도 같은 판정을 받는다."""
    body = (
        f"{TITLE}\n등록일\n2026-09-23\n담당부서\n금융정책과\n{NOTICE}\n"
        "첨부파일\n보도자료.hwpx\n보도자료.pdf"
    )
    assert classify_body(body, TITLE) is BodyKind.ATTACHMENT_REFERENCE_ONLY


def test_classification_does_not_change_general_fallback_snippet():
    """판정용 정제는 발췌(build_fallback_snippet)에 섞이지 않는다 — 기존 결과 그대로."""
    assert build_fallback_snippet(RAW_NOTICE_BODY, TITLE) == (
        f"등록일 2026-09-23 담당부서 금융정책과 {NOTICE}"
    )


@pytest.mark.parametrize("body", ["", "   \n\t ", None])
def test_empty_body_is_empty(body):
    """5"""
    assert classify_body(body) is BodyKind.EMPTY


def test_short_normal_notice_is_content():
    """6 — 짧다는 이유로 첨부 안내나 EMPTY 가 되지 않는다."""
    assert classify_body("접수기간은 9월 30일까지입니다.") is BodyKind.CONTENT


@pytest.mark.parametrize(
    "body",
    [
        # 7
        "자세한 내용은 첨부파일을 참고하시기 바랍니다.\n"
        "신청은 2026년 10월 5일까지이며 대상은 금융회사입니다.",
        # 8
        "첨부파일을 참고하여 신고 절차를 진행해야 하며,\n위반 시 과태료가 부과됩니다.",
        # 9
        "붙임 자료에 따라 신고서를 제출해야 합니다.\n제출기한은 공고일로부터 30일입니다.",
        # 안내문 앞에 실제 내용이 있는 경우
        f"{NORMAL_BODY}\n{NOTICE}",
        # 안내문이 한 줄에 실제 내용과 이어진 경우
        "신청 대상은 금융회사이며, 자세한 내용은 첨부파일 참고 부탁드립니다.",
    ],
)
def test_attachment_mentions_inside_real_content_stay_content(body):
    assert classify_body(body, TITLE) is BodyKind.CONTENT


# ── B. summarizer ────────────────────────────────────────────────────────────


def _counting_summarizer(cfg=None, *, fail=None):
    s = Summarizer(cfg or _cfg())
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        if fail is not None:
            raise fail
        return _envelope('{"summary": ["요약 1", "요약 2", "요약 3"]}')

    s._generate = _generate
    return s, calls


def test_attachment_only_post_never_calls_gemini():
    """10"""
    post = _post(attachments=_attachments())
    s, calls = _counting_summarizer()

    assert s.summarize_all({SOURCE: [post]}) == 0
    assert s.summarize(post) == []
    assert calls["n"] == 0
    assert post.summary == []


def test_attachment_only_post_is_not_an_ai_target():
    """11 — 대상 수와 실제 호출 판정이 같은 규칙을 쓴다."""
    post = _post(attachments=_attachments())
    assert ai_target_count(_cfg(), {SOURCE: [post]}) == 0


def test_normal_body_is_still_an_ai_target_and_summarized():
    """12 — 정상 본문은 이전과 같이 대상이고 Gemini 를 부른다."""
    attach_only = _post(post_id="1", url="https://example.com/1")
    normal = _post(body=NORMAL_BODY, post_id="2", url="https://example.com/2",
                   title="대부업법 시행령 개정안 의결")
    groups = {SOURCE: [attach_only, normal]}
    s, calls = _counting_summarizer()

    assert ai_target_count(_cfg(), groups) == 1
    assert s.summarize_all(groups) == 1
    assert calls["n"] == 1
    assert normal.summary == ["요약 1", "요약 2", "요약 3"]
    assert attach_only.summary == []


def test_short_normal_body_keeps_min_body_chars_policy():
    """13 — 짧은 정상 공지는 CONTENT 이지만 기존 길이 기준으로 대상에서 빠진다."""
    post = _post(body="접수기간은 9월 30일까지입니다.")
    s, calls = _counting_summarizer()

    assert classify_body(post.body, post.title) is BodyKind.CONTENT
    assert ai_target_count(_cfg(), {SOURCE: [post]}) == 0
    assert s.summarize_all({SOURCE: [post]}) == 0
    assert calls["n"] == 0
    # min_body_chars 를 낮추면 같은 글이 다시 대상이 된다 — 판정이 길이를 대신하지 않는다.
    assert ai_target_count(_cfg(min_body_chars=5), {SOURCE: [post]}) == 1


def test_gemini_failure_on_normal_body_keeps_fail_soft_fallback():
    """14 — 정상 본문의 요약 실패는 기존과 같이 빈 summary + 원문 발췌."""
    post = _post(body=NORMAL_BODY, title="대부업법 시행령 개정안 의결")
    s, calls = _counting_summarizer(fail=RuntimeError("HTTP 503"))

    assert s.summarize_all({SOURCE: [post]}) == 0
    assert calls["n"] == 1
    assert post.summary == []
    html_out = build_html({SOURCE: [post]})
    assert build_fallback_snippet(post.body, post.title) in html_out
    assert "본문 안내" not in html_out


# ── C. notifier ──────────────────────────────────────────────────────────────


def test_attachment_only_with_files_shows_notice_and_keeps_chips():
    """15·16"""
    post = _post(attachments=_attachments())
    html_out = build_html({SOURCE: [post]})
    text_out = build_text({SOURCE: [post]})

    assert "본문 안내" in html_out
    assert ATTACHMENT_ONLY_TEXT in html_out
    assert "📎 보도자료.hwpx" in html_out and "📎 보도자료.pdf" in html_out
    assert NOTICE not in html_out

    assert "[본문 안내]" in text_out
    assert ATTACHMENT_ONLY_TEXT in text_out
    assert "첨부: 보도자료.pdf (https://example.com/a.pdf)" in text_out
    assert NOTICE not in text_out
    assert "[원문 발췌]" not in text_out


def test_attachment_only_without_files_does_not_ask_to_open_missing_files():
    """17"""
    post = _post(attachments=[])
    html_out = build_html({SOURCE: [post]})
    text_out = build_text({SOURCE: [post]})

    for out in (html_out, text_out):
        assert NO_FILES_TEXT in out
        assert ATTACHMENT_ONLY_TEXT not in out
        assert NOTICE not in out


def test_summary_output_is_unchanged_by_the_classifier():
    """18 — 요약이 있으면 본문 성격과 무관하게 기존 요약 블록 그대로."""
    summary = ["첫째 요약", "둘째 요약", "셋째 요약"]
    attach_only = _post(summary=list(summary), attachments=_attachments())
    normal = _post(body=NORMAL_BODY, summary=list(summary), attachments=_attachments())

    assert build_html({SOURCE: [attach_only]}) == build_html({SOURCE: [normal]})
    assert build_text({SOURCE: [attach_only]}) == build_text({SOURCE: [normal]})
    assert "본문 안내" not in build_html({SOURCE: [attach_only]})


def test_details_output_still_takes_precedence():
    """19"""
    details = [("회신일", "2026-09-23"), ("질의요지", "대부업 등록요건")]
    attach_only = _post(details=list(details))
    normal = _post(body=NORMAL_BODY, details=list(details))

    assert build_html({SOURCE: [attach_only]}) == build_html({SOURCE: [normal]})
    assert build_text({SOURCE: [attach_only]}) == build_text({SOURCE: [normal]})
    assert "본문 안내" not in build_text({SOURCE: [attach_only]})


def test_general_fallback_rendering_is_unchanged():
    """20 — 요약이 없는 정상 글은 기존 발췌 블록 그대로(새 라벨 없음)."""
    post = _post(body=NORMAL_BODY)
    snippet = build_fallback_snippet(post.body, post.title)
    html_out = build_html({SOURCE: [post]})
    text_out = build_text({SOURCE: [post]})

    assert f"{GENERAL_FALLBACK_DIV}{snippet}</div>" in html_out
    assert "본문 안내" not in html_out
    assert f"    [원문 발췌]\n      {snippet}\n" in text_out


# ── D. 의안 경로 불변 ────────────────────────────────────────────────────────


def test_assembly_post_is_not_classified_or_relabelled():
    """의안은 판정을 거치지 않는다 — 같은 문장이어도 기존 제안이유 발췌로 나간다."""
    bill = _post(
        source_key=ASSEMBLY_SOURCE_KEY,
        source_name="국회 · 계류의안",
        body=NOTICE,
        title="대부업법 일부개정법률안",
    )
    html_out = build_html({"국회 · 계류의안": [bill]})
    text_out = build_text({"국회 · 계류의안": [bill]})

    assert "제안이유 및 주요내용 발췌" in html_out
    assert NOTICE in html_out
    assert "본문 안내" not in html_out
    assert "[제안이유 및 주요내용 발췌]" in text_out
    assert ai_target_count(_cfg(), {"국회 · 계류의안": [bill]}) == 1


# ── telemetry ────────────────────────────────────────────────────────────────


def test_body_kind_telemetry(caplog):
    attach_with_files = _post(post_id="1", attachments=_attachments())
    attach_no_files = _post(post_id="2", url="https://example.com/2")
    normal = _post(post_id="3", body=NORMAL_BODY)
    empty = _post(post_id="4", body="")
    bill = _post(post_id="5", source_key=ASSEMBLY_SOURCE_KEY, body=NOTICE)

    with caplog.at_level(logging.INFO, logger=main_mod.log.name):
        main_mod._log_body_kinds(
            {SOURCE: [attach_with_files, attach_no_files, normal, empty], "의안": [bill]}
        )

    messages = [r.getMessage() for r in caplog.records]
    assert (
        "본문 판정(일반) — content=1 / attachment_reference_only=2 / empty=1" in messages
    )
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "body_kind=attachment_reference_only" in warnings[0]
    assert "attachments=0" in warnings[0]
    assert f"raw_chars={len(RAW_NOTICE_BODY)}" in warnings[0]
    assert f"cleaned_chars={len(NOTICE)}" in warnings[0]
    # 본문 자체는 로그에 싣지 않는다.
    assert all(NOTICE not in m for m in messages)
