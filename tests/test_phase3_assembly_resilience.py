"""Phase 3 — 의안 배치 요약의 지연 내성/우아한 저하.

네트워크·실시계 대기를 쓰지 않는다. 시간은 time.monotonic 을 가짜 시계로 바꿔
결정적으로 흘린다(90/300/360초를 실제로 기다리는 테스트를 만들지 않는다).

여기서 지키는 계약:
  - 정상 실행은 25건 단위로 최대 3회 top-level 호출.
  - 한 배치의 응답 타임아웃이 같은 요청을 반복하지 않고, 뒤 배치의 시간을 훔치지 않는다.
  - 연결 실패·5xx 의 기존 bounded retry, 404 모델 폴백, 배치 브레이커는 그대로.
"""
import json
import logging
import os
import sys
import time
from copy import deepcopy

import pytest
import requests
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import assembly_summary as asm
from src.assembly_summary import (
    BATCH_SCHEMA,
    _allocate_batch_deadline,
    _request_timeout_sec,
    summarize_assembly_bills,
)
from src.config import AssemblyBatchConfig, LLMConfig, load_config
from src.main import _source_new_cap
from src.models import ASSEMBLY_SOURCE_KEY, Post
from src.summarizer import (
    _CONNECT_TIMEOUT_CAP,
    _MIN_CALL_SEC,
    _MAX_OUTPUT_TOKENS_THINKING,
    _SCHEMA,
    LLMErrorKind,
    LLMCallError,
    RequestDeadlineExceeded,
    Summarizer,
    classify_error,
    supports_thinking_level,
)

_SOURCE = "의안정보시스템 · 계류의안"
_MODEL_36 = "gemini-3.6-flash"
_ALIAS = "gemini-flash-latest"
_LITE = "gemini-3.5-flash-lite"
_M25 = "gemini-2.5-flash"


# ── 공용 헬퍼 ──────────────────────────────────────────────────────────────
class _Clock:
    """단조시계 대역. advance() 로만 시간이 흐른다(실제 sleep 없음)."""

    def __init__(self, start: float = 10_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(time, "monotonic", c)
    return c


def _llm(batch=None, **over) -> LLMConfig:
    base = dict(
        enabled=True,
        model=_MODEL_36,
        fallback_models=[],
        lines=3,
        max_line_chars=90,
        min_body_chars=10,
        max_input_chars=1000,
        max_posts=10,
        rpm=0,
        timeout_sec=45,
        max_retries=2,
        retry_backoff_sec=0,
        api_key="test-key",
        assembly_batch=batch or AssemblyBatchConfig(),
    )
    base.update(over)
    return LLMConfig(**base)


def _batch_cfg(**over) -> AssemblyBatchConfig:
    base = dict(
        max_bills=75,
        budget_sec=300.0,
        request_timeout_sec=90.0,
        retry_response_timeout=False,
        thinking_level="minimal",
    )
    base.update(over)
    return AssemblyBatchConfig(**base)


def _bill(i: int, body: str = "현행법은 규정하지 아니함. 이에 개선하려는 것임.") -> Post:
    return Post(
        source_key=ASSEMBLY_SOURCE_KEY,
        source_name=_SOURCE,
        post_id=f"PRC_{i:04d}",
        title=f"제{i}호 일부개정법률안",
        url=f"https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_{i:04d}",
        date="2026-08-01",
        body=body,
    )


def _envelope(text: str, usage: dict | None = None, model_version: str | None = None) -> dict:
    data = {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]
    }
    if usage is not None:
        data["usageMetadata"] = usage
    if model_version is not None:
        data["modelVersion"] = model_version
    return data


def _reply_for(ids, usage=None, model_version=None) -> dict:
    rows = [
        {"bill_id": b, "summary": [f"{b} 첫째임", f"{b} 둘째임", f"{b} 셋째임"]}
        for b in ids
    ]
    return _envelope(
        json.dumps({"summaries": rows}, ensure_ascii=False), usage, model_version
    )


def _ids_in(prompt: str, posts) -> list[str]:
    return [p.post_id for p in posts if f"bill_id: {p.post_id}\n" in prompt]


class _Calls:
    """_generate 대역. 호출별 요청 ID·deadline·kwargs 를 기록한다.

    behave(ids, n) 가 예외를 돌려주면 raise, dict 를 돌려주면 그대로 응답으로 쓴다.
    """

    def __init__(self, posts, behave=None, usage=None, model_version=None):
        self.posts = posts
        self.calls: list[list[str]] = []
        self.windows: list[float | None] = []
        self.kwargs: list[dict] = []
        self._behave = behave
        self._usage = usage
        self._model_version = model_version

    def __call__(self, prompt, deadline=None, *, telemetry=None, **kw):
        ids = _ids_in(prompt, self.posts)
        self.calls.append(ids)
        self.windows.append(None if deadline is None else deadline - time.monotonic())
        self.kwargs.append(kw)
        outcome = self._behave(ids, len(self.calls)) if self._behave else None
        if isinstance(outcome, BaseException):
            raise outcome
        data = outcome if isinstance(outcome, dict) else _reply_for(
            ids, self._usage, self._model_version
        )
        if telemetry is not None:
            telemetry.model = _MODEL_36
            telemetry.elapsed_sec = 1.0
            telemetry.fill_from_response(data)
        return data


def _run_batches(posts, cfg=None, behave=None, usage=None, model_version=None):
    s = Summarizer(_llm(batch=cfg or _batch_cfg()))
    calls = _Calls(posts, behave, usage, model_version)
    s._generate = calls
    ok = summarize_assembly_bills(s, {_SOURCE: posts})
    return ok, calls


class _Session:
    """정해진 응답/예외를 순서대로 돌려주는 세션(마지막 항목은 반복 사용)."""

    def __init__(self, outcomes, delay=0.0):
        self._outcomes = list(outcomes)
        self._delay = delay
        self.sent = []
        self.timeouts = []
        self.models = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.models.append(url.rstrip("/").split("/")[-1].split(":")[0])
        # 교정 재시도는 payload 를 그 자리에서 갈아끼우므로 스냅샷으로 남긴다
        # (같은 dict 를 두 번 담으면 '무엇을 보냈는가'를 검증할 수 없다).
        self.sent.append(deepcopy(json))
        self.timeouts.append(timeout)
        if self._delay:
            time.sleep(self._delay)
        outcome = self._outcomes[0] if len(self._outcomes) == 1 else self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _ok_resp():
    return _Resp(200, _envelope('{"summaries": []}'))


def _err(code, status="", message=""):
    payload = {"error": {"code": code, "status": status, "message": message}}
    return _Resp(code, payload, text=json.dumps(payload))


def _assembly_generate(s: Summarizer, cfg: AssemblyBatchConfig, prompt="p", deadline=None):
    """의안 배치가 실제로 부르는 형태의 _generate 호출."""
    return s._generate(
        prompt,
        deadline,
        schema=BATCH_SCHEMA,
        max_output_tokens=cfg.max_output_tokens,
        request_timeout_sec=cfg.request_timeout_sec,
        retry_response_timeout=cfg.retry_response_timeout,
        thinking_level=cfg.thinking_level,
    )


