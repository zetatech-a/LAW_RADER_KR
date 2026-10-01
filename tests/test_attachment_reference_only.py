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
    from src.snippet import build_key_excerpt_lines
    assert all(line in html_out for line in build_key_excerpt_lines(post.body, post.title))
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


def test_general_fallback_rendering_selects_key_sentences():
    """20 — 정상 본문은 API 없이 핵심 원문 문장을 카드로 표시한다."""
    post = _post(body=NORMAL_BODY)
    snippet = build_fallback_snippet(post.body, post.title)
    html_out = build_html({SOURCE: [post]})
    text_out = build_text({SOURCE: [post]})

    from src.snippet import build_key_excerpt_lines
    selected = build_key_excerpt_lines(post.body, post.title)
    assert all(line in html_out for line in selected)
    assert "원문 발췌 · 핵심 3줄" in html_out
    assert "본문 안내" not in html_out
    assert all(f"      · {line}" in text_out for line in selected)


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


# ═══ 보강: PR 전 regression coverage ══════════════════════════════════════════
#
# 위 테스트와 겹치지 않는 경계만 추가한다. 요구사항 문구를 **원문 그대로**(한 줄)
# 고정하고, 판정이 길이 기준·HTTP 계층·상한·집계·다른 렌더링 경로에 새지 않는지 본다.

from types import SimpleNamespace  # noqa: E402

from src.models import ProposalContentStatus  # noqa: E402
from src.snippet import is_attachment_reference_only, normalize_lines  # noqa: E402
from src.summarizer import _prepare_body  # noqa: E402

FP_SINGLE_LINE_1 = (
    "자세한 내용은 첨부파일을 참고하시기 바랍니다. "
    "신청은 2026년 10월 5일까지이며 대상은 금융회사입니다."
)
FP_SINGLE_LINE_2 = "첨부파일을 참고하여 신고 절차를 진행해야 하며, 위반 시 과태료가 부과됩니다."


# ── 1·2. classifier: 요구 문구 원문 그대로 ────────────────────────────────────


def test_required_sentences_exact_text():
    assert (
        classify_body("자세한 내용은 첨부파일 참고 부탁드립니다.")
        is BodyKind.ATTACHMENT_REFERENCE_ONLY
    )
    assert (
        classify_body("세부사항은 붙임을 참고하시기 바랍니다.")
        is BodyKind.ATTACHMENT_REFERENCE_ONLY
    )
    assert classify_body("") is BodyKind.EMPTY
    assert classify_body(" 　\n\t  ") is BodyKind.EMPTY  # 전각·nbsp 공백 포함
    assert classify_body("접수기간은 9월 30일까지입니다.") is BodyKind.CONTENT


@pytest.mark.parametrize("body", [FP_SINGLE_LINE_1, FP_SINGLE_LINE_2])
@pytest.mark.parametrize("title", ["", TITLE])
def test_false_positive_single_line_stays_content(body, title):
    """안내문과 실제 내용이 한 줄에 이어진 경우도 CONTENT (substring 판정 금지)."""
    assert classify_body(body, title) is BodyKind.CONTENT


@pytest.mark.parametrize("body", [FP_SINGLE_LINE_1, FP_SINGLE_LINE_2])
def test_false_positive_stays_content_even_wrapped_in_page_noise(body):
    """머리말·첨부 목록을 걷어낸 뒤에도 실제 내용이 남으면 CONTENT."""
    raw = (
        f"{TITLE}\n등록일 2026-09-23\n담당부서 금융정책과\n{body}\n"
        "첨부파일\n보도자료.hwpx\n보도자료.pdf"
    )
    assert classify_body(raw, TITLE) is BodyKind.CONTENT


@pytest.mark.parametrize("body", [FP_SINGLE_LINE_1, FP_SINGLE_LINE_2])
def test_false_positive_is_still_summarized(body):
    """오판이 없으면 정상 본문은 그대로 Gemini 를 부른다."""
    post = _post(body=body)
    s, calls = _counting_summarizer(_cfg(min_body_chars=10))

    assert ai_target_count(_cfg(min_body_chars=10), {SOURCE: [post]}) == 1
    assert s.summarize_all({SOURCE: [post]}) == 1
    assert calls["n"] == 1


# ── 3. raw body noise × min_body_chars ────────────────────────────────────────


