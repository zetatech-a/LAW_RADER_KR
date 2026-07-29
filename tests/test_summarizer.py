"""LLM(Gemini) 본문 요약 및 요약 기반 메일 렌더 테스트.

네트워크 호출은 하지 않는다 — Summarizer._generate 를 가짜 응답으로 대체한다.
"""
import os
import sys
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import LLMConfig, load_config
from src.models import Post
from src.notifier import build_html, build_text
from src.summarizer import Summarizer, summarize_posts


def _cfg(**over) -> LLMConfig:
    base = dict(
        enabled=True,
        model="gemini-2.5-flash",
        lines=3,
        max_line_chars=90,
        min_body_chars=10,
        max_input_chars=1000,
        max_posts=10,
        rpm=0,               # 테스트에서는 호출 간격 대기 없음
        timeout_sec=5,
        max_retries=0,
        retry_backoff_sec=0,
        api_key="test-key",
    )
    base.update(over)
    return LLMConfig(**base)


def _post(body="가나다라마바사아자차카타파하 " * 10, **over) -> Post:
    kw = dict(
        source_key="fsc_press",
        source_name="금융위 · 보도자료",
        post_id="1",
        title="테스트 제목",
        url="https://example.com/1",
        date="2026-07-29",
        body=body,
    )
    kw.update(over)
    return Post(**kw)


