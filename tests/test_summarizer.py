"""LLM(Gemini) 본문 요약 및 요약 기반 메일 렌더 테스트.

네트워크 호출은 하지 않는다 — Summarizer._generate 를 가짜 응답으로 대체한다.
"""
import json
import os
import sys
import time
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import EmailConfig, LLMConfig, load_config
from src.models import Post
from src.notifier import build_html, build_text, missing_email_settings, send_digest
from src.summarizer import (
    _MAX_OUTPUT_TOKENS,
    _MAX_OUTPUT_TOKENS_THINKING,
    _SCHEMA,
    _TEMPERATURE,
    LLMCallError,
    LLMErrorKind,
    RequestDeadlineExceeded,
    Summarizer,
    SummaryUnavailable,
    classify_error,
    classify_status,
    summarize_posts,
)


def _cfg(**over) -> LLMConfig:
    base = dict(
        enabled=True,
        model="gemini-flash-latest",
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


def test_config_uses_latest_alias_and_ordered_fallbacks():
    # 특정 버전을 하드코딩하면 그 버전 수명 종료일에 전 요청이 404 로 죽는다.
    cfg = load_config("config.yaml")
    assert cfg.llm.model == "gemini-flash-latest"
    assert cfg.llm.fallback_models == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
    # 호출 순서: primary → fallback 순서 그대로
    assert cfg.llm.model_chain == [
        "gemini-flash-latest",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
    ]


def test_model_chain_dedupes_and_preserves_order():
    c = _cfg(model="a", fallback_models=["b", "a", "c", "b", "", "  "])
    assert c.model_chain == ["a", "b", "c"]
    # 공백만 있는 이름은 버린다
    assert _cfg(model="  a  ", fallback_models=[" a "]).model_chain == ["a"]


def test_config_allows_disabling_fallbacks(tmp_path):
    import yaml

    base = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    base["llm"]["fallback_models"] = []
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.llm.fallback_models == []
    assert cfg.llm.model_chain == ["gemini-flash-latest"]


def test_config_defaults_fallbacks_when_key_missing(tmp_path):
    import yaml

    base = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    base["llm"].pop("fallback_models", None)
    base["llm"].pop("model", None)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.llm.model == "gemini-flash-latest"
    assert cfg.llm.fallback_models == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


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
    connect, read = sess.timeouts[0]
    assert 0 < connect + read <= 3.0


def test_timeout_is_split_so_phases_sum_within_budget():
    # requests 에 스칼라를 주면 연결·응답대기에 '각각' 적용돼 최대 2배가 된다.
    # 두 단계의 합이 예산을 넘지 않도록 튜플로 쪼개 넘긴다.
    from src.summarizer import _CONNECT_TIMEOUT_CAP, _split_timeout

    for budget in (0.5, 3.0, 10.0, 45.0, 300.0):
        connect, read = _split_timeout(budget)
        assert connect > 0 and read > 0
        assert connect + read <= budget + 1e-9
        assert connect <= _CONNECT_TIMEOUT_CAP

    # 예산이 넉넉하면 연결은 상한까지만 쓰고 나머지는 응답 대기에 준다
    connect, read = _split_timeout(45.0)
    assert connect == _CONNECT_TIMEOUT_CAP
    assert read == 45.0 - _CONNECT_TIMEOUT_CAP


def test_generate_passes_timeout_tuple_to_requests():
    s = Summarizer(_cfg(timeout_sec=45))
    sess = _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))])
    s.summarize(_post())
    assert isinstance(sess.timeouts[0], tuple)   # 스칼라를 넘기면 2배로 늘어난다
    assert sum(sess.timeouts[0]) <= 45.0


# --- 모델 수명 종료(404) 대응: primary alias → fallback 연쇄 ---


class _ModelSession:
    """모델별로 정해진 응답을 돌려주는 세션. 호출된 모델을 순서대로 기록한다."""

    def __init__(self, by_model, default=None):
        self._by_model = dict(by_model)
        self._default = default
        self.models = []
        self.sent = []

    def post(self, url, headers=None, json=None, timeout=None):
        model = url.rstrip("/").split("/")[-1].split(":")[0]
        self.models.append(model)
        # payload 는 재시도에서 그 자리에서 교체되므로 스냅샷으로 남긴다.
        self.sent.append(deepcopy(json))
        resp = self._by_model.get(model, self._default)
        if resp is None:
            raise AssertionError(f"호출되면 안 되는 모델: {model}")
        if isinstance(resp, list):
            return resp.pop(0)
        return resp


_PRIMARY = "gemini-flash-latest"
_FB1 = "gemini-3.6-flash"
_FB2 = "gemini-3.5-flash-lite"
# thinkingBudget 을 받는 유일한 계열(과거 기본 모델)
_M25 = "gemini-2.5-flash"


def _chain_cfg(**over):
    return _cfg(model=_PRIMARY, fallback_models=[_FB1, _FB2], **over)


def _ok_resp(text='{"summary": ["첫째임", "둘째임", "셋째임"]}'):
    return _FakeResponse(200, _envelope(text))


def _err_resp(code, status="", message=""):
    payload = {"error": {"code": code, "status": status, "message": message}}
    return _FakeResponse(code, payload, text=json.dumps(payload))


def _bare_400():
    # 운영에서 실제로 받은 응답 — 어떤 인자가 문제인지 알려주지 않는다.
    return _err_resp(400, "INVALID_ARGUMENT", "Request contains an invalid argument.")


def _gone_resp(model="models/x"):
    # 실제로 받은 응답 형태
    return _err_resp(
        404,
        "NOT_FOUND",
        f"This model {model} is no longer available to new users. "
        "Please update your code to use a newer model.",
    )


def test_primary_alias_succeeds_without_touching_fallbacks():
    s = Summarizer(_chain_cfg())
    s.session = _ModelSession({_PRIMARY: _ok_resp()})
    assert s.summarize(_post()) == ["첫째임", "둘째임", "셋째임"]
    assert s.session.models == [_PRIMARY]


def test_primary_404_falls_back_to_first_fallback():
    s = Summarizer(_chain_cfg())
    s.session = _ModelSession({_PRIMARY: _gone_resp(), _FB1: _ok_resp()})
    assert s.summarize(_post()) == ["첫째임", "둘째임", "셋째임"]
    assert s.session.models == [_PRIMARY, _FB1]


def test_two_models_404_falls_back_to_last():
    s = Summarizer(_chain_cfg())
    s.session = _ModelSession(
        {_PRIMARY: _gone_resp(), _FB1: _gone_resp(), _FB2: _ok_resp()}
    )
    assert s.summarize(_post()) == ["첫째임", "둘째임", "셋째임"]
    assert s.session.models == [_PRIMARY, _FB1, _FB2]


def test_all_models_404_falls_back_to_source_excerpt():
    posts = [
        _post(post_id="1", url="https://example.com/1", body="원문 발췌가 실릴 본문임. " * 6),
        _post(post_id="2", url="https://example.com/2", body="원문 발췌가 실릴 본문임. " * 6),
    ]
    s = Summarizer(_chain_cfg())
    s.session = _ModelSession(
        {_PRIMARY: _gone_resp(), _FB1: _gone_resp(), _FB2: _gone_resp()}
    )

    # 예외가 밖으로 새지 않고(메일 실행이 죽지 않고) 0건 요약으로 끝난다
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0
    assert all(p.summary == [] for p in posts)

    # 1번째 글에서 3개 모델을 다 확인했으므로, 2번째 글에서는 재시도하지 않는다
    assert s.session.models == [_PRIMARY, _FB1, _FB2]

    # 메일에는 기존 원문 발췌가 실린다
    html = build_html({posts[0].source_name: posts})
    assert "원문 발췌가 실릴 본문임" in html
    assert ">AI 3줄 요약</div>" not in html


def test_successful_model_is_cached_for_later_posts():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(3)]
    s = Summarizer(_chain_cfg())
    s.session = _ModelSession({_PRIMARY: _gone_resp(), _FB1: _ok_resp()})
    s.summarize_all({"금융위 · 보도자료": posts})
    # 첫 글에서만 primary 를 시도하고, 이후에는 성공한 모델만 호출한다
    assert s.session.models == [_PRIMARY, _FB1, _FB1, _FB1]
    assert all(p.summary for p in posts)


def test_rate_limit_does_not_chain_to_next_model():
    # 429 는 모델 문제가 아니다 — 다른 모델로 넘기면 한 번의 한도 초과로 목록을 소진한다.
    s = Summarizer(_chain_cfg(max_retries=0))
    s.session = _ModelSession({_PRIMARY: _err_resp(429, "RESOURCE_EXHAUSTED", "quota")})
    with pytest.raises(RuntimeError):
        s.summarize(_post())
    assert s.session.models == [_PRIMARY]
    assert s._unavailable == set()   # 사용 불가로 낙인찍지 않는다