def test_raw_noise_passes_length_gate_but_is_excluded_by_classifier():
    """raw body 는 min_body_chars 를 넘는다 — 제외 사유는 길이가 아니라 판정이다."""
    cfg = _cfg()  # min_body_chars=80
    post = _post(attachments=_attachments())
    collapsed = " ".join(post.body.split())

    assert len(collapsed) >= cfg.min_body_chars
    assert len(NOTICE) < cfg.min_body_chars
    # 원본에 바로 fullmatch 하는 구현이라면 놓친다(이 테스트가 그 회귀를 막는다).
    assert not is_attachment_reference_only(collapsed)
    assert not is_attachment_reference_only(" ".join(normalize_lines(post.body)))
    assert classify_body(post.body, post.title) is BodyKind.ATTACHMENT_REFERENCE_ONLY
    assert _prepare_body(cfg, post) == ""
    # 같은 머리말에 실제 본문이 오면 그대로 대상이다.
    normal = _post(body=RAW_NOTICE_BODY.replace(NOTICE, NORMAL_BODY))
    assert _prepare_body(cfg, normal) != ""


def test_raw_noise_long_enough_for_default_config_threshold():
    """config.yaml 기본 min_body_chars 로 로드해도 같은 결론이다."""
    from src.config import load_config

    llm = load_config().llm
    padded = RAW_NOTICE_BODY.replace(
        "보도자료.pdf", "\n".join(f"보도자료_{i}.pdf" for i in range(20))
    )
    post = _post(body=padded, attachments=_attachments())

    assert len(" ".join(padded.split())) >= llm.min_body_chars
    assert classify_body(post.body, post.title) is BodyKind.ATTACHMENT_REFERENCE_ONLY
    assert _prepare_body(llm, post) == ""


# ── 4. summarizer ─────────────────────────────────────────────────────────────


def test_attachment_only_never_reaches_http_layer():
    """_generate 보다 아래(HTTP 세션)에서도 호출이 0회다."""
    s = Summarizer(_cfg())
    http = {"n": 0}

    def _post_http(*a, **kw):
        http["n"] += 1
        raise AssertionError("attachment-only 글로 HTTP 요청을 보내면 안 된다")

    s.session.post = _post_http
    post = _post(attachments=_attachments())

    assert s.summarize_all({SOURCE: [post]}) == 0
    assert http["n"] == 0
    assert post.summary == []


def test_attachment_only_does_not_consume_max_posts_quota():
    """상한 적용 전에 빠지므로 뒤쪽 정상 글이 요약 몫을 잃지 않는다."""
    attach_only = _post(post_id="1", url="https://example.com/1")
    normal = _post(body=NORMAL_BODY, post_id="2", url="https://example.com/2")
    s, calls = _counting_summarizer(_cfg(max_posts=1))

    assert s.summarize_all({SOURCE: [attach_only, normal]}) == 1
    assert calls["n"] == 1
    assert normal.summary and not attach_only.summary


def test_ai_target_count_matches_actual_calls_on_mixed_batch():
    """집계 대상 수 == 실제 호출 수 (판정 기준이 하나다)."""
    posts = [
        _post(post_id="1", url="https://example.com/1"),                       # 첨부 안내
        _post(post_id="2", url="https://example.com/2", body=NORMAL_BODY),     # 정상
        _post(post_id="3", url="https://example.com/3", body="짧은 공지입니다."),  # 짧음
        _post(post_id="4", url="https://example.com/4", body=""),              # 빈 본문
        _post(post_id="5", url="https://example.com/5", body=NORMAL_BODY + " " + NOTICE),
    ]
    s, calls = _counting_summarizer()

    assert ai_target_count(_cfg(), {SOURCE: posts}) == 2
    s.summarize_all({SOURCE: posts})
    assert calls["n"] == 2


def test_ai_summary_log_excludes_attachment_only_from_fallback(caplog):
    """AI 집계의 대상·발췌 폴백 건수에 attachment-only 가 잡히지 않는다."""
    attach_only = _post(post_id="1", url="https://example.com/1")
    normal = _post(body=NORMAL_BODY, post_id="2", url="https://example.com/2")
    s, _ = _counting_summarizer()
    s.summarize_all({SOURCE: [attach_only, normal]})

    with caplog.at_level(logging.INFO, logger=main_mod.log.name):
        main_mod._log_ai_summary(
            SimpleNamespace(llm=_cfg()), {SOURCE: [attach_only, normal]}
        )
    assert "AI 요약 집계 — 대상 1건 / 요약 1건 / 발췌 폴백 0건" in [
        r.getMessage() for r in caplog.records
    ]