def _yaml_config(tmp_path, mutate):
    raw = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


# ==========================================================================
# C. 설정
# ==========================================================================
def test_C1_config_without_phase3_fields_keeps_previous_semantics(tmp_path):
    """새 키를 지운 custom config 는 Phase 2 까지의 동작으로 자연스럽게 돌아간다."""
    def drop(raw):
        for key in ("request_timeout_sec", "retry_response_timeout", "thinking_level"):
            raw["llm"]["assembly_batch"].pop(key, None)

    cfg = load_config(_yaml_config(tmp_path, drop)).llm
    assert cfg.assembly_batch.request_timeout_sec is None
    assert cfg.assembly_batch.retry_response_timeout is True
    assert cfg.assembly_batch.thinking_level is None
    # 요청 타임아웃은 기존 llm.timeout_sec 으로 되돌아간다
    s = Summarizer(cfg)
    assert _request_timeout_sec(s, cfg.assembly_batch) == cfg.timeout_sec


def test_C2_request_timeout_is_loaded():
    assert load_config("config.yaml").llm.assembly_batch.request_timeout_sec == 90.0


def test_C3_retry_response_timeout_false_is_loaded():
    assert load_config("config.yaml").llm.assembly_batch.retry_response_timeout is False


def test_C4_thinking_level_is_loaded():
    assert load_config("config.yaml").llm.assembly_batch.thinking_level == "minimal"


@pytest.mark.parametrize(
    "key,value",
    [
        ("request_timeout_sec", 0),
        ("request_timeout_sec", -5),
        ("request_timeout_sec", "빠르게"),
        ("budget_sec", -1),
        ("budget_sec", "오래"),
        ("thinking_level", "turbo"),
        ("thinking_level", 3),
        ("retry_response_timeout", "maybe"),
    ],
)
def test_C5_invalid_values_fail_explicitly(tmp_path, key, value):
    """설정 오타가 조용히 기본값으로 넘어가면 기능이 말없이 꺼진다 — 즉시 알린다."""
    path = _yaml_config(
        tmp_path, lambda raw: raw["llm"]["assembly_batch"].__setitem__(key, value)
    )
    with pytest.raises(ValueError) as e:
        load_config(path)
    assert key in str(e.value)


# --- 숫자 설정의 불리언 거절 (PR #26 Codex follow-up #2) --------------------
#
# bool 은 int 의 하위형이라 float(True) == 1.0, float(False) == 0.0 으로 조용히
# 통과한다. `request_timeout_sec: true` 는 25건 배치를 1초 타임아웃으로 만들고,
# `budget_sec: false` 는 0 = '무제한'과 겹쳐 전체 시간 상한을 없앤다.
@pytest.mark.parametrize("key", ["request_timeout_sec", "budget_sec"])
@pytest.mark.parametrize("value", [True, False])
def test_C12toC15_boolean_numeric_settings_are_rejected(tmp_path, key, value):
    path = _yaml_config(
        tmp_path, lambda raw: raw["llm"]["assembly_batch"].__setitem__(key, value)
    )
    with pytest.raises(ValueError) as e:
        load_config(path)
    assert key in str(e.value)


def test_C15b_budget_sec_false_is_not_silently_unlimited(tmp_path):
    """`budget_sec: false` 가 0(무제한)으로 통하면 오타 하나가 시간 상한을 없앤다."""
    path = _yaml_config(
        tmp_path, lambda raw: raw["llm"]["assembly_batch"].__setitem__("budget_sec", False)
    )
    with pytest.raises(ValueError):
        load_config(path)
    # 반면 명시적인 0 은 기존 의미(무제한) 그대로 허용된다.
    zero = _yaml_config(
        tmp_path, lambda raw: raw["llm"]["assembly_batch"].__setitem__("budget_sec", 0)
    )
    assert load_config(zero).llm.assembly_batch.budget_sec == 0.0


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("request_timeout_sec", "90", 90.0),
        ("request_timeout_sec", 90, 90.0),
        ("request_timeout_sec", 90.0, 90.0),
        ("budget_sec", "300", 300.0),
        ("budget_sec", 300, 300.0),
    ],
)
def test_C16_numeric_string_compatibility_is_unchanged(tmp_path, key, value, expected):
    path = _yaml_config(
        tmp_path, lambda raw: raw["llm"]["assembly_batch"].__setitem__(key, value)
    )
    assert getattr(load_config(path).llm.assembly_batch, key) == expected


# --- 무한대 숫자 설정 거절 (PR #26 Codex follow-up #4) ----------------------
#
# +inf 는 범위 검사(> 0 / >= 0)를 통과한다. 그런데 이 설정들은 실행 시간을
# **제한하려고** 존재한다 — 무한대 예산은 guard 를 조용히 없애고, 무한대
# request_timeout_sec 은 Thread.join(timeout=inf) 에서 OverflowError 가 된다.
_INF, _NINF, _NAN = float("inf"), float("-inf"), float("nan")


@pytest.mark.parametrize("value", [_INF, "inf", _NINF, "-inf", _NAN, "nan"])
def test_C17toC22_non_finite_request_timeout_is_rejected(tmp_path, value):
    path = _yaml_config(
        tmp_path,
        lambda raw: raw["llm"]["assembly_batch"].__setitem__("request_timeout_sec", value),
    )
    with pytest.raises(ValueError) as e:
        load_config(path)
    assert "request_timeout_sec" in str(e.value)


@pytest.mark.parametrize("value", [_INF, "inf", _NINF, _NAN])
def test_C19_non_finite_assembly_budget_is_rejected(tmp_path, value):
    path = _yaml_config(
        tmp_path, lambda raw: raw["llm"]["assembly_batch"].__setitem__("budget_sec", value)
    )
    with pytest.raises(ValueError) as e:
        load_config(path)
    assert "budget_sec" in str(e.value)


@pytest.mark.parametrize("value", [_INF, "inf", _NINF, _NAN])
def test_C20_non_finite_shared_budget_is_rejected(tmp_path, value):
    """무한대 공유 예산은 Phase 3 의 shared LLM envelope 를 통째로 무효화한다."""
    path = _yaml_config(tmp_path, lambda raw: raw["llm"].__setitem__("total_budget_sec", value))
    with pytest.raises(ValueError) as e:
        load_config(path)
    assert "total_budget_sec" in str(e.value)


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("request_timeout_sec", "90", 90.0),
        ("budget_sec", "300", 300.0),
        ("budget_sec", 0, 0.0),            # 0 = 무제한이라는 기존 의미 유지
    ],
)
def test_C23_finite_values_are_still_accepted(tmp_path, key, value, expected):
    path = _yaml_config(
        tmp_path, lambda raw: raw["llm"]["assembly_batch"].__setitem__(key, value)
    )
    assert getattr(load_config(path).llm.assembly_batch, key) == expected


def test_C23b_shared_budget_string_and_zero_are_still_accepted(tmp_path):
    for value, expected in (("360", 360.0), (0, 0.0)):
        path = _yaml_config(
            tmp_path, lambda raw: raw["llm"].__setitem__("total_budget_sec", value)
        )
        assert load_config(path).llm.total_budget_sec == expected


