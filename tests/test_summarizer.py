"""LLM(Gemini) 본문 요약 및 요약 기반 메일 렌더 테스트.

네트워크 호출은 하지 않는다 — Summarizer._generate 를 가짜 응답으로 대체한다.
"""
import os
import sys
import time
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
    s._generate = lambda prompt, deadline=None: _envelope(
        '{"summary": ["첫째 줄 요약함", "둘째 줄 요약함", "셋째 줄 요약함"]}'
    )
    assert s.summarize(_post()) == ["첫째 줄 요약함", "둘째 줄 요약함", "셋째 줄 요약함"]


def test_summarize_truncates_to_configured_lines():
    s = Summarizer(_cfg(lines=3))
    s._generate = lambda prompt, deadline=None: _envelope('{"summary": ["a", "b", "c", "d", "e"]}')
    assert s.summarize(_post()) == ["a", "b", "c"]


def test_summarize_falls_back_to_line_parsing():
    # 스키마를 지키지 못한 평문 응답도 불릿을 떼고 줄 단위로 읽어낸다
    s = Summarizer(_cfg())
    s._generate = lambda prompt, deadline=None: _envelope("- 첫 줄\n• 둘째 줄\n3. 셋째 줄")
    assert s.summarize(_post()) == ["첫 줄", "둘째 줄", "셋째 줄"]


def test_summarize_skips_short_body():
    s = Summarizer(_cfg(min_body_chars=100))

    def _fail(prompt, deadline=None):  # 호출되면 안 됨
        raise AssertionError("짧은 본문에는 API 를 호출하지 않아야 한다")

    s._generate = _fail
    assert s.summarize(_post(body="짧음")) == []


def test_summarize_truncates_long_input():
    captured = {}
    s = Summarizer(_cfg(max_input_chars=50))

    def _cap(prompt, deadline=None):
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

    def _generate(prompt, deadline=None):
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


def test_parse_keeps_leading_numbers():
    # 목록 표식만 떼고 실제 수치(시행일·금액·비율)는 절대 건드리지 않는다.
    s = Summarizer(_cfg(lines=5))
    s._generate = lambda prompt, deadline=None: _envelope(
        '{"summary": ["2026년 1월부터 시행함", "1조원 규모로 확대함", '
        '"3.5% 인상함", "2) 제재 대상은 5개사임", "- 의견제출 기한은 9월 7일임"]}'
    )
    assert s.summarize(_post()) == [
        "2026년 1월부터 시행함",
        "1조원 규모로 확대함",
        "3.5% 인상함",
        "제재 대상은 5개사임",
        "의견제출 기한은 9월 7일임",
    ]


def test_parse_strips_only_recognized_list_markers():
    s = Summarizer(_cfg(lines=6))
    s._generate = lambda prompt, deadline=None: _envelope(
        "1. 첫째 줄임\n2) 둘째 줄임\n(3) 셋째 줄임\n• 넷째 줄임\n"
        "10.5억원 규모임\n2026.1.1. 시행함"
    )
    assert s.summarize(_post()) == [
        "첫째 줄임",
        "둘째 줄임",
        "셋째 줄임",
        "넷째 줄임",
        "10.5억원 규모임",   # 마침표 뒤 공백이 없으므로 목록 표식이 아님
        "2026.1.1. 시행함",  # 연도는 두 자리 초과 → 목록 표식이 아님
    ]


def test_parse_keeps_negative_numbers():
    # 하이픈은 음수 부호와 생김새가 같다. 뒤에 공백이 없으면 목록 표식이 아니다.
    # (부호가 사라지면 '손실'이 '이익'으로 뒤집힌다)
    s = Summarizer(_cfg(lines=6))
    s._generate = lambda prompt, deadline=None: _envelope(
        '{"summary": ["-3.5% 감소함", "-2조원 순손실 기록함", "−1.2%p 하락함", '
        '"- 영업이익은 3조원임", "—2026년 시행함", "–5% 인하함"]}'
    )
    assert s.summarize(_post()) == [
        "-3.5% 감소함",          # 공백 없는 하이픈 = 음수 부호 → 보존
        "-2조원 순손실 기록함",
        "−1.2%p 하락함",         # 유니코드 마이너스는 표식 목록에 없음
        "영업이익은 3조원임",     # "- " (공백 있음) = 목록 표식 → 제거
        "—2026년 시행함",        # em dash + 공백 없음 → 보존
        "–5% 인하함",            # en dash + 공백 없음 → 보존
    ]


def test_parse_still_strips_hyphen_bullets_with_space():
    s = Summarizer(_cfg(lines=3))
    s._generate = lambda prompt, deadline=None: _envelope("- 첫째 줄임\n–  둘째 줄임\n— 셋째 줄임")
    assert s.summarize(_post()) == ["첫째 줄임", "둘째 줄임", "셋째 줄임"]


def test_generate_caps_timeout_by_remaining_budget():
    s = Summarizer(_cfg(timeout_sec=45))
    sess = _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))])
    deadline = time.monotonic() + 3.0
    s.summarize(_post(), deadline)
    # 45초가 아니라 남은 3초 이내로 잘려야 한다
    assert 0 < sess.timeouts[0] <= 3.0


def test_generate_refuses_call_past_deadline():
    s = Summarizer(_cfg())
    sess = _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))])
    try:
        s.summarize(_post(), time.monotonic() - 1.0)
    except RuntimeError as e:
        assert "예산" in str(e)
    else:
        raise AssertionError("마감이 지났으면 소켓을 열지 않아야 한다")
    assert sess.sent == []