def test_auth_error_does_not_chain_to_next_model():
    for code, status in ((401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED")):
        s = Summarizer(_chain_cfg(max_retries=0))
        s.session = _ModelSession({_PRIMARY: _err_resp(code, status, "API key not valid")})
        with pytest.raises(RuntimeError):
            s.summarize(_post())
        assert s.session.models == [_PRIMARY], code
        assert s._unavailable == set()


def test_server_error_keeps_retry_policy_and_does_not_chain():
    s = Summarizer(_chain_cfg(max_retries=2, retry_backoff_sec=0))
    s.session = _ModelSession({_PRIMARY: _err_resp(503, "UNAVAILABLE", "overloaded")})
    with pytest.raises(RuntimeError):
        s.summarize(_post())
    # 기존 재시도(1+2회)는 유지하되 다른 모델로는 넘어가지 않는다
    assert s.session.models == [_PRIMARY] * 3
    assert s._unavailable == set()


def test_server_error_still_trips_circuit_breaker():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)]
    s = Summarizer(_chain_cfg(max_retries=1, retry_backoff_sec=0, max_consecutive_failures=3))
    s.session = _ModelSession({_PRIMARY: _err_resp(503, "UNAVAILABLE", "overloaded")})
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0
    # 3건에서 브레이커가 열리고, 각 건은 재시도 1회를 포함해 2번씩 호출
    assert s.session.models == [_PRIMARY] * 6


def test_timeout_does_not_chain_to_next_model():
    import requests

    s = Summarizer(_chain_cfg(max_retries=0))

    class _Timeout:
        def __init__(self):
            self.models = []

        def post(self, url, headers=None, json=None, timeout=None):
            self.models.append(url.rstrip("/").split("/")[-1].split(":")[0])
            raise requests.ConnectTimeout("연결 시간 초과")

    s.session = _Timeout()
    with pytest.raises(requests.ConnectTimeout):
        s.summarize(_post())
    assert s.session.models == [_PRIMARY]
    assert s._unavailable == set()


def test_chains_on_400_saying_model_not_found():
    # 404 가 아니라 400 으로 '모델 없음'을 알려주는 경우도 다음 모델로 넘어간다.
    s = Summarizer(_chain_cfg())
    s.session = _ModelSession(
        {
            _PRIMARY: _err_resp(
                400, "INVALID_ARGUMENT", "Model gemini-flash-latest does not exist."
            ),
            _FB1: _ok_resp(),
        }
    )
    assert s.summarize(_post()) == ["첫째임", "둘째임", "셋째임"]
    assert s.session.models == [_PRIMARY, _FB1]


def test_thinking_config_retry_is_per_model():
    # thinkingConfig 는 Gemini 2.5 계열에만 보내고, 거절당하면 그 모델에만 기록한다.
    s = Summarizer(_cfg(model=_M25, fallback_models=[_FB1], max_retries=0))
    thinking_400 = _err_resp(
        400, "INVALID_ARGUMENT", 'Unknown name "thinkingConfig": Cannot find field.'
    )
    s.session = _ModelSession({_M25: [thinking_400, _ok_resp()]})
    assert s.summarize(_post()) == ["첫째임", "둘째임", "셋째임"]
    assert s.session.models == [_M25, _M25]             # 같은 모델로 재시도
    assert s._unavailable == set()                      # 사용 불가가 아니다
    assert s._thinking_unsupported == {_M25}            # 그 모델에만 기록


# --- 실제로 전송되는 request payload 검증 (400 INVALID_ARGUMENT 재발 방지) ---
#
# 거절 응답의 message 가 "Request contains an invalid argument." 한 줄뿐일 수 있어
# 문제 필드를 알려주지 않는다. 그래서 '어떤 옵션을 보냈는지'를 테스트가 직접 본다.
#   - thinkingConfig.thinkingBudget: Gemini 2.5 전용. Gemini 3 계열은 thinkingLevel 로
#     대체되고 생각을 끌 수 없어 budget 0 을 보내면 400 이다.
#   - temperature: Gemini 3 계열은 기본값 1.0 유지가 공식 권고(낮추면 루프·품질 저하).


def _sent_cfg(model, **over):
    return _cfg(model=model, fallback_models=[], max_retries=0, **over)


def _payloads(model, responses=None, **over):
    """모델 하나로 호출하고 (Summarizer, 실제로 보낸 payload 목록)을 돌려준다."""
    s = Summarizer(_sent_cfg(model, **over))
    sess = _stub(s, list(responses or [_ok_resp()]))
    return s, sess.sent


def _gen_config(model, **over):
    s, sent = _payloads(model, **over)
    assert s.summarize(_post()) == ["첫째임", "둘째임", "셋째임"]
    return sent[0]["generationConfig"]


def test_alias_payload_omits_generation_specific_options():
    gc = _gen_config(_PRIMARY)
    assert "thinkingConfig" not in gc
    assert "temperature" not in gc
    # 구조화 출력은 그대로 유지한다
    assert gc["responseMimeType"] == "application/json"
    assert gc["responseSchema"] == _SCHEMA
    # 생각을 끌 수 없으므로 출력 상한은 '생각 + 응답' 합계를 담을 만큼 크게 잡는다
    assert gc["maxOutputTokens"] == _MAX_OUTPUT_TOKENS_THINKING


def test_gemini_3_models_get_no_thinking_budget_or_temperature():
    for model in (_FB1, _FB2, "gemini-3-pro-preview", "gemini-flash-lite-latest"):
        gc = _gen_config(model)
        assert "thinkingConfig" not in gc, model
        assert "temperature" not in gc, model
        assert gc["responseSchema"] == _SCHEMA, model


def test_gemini_25_payload_keeps_thinking_budget_and_temperature():
    # 2.5 계열에서는 기존 동작(생각 끄기 + 낮은 온도)을 그대로 유지한다.
    gc = _gen_config(_M25)
    assert gc["thinkingConfig"] == {"thinkingBudget": 0}
    assert gc["temperature"] == _TEMPERATURE
    assert gc["maxOutputTokens"] == _MAX_OUTPUT_TOKENS


def test_configured_models_never_send_thinking_budget():
    # config.yaml 의 모델 목록이 바뀌어도 세대 종속 옵션이 새로 붙지 않게 못박는다.
    chain = load_config("config.yaml").llm.model_chain
    assert chain
    for model in chain:
        gc = _gen_config(model)
        assert "thinkingConfig" not in gc, model
        assert "temperature" not in gc, model


def test_fallback_payload_also_omits_generation_specific_options():
    s = Summarizer(_chain_cfg())
    s.session = _ModelSession({_PRIMARY: _gone_resp(), _FB1: _ok_resp()})
    assert s.summarize(_post()) == ["첫째임", "둘째임", "셋째임"]
    assert s.session.models == [_PRIMARY, _FB1]
    for model, payload in zip(s.session.models, s.session.sent):
        gc = payload["generationConfig"]
        assert "thinkingConfig" not in gc, model
        assert "temperature" not in gc, model
        assert gc["responseSchema"] == _SCHEMA, model


def test_bare_400_neither_chains_nor_retries():
    # 필드를 특정하지 않는 400 은 다음 모델로 넘기지도, 재시도하지도 않는다.
    s = Summarizer(_chain_cfg(max_retries=2, retry_backoff_sec=0))
    s.session = _ModelSession({_PRIMARY: _bare_400()})
    with pytest.raises(RuntimeError):
        s.summarize(_post())
    assert s.session.models == [_PRIMARY]
    assert s._unavailable == set()
    assert s._thinking_unsupported == set()


def test_bare_400_drops_thinking_config_once_and_retries():
    # 2.5 로 알고 보냈는데 거절당하면(alias 가 그 이름으로 옮겨간 경우 등) 우리가 붙인
    # 유일한 선택 옵션인 thinkingConfig 를 떼고 한 번만 재시도한다.
    s, sent = _payloads(_M25, [_bare_400(), _ok_resp()])
    assert s.summarize(_post()) == ["첫째임", "둘째임", "셋째임"]
    assert len(sent) == 2
    first, second = (p["generationConfig"] for p in sent)
    assert first["thinkingConfig"] == {"thinkingBudget": 0}
    assert "thinkingConfig" not in second
    assert second["maxOutputTokens"] == _MAX_OUTPUT_TOKENS_THINKING
    assert second["responseSchema"] == _SCHEMA      # 구조화 출력은 유지
    assert s._thinking_unsupported == {_M25}
    assert s._unavailable == set()                  # 사용 불가가 아니다


def test_thinking_config_removal_retries_at_most_once():
    s, sent = _payloads(_M25, [_bare_400(), _bare_400()])
    with pytest.raises(RuntimeError):
        s.summarize(_post())
    assert len(sent) == 2   # 옵션 제거 재시도 1회로 끝(무한 루프 없음)