def test_normal_sufficient_body_prepared_input_is_unchanged():
    """정상 본문의 요약 입력(공백 정규화 + 절단)은 판정 추가 전과 같다."""
    cfg = _cfg(max_input_chars=50)
    post = _post(body=NORMAL_BODY)
    assert _prepare_body(cfg, post) == " ".join(NORMAL_BODY.split())[:50]


def test_short_attachment_only_is_excluded_by_length_first():
    """min_body_chars 미달이면 판정 이전에 기존 규칙으로 빠진다(순서 불변)."""
    post = _post(body=NOTICE)
    assert len(NOTICE) < _cfg().min_body_chars
    assert _prepare_body(_cfg(), post) == ""
    assert ai_target_count(_cfg(), {SOURCE: [post]}) == 0


# ── 5. notifier ───────────────────────────────────────────────────────────────


def test_attachment_only_short_body_still_gets_notice():
    """렌더링은 길이와 무관하다 — 안내문 한 줄짜리 글도 재출력하지 않는다."""
    post = _post(body=NOTICE, attachments=_attachments())
    html_out = build_html({SOURCE: [post]})
    text_out = build_text({SOURCE: [post]})

    assert "본문 안내" in html_out and ATTACHMENT_ONLY_TEXT in html_out
    assert "[본문 안내]" in text_out and ATTACHMENT_ONLY_TEXT in text_out
    assert NOTICE not in html_out and NOTICE not in text_out
    assert build_fallback_snippet(post.body, post.title) not in html_out


def test_attachment_only_notice_appears_exactly_once_per_part():
    post = _post(attachments=_attachments())
    assert build_html({SOURCE: [post]}).count(ATTACHMENT_ONLY_TEXT) == 1
    assert build_text({SOURCE: [post]}).count(ATTACHMENT_ONLY_TEXT) == 1


def test_attachment_only_without_files_mentions_no_file_check():
    post = _post(attachments=[])
    for out in (build_html({SOURCE: [post]}), build_text({SOURCE: [post]})):
        assert "첨부파일을 확인" not in out
        assert "📎" not in out
        assert "첨부:" not in out


def test_attachment_only_does_not_add_ai_notice():
    """AI 요약이 없으므로 하단 AI 유의사항도 붙지 않는다."""
    post = _post(attachments=_attachments())
    assert "AI 요약은 생성형 AI" not in build_html({SOURCE: [post]})
    assert "AI 요약은 생성형 AI" not in build_text({SOURCE: [post]})


def test_mixed_digest_changes_only_the_attachment_only_card():
    """같은 메일 안에서 정상 글의 발췌·요약 카드는 단독 렌더링과 같다."""
    attach_only = _post(post_id="1", url="https://example.com/1",
                        attachments=_attachments())
    fallback = _post(post_id="2", url="https://example.com/2", body=NORMAL_BODY)
    summarized = _post(post_id="3", url="https://example.com/3", body=NORMAL_BODY,
                       summary=["요약 1", "요약 2", "요약 3"])
    text_out = build_text({SOURCE: [attach_only, fallback, summarized]})
    html_out = build_html({SOURCE: [attach_only, fallback, summarized]})

    snippet = build_fallback_snippet(NORMAL_BODY, TITLE)
    from src.snippet import build_key_excerpt_lines
    selected = build_key_excerpt_lines(NORMAL_BODY, TITLE)
    assert all(f"      · {line}" in text_out for line in selected)
    assert all(line in html_out for line in selected)
    assert "    [AI 3줄 요약]\n      · 요약 1\n      · 요약 2\n      · 요약 3\n" in text_out
    assert html_out.count("본문 안내") == 1
    assert text_out.count("[본문 안내]") == 1


def test_empty_general_body_output_is_unchanged():
    """EMPTY 는 관찰용이다 — 빈 본문 카드는 예전처럼 본문 블록이 없다."""
    post = _post(body="")
    html_out = build_html({SOURCE: [post]})
    text_out = build_text({SOURCE: [post]})

    assert "본문 안내" not in html_out and "[본문 안내]" not in text_out
    assert "[원문 발췌]" not in text_out
    assert GENERAL_FALLBACK_DIV not in html_out