def test_C6_global_new_cap_is_unchanged():
    assert load_config("config.yaml").fetch.max_new_per_source == 50


def test_C7_assembly_overrides_new_cap_to_75():
    cfg = load_config("config.yaml")
    caps = {s.key: _source_new_cap(s, cfg.fetch.max_new_per_source) for s in cfg.sources}
    assert caps[ASSEMBLY_SOURCE_KEY] == 75
    others = {k: v for k, v in caps.items() if k != ASSEMBLY_SOURCE_KEY}
    assert others and set(others.values()) == {50}


def test_C7b_invalid_source_override_falls_back_to_global():
    from src.config import SourceConfig

    src = SourceConfig(key="x", name="x", type="t", list_url="u", extra={"max_new_per_run": "많이"})
    assert _source_new_cap(src, 50) == 50
    src.extra["max_new_per_run"] = 0
    assert _source_new_cap(src, 50) == 50


def test_C_assembly_detail_budget_is_360_in_production_config():
    cfg = load_config("config.yaml")
    src = next(s for s in cfg.sources if s.key == ASSEMBLY_SOURCE_KEY)
    assert src.extra["detail_budget_sec"] == 360


# ==========================================================================
# S. Summarizer — generation config / 타임아웃 / 재시도
# ==========================================================================
def _general_gc(model):
    s = Summarizer(_llm(model=model, max_retries=0))
    s.session = _Session([_Resp(200, _envelope('{"summary": ["첫째임","둘째임","셋째임"]}'))])
    s.summarize(
        Post(
            source_key="fsc_press", source_name="금융위 · 보도자료", post_id="1",
            title="t", url="https://example.com/1", date="2026-08-01",
            body="가나다라마바사아자차카타파하 " * 10,
        )
    )
    return s.session.sent[0]["generationConfig"]


def test_S1_general_gemini36_generation_config_is_unchanged():
    gc = _general_gc(_MODEL_36)
    assert "thinkingConfig" not in gc          # Phase 3 이전과 동일
    assert "temperature" not in gc
    assert gc["responseSchema"] == _SCHEMA
    assert gc["maxOutputTokens"] == _MAX_OUTPUT_TOKENS_THINKING


def _assembly_gc(model, cfg=None):
    s = Summarizer(_llm(model=model, max_retries=0, batch=cfg or _batch_cfg()))
    s.session = _Session([_ok_resp()])
    _assembly_generate(s, s.cfg.assembly_batch)
    return s.session.sent[0]["generationConfig"]


def test_S2_assembly_gemini36_sends_minimal_thinking_level():
    gc = _assembly_gc(_MODEL_36)
    assert gc["thinkingConfig"] == {"thinkingLevel": "minimal"}
    # 출력 상한의 기존 의미(호출별 지정값)는 그대로다
    assert gc["maxOutputTokens"] == AssemblyBatchConfig().max_output_tokens
    assert gc["responseSchema"] == BATCH_SCHEMA


def test_S3_gemini25_keeps_thinking_budget_contract():
    gc = _assembly_gc(_M25)
    assert gc["thinkingConfig"] == {"thinkingBudget": 0}   # thinkingLevel 로 바뀌지 않는다
    assert "temperature" in gc


@pytest.mark.parametrize("model", [_ALIAS, _LITE, "gemini-9-ultra", "gemini-3.6-flash-lite"])
def test_S4_unknown_models_never_receive_thinking_level(model):
    assert supports_thinking_level(model, "minimal") is False
    assert "thinkingConfig" not in _assembly_gc(model)


def test_S5_assembly_request_budget_is_90_and_split():
    gc_session = Summarizer(_llm(max_retries=0, batch=_batch_cfg()))
    gc_session.session = _Session([_ok_resp()])
    _assembly_generate(gc_session, gc_session.cfg.assembly_batch)
    connect, read = gc_session.session.timeouts[0]
    assert connect <= _CONNECT_TIMEOUT_CAP
    assert 89.0 <= connect + read <= 90.0


def test_S6_general_request_budget_stays_at_llm_timeout():
    s = Summarizer(_llm(timeout_sec=45, max_retries=0))
    s.session = _Session([_Resp(200, _envelope('{"summary": ["첫째임","둘째임","셋째임"]}'))])
    s.summarize(
        Post(
            source_key="fsc_press", source_name="금융위 · 보도자료", post_id="1",
            title="t", url="https://example.com/1", date="2026-08-01",
            body="가나다라마바사아자차카타파하 " * 10,
        )
    )
    connect, read = s.session.timeouts[0]
    assert 44.0 <= connect + read <= 45.0


def test_S7_assembly_read_timeout_is_not_retried():
    s = Summarizer(_llm(max_retries=2, batch=_batch_cfg()))
    s.session = _Session([requests.ReadTimeout("read timed out")])
    with pytest.raises(requests.ReadTimeout) as e:
        _assembly_generate(s, s.cfg.assembly_batch)
    assert len(s.session.sent) == 1                       # 같은 요청 재시도 없음
    assert classify_error(e.value) is LLMErrorKind.TRANSIENT   # 분류는 그대로
    assert s.session.models == [_MODEL_36]                # 모델 전환도 없음


def test_S8_assembly_own_deadline_is_not_retried():
    cfg = _batch_cfg(request_timeout_sec=0.3)
    s = Summarizer(_llm(max_retries=2, batch=cfg))
    s.session = _Session([_ok_resp()], delay=2.0)
    with pytest.raises(RequestDeadlineExceeded) as e:
        _assembly_generate(s, cfg)
    assert len(s.session.sent) == 1
    assert e.value.kind is LLMErrorKind.TRANSIENT


def test_S9_general_read_timeout_keeps_bounded_retry():
    s = Summarizer(_llm(max_retries=2, retry_backoff_sec=0))
    s.session = _Session([requests.ReadTimeout("read timed out")])
    with pytest.raises(requests.ReadTimeout):
        s.summarize(
            Post(
                source_key="fsc_press", source_name="금융위 · 보도자료", post_id="1",
                title="t", url="https://example.com/1", date="2026-08-01",
                body="가나다라마바사아자차카타파하 " * 10,
            )
        )
    assert len(s.session.sent) == 3            # 최초 1회 + max_retries 2회


def test_S10_assembly_connect_timeout_keeps_bounded_retry():
    s = Summarizer(_llm(max_retries=2, retry_backoff_sec=0, batch=_batch_cfg()))
    s.session = _Session([requests.ConnectTimeout("connect timed out")])
    with pytest.raises(requests.ConnectTimeout):
        _assembly_generate(s, s.cfg.assembly_batch)
    assert len(s.session.sent) == 3


def test_S11_assembly_connection_error_keeps_bounded_retry():
    s = Summarizer(_llm(max_retries=2, retry_backoff_sec=0, batch=_batch_cfg()))
    s.session = _Session([requests.ConnectionError("connection reset")])
    with pytest.raises(requests.ConnectionError):
        _assembly_generate(s, s.cfg.assembly_batch)
    assert len(s.session.sent) == 3