def test_json_schema_validation_preserved_across_fallback():
    # 폴백으로 넘어가도 3줄 JSON 검증은 그대로 적용된다.
    s = Summarizer(_chain_cfg(lines=3))
    s.session = _ModelSession(
        {
            _PRIMARY: _gone_resp(),
            _FB1: _ok_resp('{"summary": ["1", "2", "3", "4", "5"]}'),
        }
    )
    assert s.summarize(_post()) == ["1", "2", "3"]      # lines 만큼 절단

    # 스키마 위반은 폴백 모델에서도 폐기된다
    s2 = Summarizer(_chain_cfg())
    s2.session = _ModelSession(
        {_PRIMARY: _gone_resp(), _FB1: _ok_resp('{"summary": {"a": "b"}}')}
    )
    with pytest.raises(SummaryUnavailable):
        s2.summarize(_post())


def test_all_unavailable_reports_without_further_calls():
    s = Summarizer(_chain_cfg())
    s._unavailable = {_PRIMARY, _FB1, _FB2}
    s.session = _ModelSession({})   # 어떤 호출도 하면 AssertionError
    with pytest.raises(SummaryUnavailable):
        s.summarize(_post())
    assert s.session.models == []


# --- 스키마를 어긴 / 잘린 응답 처리 ---
#
# 공통 요구사항: 쓸 만한 요약을 못 얻으면 (1) 조각을 메일에 싣지 않고
# (2) SummaryUnavailable 로 올려 서킷 브레이커가 '실패'로 세게 한다.


def _expect_unavailable(response, cfg=None):
    s = Summarizer(cfg or _cfg())
    s._generate = lambda prompt, deadline=None: response
    with pytest.raises(SummaryUnavailable):
        s.summarize(_post())


def test_rejects_dict_shaped_summary():
    # {"summary": {"first": "금리 인하"}} 를 순회하면 dict 의 '키'가 요약문이 된다.
    _expect_unavailable(_envelope('{"summary": {"first": "금리 인하", "second": "시행 연기"}}'))


def test_rejects_list_with_non_strings():
    # "1"·"None" 이 요약문으로 찍히면 안 된다.
    _expect_unavailable(_envelope('{"summary": ["정상 문장임", 1, null, {"a": "b"}]}'))


def test_rejects_json_scalar_and_null_summary():
    for body in ('{"summary": null}', '{"summary": 3}', '{"summary": true}', "42"):
        _expect_unavailable(_envelope(body))


def test_rejects_truncated_structured_output():
    # MAX_TOKENS 등으로 잘린 JSON 을 줄 단위 폴백하면 조각이 그대로 메일에 실린다.
    for body in (
        '{"summary": ["첫째 문장",',
        '{"summary": [',
        '{"summary": ["첫째 문장", "둘째 문장"',
        '["첫째 문장",',
    ):
        _expect_unavailable(_envelope(body))


def test_rejects_nonterminal_finish_reason_even_with_text():
    # 잘린 응답에 텍스트가 남아 있어도 신뢰할 수 없다.
    for reason in ("MAX_TOKENS", "SAFETY", "RECITATION"):
        _expect_unavailable(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": '{"summary": ["첫째 문장",'}]},
                        "finishReason": reason,
                    }
                ]
            }
        )


def test_rejects_blocked_response_without_text():
    _expect_unavailable({"candidates": [{"finishReason": "SAFETY"}]})
    _expect_unavailable({"candidates": []})


def test_rejects_missing_finish_reason():
    # finishReason 이 없으면 종료가 '확인되지 않은' 응답이다. 유효한 JSON 이라도
    # 조각일 수 있고, 통과시키면 실패로도 세지 않아 브레이커까지 리셋된다.
    good = '{"summary": ["정상처럼 보이는 문장임"]}'
    _expect_unavailable({"candidates": [{"content": {"parts": [{"text": good}]}}]})
    _expect_unavailable(
        {"candidates": [{"content": {"parts": [{"text": good}]}, "finishReason": None}]}
    )
    _expect_unavailable(
        {"candidates": [{"content": {"parts": [{"text": good}]}, "finishReason": ""}]}
    )
    _expect_unavailable({})   # candidates 키 자체가 없는 응답

    # 대조군: finishReason=STOP 이면 같은 본문이 정상 통과한다
    s = Summarizer(_cfg())
    s._generate = lambda prompt, deadline=None: _envelope(good)
    assert s.summarize(_post()) == ["정상처럼 보이는 문장임"]


def test_missing_finish_reason_counts_toward_breaker():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        return {"candidates": [{"content": {"parts": [{"text": '{"summary": ["x"]}'}]}}]}

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0
    assert calls["n"] == 3                       # 브레이커가 열린다
    assert all(p.summary == [] for p in posts)   # 원문 발췌 폴백


def test_no_json_fragment_leaks_into_summary():
    # 어떤 깨진 응답에서도 JSON/펜스 조각이 요약문으로 발행되면 안 된다.
    broken = [
        '{"summary": ["첫째 문장",',
        '{"summary": {"a": "b"}}',
        '```json\n{"summary": ["첫째 문장",',
        '```json\n{"summary": [\n```',
    ]
    for body in broken:
        s = Summarizer(_cfg())
        s._generate = lambda prompt, deadline=None, b=body: _envelope(b)
        try:
            out = s.summarize(_post())
        except SummaryUnavailable:
            continue
        assert not any(
            tok in line for line in out for tok in ("{", "}", "```", "summary")
        ), (body, out)


def test_accepts_fenced_json():
    # 모델이 ```json 펜스로 감싸 보내도 정상 파싱한다.
    s = Summarizer(_cfg())
    s._generate = lambda prompt, deadline=None: _envelope(
        '```json\n{"summary": ["첫째임", "둘째임"]}\n```'
    )
    assert s.summarize(_post()) == ["첫째임", "둘째임"]


def test_fenced_prose_falls_back_to_lines_without_fence_markers():
    # 펜스 안이 JSON 이 아니면 평문 요약으로 보고 줄 단위 폴백하되, 펜스 표식은 뗀다.
    s = Summarizer(_cfg())
    s._generate = lambda prompt, deadline=None: _envelope(
        "```\n- 첫째 줄임\n- 둘째 줄임\n```"
    )
    assert s.summarize(_post()) == ["첫째 줄임", "둘째 줄임"]


def test_accepts_bare_string_summary():
    # 스키마 위반이지만 내용은 모델이 쓴 문장이라 구조적 잡음이 섞일 여지가 없다.
    s = Summarizer(_cfg())
    s._generate = lambda prompt, deadline=None: _envelope('{"summary": "한 문장 요약함"}')
    assert s.summarize(_post()) == ["한 문장 요약함"]


def test_accepts_top_level_string_array():
    s = Summarizer(_cfg())
    s._generate = lambda prompt, deadline=None: _envelope('["첫째임", "둘째임"]')
    assert s.summarize(_post()) == ["첫째임", "둘째임"]


def test_empty_summary_array_is_success_not_failure():
    # "요약할 내용이 없으면 빈 배열" 은 프롬프트가 허용한 정상 응답이다.
    # 이걸 실패로 세면 멀쩡한 실행에서 브레이커가 열린다.
    s = Summarizer(_cfg())
    s._generate = lambda prompt, deadline=None: _envelope('{"summary": []}')
    assert s.summarize(_post()) == []


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


def test_throttle_wait_bounded_by_deadline_and_rechecks_after():
    # RPM 간격(60초)이 남은 예산보다 길면, 예산만큼만 기다린 뒤 '대기 후' 마감을
    # 다시 확인해 요청을 열지 않아야 한다. 대기 전에 계산한 낡은 타임아웃으로
    # 소켓을 열면 예산을 넘겨 버린다.
    s = Summarizer(_cfg(rpm=1))
    sess = _stub(
        s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}')) for _ in range(2)]
    )
    s.summarize(_post())            # 첫 호출로 _last_call 설정
    started = time.monotonic()
    try:
        s.summarize(_post(), started + 0.3)
    except RuntimeError as e:
        assert "예산" in str(e)
    else:
        raise AssertionError("간격 대기로 예산이 소진되면 요청을 열지 않아야 한다")
    assert len(sess.sent) == 1                # 두 번째 요청은 열리지 않음
    assert time.monotonic() - started < 2.0   # 60초를 통째로 자지도 않음


