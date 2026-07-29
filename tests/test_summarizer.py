"""LLM(Gemini) 본문 요약 및 요약 기반 메일 렌더 테스트.

네트워크 호출은 하지 않는다 — Summarizer._generate 를 가짜 응답으로 대체한다.
"""
import os
import sys
import time
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import EmailConfig, LLMConfig, load_config
from src.models import Post
from src.notifier import build_html, build_text, missing_email_settings, send_digest
from src.summarizer import Summarizer, SummaryUnavailable, summarize_posts


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


def test_cap_is_spread_across_sources_not_config_order():
    # 앞쪽 소스가 상한을 다 먹으면 뒤쪽 소스는 글이 더 최신이어도 전부 원문 발췌로
    # 나간다. 상한은 소스별로 고르게 배분되어야 한다.
    first = [
        _post(source_key="fsc_press", source_name="금융위 · 보도자료",
              post_id=f"a{i}", url=f"https://example.com/a{i}")
        for i in range(10)
    ]
    later = [
        _post(source_key="assembly_bill", source_name="의안정보시스템 · 계류의안",
              post_id=f"b{i}", url=f"https://example.com/b{i}")
        for i in range(3)
    ]
    s = Summarizer(_cfg(max_posts=6))
    s._generate = lambda prompt, deadline=None: _envelope('{"summary": ["요약함"]}')

    s.summarize_all({
        "금융위 · 보도자료": first,          # config 앞쪽 소스
        "의안정보시스템 · 계류의안": later,   # 뒤쪽 소스
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