def test_S12_assembly_503_retries_without_switching_model():
    s = Summarizer(
        _llm(
            fallback_models=[_LITE],
            max_retries=2,
            retry_backoff_sec=0,
            batch=_batch_cfg(),
        )
    )
    s.session = _Session([_err(503, "UNAVAILABLE", "overloaded")])
    with pytest.raises(Exception) as e:
        _assembly_generate(s, s.cfg.assembly_batch)
    assert classify_error(e.value) is LLMErrorKind.TRANSIENT
    assert s.session.models == [_MODEL_36] * 3      # 다른 모델로 넘어가지 않는다


def test_S13_assembly_404_still_falls_back_to_configured_model():
    s = Summarizer(_llm(fallback_models=[_LITE], max_retries=0, batch=_batch_cfg()))
    s.session = _Session(
        [
            _err(404, "NOT_FOUND", "This model models/x is no longer available"),
            _ok_resp(),
        ]
    )
    _assembly_generate(s, s.cfg.assembly_batch)
    assert s.session.models == [_MODEL_36, _LITE]
    # 지원이 확인되지 않은 대체 모델에는 thinkingLevel 을 보내지 않는다
    assert "thinkingConfig" not in s.session.sent[1]["generationConfig"]


# ==========================================================================
# A. 배치 구성과 시간 배분
# ==========================================================================
@pytest.mark.parametrize(
    "count,batches",
    [(1, 1), (25, 1), (26, 2), (50, 2), (51, 3), (60, 3), (75, 3)],
)
def test_A1toA6_top_level_batch_counts(count, batches):
    posts = [_bill(i) for i in range(count)]
    ok, calls = _run_batches(posts)
    assert ok == count
    assert len(calls.calls) == batches
    assert all(len(c) <= 25 for c in calls.calls)


def test_A7_char_limit_can_still_produce_more_than_three_batches():
    """max_batch_chars 계약은 그대로 — 긴 본문이면 25건 전에 나뉜다."""
    posts = [_bill(i, body="가" * 300) for i in range(40)]
    cfg = _batch_cfg(max_batch_chars=1000)
    ok, calls = _run_batches(posts, cfg)
    assert ok == 40
    assert len(calls.calls) > 3


def test_A8_first_of_three_batches_gets_about_the_request_timeout(clock):
    posts = [_bill(i) for i in range(75)]
    _, calls = _run_batches(posts)
    # 300/3=100 이지만 요청 타임아웃 90 이 상한이다
    assert 89.0 <= calls.windows[0] <= 90.0


def test_A9_slow_first_batch_does_not_shrink_the_next_window(clock):
    """첫 배치가 window 를 다 쓰고 타임아웃해도 뒤 배치는 다시 ~90초를 받는다."""
    posts = [_bill(i) for i in range(75)]

    def behave(ids, n):
        if n == 1:
            clock.advance(90.0)
            return requests.ReadTimeout("read timed out")
        return None

    ok, calls = _run_batches(posts, behave=behave)
    assert len(calls.calls) == 3
    assert 89.0 <= calls.windows[1] <= 90.0     # 몇 초짜리로 쪼그라들지 않는다
    assert 89.0 <= calls.windows[2] <= 90.0
    assert ok == 50                              # 뒤 두 배치만 요약됨


def test_A10_28_items_first_batch_timeout_second_batch_succeeds(clock):
    """오늘 장애의 직접 회귀: [25, 3] 에서 첫 배치가 죽어도 3건은 살아야 한다."""
    posts = [_bill(i) for i in range(28)]

    def behave(ids, n):
        if n == 1:
            clock.advance(90.0)
            return requests.ReadTimeout("read timed out")
        return None

    ok, calls = _run_batches(posts, behave=behave)
    assert [len(c) for c in calls.calls] == [25, 3]   # 첫 배치 호출은 1회뿐
    assert 89.0 <= calls.windows[1] <= 90.0
    assert ok == 3
    assert all(p.summary == [] for p in posts[:25])   # 첫 25건만 발췌
    assert all(p.summary for p in posts[25:])


def test_A11_four_batches_share_the_overall_envelope_fairly(clock):
    posts = [_bill(i, body="가" * 300) for i in range(40)]
    cfg = _batch_cfg(max_batch_chars=3300)           # 10건씩 4배치
    _, calls = _run_batches(posts, cfg)
    assert len(calls.calls) == 4
    assert 74.0 <= calls.windows[0] <= 75.0          # 300/4 = 75
    assert all(w <= 90.0 for w in calls.windows)


def test_A12_missing_retry_stays_inside_the_current_batch_window(clock):
    """누락 재요청은 이 배치의 window 를 나눠 쓸 뿐 새 window 를 만들지 않는다."""
    posts = [_bill(i) for i in range(30)]

    def behave(ids, n):
        if n == 1:
            clock.advance(30.0)
            return _reply_for(ids[:-1])              # 한 건 누락 → 재요청 유발
        return None

    _, calls = _run_batches(posts, behave=behave)
    assert len(calls.calls) == 3                      # 배치1 + 누락 재요청 + 배치2
    assert len(calls.calls[1]) == 1
    # 재요청은 배치1 마감까지 남은 시간만 받는다(새로 90초를 만들지 않는다)
    assert calls.windows[1] <= 60.5
    assert 89.0 <= calls.windows[2] <= 90.0           # 배치2 는 자기 몫을 온전히 받는다


def test_A13_content_split_stays_inside_the_current_batch_window(clock):
    posts = [_bill(i) for i in range(26)]

    def behave(ids, n):
        if n == 1:
            clock.advance(40.0)
            return _envelope("깨진 JSON {{{")         # _Splittable → 절반 분할
        return None

    _, calls = _run_batches(posts, behave=behave)
    assert [len(c) for c in calls.calls[:3]] == [25, 12, 13]
    assert calls.windows[1] <= 50.5                   # 남은 window 안에서만
    assert calls.windows[2] <= 50.5


def test_A14_overall_deadline_always_caps_batch_windows(clock):
    now = time.monotonic()
    # 남은 예산이 요청 타임아웃보다 짧으면 남은 예산이 상한이다
    assert _allocate_batch_deadline(now + 10.0, 1, 90.0) == pytest.approx(now + 10.0)
    assert _allocate_batch_deadline(now + 300.0, 3, 90.0) == pytest.approx(now + 90.0)
    assert _allocate_batch_deadline(now + 300.0, 4, 90.0) == pytest.approx(now + 75.0)
    # 이미 지난 마감은 그대로 돌려준다(호출자가 예산 소진으로 판단)
    assert _allocate_batch_deadline(now - 1.0, 3, 90.0) == now - 1.0
    # budget_sec=0(무제한)의 기존 의미는 유지된다
    assert _allocate_batch_deadline(None, 3, 90.0) is None