def test_throttle_does_not_penalize_abandoned_call():
    # 예산 초과로 포기한 호출은 _last_call 을 갱신하지 않으므로, 다음 호출이
    # 공연히 한 간격을 더 기다리지 않는다.
    s = Summarizer(_cfg(rpm=600))   # 간격 0.1초
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}')) for _ in range(2)])
    s.summarize(_post())
    before = s._last_call
    try:
        s.summarize(_post(), time.monotonic() - 1.0)   # 이미 마감 지남
    except RuntimeError:
        pass
    assert s._last_call == before   # 보내지 않은 요청은 간격 기준을 옮기지 않는다


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
    # 503(일시 장애)은 다음 글에서 살아날 수 있으므로 연속 실패 카운터로 판단한다.
    # 여기서 bare RuntimeError 를 쓰면 '우리 코드의 버그'(INTERNAL)를 흉내 내는 것이라
    # 즉시 중단 경로를 타고, 정작 브레이커는 검증되지 않는다.
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        raise LLMCallError("HTTP 503", kind=LLMErrorKind.TRANSIENT, status=503)

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
            raise LLMCallError("일시 오류", kind=LLMErrorKind.TRANSIENT, status=503)
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


def test_cap_is_spread_across_sources_not_config_order():
    # 앞쪽 소스가 상한을 다 먹으면 뒤쪽 소스는 글이 더 최신이어도 전부 원문 발췌로
    # 나간다. 상한은 소스별로 고르게 배분되어야 한다.
    # (의안은 별도 배치 경로를 타므로 여기서는 일반 소스 둘로 확인한다.)
    first = [
        _post(source_key="fsc_press", source_name="금융위 · 보도자료",
              post_id=f"a{i}", url=f"https://example.com/a{i}")
        for i in range(10)
    ]
    later = [
        _post(source_key="fss_press", source_name="금감원 · 보도자료",
              post_id=f"b{i}", url=f"https://example.com/b{i}")
        for i in range(3)
    ]
    s = Summarizer(_cfg(max_posts=6))
    s._generate = lambda prompt, deadline=None: _envelope('{"summary": ["요약함"]}')

    s.summarize_all({
        "금융위 · 보도자료": first,      # config 앞쪽 소스
        "금감원 · 보도자료": later,      # 뒤쪽 소스
    })

    assert sum(1 for p in first if p.summary) + sum(1 for p in later if p.summary) == 6
    # 뒤쪽 소스도 요약을 받는다(예전에는 0건이었다)
    assert sum(1 for p in later if p.summary) == 3
    # 각 소스 안에서는 앞(최신)부터 채워진다
    assert [bool(p.summary) for p in later] == [True, True, True]
    assert [bool(p.summary) for p in first][:3] == [True, True, True]


def test_cap_spread_falls_back_to_one_source_when_alone():
    # 소스가 하나뿐이면 그 소스가 상한을 다 쓴다(배분할 상대가 없음).
    only = [
        _post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)
    ]
    s = Summarizer(_cfg(max_posts=4))
    s._generate = lambda prompt, deadline=None: _envelope('{"summary": ["요약함"]}')
    s.summarize_all({"금융위 · 보도자료": only})
    assert [bool(p.summary) for p in only] == [True] * 4 + [False] * 6


def test_interleave_preserves_within_source_order():
    from src.summarizer import _interleave

    a = [_post(post_id=f"a{i}") for i in range(3)]
    b = [_post(post_id=f"b{i}") for i in range(1)]
    c = [_post(post_id=f"c{i}") for i in range(2)]
    got = [p.post_id for p in _interleave([a, b, c])]
    assert got == ["a0", "b0", "c0", "a1", "c1", "a2"]


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


def test_blocked_responses_trip_the_circuit_breaker():
    # 전량 차단 시 응답이 200 이라 '성공'으로 세면 브레이커가 안 열려 40건을 다 태운다.
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        return {"candidates": [{"finishReason": "SAFETY"}]}

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0
    assert calls["n"] == 3                          # 10건 전부가 아니라 3건에서 멈춤
    assert all(p.summary == [] for p in posts)      # 원문 발췌 폴백은 그대로


def test_empty_array_responses_do_not_trip_the_breaker():
    # 정상 종료 + 빈 배열은 실패가 아니므로 끝까지 진행해야 한다.
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(6)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        return _envelope('{"summary": []}')

    s._generate = _generate
    s.summarize_all({"금융위 · 보도자료": posts})
    assert calls["n"] == 6


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

    def __init__(self, responses, delay=0.0):
        self._responses = list(responses)
        self._delay = delay        # 응답이 늦는(=바이트를 질질 끄는) 서버 흉내
        self.sent = []
        self.timeouts = []

    def post(self, url, headers=None, json=None, timeout=None):
        # _generate 는 payload 를 그 자리에서 고쳐 재시도하므로 스냅샷으로 남긴다.
        self.sent.append(deepcopy(json))
        self.timeouts.append(timeout)
        if self._delay:
            time.sleep(self._delay)
        return self._responses.pop(0)


def _stub(s, responses, delay=0.0):
    s.session = _FakeSession(responses, delay=delay)
    return s.session


def test_request_is_bounded_by_wall_clock_deadline():
    # read 타임아웃은 소켓 읽기 '사이'의 무응답에만 걸리므로, 응답을 조금씩 흘려보내는
    # 서버에는 총 소요를 제한하지 못한다. 요청 전체에 단조시계 마감을 강제해야 한다.
    s = Summarizer(_cfg(timeout_sec=45, max_retries=0))
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))], delay=3.0)

    started = time.monotonic()
    with pytest.raises(SummaryUnavailable):
        s.summarize(_post(), started + 0.4)
    elapsed = time.monotonic() - started

    # 3초짜리 응답을 끝까지 기다리지 않고 예산에서 끊는다
    assert elapsed < 1.5, elapsed


def test_request_deadline_applies_without_explicit_budget():
    # deadline 을 안 넘겨도 timeout_sec 이 요청 전체의 상한이어야 한다.
    s = Summarizer(_cfg(timeout_sec=0.3, max_retries=0))
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))], delay=3.0)

    started = time.monotonic()
    with pytest.raises(SummaryUnavailable):
        s.summarize(_post())
    assert time.monotonic() - started < 1.5


def test_slow_request_does_not_block_process_exit():
    # 포기한 요청은 데몬 스레드에 남는다 — 프로세스 종료를 막으면 안 된다.
    import threading

    s = Summarizer(_cfg(timeout_sec=0.2, max_retries=0))
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))], delay=2.0)
    before = {t for t in threading.enumerate()}
    with pytest.raises(SummaryUnavailable):
        s.summarize(_post())
    leftover = [t for t in threading.enumerate() if t not in before]
    assert leftover, "요청 스레드가 아직 살아 있어야 이 테스트가 의미 있다"
    assert all(t.daemon for t in leftover)   # 데몬이라 인터프리터 종료를 붙잡지 않는다


def test_fast_request_leaves_no_thread_behind():
    s = Summarizer(_cfg())
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))])
    import threading

    before = len(threading.enumerate())
    assert s.summarize(_post()) == ["요약함"]
    assert len(threading.enumerate()) == before


def test_request_error_propagates_with_original_type():
    # 워커 스레드에서 난 예외는 원형 그대로 올라와야 재시도 판정이 유지된다.
    import requests

    s = Summarizer(_cfg(max_retries=0))

    class _Boom:
        def post(self, *a, **kw):
            raise requests.ConnectionError("연결 실패")

    s.session = _Boom()
    with pytest.raises(requests.ConnectionError):
        s.summarize(_post())


def test_generate_drops_thinking_config_without_spending_retries():
    # max_retries=0 이어도 thinkingConfig 미지원 400 은 한 번 교정 재시도한다.
    # (thinkingConfig 를 애초에 보내는 계열, 즉 Gemini 2.5 에서만 일어난다)
    s = Summarizer(_cfg(model=_M25, max_retries=0))
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


# --- 발송 설정 사전 검증 (LLM 호출 전에 걸러야 하는 조건) ---


def _email_cfg(**over) -> EmailConfig:
    base = dict(
        recipients=["a@example.com"],
        from_name="LAW RADER",
        subject_prefix="[LAW RADER]",
        max_attach_mb=15,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="sender@example.com",
        smtp_password="pw",
        mail_from="sender@example.com",
    )
    base.update(over)
    return EmailConfig(**base)


def test_missing_email_settings_detects_each_field():
    assert missing_email_settings(_email_cfg()) == []
    assert missing_email_settings(_email_cfg(smtp_user="")) == ["SMTP_USER"]
    assert missing_email_settings(_email_cfg(smtp_password="")) == ["SMTP_PASSWORD"]
    assert "MAIL_TO" in missing_email_settings(_email_cfg(recipients=[]))[0]
    # 여러 개가 빠지면 전부 알려준다(한 번에 고칠 수 있도록)
    assert len(missing_email_settings(_email_cfg(smtp_user="", recipients=[]))) == 2