def _envelope(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


def test_config_exposes_llm_defaults():
    cfg = load_config("config.yaml")
    assert cfg.llm.lines == 3
    assert cfg.llm.model  # 모델명이 비어 있으면 안 됨


def test_summarize_parses_json_schema_response():
    s = Summarizer(_cfg())
    s._generate = lambda prompt: _envelope(
        '{"summary": ["첫째 줄 요약함", "둘째 줄 요약함", "셋째 줄 요약함"]}'
    )
    assert s.summarize(_post()) == ["첫째 줄 요약함", "둘째 줄 요약함", "셋째 줄 요약함"]


def test_summarize_truncates_to_configured_lines():
    s = Summarizer(_cfg(lines=3))
    s._generate = lambda prompt: _envelope('{"summary": ["a", "b", "c", "d", "e"]}')
    assert s.summarize(_post()) == ["a", "b", "c"]


def test_summarize_falls_back_to_line_parsing():
    # 스키마를 지키지 못한 평문 응답도 불릿을 떼고 줄 단위로 읽어낸다
    s = Summarizer(_cfg())
    s._generate = lambda prompt: _envelope("- 첫 줄\n• 둘째 줄\n3. 셋째 줄")
    assert s.summarize(_post()) == ["첫 줄", "둘째 줄", "셋째 줄"]


def test_summarize_skips_short_body():
    s = Summarizer(_cfg(min_body_chars=100))

    def _fail(prompt):  # 호출되면 안 됨
        raise AssertionError("짧은 본문에는 API 를 호출하지 않아야 한다")

    s._generate = _fail
    assert s.summarize(_post(body="짧음")) == []


def test_summarize_truncates_long_input():
    captured = {}
    s = Summarizer(_cfg(max_input_chars=50))

    def _cap(prompt):
        captured["prompt"] = prompt
        return _envelope('{"summary": ["요약함"]}')

    s._generate = _cap
    s.summarize(_post(body="가" * 500))
    assert "가" * 50 in captured["prompt"]
    assert "가" * 51 not in captured["prompt"]


def test_summarize_all_fills_summary_and_survives_failure():
    ok = _post(post_id="1", url="https://example.com/1")
    bad = _post(post_id="2", url="https://example.com/2")
    nobody = _post(post_id="3", url="https://example.com/3", body="")

    s = Summarizer(_cfg())
    calls = {"n": 0}

    def _generate(prompt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("HTTP 429")
        return _envelope('{"summary": ["요약 1", "요약 2", "요약 3"]}')

    s._generate = _generate
    n = s.summarize_all({"금융위 · 보도자료": [ok, bad, nobody]})

    assert n == 1
    assert ok.summary == ["요약 1", "요약 2", "요약 3"]
    assert bad.summary == []      # 실패해도 예외 없이 빈 채로 남는다
    assert nobody.summary == []   # 본문 없는 글은 호출 대상 아님
    assert calls["n"] == 2        # 본문 있는 2건만 호출


def test_summarize_all_respects_max_posts():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(5)]
    s = Summarizer(_cfg(max_posts=2))
    calls = {"n": 0}

    def _generate(prompt):
        calls["n"] += 1
        return _envelope('{"summary": ["요약"]}')

    s._generate = _generate
    s.summarize_all({"금융위 · 보도자료": posts})
    assert calls["n"] == 2
    assert [bool(p.summary) for p in posts] == [True, True, False, False, False]


def test_summarize_posts_skips_without_api_key():
    posts = {"금융위 · 보도자료": [_post()]}
    assert summarize_posts(_cfg(api_key=""), posts) == 0
    assert posts["금융위 · 보도자료"][0].summary == []


def test_summarize_posts_skips_when_disabled():
    assert summarize_posts(_cfg(enabled=False), {"x": [_post()]}) == 0


def test_blocked_response_returns_empty():
    s = Summarizer(_cfg())
    s._generate = lambda prompt: {"candidates": [{"finishReason": "SAFETY"}]}
    assert s.summarize(_post()) == []


# --- HTTP 호출 동작 ---


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    """미리 정한 응답을 순서대로 돌려주고 보낸 payload 를 기록하는 세션."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []

    def post(self, url, headers=None, json=None, timeout=None):
        # _generate 는 payload 를 그 자리에서 고쳐 재시도하므로 스냅샷으로 남긴다.
        self.sent.append(deepcopy(json))
        return self._responses.pop(0)


def _stub(s, responses):
    s.session = _FakeSession(responses)
    return s.session


def test_generate_drops_thinking_config_without_spending_retries():
    # max_retries=0 이어도 thinkingConfig 미지원 400 은 한 번 교정 재시도한다.
    s = Summarizer(_cfg(max_retries=0))
    sess = _stub(
        s,
        [
            _FakeResponse(400, text="Unknown name \"thinkingConfig\": Cannot find field."),
            _FakeResponse(200, _envelope('{"summary": ["요약함"]}')),
        ],
    )
    assert s.summarize(_post()) == ["요약함"]
    assert "thinkingConfig" in sess.sent[0]["generationConfig"]
    assert "thinkingConfig" not in sess.sent[1]["generationConfig"]
    # 이후 호출부터는 처음부터 빼고 보낸다
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["또 요약함"]}'))])
    s.summarize(_post())
    assert "thinkingConfig" not in s.session.sent[0]["generationConfig"]


def test_generate_retries_rate_limit_then_fails():
    s = Summarizer(_cfg(max_retries=2, retry_backoff_sec=0))
    sess = _stub(s, [_FakeResponse(429, text="quota") for _ in range(3)])
    try:
        s.summarize(_post())
    except RuntimeError as e:
        assert "429" in str(e)
    else:
        raise AssertionError("429 가 계속되면 예외여야 한다")
    assert len(sess.sent) == 3  # 최초 1 + 재시도 2


def test_generate_does_not_retry_auth_error():
    s = Summarizer(_cfg(max_retries=2, retry_backoff_sec=0))
    sess = _stub(s, [_FakeResponse(403, text="API key not valid")])
    try:
        s.summarize(_post())
    except RuntimeError as e:
        assert "403" in str(e)
    else:
        raise AssertionError("설정 오류는 즉시 실패해야 한다")
    assert len(sess.sent) == 1


def test_generate_sends_api_key_and_prompt():
    s = Summarizer(_cfg())
    sess = _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))])
    s.summarize(_post(title="증권사 내부통제 개선방안"))
    body = sess.sent[0]
    prompt = body["contents"][0]["parts"][0]["text"]
    assert "증권사 내부통제 개선방안" in prompt
    assert "금융위 · 보도자료" in prompt
    assert body["generationConfig"]["responseMimeType"] == "application/json"


# --- 메일 렌더 ---


def test_email_shows_summary_instead_of_body():
    p = _post(body="원문 본문 발췌가 여기 나온다")
    p.summary = ["첫째 요약", "둘째 요약", "셋째 요약"]
    grouped = {p.source_name: [p]}

    html = build_html(grouped)
    assert ">AI 3줄 요약</div>" in html          # 카드 안 요약 배지
    assert "첫째 요약" in html and "셋째 요약" in html
    assert "원문 본문 발췌가 여기 나온다" not in html  # 원문 발췌는 대체된다

    text = build_text(grouped)
    assert "첫째 요약" in text
    assert "원문 본문 발췌가 여기 나온다" not in text


def test_email_falls_back_to_body_without_summary():
    p = _post(body="원문 본문 발췌가 여기 나온다")
    grouped = {p.source_name: [p]}

    html = build_html(grouped)
    assert "원문 본문 발췌가 여기 나온다" in html
    assert ">AI 3줄 요약</div>" not in html
    assert "생성형 AI" not in html   # 요약이 없으면 푸터 유의사항도 붙지 않는다

    assert "원문 본문 발췌가 여기 나온다" in build_text(grouped)


def test_email_escapes_summary_html():
    p = _post()
    p.summary = ["<script>alert(1)</script> 포함함"]
    assert "<script>" not in build_html({p.source_name: [p]})