def test_assembly_pending_output_is_unchanged():
    bill = _post(
        source_key=ASSEMBLY_SOURCE_KEY,
        source_name="국회 · 계류의안",
        title="대부업법 일부개정법률안",
        body="",
        proposal_status=ProposalContentStatus.PENDING,
    )
    html_out = build_html({"국회 · 계류의안": [bill]})
    text_out = build_text({"국회 · 계류의안": [bill]})

    assert "제안이유 및 주요내용 · 등록 대기" in html_out
    assert "[제안이유 및 주요내용 · 등록 대기]" in text_out
    assert "본문 안내" not in html_out and "[본문 안내]" not in text_out


def test_assembly_summary_and_detail_update_output_unchanged():
    """의안 AI 요약·상세 업데이트 섹션은 본문이 안내문이어도 기존 라벨 그대로."""
    bill = _post(
        source_key=ASSEMBLY_SOURCE_KEY,
        source_name="국회 · 계류의안",
        title="대부업법 일부개정법률안",
        body=NOTICE,
        summary=["의안 요약 1", "의안 요약 2", "의안 요약 3"],
        proposal_status=ProposalContentStatus.AVAILABLE,
    )
    updates = {"국회 · 계류의안": [bill]}
    html_out = build_html({}, detail_updates_by_source=updates)
    text_out = build_text({}, detail_updates_by_source=updates)

    assert "제안이유 및 주요내용 · AI 3줄 요약" in html_out
    assert "[제안이유 및 주요내용 · AI 3줄 요약]" in text_out
    assert "의안 상세 업데이트" in text_out
    assert "본문 안내" not in html_out and "[본문 안내]" not in text_out


# ═══ PR #34 Codex 리뷰 대응 ══════════════════════════════════════════════════
#
# R1: 판정은 발췌용 상용구 규칙(_PRESS_NOTICE·_JS_NOTICE·_SURVEY)으로 실제 문장을 지우면
#     안 된다 — 지우고 나면 첨부 안내만 남아 본문 전체가 요약에서 빠진다.
# R2: "첨부된 파일/자료" 표현.
# R3: '붙임' 라벨 + 파일명 목록.

from src.snippet import (  # noqa: E402
    _JS_NOTICE,
    _PRESS_NOTICE,
    _SURVEY,
    clean_body_text,
    strip_edge_noise,
)

# Codex 가 제시한 문장 그대로("명확히 표기"). 현재 _PRESS_NOTICE 는 "출처를" 바로 뒤에
# 동사가 와야 걸리므로 이 문장은 원래도 걸리지 않았다 — 그래도 회귀 방지로 고정한다.
R1_CODEX_EXACT = (
    "온라인 금융상품 광고에는 수익률을 인용할 때 소비자가 확인할 수 있도록 "
    "자료의 출처를 명확히 표기해 주시기 바랍니다."
)
# 같은 취지로 _PRESS_NOTICE 에 **실제로 걸리는** 문장(수정 전 오판 재현).
R1_PRESS_MATCHING = (
    "온라인 금융상품 광고에는 수익률을 인용할 때 "
    "소비자가 확인할 수 있도록 자료의 출처를 표기해 주시기 바랍니다."
)
R1_PRESS_SHORT = "온라인 금융상품 광고에는 자료의 출처를 표기해 주시기 바랍니다."
R1_JS = "이 서비스는 자바스크립트를 사용하므로 브라우저 설정에서 허용해 주시기 바랍니다."
R1_SURVEY = "이 페이지의 정보에 만족하십니까?"


def test_r1_codex_exact_sentence_does_not_hit_press_notice_rule():
    """재현 기록: Codex 원문("명확히 표기")은 현재 규칙에 걸리지 않는다."""
    assert not _PRESS_NOTICE.match(R1_CODEX_EXACT)
    assert classify_body(f"{R1_CODEX_EXACT}\n{NOTICE}") is BodyKind.CONTENT