def test_send_digest_reports_missing_settings():
    p = _post()
    try:
        send_digest(_email_cfg(smtp_password=""), {p.source_name: [p]})
    except RuntimeError as e:
        assert "SMTP_PASSWORD" in str(e)
    else:
        raise AssertionError("설정이 비면 발송을 시도하지 않아야 한다")


def test_send_digest_skips_check_when_nothing_to_send():
    # 신규가 없으면 설정이 비어 있어도 조용히 통과(기존 동작 유지)
    send_digest(_email_cfg(smtp_user="", smtp_password="", recipients=[]), {})


def _run_with_one_new_post(tmp_path, monkeypatch, dry_run=False, smtp_error=None):
    """신규 1건이 잡힌 상태로 main.run() 을 돌리고 (반환코드, 호출횟수) 를 준다."""
    from src import main as main_mod
    from src.scrapers.base import CollectResult
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    st.mark_seen("fss_press", ["old"], baselined=True)
    st.save()

    fresh = _post(source_key="fss_press", source_name="금감원 · 보도자료", post_id="new1")

    class _FakeScraper:
        def collect(self, limit, seen_ids, max_pages):
            return CollectResult(posts=[fresh], reached_boundary=True, scanned=1)

        def enrich(self, post):
            return None

    calls = {"summarize": 0, "send": 0, "verify": 0, "order": []}

    def _record(name):
        def _fn(cfg, *rest):
            calls[name] += 1
            calls["order"].append(name)
            if name == "verify" and smtp_error is not None:
                raise smtp_error

        return _fn

    monkeypatch.setattr(main_mod, "verify_smtp_login", _record("verify"))
    monkeypatch.setattr(main_mod, "summarize_posts", _record("summarize"))
    monkeypatch.setattr(main_mod, "send_digest", _record("send"))
    monkeypatch.setattr(main_mod, "build_scraper", lambda src, fetcher: _FakeScraper())

    argv = ["--state", str(state_path), "--only", "fss_press"]
    if dry_run:
        argv.append("--dry-run")
    return main_mod.run(argv), calls


def test_run_skips_llm_when_mail_settings_missing(tmp_path, monkeypatch):
    # 발송이 불가능한 설정이면 Gemini 를 부르기 전에 멈춰야 한다. 그러지 않으면
    # 실패로 신규가 미확정으로 남아 매 실행마다 같은 글을 다시 요약한다.
    for var in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO", "MAIL_FROM"):
        monkeypatch.delenv(var, raising=False)

    rc, calls = _run_with_one_new_post(tmp_path, monkeypatch)
    assert rc == 1
    assert calls["order"] == []   # 설정이 비면 SMTP 접속조차 시도하지 않는다


def test_run_summarizes_when_mail_settings_present(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")

    rc, calls = _run_with_one_new_post(tmp_path, monkeypatch)
    assert rc == 0
    assert calls["summarize"] == 1
    assert calls["send"] == 1


def test_run_skips_llm_when_smtp_login_fails(tmp_path, monkeypatch):
    # 설정값은 다 채워져 있지만 앱 비밀번호 폐기·호스트 도달 불가로 로그인이 안 되는
    # 경우. 존재 검사만으로는 통과하므로 실제 로그인까지 확인해야 할당량이 안 샌다.
    import smtplib

    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "revoked")
    monkeypatch.setenv("MAIL_TO", "to@example.com")

    rc, calls = _run_with_one_new_post(
        tmp_path,
        monkeypatch,
        smtp_error=smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted"),
    )
    assert rc == 1
    assert calls["verify"] == 1
    assert calls["summarize"] == 0   # 요약에 할당량을 쓰지 않는다
    assert calls["send"] == 0


def test_run_verifies_smtp_before_summarizing(tmp_path, monkeypatch):
    # 순서 보장: 검증(verify) → 요약(summarize) → 발송(send).
    # 검증이 요약보다 뒤로 가면 할당량을 쓰고 나서야 발송 불가를 알게 된다.
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")

    rc, calls = _run_with_one_new_post(tmp_path, monkeypatch)
    assert rc == 0
    assert calls["order"] == ["verify", "summarize", "send"]


def test_run_dry_run_still_summarizes_without_mail_settings(tmp_path, monkeypatch):
    # --dry-run 은 원래 발송하지 않으므로 기존대로 요약을 확인할 수 있어야 한다.
    for var in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO", "MAIL_FROM"):
        monkeypatch.delenv(var, raising=False)

    rc, calls = _run_with_one_new_post(tmp_path, monkeypatch, dry_run=True)
    assert rc == 0
    assert calls["order"] == ["summarize"]   # 발송 검증·발송은 건너뛴다


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

# --- 모델 설정 precedence: GEMINI_MODEL > MODEL > config.yaml > 코드 기본값 ---
#
# GitHub Actions 의 repository Variable `MODEL` 이 workflow 를 통해 GEMINI_MODEL 로
# 들어온다. 그 Variable 은 유일한 출처가 아니라 override 다 — 없으면 config.yaml 의
# llm.model, 그것도 없으면 _DEFAULT_MODEL 로 내려간다. 아래 테스트는 실제 모델명이
# 아닌 임의 문자열을 써서 '어느 출처가 이겼는가'만 확인한다(특정 모델에 묶이지 않도록).


@pytest.fixture(autouse=True)
def _clear_model_env(monkeypatch):
    """이 파일의 모든 테스트가 깨끗한 환경에서 돌도록 모델 환경변수를 지운다."""
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("MODEL", raising=False)


def _yaml_with(tmp_path, **llm_over):
    import yaml

    base = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    for k, v in llm_over.items():
        if v is _MISSING:
            base["llm"].pop(k, None)
        else:
            base["llm"][k] = v
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    return p


_MISSING = object()


def test_gemini_model_env_wins_over_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "env-primary")
    monkeypatch.setenv("MODEL", "legacy-env")
    cfg = load_config(_yaml_with(tmp_path, model="from-yaml"))
    assert cfg.llm.model == "env-primary"
    assert "GEMINI_MODEL" in cfg.llm.model_source


def test_model_env_is_used_when_gemini_model_is_absent(tmp_path, monkeypatch):
    # 로컬 실행/기존 설정 호환성을 위해 MODEL 도 계속 인정한다.
    monkeypatch.setenv("MODEL", "legacy-env")
    cfg = load_config(_yaml_with(tmp_path, model="from-yaml"))
    assert cfg.llm.model == "legacy-env"
    assert "MODEL" in cfg.llm.model_source


def test_config_yaml_is_used_when_no_env_is_set(tmp_path):
    cfg = load_config(_yaml_with(tmp_path, model="from-yaml"))
    assert cfg.llm.model == "from-yaml"
    assert "config.yaml" in cfg.llm.model_source


def test_code_default_is_used_when_yaml_has_no_model(tmp_path):
    from src.config import _DEFAULT_MODEL

    cfg = load_config(_yaml_with(tmp_path, model=_MISSING))
    assert cfg.llm.model == _DEFAULT_MODEL
    assert "기본값" in cfg.llm.model_source


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_env_is_not_a_model_name(tmp_path, monkeypatch, blank):
    # 워크플로가 vars.MODEL 을 넘길 때 Variable 이 비어 있으면 빈 문자열이 들어온다.
    # 그것을 모델명으로 쓰면 전 요청이 죽는다 — 반드시 다음 순위로 넘어가야 한다.
    monkeypatch.setenv("GEMINI_MODEL", blank)
    cfg = load_config(_yaml_with(tmp_path, model="from-yaml"))
    assert cfg.llm.model == "from-yaml"


def test_blank_gemini_model_falls_through_to_model_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "")
    monkeypatch.setenv("MODEL", "legacy-env")
    cfg = load_config(_yaml_with(tmp_path, model="from-yaml"))
    assert cfg.llm.model == "legacy-env"


def test_blank_yaml_model_falls_through_to_default(tmp_path):
    from src.config import _DEFAULT_MODEL

    cfg = load_config(_yaml_with(tmp_path, model="   "))
    assert cfg.llm.model == _DEFAULT_MODEL


def test_env_model_is_surrounded_by_configured_fallbacks(tmp_path, monkeypatch):
    # env 로 지정한 primary 가 체인의 맨 앞이고, 설정된 대체 모델은 그대로 남는다.
    monkeypatch.setenv("GEMINI_MODEL", "env-primary")
    cfg = load_config(_yaml_with(tmp_path, fallback_models=["fb-a", "fb-b"]))
    assert cfg.llm.model_chain == ["env-primary", "fb-a", "fb-b"]