def test_A15_first_transient_timeout_still_probes_the_next_batch(clock):
    posts = [_bill(i) for i in range(50)]

    def behave(ids, n):
        if n == 1:
            clock.advance(90.0)
            return requests.ReadTimeout("read timed out")
        return None

    ok, calls = _run_batches(posts, behave=behave)
    assert len(calls.calls) == 2
    assert ok == 25


def test_A16_success_resets_the_consecutive_transient_breaker(clock):
    posts = [_bill(i) for i in range(75)]

    def behave(ids, n):
        if n in (1, 3):
            clock.advance(10.0)
            return requests.ReadTimeout("read timed out")
        return None

    ok, calls = _run_batches(posts, behave=behave)
    # 실패 → 성공(리셋) → 실패 이므로 연속 2회에 도달하지 않는다
    assert len(calls.calls) == 3
    assert ok == 25


def test_A17_two_consecutive_transient_failures_stop_remaining_batches(clock):
    posts = [_bill(i) for i in range(75)]

    def behave(ids, n):
        clock.advance(10.0)
        return requests.ReadTimeout("read timed out")

    ok, calls = _run_batches(posts, behave=behave)
    assert len(calls.calls) == 2         # 임계치 2 도달 → 세 번째 배치는 호출하지 않음
    assert ok == 0


# ==========================================================================
# T. 텔레메트리
# ==========================================================================
_SECRETS = ("test-key", "x-goog-api-key", "Authorization")


def _log_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_T1_pre_call_log_reports_input_size_and_window(clock, caplog):
    caplog.set_level(logging.INFO, logger="src.assembly_summary")
    _run_batches([_bill(i) for i in range(30)])
    pre = [r for r in caplog.records if r.getMessage().startswith("Assembly AI call —")]
    assert len(pre) == 2
    msg = pre[0].getMessage()
    for field in (
        "batch=1/2", "items=25", "body_chars=", "prompt_chars=",
        "min_bill_chars=", "avg_bill_chars=", "max_bill_chars=",
        "allocated_window_sec=", "overall_remaining_sec=",
    ):
        assert field in msg, msg


def test_T2_usage_metadata_is_logged_when_present(clock, caplog):
    caplog.set_level(logging.INFO, logger="src.assembly_summary")
    usage = {
        "promptTokenCount": 1234,
        "candidatesTokenCount": 567,
        "thoughtsTokenCount": 89,
        "totalTokenCount": 1890,
    }
    _run_batches([_bill(i) for i in range(3)], usage=usage, model_version="gemini-3.6-flash-001")
    ok = next(r.getMessage() for r in caplog.records if "Assembly AI call ok" in r.getMessage())
    assert "prompt_tokens=1234" in ok
    assert "candidate_tokens=567" in ok
    assert "thought_tokens=89" in ok
    assert "total_tokens=1890" in ok
    assert "model_version=gemini-3.6-flash-001" in ok


def test_T3_missing_usage_metadata_does_not_crash(clock, caplog):
    caplog.set_level(logging.INFO, logger="src.assembly_summary")
    ok_count, _ = _run_batches([_bill(i) for i in range(3)])
    assert ok_count == 3
    ok = next(r.getMessage() for r in caplog.records if "Assembly AI call ok" in r.getMessage())
    assert "prompt_tokens=None" in ok
    assert "total_tokens=None" in ok


def test_T4_failure_log_reports_elapsed_and_retry_decision(clock, caplog):
    caplog.set_level(logging.INFO, logger="src.assembly_summary")

    def behave(ids, n):
        clock.advance(90.0)
        return requests.ReadTimeout("read timed out")

    _run_batches([_bill(i) for i in range(3)], behave=behave)
    failed = next(
        r.getMessage() for r in caplog.records if "Assembly AI call failed" in r.getMessage()
    )
    assert "elapsed_sec=90.0" in failed
    assert "error_kind=transient" in failed
    assert "error_type=ReadTimeout" in failed
    assert "response_retry=false" in failed


def test_T5_logs_never_contain_secrets_prompt_or_body(clock, caplog):
    caplog.set_level(logging.INFO)
    body = "절대로 로그에 통째로 실리면 안 되는 제안이유 본문임. " * 5
    _run_batches([_bill(i, body=body) for i in range(3)])
    text = _log_text(caplog)
    assert text
    for secret in _SECRETS:
        assert secret not in text
    assert body[:40] not in text          # 본문 전문/조각이 실리지 않는다
    assert "[의안 목록]" not in text       # 프롬프트 전문도 실리지 않는다


# ==========================================================================
# PR #26 Codex follow-up — 소스별 cap 의 bool 거절
#
# bool 은 int 의 하위형이라 int(True) == 1 이다. `max_new_per_run: true` 오타가
# '상한 1건'으로 통하면, 신규가 여러 건인 날 1건만 발송되고 나머지 ID 는 기존
# overflow 규칙에 따라 seen 처리된다 — 설정 오타가 영구 알림 누락이 된다.
# ==========================================================================
def _src(value):
    from src.config import SourceConfig

    return SourceConfig(
        key=ASSEMBLY_SOURCE_KEY, name="n", type="t", list_url="u",
        extra={"max_new_per_run": value},
    )


@pytest.mark.parametrize("value", [True, False])
def test_C8_C9_boolean_source_cap_falls_back_to_the_global_cap(value):
    assert _source_new_cap(_src(value), 50) == 50


def test_C10_boolean_source_cap_logs_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="src.main")
    assert _source_new_cap(_src(True), 50) == 50
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("max_new_per_run" in m and ASSEMBLY_SOURCE_KEY in m for m in warnings)


@pytest.mark.parametrize("value,expected", [(75, 75), ("75", 75), (60, 60)])
def test_C11_valid_source_cap_compatibility_is_unchanged(value, expected):
    """숫자·숫자문자열의 기존 호환성을 이번 수정으로 좁히지 않는다."""
    assert _source_new_cap(_src(value), 50) == expected


# ==========================================================================
# PR #26 Codex follow-up — 보내지도 않은 thinking 옵션 때문에 400 을 재시도하지 않는다
#
# 요청된 수준(thinking_level)과 실제로 payload 에 들어간 수준(effective)이 다르면,
# allowlist 밖 모델의 진짜 400(스키마 오류 등)이 'thinking 교정' 분기로 들어가
# **완전히 같은 요청**을 한 번 더 보낸다(max_retries=0 에서도).
# ==========================================================================
def _schema_400():
    return _err(400, "INVALID_ARGUMENT", 'Unknown name "responseSchemaX".')


@pytest.mark.parametrize("model", [_ALIAS, _LITE, "gemini-9-ultra"])
def test_S14_genuine_400_on_unsupported_model_is_not_retried_as_thinking(model):
    s = Summarizer(_llm(model=model, max_retries=0, batch=_batch_cfg()))
    s.session = _Session([_schema_400(), _ok_resp()])
    with pytest.raises(Exception) as e:
        _assembly_generate(s, s.cfg.assembly_batch)

    assert len(s.session.sent) == 1                       # 동일 요청을 두 번 보내지 않는다
    assert "thinkingConfig" not in s.session.sent[0]["generationConfig"]
    assert classify_error(e.value) is LLMErrorKind.BAD_REQUEST   # 기존 terminal 분류 유지


