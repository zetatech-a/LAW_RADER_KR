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
    _MAX_OUTPUT_TOKENS_THINKING,
    _SCHEMA,
    LLMErrorKind,
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
        self.sent.append(json)
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