def test_env_model_actually_reaches_the_request_url(tmp_path, monkeypatch):
    # 설정 precedence 가 실제 호출 URL 까지 이어지는지(연결이 끊긴 곳이 없는지).
    monkeypatch.setenv("GEMINI_MODEL", "env-primary")
    cfg = load_config(_yaml_with(tmp_path, fallback_models=[]))
    s = Summarizer(cfg.llm)
    s.cfg.api_key = "test-key"
    sess = _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))])

    class _Recording:
        def __init__(self, inner):
            self.inner = inner
            self.urls = []

        def post(self, url, **kw):
            self.urls.append(url)
            return self.inner.post(url, **kw)

    s.session = rec = _Recording(sess)
    s.summarize(_post())
    assert "models/env-primary:generateContent" in rec.urls[0]


def test_startup_log_names_the_effective_model_without_secrets(caplog):
    import logging

    from src.main import _log_llm_settings

    cfg = _cfg(model="env-primary", fallback_models=["fb-a"], api_key="super-secret-key")
    cfg.model_source = "GEMINI_MODEL 환경변수"
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_llm_settings(cfg)
    text = caplog.text
    assert "LLM 설정 — primary=env-primary" in text
    assert "fb-a" in text
    assert "GEMINI_MODEL" in text
    assert "super-secret-key" not in text     # API key 는 절대 로그로 나가지 않는다


# --- 실패 분류 ---


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, LLMErrorKind.AUTH),
        (403, LLMErrorKind.AUTH),
        (429, LLMErrorKind.RATE_LIMIT),
        (404, LLMErrorKind.MODEL_UNAVAILABLE),
        (400, LLMErrorKind.BAD_REQUEST),
        (408, LLMErrorKind.TRANSIENT),
        (500, LLMErrorKind.TRANSIENT),
        (502, LLMErrorKind.TRANSIENT),
        (503, LLMErrorKind.TRANSIENT),
        (504, LLMErrorKind.TRANSIENT),
    ],
)
def test_status_classification(status, kind):
    assert classify_status(status) is kind


def test_rate_limit_and_server_error_are_not_the_same_kind():
    # 429 는 '한도가 막혔다', 503 은 '잠깐 흔들렸다' — 상위 정책이 달라야 한다.
    assert classify_status(429) is not classify_status(503)


def test_network_errors_classify_as_transient():
    import requests

    assert classify_error(requests.ConnectTimeout("x")) is LLMErrorKind.TRANSIENT
    assert classify_error(requests.ConnectionError("x")) is LLMErrorKind.TRANSIENT
    assert classify_error(requests.ReadTimeout("x")) is LLMErrorKind.TRANSIENT


def test_content_failure_classifies_as_content():
    assert classify_error(SummaryUnavailable("깨짐")) is LLMErrorKind.CONTENT


@pytest.mark.parametrize(
    "exc",
    [ValueError("?"), TypeError("?"), KeyError("?"), AssertionError("?"), AttributeError("?")],
)
def test_programming_errors_classify_as_internal(exc):
    """우리 코드의 버그를 'Gemini 일시 장애'로 오진하면 안 된다.

    TRANSIENT 로 세면 같은 버그를 남은 모든 글·배치에서 다시 밟으면서 백오프까지
    자게 된다. INTERNAL 은 재시도하지 않고 즉시 남은 호출을 멈춘다.
    """
    assert classify_error(exc) is LLMErrorKind.INTERNAL


def test_os_level_socket_errors_still_classify_as_transient():
    # builtin ConnectionError/TimeoutError 는 OSError 하위의 '네트워크 장애'다.
    # 프로그래밍 오류가 아니므로 INTERNAL 로 승격해 실행을 멈추면 안 된다.
    assert classify_error(ConnectionError("연결 끊김")) is LLMErrorKind.TRANSIENT
    assert classify_error(TimeoutError("시간 초과")) is LLMErrorKind.TRANSIENT


def test_internal_is_a_stop_calling_kind():
    from src.summarizer import _STOP_CALLING_KINDS

    assert LLMErrorKind.INTERNAL in _STOP_CALLING_KINDS
    # 반대로 이 둘은 절대 전역 중단으로 승격되면 안 된다.
    assert LLMErrorKind.TRANSIENT not in _STOP_CALLING_KINDS
    assert LLMErrorKind.CONTENT not in _STOP_CALLING_KINDS


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, LLMErrorKind.AUTH),
        (403, LLMErrorKind.AUTH),
        (429, LLMErrorKind.RATE_LIMIT),
        (400, LLMErrorKind.BAD_REQUEST),
        (503, LLMErrorKind.TRANSIENT),
    ],
)
def test_generate_raises_typed_error_with_model_and_status(status, kind):
    s = Summarizer(_cfg(max_retries=0))
    _stub(s, [_FakeResponse(status, text="boom")])
    with pytest.raises(LLMCallError) as exc:
        s.summarize(_post())
    assert exc.value.kind is kind
    assert exc.value.status == status
    assert exc.value.model == "gemini-flash-latest"
    # 기존 호출부 호환: 여전히 RuntimeError 다
    assert isinstance(exc.value, RuntimeError)


def test_all_models_unavailable_is_typed_model_unavailable():
    s = Summarizer(_chain_cfg())
    s.session = _ModelSession(
        {_PRIMARY: _gone_resp(), _FB1: _gone_resp(), _FB2: _gone_resp()}
    )
    with pytest.raises(SummaryUnavailable) as exc:
        s.summarize(_post())
    assert exc.value.kind is LLMErrorKind.MODEL_UNAVAILABLE


# --- 재시도 정책 ---


@pytest.fixture
def no_sleep(monkeypatch):
    """실제 시간을 기다리지 않는다. 요청된 대기 시간만 기록한다."""
    waits = []
    # src.summarizer 는 `import time` 이므로 time 모듈의 sleep 을 갈면 그대로 적용된다.
    # 다만 이 패치는 전역이다 — 응답 지연을 흉내 내려고 time.sleep 을 쓰는 테스트
    # (_FakeSession(delay=...))에는 이 fixture 를 쓰면 안 된다.
    monkeypatch.setattr(time, "sleep", lambda s: waits.append(s))
    return waits


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_is_not_retried(status, no_sleep):
    s = Summarizer(_cfg(max_retries=3, retry_backoff_sec=5))
    sess = _stub(s, [_FakeResponse(status, text="API key not valid")])
    with pytest.raises(LLMCallError):
        s.summarize(_post())
    assert len(sess.sent) == 1      # 재시도 없음
    assert no_sleep == []           # 백오프도 자지 않음


def test_bad_request_is_not_retried(no_sleep):
    # 400 은 payload/설정 문제라 재시도해도 같은 답이 온다.
    s = Summarizer(_cfg(max_retries=3, retry_backoff_sec=5))
    sess = _stub(s, [_FakeResponse(400, text="Invalid argument")])
    with pytest.raises(LLMCallError):
        s.summarize(_post())
    assert len(sess.sent) == 1
    assert no_sleep == []


def test_rate_limit_is_retried_boundedly(no_sleep):
    s = Summarizer(_cfg(max_retries=2, retry_backoff_sec=1))
    sess = _stub(s, [_FakeResponse(429, text="quota") for _ in range(3)])
    with pytest.raises(LLMCallError) as exc:
        s.summarize(_post())
    assert exc.value.kind is LLMErrorKind.RATE_LIMIT
    assert len(sess.sent) == 3          # 최초 1 + 재시도 2 (무한 재시도 금지)
    assert len(no_sleep) == 2


def test_server_error_is_retried_the_configured_number_of_times(no_sleep):
    s = Summarizer(_cfg(max_retries=2, retry_backoff_sec=1))
    sess = _stub(s, [_FakeResponse(503, text="unavailable") for _ in range(3)])
    with pytest.raises(LLMCallError) as exc:
        s.summarize(_post())
    assert exc.value.kind is LLMErrorKind.TRANSIENT
    assert len(sess.sent) == 3


def test_backoff_is_exponential_with_bounded_jitter(no_sleep):
    from src.summarizer import _RETRY_JITTER_RATIO

    s = Summarizer(_cfg(max_retries=3, retry_backoff_sec=2))
    _stub(s, [_FakeResponse(503, text="unavailable") for _ in range(4)])
    with pytest.raises(LLMCallError):
        s.summarize(_post())

    assert len(no_sleep) == 3
    for i, waited in enumerate(no_sleep):
        base = 2 * (2**i)
        # 지터는 대기를 늘리는 방향으로만 붙는다(시간예산 판정이 낙관적으로 흔들리지 않게).
        assert base <= waited <= base * (1 + _RETRY_JITTER_RATIO)


def test_jitter_is_deterministic_when_the_random_source_is_pinned(monkeypatch, no_sleep):
    monkeypatch.setattr("src.summarizer.random.uniform", lambda a, b: b)
    s = Summarizer(_cfg(max_retries=1, retry_backoff_sec=4))
    _stub(s, [_FakeResponse(503, text="unavailable") for _ in range(2)])
    with pytest.raises(LLMCallError):
        s.summarize(_post())
    from src.summarizer import _RETRY_JITTER_RATIO

    assert no_sleep == [4 * (1 + _RETRY_JITTER_RATIO)]