def test_S15_supported_model_keeps_the_one_shot_thinking_correction():
    s = Summarizer(_llm(model=_MODEL_36, max_retries=0, batch=_batch_cfg()))
    s.session = _Session([_schema_400(), _ok_resp()])
    _assembly_generate(s, s.cfg.assembly_batch)           # 교정 재시도로 성공

    assert len(s.session.sent) == 2                       # max_retries=0 이어도 1회 교정
    assert s.session.sent[0]["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "minimal"
    }
    assert "thinkingConfig" not in s.session.sent[1]["generationConfig"]


def test_S16_gemini25_thinking_budget_correction_is_unchanged():
    s = Summarizer(_llm(model=_M25, max_retries=0, batch=_batch_cfg()))
    s.session = _Session([_schema_400(), _ok_resp()])
    _assembly_generate(s, s.cfg.assembly_batch)

    assert len(s.session.sent) == 2
    assert s.session.sent[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert "thinkingConfig" not in s.session.sent[1]["generationConfig"]


def test_S17_unsupported_model_is_not_cached_as_thinking_unsupported():
    """보낸 적 없는 옵션 때문에 모델을 '미지원'으로 기억하면 안 된다."""
    s = Summarizer(_llm(model=_ALIAS, max_retries=0, batch=_batch_cfg()))
    s.session = _Session([_schema_400(), _ok_resp()])
    with pytest.raises(Exception):
        _assembly_generate(s, s.cfg.assembly_batch)
    assert s._thinking_unsupported == set()


def test_S18_404_fallback_to_an_unsupported_model_sends_no_thinking_level():
    """지원 모델이 404 로 사라져도 대체 모델에 미지원 thinkingLevel 을 보내지 않는다."""
    s = Summarizer(_llm(model=_MODEL_36, fallback_models=[_LITE], max_retries=0,
                        batch=_batch_cfg()))
    s.session = _Session(
        [_err(404, "NOT_FOUND", "This model models/x is no longer available"), _ok_resp()]
    )
    _assembly_generate(s, s.cfg.assembly_batch)

    assert s.session.models == [_MODEL_36, _LITE]
    assert s.session.sent[0]["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "minimal"
    }
    assert "thinkingConfig" not in s.session.sent[1]["generationConfig"]
    assert s._thinking_unsupported == set()               # 404 는 thinking 문제가 아니다


# ==========================================================================
# PR #26 Codex follow-up #3 — 공유 LLM 시간예산 (llm.total_budget_sec)
#
# 일반(240)과 의안(300)은 연달아 실행되므로 단순 합 540초가 최악값이 된다. 의안
# 상세수집 360초까지 더하면 15분 monitor 주기를 통째로 먹고(워크플로는
# cancel-in-progress:false 라) 다음 실행이 큐에 쌓인다. 두 단계가 **같은 절대
# 마감**을 공유하게 해서 합계를 360초에서 자른다.
# ==========================================================================
def _general_post(i: int) -> Post:
    return Post(
        source_key="fsc_press", source_name="금융위 · 보도자료", post_id=f"g{i}",
        title="보도자료", url=f"https://www.fsc.go.kr/no010101/{i}",
        date="2026-08-01", body="가나다라마바사아자차카타파하 " * 10,
    )


class _StageSpy:
    """_generate 대역. 호출별 deadline 을 기록하고 clock 을 원하는 만큼 흘린다."""

    def __init__(self, clock, posts, spend=0.0):
        self.clock = clock
        self.posts = posts
        self.spend = spend
        self.deadlines: list[float | None] = []
        self.windows: list[float | None] = []

    def __call__(self, prompt, deadline=None, *, telemetry=None, **kw):
        self.deadlines.append(deadline)
        self.windows.append(None if deadline is None else deadline - time.monotonic())
        if "[의안 목록]" in prompt:
            # 의안 호출은 시간을 쓰지 않는다 — 이 spy 는 '일반 단계가 공유 예산을
            # 얼마나 먹었을 때 의안이 무엇을 받는가'만 재기 위한 것이다.
            return _reply_for(_ids_in(prompt, self.posts))
        if self.spend:
            self.clock.advance(self.spend)
        return _envelope('{"summary": ["첫째임", "둘째임", "셋째임"]}')


def _run_both_stages(clock, *, total, general_spend=0.0, general_posts=1, bills=3,
                     terminal=None):
    """일반 → 의안 두 단계를 한 번의 summarize_all 로 돌리고 spy 를 돌려준다."""
    posts = [_bill(i) for i in range(bills)]
    generals = [_general_post(i) for i in range(general_posts)]
    s = Summarizer(_llm(total_budget_sec=total, batch=_batch_cfg()))
    spy = _StageSpy(clock, posts, spend=general_spend)
    if terminal is not None:
        def _fail(prompt, deadline=None, *, telemetry=None, **kw):
            spy(prompt, deadline, telemetry=telemetry, **kw)
            raise LLMCallError("boom", kind=terminal, status=401)
        s._generate = _fail
    else:
        s._generate = spy
    ok = s.summarize_all({"금융위 · 보도자료": generals, _SOURCE: posts})
    return ok, spy


def test_BUD1_no_shared_budget_keeps_per_stage_semantics(clock):
    """total_budget_sec=0 이면 공유 마감이 없다 — 각 단계 예산만 적용된다."""
    _, spy = _run_both_stages(clock, total=0.0)
    assert len(spy.windows) == 2                    # 일반 1건 + 의안 1배치
    assert 239.0 <= spy.windows[0] <= 240.0         # 일반은 자기 240초
    assert 89.0 <= spy.windows[1] <= 90.0           # 의안은 자기 배치 window


def test_BUD2_general_only_still_gets_its_full_stage_budget(clock):
    _, spy = _run_both_stages(clock, total=360.0)
    assert 239.0 <= spy.windows[0] <= 240.0         # min(240, 360) = 240


def test_BUD3_assembly_only_still_gets_its_full_stage_budget(clock):
    """일반이 없으면 의안은 공유 예산 안에서 자기 300초 상한을 그대로 쓴다."""
    posts = [_bill(i) for i in range(75)]           # 3 batches
    s = Summarizer(_llm(total_budget_sec=360.0, batch=_batch_cfg()))
    calls = _Calls(posts)
    s._generate = calls
    s.summarize_all({_SOURCE: posts})
    # 300/3=100 이지만 요청 타임아웃 90 이 상한 — 기존 배분 그대로다
    assert [round(w) for w in calls.windows] == [90, 90, 90]


def test_BUD4_slow_general_shrinks_the_assembly_envelope(clock):
    """일반이 200초를 쓰면 의안에는 약 160초만 남는다(새 300초를 만들지 않는다)."""
    _, spy = _run_both_stages(clock, total=360.0, general_spend=200.0)
    assembly_window = spy.windows[1]
    assert assembly_window < 300.0
    assert 89.0 <= assembly_window <= 90.0          # 1배치라 요청 타임아웃이 상한
    # 의안 마감이 공유 마감을 넘지 않는다
    assert spy.deadlines[1] <= spy.deadlines[0] + 240.0


def test_BUD5_general_using_its_full_budget_leaves_the_remainder(clock):
    posts = [_bill(i) for i in range(75)]           # 3 batches
    generals = [_general_post(0)]
    s = Summarizer(_llm(total_budget_sec=360.0, batch=_batch_cfg()))
    spy = _StageSpy(clock, posts, spend=240.0)
    s._generate = spy
    s.summarize_all({"금융위 · 보도자료": generals, _SOURCE: posts})
    # 공유 잔여 ≈120초 안에서 3배치가 공정 분배된다 — 새 300초가 생기지 않는다.
    # (앞 배치가 자기 몫을 다 쓰지 않으면 남은 몫은 뒤 배치로 넘어가므로 뒤 window
    #  가 더 클 수 있다. 중요한 것은 첫 몫과 '공유 잔여를 넘지 않는다'는 상한이다.)
    assembly_windows = spy.windows[1:]
    assert len(assembly_windows) == 3
    assert assembly_windows[0] == pytest.approx(40.0)      # 120 / 3
    assert all(w <= 120.0 for w in assembly_windows), assembly_windows


def test_BUD6_fast_general_leaves_the_assembly_stage_cap_intact(clock):
    posts = [_bill(i) for i in range(75)]
    generals = [_general_post(0)]
    s = Summarizer(_llm(total_budget_sec=360.0, batch=_batch_cfg()))
    spy = _StageSpy(clock, posts, spend=30.0)
    s._generate = spy
    s.summarize_all({"금융위 · 보도자료": generals, _SOURCE: posts})
    # 공유 잔여 ≈330 > 의안 상한 300 이므로 기존 배분(90/90/90) 그대로
    assert [round(w) for w in spy.windows[1:]] == [90, 90, 90]


def test_BUD7_exhausted_shared_budget_skips_the_assembly_stage(clock, caplog):
    """공유 예산이 한 번의 호출도 담지 못하면 의안 API 를 부르지 않는다."""
    caplog.set_level(logging.WARNING, logger="src.summarizer")
    _, spy = _run_both_stages(clock, total=360.0, general_spend=359.0)
    assert len(spy.windows) == 1                    # 일반 1회뿐 — 의안 호출 없음
    assert any("공유 LLM 시간예산" in r.getMessage() for r in caplog.records)


def test_BUD8_terminal_general_failure_still_skips_assembly(clock):
    """Phase 1 계약 — 종료성 실패면 공유 예산과 무관하게 의안을 건너뛴다."""
    _, spy = _run_both_stages(clock, total=360.0, terminal=LLMErrorKind.AUTH)
    assert len(spy.windows) == 1


@pytest.mark.parametrize("value", [True, False])
def test_BUD9_boolean_total_budget_is_rejected(tmp_path, value):
    path = _yaml_config(tmp_path, lambda raw: raw["llm"].__setitem__("total_budget_sec", value))
    with pytest.raises(ValueError) as e:
        load_config(path)
    assert "total_budget_sec" in str(e.value)


def test_BUD_production_config_declares_the_shared_envelope():
    llm = load_config("config.yaml").llm
    assert llm.total_budget_sec == 360.0
    assert llm.budget_sec == 240.0                  # 단계 ceiling 은 그대로
    assert llm.assembly_batch.budget_sec == 300.0


# ==========================================================================
# PR #26 Codex follow-up #3 — 호출 가능한 window 를 보장하고 진행한다
#
# fair share 가 _MIN_CALL_SEC 미만이면 첫 배치는 호출 없이 버려지는데, 배치가
# 줄어 몫이 커진 뒤 배치는 실제로 호출됐다 — 앞 배치의 시간을 뒤가 가져간 셈이다.
# ==========================================================================
def _run_with_budget(budget, bills=75, **over):
    return _run_batches([_bill(i) for i in range(bills)], _batch_cfg(budget_sec=budget, **over))


def test_FAIR1_unusable_fair_share_stops_every_remaining_batch(clock, caplog):
    caplog.set_level(logging.WARNING, logger="src.assembly_summary")
    ok, calls = _run_with_budget(5.0)               # 5/3 ≈ 1.67 < 2.0
    assert calls.calls == []                        # 어떤 배치도 호출되지 않는다
    assert ok == 0
    assert any("호출 없이 발췌" in r.getMessage() for r in caplog.records)


def test_FAIR2_fair_share_exactly_at_the_threshold_is_callable(clock):
    _, calls = _run_with_budget(6.0)                # 6/3 = 2.0 == _MIN_CALL_SEC
    assert len(calls.calls) == 3
    assert calls.windows[0] == pytest.approx(2.0)


def test_FAIR3_two_batches_at_the_threshold_are_callable(clock):
    _, calls = _run_with_budget(4.0, bills=50)      # 4/2 = 2.0
    assert len(calls.calls) == 2


def test_FAIR4_just_below_the_threshold_stops_both_batches(clock):
    _, calls = _run_with_budget(3.9, bills=50)      # 3.9/2 = 1.95 < 2.0
    assert calls.calls == []


def test_FAIR5_many_char_split_batches_stop_together(clock):
    """max_batch_chars 로 배치가 늘어난 경우에도 첫 배치부터 전체를 중단한다."""
    posts = [_bill(i, body="가" * 300) for i in range(40)]
    ok, calls = _run_batches(posts, _batch_cfg(budget_sec=15.0, max_batch_chars=1300))
    assert len(calls.calls) == 0                    # 15/10 = 1.5 < 2.0
    assert ok == 0


def test_FAIR6_unlimited_budget_is_not_affected_by_the_guard(clock):
    _, calls = _run_with_budget(0.0)                # budget_sec=0 → 무제한
    assert len(calls.calls) == 3
    assert all(w is None for w in calls.windows)


def test_FAIR7_production_allocation_regression_is_unchanged(clock):
    _, calls = _run_with_budget(300.0)
    assert [round(w) for w in calls.windows] == [90, 90, 90]


def test_FAIR8_shared_envelope_shrinks_windows_but_keeps_them_callable(clock):
    """공유 마감으로 의안 잔여가 120초여도 3배치가 각각 호출 가능한 몫을 받는다."""
    posts = [_bill(i) for i in range(75)]
    s = Summarizer(_llm(batch=_batch_cfg()))
    calls = _Calls(posts)
    s._generate = calls
    outer = time.monotonic() + 120.0
    summarize_assembly_bills(s, {_SOURCE: posts}, outer)
    assert len(calls.calls) == 3
    assert all(w >= _MIN_CALL_SEC for w in calls.windows)
    assert calls.windows[0] == pytest.approx(40.0)         # 120 / 3
    # 어떤 배치도 공유 잔여(120초)를 넘겨 받지 않는다
    assert all(w <= 120.0 for w in calls.windows), calls.windows


# ==========================================================================
# PR #26 Codex follow-up #5 — 소스별 cap 의 분수·비유한 값 거절
#
# int() 는 실수를 절단하므로 `max_new_per_run: 1.9` 가 '상한 1건'이 된다. 초과분은
# 기존 overflow 규칙에 따라 seen 으로 확정되므로 오타 하나가 영구 알림 누락이 된다.
# int(float("inf")) 는 OverflowError 인데 기존 핸들러가 잡지 않아 실행이 죽었다.
# ==========================================================================
@pytest.mark.parametrize("value", [1.9, 75.5, 0.5, -75.5])
def test_C24_fractional_source_cap_falls_back_to_the_global_cap(value):
    assert _source_new_cap(_src(value), 50) == 50


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_C25_non_finite_source_cap_falls_back_without_raising(value):
    assert _source_new_cap(_src(value), 50) == 50


def test_C26_integral_float_source_cap_stays_compatible():
    """75.0 처럼 값이 실제로 정수인 실수는 기존처럼 받아들인다(절단이 아니다)."""
    assert _source_new_cap(_src(75.0), 50) == 75
    assert _source_new_cap(_src(60.0), 50) == 60


def test_C27_string_parsing_is_not_broadened():
    """숫자 문자열 호환은 유지하되 "75.0" 을 새로 받아들이지는 않는다."""
    assert _source_new_cap(_src("75"), 50) == 75
    assert _source_new_cap(_src("75.0"), 50) == 50


def test_C28_invalid_fractional_cap_logs_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="src.main")
    assert _source_new_cap(_src(1.9), 50) == 50
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("max_new_per_run" in m for m in warnings)