@pytest.mark.parametrize(
    "sentence, rule",
    [
        (R1_PRESS_MATCHING, _PRESS_NOTICE),
        (R1_PRESS_SHORT, _PRESS_NOTICE),
        (R1_JS, _JS_NOTICE),
        (R1_SURVEY, _SURVEY),
    ],
)
def test_r1_sentence_rules_do_not_strip_text_before_classification(sentence, rule):
    """발췌에서 지우는 문장 모양 규칙에 걸려도 판정에서는 남는다 → CONTENT."""
    assert rule.match(sentence)  # 전제: 발췌용 규칙에는 실제로 걸리는 문장이다
    body = f"{sentence}\n{NOTICE}"
    assert clean_body_text(body) == f"{sentence} {NOTICE}"
    assert classify_body(body) is BodyKind.CONTENT
    # 뒤에 붙은 경우도 같다.
    assert classify_body(f"{NOTICE}\n{sentence}") is BodyKind.CONTENT


def test_r1_semantic_edge_sentence_stays_ai_target_even_with_page_noise():
    """머리말이 붙어 raw 가 길어도 실제 안내 문장이 있으면 요약 대상 그대로."""
    body = RAW_NOTICE_BODY.replace(NOTICE, f"{R1_PRESS_MATCHING}\n{NOTICE}")
    post = _post(body=body, attachments=_attachments())
    s, calls = _counting_summarizer()

    assert classify_body(post.body, post.title) is BodyKind.CONTENT
    assert ai_target_count(_cfg(), {SOURCE: [post]}) == 1
    assert s.summarize_all({SOURCE: [post]}) == 1
    assert calls["n"] == 1


def test_r1_general_fallback_still_strips_press_notice():
    """발췌(기본 모드)는 예전처럼 보도 안내를 걷어낸다 — 판정 모드만 보수적이다."""
    lines = [R1_PRESS_MATCHING, BODY_FOR_FALLBACK]
    assert strip_edge_noise(lines) == [BODY_FOR_FALLBACK]
    assert strip_edge_noise(lines, for_classification=True) == lines
    assert build_fallback_snippet("\n".join(lines)) == BODY_FOR_FALLBACK


BODY_FOR_FALLBACK = "금융위원회는 대부업 등록요건 강화 방안을 발표하였다."


@pytest.mark.parametrize(
    "body",
    [
        "자세한 내용은 첨부된 파일을 확인해 주시기 바랍니다.",
        "세부사항은 첨부된 자료를 참고하시기 바랍니다.",
    ],
)
def test_r2_attached_file_phrase_is_attachment_reference(body):
    assert classify_body(body) is BodyKind.ATTACHMENT_REFERENCE_ONLY


@pytest.mark.parametrize(
    "body",
    [
        "첨부된 파일을 검토한 결과 위반 사실이 확인되었습니다.\n과태료는 500만원입니다.",
        "첨부된 자료는 신고 시 제출해야 하며,\n제출기한은 2026년 10월 10일까지입니다.",
        # 대상 명사 없는 "첨부된" 은 받지 않는다.
        "자세한 내용은 첨부된 참고하시기 바랍니다.",
    ],
)
def test_r2_attached_file_mentions_in_content_stay_content(body):
    assert classify_body(body) is BodyKind.CONTENT


def test_r2_attached_file_phrase_with_page_noise_skips_gemini():
    body = RAW_NOTICE_BODY.replace(
        NOTICE, "자세한 내용은 첨부된 파일을 확인해 주시기 바랍니다."
    )
    post = _post(body=body, attachments=_attachments())
    s, calls = _counting_summarizer()

    assert len(" ".join(body.split())) >= _cfg().min_body_chars
    assert classify_body(post.body, post.title) is BodyKind.ATTACHMENT_REFERENCE_ONLY
    assert ai_target_count(_cfg(), {SOURCE: [post]}) == 0
    assert s.summarize_all({SOURCE: [post]}) == 0
    assert calls["n"] == 0


R3_BODY = "자세한 내용은 붙임을 참고하시기 바랍니다.\n붙임\n보도자료.hwp\n보도자료.pdf"
R3_RAW_BODY = (
    f"{TITLE}\n"
    "등록일 2026-09-29\n"
    "담당부서 금융정책과\n"
    "\n"
    "자세한 내용은 붙임을 참고하시기 바랍니다.\n"
    "\n"
    "붙임\n"
    "보도자료.hwp\n"
    "보도자료.pdf\n"
)