def test_network_timeout_is_retried_as_transient(no_sleep):
    import requests

    s = Summarizer(_cfg(max_retries=2, retry_backoff_sec=1))

    class _Timeout:
        def __init__(self):
            self.n = 0

        def post(self, *a, **kw):
            self.n += 1
            raise requests.ReadTimeout("응답 시간 초과")

    s.session = _Timeout()
    with pytest.raises(requests.ReadTimeout):
        s.summarize(_post())
    # 최초 1 + 재시도 2. 예전에는 첫 실패에서 곧장 포기했다.
    assert s.session.n == 3
    assert len(no_sleep) == 2


def test_network_error_still_propagates_its_original_type_after_retries(no_sleep):
    # 재시도를 소진해도 원래 예외형을 유지한다(상위의 분류·처리가 기존과 같게).
    import requests

    s = Summarizer(_cfg(max_retries=1, retry_backoff_sec=0))

    class _Boom:
        def post(self, *a, **kw):
            raise requests.ConnectionError("연결 실패")

    s.session = _Boom()
    with pytest.raises(requests.ConnectionError):
        s.summarize(_post())


def test_retry_exhaustion_reports_the_typed_transient_failure(no_sleep):
    s = Summarizer(_cfg(max_retries=1, retry_backoff_sec=0))
    _stub(s, [_FakeResponse(503, text="unavailable") for _ in range(2)])
    with pytest.raises(LLMCallError) as exc:
        s.summarize(_post())
    assert exc.value.kind is LLMErrorKind.TRANSIENT
    assert exc.value.status == 503


def test_transient_failure_never_switches_models(no_sleep):
    """503 으로는 다른 모델로 자동 전환하지 않는다(운영자가 고정한 모델을 존중).

    404(MODEL_UNAVAILABLE)의 기존 대체 정책은 그대로 유지된다 —
    test_primary_404_falls_back_to_first_fallback 참고.
    """
    for status in (500, 502, 503, 504, 408):
        s = Summarizer(_chain_cfg(max_retries=1, retry_backoff_sec=0))
        s.session = _ModelSession({_PRIMARY: _err_resp(status, "UNAVAILABLE", "boom")})
        with pytest.raises(LLMCallError):
            s.summarize(_post())
        assert set(s.session.models) == {_PRIMARY}, status
        assert s._unavailable == set(), status


# --- 로그 위생: API key / 프롬프트 본문이 로그에 새면 안 된다 ---


def test_logs_name_the_model_without_leaking_key_or_prompt(caplog, no_sleep):
    import logging

    secret = "AIza-super-secret-key"
    body = "절대로 로그에 남으면 안 되는 본문 문장임. " * 8
    s = Summarizer(_cfg(api_key=secret, max_retries=1, retry_backoff_sec=0))
    _stub(s, [_FakeResponse(503, text="unavailable") for _ in range(2)])

    with caplog.at_level(logging.DEBUG, logger="src.summarizer"):
        with pytest.raises(LLMCallError):
            s.summarize(_post(body=body, title="증권사 내부통제 개선방안"))

    text = caplog.text
    assert "gemini-flash-latest" in text          # 모델은 추적 가능해야 하고
    assert "503" in text                          # 실패 원인도 보여야 한다
    assert "transient" in text
    assert secret not in text                     # 키는 절대 안 된다
    assert "절대로 로그에 남으면 안 되는 본문" not in text


def test_successful_call_logs_the_model_at_info(caplog):
    import logging

    s = Summarizer(_cfg())
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}'))])
    with caplog.at_level(logging.INFO, logger="src.summarizer"):
        s.summarize(_post())
    assert "model=gemini-flash-latest" in caplog.text
    assert "attempt=1/1" in caplog.text


# --- 종료성 실패는 일반 요약 경로에서도 남은 글을 태우지 않는다 ---


@pytest.mark.parametrize(
    "kind", [LLMErrorKind.AUTH, LLMErrorKind.RATE_LIMIT, LLMErrorKind.BAD_REQUEST]
)
def test_terminal_kind_stops_the_general_loop_immediately(kind):
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        raise LLMCallError("boom", kind=kind)

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0
    assert calls["n"] == 1                        # 브레이커(3회)를 기다리지 않는다
    assert all(p.summary == [] for p in posts)    # 전부 원문 발췌로 나간다


def test_transient_kind_still_uses_the_consecutive_breaker():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        raise LLMCallError("boom", kind=LLMErrorKind.TRANSIENT)

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0
    assert calls["n"] == 3


def test_content_kind_still_uses_the_consecutive_breaker():
    posts = [_post(post_id=str(i), url=f"https://example.com/{i}") for i in range(10)]
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None):
        calls["n"] += 1
        raise SummaryUnavailable("응답이 깨짐")

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": posts}) == 0
    assert calls["n"] == 3


# --- A. _post_within 자체 wall-clock 타임아웃도 재시도 정책을 탄다 -------------
#
# 회귀: 이 타임아웃은 TRANSIENT 로 '분류'되어 있으면서도 _generate_with 의
# `except requests.RequestException` 에 걸리지 않아 재시도를 전혀 타지 못했다.
# 실제 시간을 기다리지 않도록 워커 경계를 스텁으로 대체한다(데몬 스레드도 만들지 않는다).