# ==========================================================================
# PR #26 Codex follow-up #5 — 유효 window 로 판정한 뒤에만 배치로 진입한다
#
# 판정(_has_callable_window)은 공정 몫만 보고, 실제 window 는 요청 타임아웃까지
# 적용됐다. request_timeout_sec 이 _MIN_CALL_SEC 보다 작으면 바깥 루프가 모든
# 배치를 차례로 통과시키면서 정작 어느 배치도 호출하지 못했다.
#
# **API 호출 0회만 확인하는 테스트로는 부족하다** — 버그 코드도 0회였다(_run 에
# 들어간 뒤 내부에서 스킵). 그래서 _run 진입 자체를 관찰한다.
# ==========================================================================
@pytest.fixture
def run_spy(monkeypatch):
    """_run 이 몇 번째 배치까지 실제로 진입했는지 기록한다."""
    seen: list[int] = []
    original = asm._run

    def _spy(summarizer, items, deadline, cfg, ctx=None, **kw):
        seen.append(len(items))
        return original(summarizer, items, deadline, cfg, ctx, **kw)

    monkeypatch.setattr(asm, "_run", _spy)
    return seen


def _batch_log_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Assembly AI batch —")
    ]


def test_EFF1_request_timeout_below_min_call_stops_before_entering_any_batch(
    clock, caplog, run_spy
):
    caplog.set_level(logging.INFO, logger="src.assembly_summary")
    ok, calls = _run_batches(
        [_bill(i) for i in range(75)],
        _batch_cfg(budget_sec=300.0, request_timeout_sec=1.0),
    )
    assert run_spy == []                     # 첫 배치조차 _run 에 들어가지 않는다
    assert _batch_log_lines(caplog) == []    # per-batch 로그도 남지 않는다
    assert calls.calls == []                 # Gemini 호출 0회
    assert ok == 0
    stops = [r.getMessage() for r in caplog.records if "호출 없이 발췌로" in r.getMessage()]
    assert len(stops) == 1                   # 한 번의 중단 결정으로 끝난다
    assert "요청 타임아웃" in stops[0]        # 원인을 로그만으로 구분할 수 있다


