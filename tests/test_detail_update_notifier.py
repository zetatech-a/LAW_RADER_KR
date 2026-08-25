"""'의안 상세 업데이트' 알림 카테고리의 메일 렌더링 계약.

핵심:
  - 기본 호출(신규만)의 출력은 한 글자도 달라지지 않는다.
  - 업데이트만 있는 메일을 '신규 게시물'이라고 표시하지 않는다.
  - 신규 + 업데이트는 한 통 안에 두 섹션으로 들어간다.
  - HTML escape, AI 유의사항, 첨부 처리는 기존 그대로다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import Attachment, Post, ProposalContentStatus as S
from src.notifier import build_html, build_subject, build_text, send_digest

ASSEMBLY = "의안정보시스템 · 계류의안"
PRESS = "금감원 · 보도자료"


class _Cfg:
    subject_prefix = "[LAW RADER]"
    from_name = "LAW RADER"
    mail_from = "s@example.com"
    recipients = ["to@example.com"]
    smtp_host = "smtp.example.com"
    smtp_port = 587
    smtp_user = "s@example.com"
    smtp_password = "pw"
    max_attach_bytes = 10 * 1024 * 1024


def _bill(pid="PRC_1", title="법률안 (홍길동의원)", body="제안이유 및 주요내용 본문", summary=None):
    p = Post(
        source_key="assembly_bill",
        source_name=ASSEMBLY,
        post_id=pid,
        title=title,
        url=f"https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={pid}",
        date="2026-08-24",
        body=body,
    )
    p.proposal_status = S.AVAILABLE if body else S.PENDING
    p.summary = summary or []
    return p


def _press(pid="1"):
    return Post(
        source_key="fss_press", source_name=PRESS, post_id=pid,
        title=f"보도자료 {pid}", url=f"https://e.com/{pid}", date="2026-08-24",
        body="본문 " * 40,
    )


# --- 기본 호출 회귀 ---


def test_default_call_signature_unchanged():
    """기존 호출(두 번째 인자 없음)이 그대로 동작해야 한다."""
    grouped = {PRESS: [_press()]}
    assert "신규 게시물 1건" in build_html(grouped)
    assert build_text(grouped).startswith("신규 게시물 1건")


def test_empty_updates_render_identically_to_none():
    grouped = {PRESS: [_press()]}
    assert build_html(grouped, {}) == build_html(grouped)
    assert build_text(grouped, {}) == build_text(grouped)
    assert "의안 상세 업데이트" not in build_html(grouped)
    assert "의안 상세 업데이트" not in build_text(grouped)


def test_new_only_subject_is_unchanged():
    grouped = {PRESS: [_press("1"), _press("2")]}
    assert build_subject(_Cfg(), grouped) == "[LAW RADER] 신규 2건 · 금감원 · 보도자료"


def test_new_only_subject_with_multiple_sources_unchanged():
    grouped = {PRESS: [_press("1"), _press("2")], ASSEMBLY: [_bill()]}
    assert build_subject(_Cfg(), grouped).endswith("외 1개 소스")


# --- 업데이트만 있는 경우 ---


def test_update_only_never_says_new():
    import re

    updates = {ASSEMBLY: [_bill()]}
    html = build_html({}, updates)
    text = build_text({}, updates)
    for out in (html, text):
        assert "의안 상세 업데이트 1건" in out
        # 건수를 세는 문구에 '신규'가 나오면 안 된다(푸터의 고정 설명문은 예외).
        assert re.search(r"신규 게시물 \d+건", out) is None
        assert "제안이유 및 주요내용이 확인되었습니다" in out


def test_update_only_text_starts_with_update_heading():
    text = build_text({}, {ASSEMBLY: [_bill()]})
    assert text.splitlines()[0] == "의안 상세 업데이트 1건"


def test_update_only_subject():
    updates = {ASSEMBLY: [_bill(), _bill("PRC_2")]}
    assert build_subject(_Cfg(), {}, updates) == "[LAW RADER] 의안 상세 업데이트 2건"


def test_update_only_renders_the_bill_card():
    updates = {ASSEMBLY: [_bill(summary=["요약1", "요약2", "요약3"])]}
    html = build_html({}, updates)
    assert "법률안 (홍길동의원)" in html
    assert "billId=PRC_1" in html
    assert "요약2" in html
    # 기존 카드 렌더링(AI 요약 라벨)을 그대로 재사용한다.
    assert "제안이유 및 주요내용 · AI 3줄 요약" in html


def test_update_title_is_not_mutated():
    """'상세 업데이트'라고 제목에 문자열을 억지로 붙이지 않는다."""
    bill = _bill(title="원래 제목 그대로")
    build_html({}, {ASSEMBLY: [bill]})
    build_text({}, {ASSEMBLY: [bill]})
    assert bill.title == "원래 제목 그대로"


# --- 신규 + 업데이트 ---


def test_mixed_has_both_counts_and_sections():
    grouped = {PRESS: [_press("1"), _press("2")]}
    updates = {ASSEMBLY: [_bill()]}
    html = build_html(grouped, updates)
    assert "신규 게시물 2건" in html
    assert "의안 상세 업데이트 1건" in html
    assert "보도자료 1" in html and "법률안 (홍길동의원)" in html


def test_mixed_text_has_both_sections():
    grouped = {PRESS: [_press("1")]}
    updates = {ASSEMBLY: [_bill()]}
    text = build_text(grouped, updates)
    assert text.startswith("신규 게시물 1건\n의안 상세 업데이트 1건")
    assert f"[{PRESS}] (1건)" in text
    assert "=== 의안 상세 업데이트 (1건) ===" in text
    assert f"[{ASSEMBLY}] (1건)" in text


def test_mixed_subject_shows_both_kinds():
    grouped = {PRESS: [_press("1"), _press("2"), _press("3"), _press("4")]}
    updates = {ASSEMBLY: [_bill(), _bill("PRC_2")]}
    assert build_subject(_Cfg(), grouped, updates) == (
        "[LAW RADER] 신규 4건 · 의안 상세 업데이트 2건"
    )


def test_update_section_comes_after_new_section():
    grouped = {PRESS: [_press("1")]}
    updates = {ASSEMBLY: [_bill()]}
    html = build_html(grouped, updates)
    assert html.index("보도자료 1") < html.index("법률안 (홍길동의원)")


# --- escape / AI 유의사항 / 첨부 ---


def test_html_escaping_is_kept_in_updates():
    bill = _bill(title="<script>alert(1)</script> & 법률안")
    bill.summary = ["<b>굵게</b>"]
    html = build_html({}, {ASSEMBLY: [bill]})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;굵게&lt;/b&gt;" in html


def test_ai_notice_appears_for_update_only_mail():
    updates = {ASSEMBLY: [_bill(summary=["a", "b", "c"])]}
    assert "생성형 AI" in build_html({}, updates)
    assert "생성형 AI" in build_text({}, updates)


def test_ai_notice_absent_when_no_summary_anywhere():
    updates = {ASSEMBLY: [_bill(summary=[])]}
    assert "생성형 AI" not in build_html({}, updates)
    assert "생성형 AI" not in build_text({}, updates)


def test_update_without_summary_falls_back_to_excerpt():
    """LLM 이 실패해도 제안이유 원문 발췌로 사용자에게 전달된다."""
    bill = _bill(body="제안이유 원문입니다. " * 20, summary=[])
    html = build_html({}, {ASSEMBLY: [bill]})
    text = build_text({}, {ASSEMBLY: [bill]})
    assert "제안이유 및 주요내용 발췌" in html
    assert "제안이유 및 주요내용 발췌" in text
    assert "제안이유 원문입니다" in text


# --- send_digest 통합 ---


class _FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg, to_addrs=None):
        _FakeSMTP.sent.append(msg)
        return {}


def _capture(monkeypatch):
    import smtplib
    _FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP.sent


def test_send_digest_sends_one_mail_for_new_and_updates(monkeypatch):
    sent = _capture(monkeypatch)
    send_digest(
        _Cfg(),
        {PRESS: [_press("1")]},
        detail_updates_by_source={ASSEMBLY: [_bill()]},
    )
    assert len(sent) == 1
    assert sent[0]["Subject"] == "[LAW RADER] 신규 1건 · 의안 상세 업데이트 1건"
    body = sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "의안 상세 업데이트" in body


def test_send_digest_sends_when_only_updates(monkeypatch):
    sent = _capture(monkeypatch)
    send_digest(_Cfg(), {}, detail_updates_by_source={ASSEMBLY: [_bill()]})
    assert len(sent) == 1
    assert sent[0]["Subject"] == "[LAW RADER] 의안 상세 업데이트 1건"


def test_send_digest_skips_when_nothing_at_all(monkeypatch):
    sent = _capture(monkeypatch)
    send_digest(_Cfg(), {}, detail_updates_by_source={})
    assert sent == []


def test_send_digest_keeps_attachments(monkeypatch):
    sent = _capture(monkeypatch)
    p = _press("1")
    p.attachments = [Attachment(filename="붙임.pdf", url="https://e.com/f.pdf", data=b"%PDF-1.4")]
    send_digest(_Cfg(), {PRESS: [p]})
    names = [part.get_filename() for part in sent[0].iter_attachments()]
    assert "붙임.pdf" in names