class _PostWithinStub:
    """_post_within 을 대신해 '타임아웃 / 응답'을 순서대로 돌려준다.

    실제 스레드·소켓을 쓰지 않으므로 테스트가 기다리지도, 스레드를 남기지도 않는다.
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.budgets = []

    def __call__(self, url, headers, payload, budget):
        self.calls += 1
        self.budgets.append(budget)
        out = self._outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def _timeout(budget=5.0):
    return RequestDeadlineExceeded(f"요청이 시간예산({budget:.1f}초)을 초과함")


def test_a1_own_timeout_is_retried_and_can_recover(no_sleep):
    """A1 — 1회차 자체 타임아웃, 2회차 성공."""
    s = Summarizer(_chain_cfg(max_retries=2, retry_backoff_sec=1))
    stub = _PostWithinStub(
        [_timeout(), _FakeResponse(200, _envelope('{"summary": ["요약함"]}'))]
    )
    s._post_within = stub
    # 세션을 건드리면 안 된다 — 건드리면 스텁을 우회했다는 뜻이다.
    s.session = _ModelSession({})

    assert s.summarize(_post()) == ["요약함"]
    assert stub.calls == 2                    # API 계층 시도 2회
    assert len(no_sleep) == 1                 # 백오프 1회
    assert s._active_model == _PRIMARY        # 다른 모델로 넘어가지 않았다
    assert s._unavailable == set()


def test_a2_own_timeout_exhaustion_is_bounded_and_typed(no_sleep):
    """A2 — 모든 시도가 자체 타임아웃. 1 + max_retries 로 끝나고 kind 는 TRANSIENT."""
    s = Summarizer(_chain_cfg(max_retries=2, retry_backoff_sec=1))
    stub = _PostWithinStub([_timeout() for _ in range(5)])
    s._post_within = stub
    s.session = _ModelSession({})

    with pytest.raises(SummaryUnavailable) as exc:
        s.summarize(_post())

    assert stub.calls == 3                    # 무한 재시도 없음
    assert len(no_sleep) == 2
    assert exc.value.kind is LLMErrorKind.TRANSIENT
    assert classify_error(exc.value) is LLMErrorKind.TRANSIENT
    assert s._unavailable == set()            # 타임아웃으로 모델을 낙인찍지 않는다


def test_own_timeout_does_not_switch_models(no_sleep):
    # 타임아웃은 모델 문제가 아니다 — fallback 을 소진하면 안 된다.
    s = Summarizer(_chain_cfg(max_retries=1, retry_backoff_sec=0))
    calls = []

    def _post_within(url, headers, payload, budget):
        calls.append(url)
        raise _timeout()

    s._post_within = _post_within
    with pytest.raises(SummaryUnavailable):
        s.summarize(_post())
    assert {u.split("/models/")[1].split(":")[0] for u in calls} == {_PRIMARY}


def test_own_timeout_backoff_respects_the_remaining_budget(no_sleep):
    # 남은 예산 밖의 백오프는 자지 않는다(기존 정책 재사용을 확인).
    s = Summarizer(_cfg(max_retries=2, retry_backoff_sec=30))
    stub = _PostWithinStub([_timeout() for _ in range(3)])
    s._post_within = stub
    with pytest.raises(SummaryUnavailable):
        s.summarize(_post(), time.monotonic() + 5.0)
    assert stub.calls == 1
    assert no_sleep == []


def test_content_failure_is_not_retried_by_the_call_layer():
    """파싱 단계의 CONTENT 실패는 호출 재시도 정책과 무관하다(기존 의미 유지)."""
    s = Summarizer(_cfg(max_retries=2, retry_backoff_sec=0))
    sess = _stub(s, [_FakeResponse(200, _envelope("{쓰레기"))])
    with pytest.raises(SummaryUnavailable) as exc:
        s.summarize(_post())
    assert exc.value.kind is LLMErrorKind.CONTENT
    assert len(sess.sent) == 1                # 호출은 한 번뿐


def test_timeout_retries_do_not_grow_threads_without_bound():
    """실제 워커 경계로도 재시도가 도는지, 그리고 고아 스레드가 시도 수만큼만 생기는지.

    _post_within 은 마감을 넘긴 워커를 데몬으로 남겨 둔 채 빠져나온다. 재시도를
    추가하면 그 고아가 시도 수만큼 늘어나므로, max_retries 로 확실히 묶여 있어야 한다.
    """
    import threading

    s = Summarizer(_cfg(timeout_sec=0.2, max_retries=2, retry_backoff_sec=0))
    _stub(s, [_FakeResponse(200, _envelope('{"summary": ["요약함"]}')) for _ in range(3)],
          delay=2.0)

    before = {t for t in threading.enumerate()}
    with pytest.raises(SummaryUnavailable):
        s.summarize(_post())
    leftover = [t for t in threading.enumerate() if t not in before]

    # 1 + max_retries 만큼만 시도하고, 남은 워커는 전부 데몬이라 종료를 막지 않는다.
    assert len(leftover) <= 3
    assert all(t.daemon for t in leftover)


# --- B/C. 종료성 실패는 의안 배치까지 전파되고, 비종료성 실패는 그러지 않는다 ---


def _mixed_posts():
    """일반 게시물 1건 + 의안 2건."""
    general = _post(post_id="g1", url="https://example.com/g1")
    bills = [
        Post(
            source_key="assembly_bill",
            source_name="의안정보시스템 · 계류의안",
            post_id=f"PRC_{i}",
            title=f"제{i}호 일부개정법률안",
            url=f"https://likms.assembly.go.kr/bill/{i}",
            date="2026-07-01",
            body="제안이유 및 주요내용 원문임. " * 10,
        )
        for i in range(2)
    ]
    return general, bills


def _run_mixed(s):
    """summarize_all 을 돌리고 (일반 호출 수, 의안 호출 수) 를 돌려준다."""
    general, bills = _mixed_posts()
    counts = {"general": 0, "assembly": 0}
    original = s._generate

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None, **_):
        if "bill_id:" in prompt:
            counts["assembly"] += 1
        else:
            counts["general"] += 1
        return original(prompt, deadline, schema=schema, max_output_tokens=max_output_tokens)

    s._generate = _generate
    ok = s.summarize_all(
        {"금융위 · 보도자료": [general], "의안정보시스템 · 계류의안": bills}
    )
    return ok, counts, general, bills


@pytest.mark.parametrize(
    "kind",
    [
        LLMErrorKind.AUTH,
        LLMErrorKind.RATE_LIMIT,
        LLMErrorKind.BAD_REQUEST,
        LLMErrorKind.MODEL_UNAVAILABLE,
        LLMErrorKind.INTERNAL,
    ],
)
def test_b_general_terminal_failure_skips_the_assembly_stage(kind):
    """B — 일반 경로의 종료성 실패가 확정되면 의안 Gemini 호출은 0회다.

    회귀: 예전에는 일반 루프만 break 하고 summarize_all 이 그대로 의안 배치를 불러,
    죽은 자격증명·소진된 한도로 같은 서비스를 다시 두드렸다.
    """
    s = Summarizer(_cfg())
    s._generate = lambda *a, **k: (_ for _ in ()).throw(LLMCallError("boom", kind=kind))

    ok, counts, general, bills = _run_mixed(s)

    assert ok == 0
    assert counts["general"] == 1        # 첫 글에서 종료성 실패 확정
    assert counts["assembly"] == 0       # 의안은 한 번도 부르지 않는다
    # 메일 폴백은 그대로 — 요약이 비어 있을 뿐 예외는 새지 않는다
    assert general.summary == []
    assert all(b.summary == [] for b in bills)


def test_c1_general_transient_failure_still_allows_the_assembly_stage():
    """C1 — 일시 장애는 전역 중단이 아니다. 서비스가 회복됐을 수 있으므로 의안은 시도한다."""
    s = Summarizer(_cfg(max_consecutive_failures=3))
    calls = {"n": 0}

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None, **_):
        calls["n"] += 1
        if "bill_id:" in prompt:
            return _envelope(
                json.dumps(
                    {
                        "summaries": [
                            {"bill_id": f"PRC_{i}", "summary": ["첫째임", "둘째임", "셋째임"]}
                            for i in range(2)
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        raise LLMCallError("HTTP 503", kind=LLMErrorKind.TRANSIENT, status=503)

    s._generate = _generate
    ok, counts, general, bills = _run_mixed(s)

    assert s._terminal_failure is None
    assert counts["assembly"] == 1        # 의안 배치는 정상적으로 호출됐다
    assert ok == 2 and all(b.summary for b in bills)
    assert general.summary == []          # 일반 글만 발췌 폴백


def test_c2_general_content_failure_still_allows_the_assembly_stage():
    """C2 — 응답 내용 문제는 그 글에 달린 것이다. 의안 배치를 막으면 안 된다."""
    s = Summarizer(_cfg(max_consecutive_failures=3))

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None, **_):
        if "bill_id:" in prompt:
            return _envelope(
                json.dumps(
                    {
                        "summaries": [
                            {"bill_id": f"PRC_{i}", "summary": ["첫째임", "둘째임", "셋째임"]}
                            for i in range(2)
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        raise SummaryUnavailable("응답이 깨짐")

    s._generate = _generate
    ok, counts, general, bills = _run_mixed(s)

    assert s._terminal_failure is None
    assert counts["assembly"] == 1
    assert ok == 2


def test_terminal_state_is_not_sticky_across_invocations():
    """run A 가 AUTH 로 끝나도 run B 는 정상적으로 다시 호출할 수 있어야 한다."""
    s = Summarizer(_cfg())
    s._generate = lambda *a, **k: (_ for _ in ()).throw(
        LLMCallError("boom", kind=LLMErrorKind.AUTH)
    )
    ok_a, counts_a, _, _ = _run_mixed(s)
    assert counts_a["assembly"] == 0
    assert s._terminal_failure is LLMErrorKind.AUTH

    # 키가 복구된 다음 실행
    def _healthy(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        if "bill_id:" in prompt:
            return _envelope(
                json.dumps(
                    {
                        "summaries": [
                            {"bill_id": f"PRC_{i}", "summary": ["첫째임", "둘째임", "셋째임"]}
                            for i in range(2)
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        return _envelope('{"summary": ["요약함"]}')

    s._generate = _healthy
    ok_b, counts_b, general_b, bills_b = _run_mixed(s)

    assert s._terminal_failure is None
    assert counts_b["general"] == 1 and counts_b["assembly"] == 1
    assert ok_b == 3
    assert general_b.summary == ["요약함"] and all(b.summary for b in bills_b)


def test_terminal_skip_still_lets_the_mail_pipeline_run(caplog):
    """종료성 실패에서도 예외는 새지 않고, 로그만 남기고 정상 종료한다(메일 계속)."""
    import logging

    s = Summarizer(_cfg())
    s._generate = lambda *a, **k: (_ for _ in ()).throw(
        LLMCallError("boom", kind=LLMErrorKind.AUTH)
    )
    with caplog.at_level(logging.WARNING, logger="src.summarizer"):
        ok, counts, general, bills = _run_mixed(s)

    assert ok == 0
    assert "의안 배치 요약을 건너뜁니다" in caplog.text
    assert "auth" in caplog.text
    # 메일 렌더는 원문 발췌로 정상 동작한다
    html = build_html({b.source_name: bills for b in bills[:1]})
    assert "제안이유 및 주요내용 원문임" in html


def test_internal_error_stops_further_calls_but_keeps_excerpt_fallback():
    """D — INTERNAL 은 같은 버그를 반복하지 않되 메일 폴백은 그대로 둔다."""
    s = Summarizer(_cfg(max_consecutive_failures=3))
    s._generate = lambda *a, **k: (_ for _ in ()).throw(TypeError("내부 버그"))

    ok, counts, general, bills = _run_mixed(s)

    assert ok == 0
    assert counts["general"] == 1        # 브레이커(3회)를 기다리지 않는다
    assert counts["assembly"] == 0
    assert general.summary == [] and all(b.summary == [] for b in bills)
    html = build_html({general.source_name: [general]})
    assert ">AI 3줄 요약</div>" not in html    # 발췌로 나간다