def test_r3_enclosure_label_with_file_list_is_attachment_reference():
    assert classify_body(R3_BODY) is BodyKind.ATTACHMENT_REFERENCE_ONLY
    assert classify_body(R3_RAW_BODY, TITLE) is BodyKind.ATTACHMENT_REFERENCE_ONLY


@pytest.mark.parametrize("label", ["붙임", "붙임자료", "붙임 자료", "붙임파일", "붙임:"])
def test_r3_enclosure_label_variants(label):
    body = f"자세한 내용은 붙임을 참고하시기 바랍니다.\n{label}\n1. 보도자료.hwpx"
    assert classify_body(body) is BodyKind.ATTACHMENT_REFERENCE_ONLY


def test_r3_enclosure_list_before_notice_is_also_stripped():
    body = "붙임\n보도자료.hwp\n자세한 내용은 붙임을 참고하시기 바랍니다."
    assert classify_body(body) is BodyKind.ATTACHMENT_REFERENCE_ONLY


@pytest.mark.parametrize(
    "body",
    [
        # 파일명이 없으면 '붙임' 줄은 목록이 아니다 — 구조적 증거가 없으면 지우지 않는다.
        "자세한 내용은 붙임을 참고하시기 바랍니다.\n붙임",
        # 목록 뒤에 실제 내용이 오면 CONTENT.
        "붙임\n보도자료.hwp\n신청기간은 10월 5일까지입니다.\n"
        "자세한 내용은 붙임을 참고하시기 바랍니다.",
        # 목록이 있어도 실제 내용 문장이 있으면 CONTENT.
        "신청기간은 10월 5일까지입니다.\n자세한 내용은 붙임을 참고하시기 바랍니다.\n"
        "붙임\n보도자료.hwp",
    ],
)
def test_r3_enclosure_handling_does_not_create_false_positives(body):
    assert classify_body(body) is BodyKind.CONTENT


def test_r3_raw_body_over_min_body_chars_is_excluded_and_skips_gemini():
    """길이 기준은 통과하는 raw body — 제외 사유는 판정이다."""
    cfg = _cfg(min_body_chars=60)
    post = _post(body=R3_RAW_BODY, attachments=_attachments())
    s, calls = _counting_summarizer(cfg)

    assert len(" ".join(R3_RAW_BODY.split())) >= cfg.min_body_chars
    assert _prepare_body(cfg, post) == ""
    assert ai_target_count(cfg, {SOURCE: [post]}) == 0
    assert s.summarize_all({SOURCE: [post]}) == 0
    assert calls["n"] == 0


def test_r3_general_fallback_snippet_is_unchanged():
    """'붙임' 목록 처리는 판정 전용이다 — 발췌 결과는 수정 전과 같다."""
    assert build_fallback_snippet(R3_BODY) == (
        "자세한 내용은 붙임을 참고하시기 바랍니다. 붙임 보도자료.hwp 보도자료.pdf"
    )


# ── 세 리뷰를 함께 고친 뒤의 invariant ─────────────────────────────────────────


@pytest.mark.parametrize(
    "body, title, expected",
    [
        (NOTICE, "", BodyKind.ATTACHMENT_REFERENCE_ONLY),
        ("자세한 내용은 첨부된 파일을 확인해 주시기 바랍니다.", "",
         BodyKind.ATTACHMENT_REFERENCE_ONLY),
        ("자세한 내용은 붙임을 참고하시기 바랍니다.\n붙임\nfoo.pdf", "",
         BodyKind.ATTACHMENT_REFERENCE_ONLY),
        (f"{TITLE}\n등록일 2026-09-23\n담당부서 금융정책과\n{NOTICE}\n첨부파일\nfoo.pdf",
         TITLE, BodyKind.ATTACHMENT_REFERENCE_ONLY),
        (f"신청기간은 10월 5일까지입니다.\n{NOTICE}", "", BodyKind.CONTENT),
        (f"{R1_PRESS_SHORT}\n{NOTICE}", "", BodyKind.CONTENT),
        ("첨부된 파일을 검토한 결과 위반 사실이 확인되었습니다.\n과태료는 500만원입니다.",
         "", BodyKind.CONTENT),
    ],
)
def test_codex_review_invariants(body, title, expected):
    assert classify_body(body, title) is expected