def test_EFF2_request_timeout_exactly_at_min_call_is_callable(clock, run_spy):
    ok, calls = _run_batches(
        [_bill(i) for i in range(75)],
        _batch_cfg(budget_sec=300.0, request_timeout_sec=_MIN_CALL_SEC),
    )
    assert len(run_spy) == 3                 # 경계는 포함이다
    assert len(calls.calls) == 3
    assert calls.windows == [pytest.approx(_MIN_CALL_SEC)] * 3
    assert ok == 75


def test_EFF3_fair_share_below_min_call_keeps_the_followup3_behavior(clock, run_spy):
    """요청 타임아웃이 넉넉해도 공정 몫이 모자라면 기존대로 전체를 멈춘다."""
    _run_batches([_bill(i) for i in range(75)], _batch_cfg(budget_sec=5.0))
    assert run_spy == []


def test_EFF4_fair_share_exactly_at_min_call_is_callable(clock, run_spy):
    _, calls = _run_batches([_bill(i) for i in range(75)], _batch_cfg(budget_sec=6.0))
    assert len(run_spy) == 3
    assert calls.windows[0] == pytest.approx(_MIN_CALL_SEC)


def test_EFF5_production_allocation_is_unchanged(clock, run_spy):
    _, calls = _run_batches([_bill(i) for i in range(75)], _batch_cfg())
    assert len(run_spy) == 3
    assert [round(w) for w in calls.windows] == [90, 90, 90]


def test_EFF6_unlimited_budget_keeps_request_timeout_as_request_level_only(
    clock, run_spy
):
    """budget_sec=0 은 '전체 마감 없음'이다 — 요청 타임아웃이 이를 대신하지 않는다."""
    _, calls = _run_batches(
        [_bill(i) for i in range(75)],
        _batch_cfg(budget_sec=0.0, request_timeout_sec=1.0),
    )
    assert len(run_spy) == 3                 # 전체 마감이 없으므로 판정하지 않는다
    assert len(calls.calls) == 3
    assert all(w is None for w in calls.windows)
    # 요청 타임아웃 자체는 호출별 제약으로 그대로 전달된다
    assert all(kw["request_timeout_sec"] == 1.0 for kw in calls.kwargs)


def test_EFF7_no_window_is_not_reported_as_a_call_failure(clock):
    """호출 window 부족은 HTTP/서비스 실패가 아니다 — 브레이커 분류를 오염시키지 않는다."""
    ok, calls = _run_batches(
        [_bill(i) for i in range(75)],
        _batch_cfg(budget_sec=300.0, request_timeout_sec=1.0),
    )
    assert ok == 0 and calls.calls == []
    # 예외가 새지 않고, 요약 없이 발췌로 넘어간 것뿐이다
    assert all(p.summary == [] for p in calls.posts)


def test_EFF8_effective_window_helper_uses_one_calculation():
    """판정과 배분이 같은 식을 쓰는지 직접 확인한다(두 계산이 다시 어긋나지 않도록)."""
    now = 1000.0
    deadline = now + 300.0
    for timeout in (1.0, 2.0, 90.0, 0.0):
        window = asm._effective_window_sec(deadline, 3, timeout, now)
        allocated = asm._allocate_batch_deadline(deadline, 3, timeout, now)
        assert allocated - now == pytest.approx(window)
        assert asm._has_callable_window(deadline, 3, timeout, now) is (
            window >= _MIN_CALL_SEC
        )
    # 전체 마감이 없으면 판정 자체를 하지 않는다
    assert asm._effective_window_sec(None, 3, 1.0, now) is None
    assert asm._has_callable_window(None, 3, 1.0, now) is True