def test_generate_skips_retry_that_would_not_fit_budget():
    # 백오프 대기가 남은 예산 밖이면 재시도하지 않고 바로 실패로 넘긴다.
    s = Summarizer(_cfg(max_retries=2, retry_backoff_sec=30))
    sess = _stub(s, [_FakeResponse(503, text="unavailable") for _ in range(3)])
    started = time.monotonic()
    try:
        s.summarize(_post(), started + 5.0)
    except RuntimeError:
        pass
    assert len(sess.sent) == 1                    # 재시도 없음
    assert time.monotonic() - started < 5.0       # 30초 백오프를 자지 않음


def test_summarize_all_stays_within_budget():
    # 건수를 다 돌지 않고 예산 안에서 멈춘다(남은 글은 원문 발췌로 발송).
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(50)]
    s = Summarizer(_cfg(budget_sec=3.0, timeout_sec=45, max_consecutive_failures=0))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        time.sleep(0.3)
        return _envelope('{"summary": ["요약함"]}')

    s._generate = _generate
    started = time.monotonic()
    s.summarize_all({"금융위 · 보도자료": posts})
    elapsed = time.monotonic() - started

    assert 0 < calls["n"] < 50           # 몇 건은 요약하되 전부는 아님
    assert elapsed < 3.5                 # 50건 × 0.3초(=15초)가 아니라 예산 근처
    assert sum(1 for p in posts if p.summary) == calls["n"]
    assert any(p.summary == [] for p in posts)   # 남은 글은 원문 발췌로


def test_summarize_all_skips_calls_when_budget_too_small():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(3)]
    s = Summarizer(_cfg(budget_sec=0.01))

    def _fail(prompt, deadline=None):
        raise AssertionError("예산이 한 건도 못 담으면 호출하지 않아야 한다")

    s._generate = _fail
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0


def test_throttle_wait_bounded_by_deadline():
    # RPM 간격(60초)이 남은 예산보다 길어도 예산만큼만 기다린다.
    s = Summarizer(_cfg(rpm=1))
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}')) for _ in range(2)])
    s.summarize(_post())            # 첫 호출로 _last_call 설정
    started = time.monotonic()
    s.summarize(_post(), started + 0.5)
    assert time.monotonic() - started < 2.0   # 60초를 통째로 자지 않는다


def test_max_posts_counts_only_eligible_bodies():
    # 상한보다 앞자리에 '너무 짧은 본문'이 있어도 할당량을 소모하지 않고,
    # 뒤쪽의 실제 요약 대상이 정상적으로 요약된다.
    short = [
        _post(post_id=f"s{i}", url=f"https://example.com/s{i}", body="짧음")
        for i in range(3)
    ]
    real = [
        _post(post_id=f"r{i}", url=f"https://example.com/r{i}") for i in range(2)
    ]
    s = Summarizer(_cfg(min_body_chars=50, max_posts=2))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        return _envelope('{"summary": ["요약함"]}')

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": short + real}) == 2
    assert calls["n"] == 2
    assert all(p.summary == [] for p in short)
    assert all(p.summary == ["요약함"] for p in real)


def test_circuit_breaker_stops_after_consecutive_failures():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        raise RuntimeError("HTTP 503")

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0
    assert calls["n"] == 3   # 10건 전부가 아니라 3건에서 멈춘다
    assert all(p.summary == [] for p in posts)


def test_circuit_breaker_resets_on_success():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(6)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        # 실패, 실패, 성공, 실패, 실패, 성공 — 연속 2회를 넘지 않으므로 끝까지 간다
        if calls["n"] % 3 != 0:
            raise RuntimeError("일시 오류")
        return _envelope('{"summary": ["요약함"]}')

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 2
    assert calls["n"] == 6


def test_time_budget_stops_summarizing():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(5)]
    s = Summarizer(_cfg(budget_sec=0.05, max_consecutive_failures=0))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        time.sleep(0.04)
        return _envelope('{"summary": ["요약함"]}')

    s._generate = _generate
    s.summarize_all({"금융위 · 보도자료": posts})
    assert calls["n"] < 5   # 시간예산에 걸려 전부 돌지 않는다


def test_summarize_all_respects_max_posts():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(5)]
    s = Summarizer(_cfg(max_posts=2))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
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
    s._generate = lambda prompt, deadline=None: {"candidates": [{"finishReason": "SAFETY"}]}
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
        self.timeouts = []

    def post(self, url, headers=None, json=None, timeout=None):
        # _generate 는 payload 를 그 자리에서 고쳐 재시도하므로 스냅샷으로 남긴다.
        self.sent.append(deepcopy(json))
        self.timeouts.append(timeout)
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
    # text/plain 만 보는 수신자도 AI 생성물임을 알 수 있어야 한다
    assert "[AI 3줄 요약]" in text
    assert "생성형 AI" in text


def test_email_falls_back_to_body_without_summary():
    p = _post(body="원문 본문 발췌가 여기 나온다")
    grouped = {p.source_name: [p]}

    html = build_html(grouped)
    assert "원문 본문 발췌가 여기 나온다" in html
    assert ">AI 3줄 요약</div>" not in html
    assert "생성형 AI" not in html   # 요약이 없으면 푸터 유의사항도 붙지 않는다

    text = build_text(grouped)
    assert "원문 본문 발췌가 여기 나온다" in text
    assert "[원문 발췌]" in text
    assert "생성형 AI" not in text   # 요약이 없으면 텍스트 파트에도 유의사항이 없다


def test_text_label_matches_actual_line_count():
    p = _post()
    p.summary = ["한 줄만 나왔음"]
    text = build_text({p.source_name: [p]})
    assert "[AI 1줄 요약]" in text
    assert ">AI 1줄 요약</div>" in build_html({p.source_name: [p]})


def test_email_escapes_summary_html():
    p = _post()
    p.summary = ["<script>alert(1)</script> 포함함"]
    assert "<script>" not in build_html({p.source_name: [p]})
