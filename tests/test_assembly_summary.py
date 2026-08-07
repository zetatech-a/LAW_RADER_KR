"""의안 전용 Gemini 배치 요약 테스트.

네트워크를 쓰지 않는다 — Summarizer._generate 또는 세션을 가짜 응답으로 대체한다.
"""
import json
import os
import re
import sys
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.assembly_summary import BATCH_SCHEMA, summarize_assembly_bills
from src.config import AssemblyBatchConfig, LLMConfig, load_config
from src.models import Post
from src.notifier import build_html, build_text
from src.summarizer import _SCHEMA, Summarizer, summarize_posts

_SOURCE = "의안정보시스템 · 계류의안"
_REASON = "현행법은 가상자산 이용자 예치금 보호 의무를 명확히 규정하지 아니함. " * 3


def _cfg(batch=None, **over) -> LLMConfig:
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
        assembly_batch=batch or AssemblyBatchConfig(),
    )
    base.update(over)
    return LLMConfig(**base)


def _bill(i: int, body: str = _REASON) -> Post:
    return Post(
        source_key="assembly_bill",
        source_name=_SOURCE,
        post_id=f"PRC_{i:04d}",
        title=f"제{i}호 일부개정법률안 (홍길동)",
        url=f"https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_{i:04d}",
        date="2026-07-01",
        body=body,
    )


def _general(i: int) -> Post:
    return Post(
        source_key="fsc_press",
        source_name="금융위 · 보도자료",
        post_id=f"g{i}",
        title="금융위 보도자료",
        url=f"https://www.fsc.go.kr/no010101/{i}",
        date="2026-07-01",
        body="가나다라마바사아자차카타파하 " * 10,
    )


def _envelope(text: str, finish: str = "STOP") -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]}


def _lines(bill_id: str) -> list[str]:
    return [f"{bill_id} 첫째 문장임", f"{bill_id} 둘째 문장임", f"{bill_id} 셋째 문장임"]


def _reply(bill_ids) -> dict:
    return _envelope(
        json.dumps(
            {"summaries": [{"bill_id": b, "summary": _lines(b)} for b in bill_ids]},
            ensure_ascii=False,
        )
    )


def _ids_in(prompt: str, posts) -> list[str]:
    """프롬프트에 실제로 담긴 bill_id 를 순서대로."""
    return [p.post_id for p in posts if f"bill_id: {p.post_id}\n" in prompt]


class _Recorder:
    """_generate 를 대신해 호출을 기록하고, 요청된 ID 를 그대로 돌려주는 스텁."""

    def __init__(self, posts, reply=None):
        self.posts = posts
        self.calls = []          # 호출별 요청 ID 목록
        self.kwargs = []
        self._reply = reply

    def __call__(self, prompt, deadline=None, *, schema=None, max_output_tokens=None):
        ids = _ids_in(prompt, self.posts)
        self.calls.append(ids)
        self.kwargs.append({"schema": schema, "max_output_tokens": max_output_tokens})
        if self._reply is not None:
            return self._reply(ids, len(self.calls))
        return _reply(ids)


def _run(posts, cfg=None, reply=None):
    s = Summarizer(cfg or _cfg())
    rec = _Recorder(posts, reply)
    s._generate = rec
    ok = s.summarize_all({_SOURCE: posts})
    return ok, rec


# --- 설정 ---


def test_config_exposes_assembly_batch_defaults():
    cfg = load_config("config.yaml").llm.assembly_batch
    assert cfg.enabled is True
    assert cfg.batch_size == 25
    assert cfg.max_bills == 50
    assert cfg.max_input_chars_per_bill == 20000
    assert cfg.max_batch_chars == 250000
    assert cfg.max_output_tokens == 16384
    assert cfg.budget_sec == 120
    assert cfg.retry_missing_once is True


def test_general_llm_settings_are_untouched():
    # 의안 설정을 더해도 일반 경로 설정값은 그대로여야 한다.
    llm = load_config("config.yaml").llm
    assert llm.max_posts == 40
    assert llm.budget_sec == 240
    assert llm.max_consecutive_failures == 3
    assert llm.max_retries == 2
    assert llm.rpm == 10


def test_disabled_batch_makes_no_call():
    posts = [_bill(1)]
    ok, rec = _run(posts, _cfg(batch=AssemblyBatchConfig(enabled=False)))
    assert ok == 0
    assert rec.calls == []
    assert posts[0].summary == []


# --- 배치 크기: 25건=1회, 37건=2회, 50건=2회 ---


@pytest.mark.parametrize("count,expected_calls", [(25, 1), (37, 2), (50, 2)])
def test_batch_call_counts(count, expected_calls):
    posts = [_bill(i) for i in range(count)]
    ok, rec = _run(posts)
    assert len(rec.calls) == expected_calls
    assert ok == count
    assert all(p.summary == _lines(p.post_id) for p in posts)
    # 배치마다 batch_size 를 넘지 않는다
    assert all(len(c) <= 25 for c in rec.calls)
    # 모든 의안이 정확히 한 번씩 요청된다
    assert sorted(i for c in rec.calls for i in c) == sorted(p.post_id for p in posts)


def test_batch_uses_batch_schema_and_output_tokens():
    ok, rec = _run([_bill(1)])
    assert ok == 1
    assert rec.kwargs[0]["schema"] == BATCH_SCHEMA
    assert rec.kwargs[0]["max_output_tokens"] == 16384
    assert BATCH_SCHEMA != _SCHEMA


def test_max_bills_caps_targets():
    posts = [_bill(i) for i in range(60)]
    cfg = _cfg(batch=AssemblyBatchConfig(max_bills=50))
    ok, rec = _run(posts, cfg)
    assert ok == 50
    assert len(rec.calls) == 2
    assert [bool(p.summary) for p in posts[50:]] == [False] * 10


def test_max_batch_chars_splits_before_batch_size():
    # 건수 상한에 못 미쳐도 입력 총량 상한이 먼저 걸리면 배치를 나눈다.
    posts = [_bill(i, body="가" * 1000) for i in range(10)]
    cfg = _cfg(batch=AssemblyBatchConfig(batch_size=25, max_batch_chars=3000))
    ok, rec = _run(posts, cfg)
    assert ok == 10
    assert len(rec.calls) > 1
    assert all(len(c) <= 3 for c in rec.calls)


def test_oversized_single_bill_still_gets_its_own_batch():
    posts = [_bill(0, body="가" * 5000), _bill(1)]
    cfg = _cfg(batch=AssemblyBatchConfig(max_batch_chars=1000))
    ok, rec = _run(posts, cfg)
    assert ok == 2
    assert [len(c) for c in rec.calls] == [1, 1]


def test_per_bill_input_is_truncated():
    posts = [_bill(0, body="가" * 5000)]
    cfg = _cfg(batch=AssemblyBatchConfig(max_input_chars_per_bill=100))
    s = Summarizer(cfg)
    captured = {}

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        captured["prompt"] = prompt
        return _reply([posts[0].post_id])

    s._generate = _generate
    s.summarize_all({_SOURCE: posts})
    assert "가" * 100 in captured["prompt"]
    assert "가" * 101 not in captured["prompt"]


def test_bills_without_body_are_not_requested():
    posts = [_bill(0), _bill(1, body=""), _bill(2, body="   ")]
    ok, rec = _run(posts)
    assert ok == 1
    assert rec.calls == [["PRC_0000"]]


# --- BILL_ID 매핑 ---


def test_results_map_by_bill_id_not_array_order():
    posts = [_bill(i) for i in range(3)]
    ids = [p.post_id for p in posts]

    def _reversed(requested, n):
        return _reply(list(reversed(requested)))

    ok, rec = _run(posts, reply=_reversed)
    assert ok == 3
    # 순서를 뒤집어 보내도 각 의안이 '자기' 요약을 받는다
    for p in posts:
        assert p.summary == _lines(p.post_id)
    assert ids == [p.post_id for p in posts]


def test_unknown_bill_id_is_rejected():
    posts = [_bill(0), _bill(1)]

    def _with_unknown(requested, n):
        return _reply([requested[0], "PRC_DOES_NOT_EXIST"])

    cfg = _cfg(batch=AssemblyBatchConfig(retry_missing_once=False))
    ok, rec = _run(posts, cfg, reply=_with_unknown)
    assert ok == 1
    assert posts[0].summary == _lines("PRC_0000")
    assert posts[1].summary == []          # 지어낸 ID 를 대신 싣지 않는다


def test_duplicate_bill_id_drops_that_bill_entirely():
    posts = [_bill(0), _bill(1)]

    def _dupe(requested, n):
        # PRC_0000 에 대해 서로 다른 요약 둘 → 어느 쪽이 맞는지 알 수 없다
        return _envelope(
            json.dumps(
                {
                    "summaries": [
                        {"bill_id": "PRC_0000", "summary": _lines("A")},
                        {"bill_id": "PRC_0001", "summary": _lines("PRC_0001")},
                        {"bill_id": "PRC_0000", "summary": _lines("B")},
                    ]
                },
                ensure_ascii=False,
            )
        )

    cfg = _cfg(batch=AssemblyBatchConfig(retry_missing_once=False))
    ok, _ = _run(posts, cfg, reply=_dupe)
    assert ok == 1
    assert posts[0].summary == []                     # 통째로 버린다
    assert posts[1].summary == _lines("PRC_0001")


def test_duplicate_is_rejected_even_when_the_first_row_was_malformed():
    """첫 행이 형식 위반이어도 '등장했다'는 사실은 남아야 한다.

    회귀: 예전에는 형식 위반 행을 등장 기록 없이 버려서, 뒤따르는 같은 ID 의 멀쩡해
    보이는 행이 그대로 실렸다. 그 행의 내용이 실은 다른(누락된) 의안의 것일 수 있어
    잘못된 요약이 알림에 붙는다. 중복은 어느 쪽이 맞는지 알 수 없으므로 통째로 버린다.
    """
    posts = [_bill(0), _bill(1)]

    def _bad_then_good(requested, n):
        return _envelope(
            json.dumps(
                {
                    "summaries": [
                        # 첫 행: 같은 ID 인데 3줄이 아니라 형식 위반
                        {"bill_id": "PRC_0000", "summary": ["한 줄뿐임"]},
                        {"bill_id": "PRC_0001", "summary": _lines("PRC_0001")},
                        # 둘째 행: 형식은 멀쩡하지만 중복이므로 믿을 수 없다
                        {"bill_id": "PRC_0000", "summary": _lines("B")},
                    ]
                },
                ensure_ascii=False,
            )
        )

    cfg = _cfg(batch=AssemblyBatchConfig(retry_missing_once=False))
    ok, _ = _run(posts, cfg, reply=_bad_then_good)
    assert ok == 1
    assert posts[0].summary == []                     # 뒤 행을 주워 담지 않는다
    assert posts[1].summary == _lines("PRC_0001")


@pytest.mark.parametrize(
    "bad",
    [
        {"bill_id": "PRC_0000", "summary": ["한 줄뿐임"]},                    # 3줄 아님
        {"bill_id": "PRC_0000", "summary": ["a", "b", "c", "d"]},            # 3줄 아님
        {"bill_id": "PRC_0000", "summary": ["a", 2, "c"]},                   # 비문자열
        {"bill_id": "PRC_0000", "summary": ["a", "   ", "c"]},               # 빈 문장
        {"bill_id": "PRC_0000", "summary": ["a", "-  ", "c"]},               # 표식만 남음
        {"bill_id": "PRC_0000", "summary": "문자열임"},                       # 배열 아님
        {"bill_id": "PRC_0000", "summary": {"a": "b"}},                      # dict
        {"bill_id": 123, "summary": ["a", "b", "c"]},                        # 비문자열 ID
        {"summary": ["a", "b", "c"]},                                        # ID 없음
        ["PRC_0000", ["a", "b", "c"]],                                       # 객체 아님
    ],
)
def test_malformed_entries_are_rejected(bad):
    posts = [_bill(0)]
    cfg = _cfg(batch=AssemblyBatchConfig(retry_missing_once=False))
    ok, _ = _run(
        posts, cfg, reply=lambda req, n: _envelope(json.dumps({"summaries": [bad]}))
    )
    assert ok == 0
    assert posts[0].summary == []      # 발췌 폴백


# --- 문장 길이(max_line_chars) 검증 ---


def test_line_over_90_chars_is_rejected():
    # 프롬프트로 '90자 이내'를 지시해도 지켜지지 않을 수 있다. 응답에서 다시 검사한다.
    posts = [_bill(0)]
    long_line = "가" * 91
    cfg = _cfg(batch=AssemblyBatchConfig(retry_missing_once=False))
    ok, _ = _run(
        posts,
        cfg,
        reply=lambda req, n: _envelope(
            json.dumps(
                {"summaries": [{"bill_id": "PRC_0000",
                                "summary": ["정상 문장임", long_line, "정상 문장임"]}]},
                ensure_ascii=False,
            )
        ),
    )
    assert ok == 0
    assert posts[0].summary == []      # 긴 줄 하나 때문에 그 의안 전체를 버린다


def test_line_exactly_at_limit_is_accepted():
    posts = [_bill(0)]
    exact = "가" * 90
    ok, _ = _run(
        posts,
        reply=lambda req, n: _envelope(
            json.dumps(
                {"summaries": [{"bill_id": "PRC_0000", "summary": [exact] * 3}]},
                ensure_ascii=False,
            )
        ),
    )
    assert ok == 1
    assert posts[0].summary == [exact] * 3


def test_over_long_line_becomes_missing_and_is_re_requested_once():
    # 길이 위반은 '누락'과 같은 취급 — 해당 ID 만 한 번 다시 요청한다.
    posts = [_bill(0), _bill(1)]

    def _long_then_ok(requested, n):
        if n == 1:
            return _envelope(
                json.dumps(
                    {
                        "summaries": [
                            {"bill_id": "PRC_0000", "summary": ["가" * 200] * 3},
                            {"bill_id": "PRC_0001", "summary": _lines("PRC_0001")},
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        return _reply(requested)

    ok, rec = _run(posts, reply=_long_then_ok)
    assert ok == 2
    assert rec.calls[1] == ["PRC_0000"]          # 길이를 어긴 의안만 재요청
    assert posts[0].summary == _lines("PRC_0000")


@pytest.mark.parametrize(
    "max_line_chars,length,expect_ok",
    [
        (30, 31, 0),     # 설정을 줄이면 더 짧은 줄도 거부된다
        (30, 30, 1),
        (200, 150, 1),   # 설정을 늘리면 90자 초과도 통과한다
        (200, 201, 0),
        (0, 500, 1),     # 0 = 길이 제한 없음
    ],
)
def test_line_length_limit_follows_config(max_line_chars, length, expect_ok):
    posts = [_bill(0)]
    cfg = _cfg(
        max_line_chars=max_line_chars,
        batch=AssemblyBatchConfig(retry_missing_once=False),
    )
    ok, _ = _run(
        posts,
        cfg,
        reply=lambda req, n: _envelope(
            json.dumps(
                {"summaries": [{"bill_id": "PRC_0000", "summary": ["가" * length] * 3}]},
                ensure_ascii=False,
            )
        ),
    )
    assert ok == expect_ok


def test_length_is_measured_after_cleanup():
    # 공백 정리·목록표식 제거를 마친 '실제 발행될' 문자열로 재야 한다.
    # 아래는 원문 96자지만 정리 후 90자라 통과해야 한다.
    posts = [_bill(0)]
    raw = "- " + "가" * 45 + "    " + "나" * 45
    assert len(raw) > 90 and len(" ".join(raw[2:].split())) == 91
    cleaned_ok = "- " + "가" * 45 + "    " + "나" * 44   # 정리 후 90자
    ok, _ = _run(
        posts,
        reply=lambda req, n: _envelope(
            json.dumps(
                {"summaries": [{"bill_id": "PRC_0000", "summary": [cleaned_ok] * 3}]},
                ensure_ascii=False,
            )
        ),
    )
    assert ok == 1
    assert all(len(s) == 90 for s in posts[0].summary)


def test_general_single_path_line_length_is_unchanged():
    # 요구 3은 의안 배치 검증에만 적용한다. 일반 단건 요약의 기존 동작(길이를
    # 프롬프트로만 지시하고 응답은 그대로 수용)을 바꾸지 않는다.
    p = _general(0)
    s = Summarizer(_cfg())
    long_line = "가" * 300
    s._generate = lambda prompt, deadline=None: _envelope(
        json.dumps({"summary": [long_line]}, ensure_ascii=False)
    )
    assert s.summarize(p) == [long_line]


def test_list_markers_are_stripped_from_valid_lines():
    posts = [_bill(0)]
    ok, _ = _run(
        posts,
        reply=lambda req, n: _envelope(
            json.dumps(
                {"summaries": [{"bill_id": "PRC_0000",
                                "summary": ["- 첫째임", "• 둘째임", "3. 셋째임"]}]},
                ensure_ascii=False,
            )
        ),
    )
    assert ok == 1
    assert posts[0].summary == ["첫째임", "둘째임", "셋째임"]


# --- 누락 재요청 ---


def test_missing_ids_are_re_requested_once():
    posts = [_bill(i) for i in range(4)]

    def _partial(requested, n):
        if n == 1:
            return _reply(requested[:2])   # 2건 누락
        return _reply(requested)           # 재요청에서 채워짐

    ok, rec = _run(posts, reply=_partial)
    assert ok == 4
    assert len(rec.calls) == 2
    assert rec.calls[1] == ["PRC_0002", "PRC_0003"]   # 누락된 ID 만
    assert all(p.summary == _lines(p.post_id) for p in posts)


def test_missing_retry_happens_at_most_once():
    posts = [_bill(i) for i in range(4)]

    def _always_partial(requested, n):
        return _reply(requested[:1])

    ok, rec = _run(posts, reply=_always_partial)
    assert len(rec.calls) == 2          # 최초 + 재요청 1회로 끝(무한 루프 없음)
    assert ok == 2


def test_retry_missing_can_be_disabled():
    posts = [_bill(i) for i in range(4)]
    cfg = _cfg(batch=AssemblyBatchConfig(retry_missing_once=False))
    ok, rec = _run(posts, cfg, reply=lambda req, n: _reply(req[:2]))
    assert len(rec.calls) == 1
    assert ok == 2


# --- 분할 (깨진 JSON / MAX_TOKENS / 스키마 위반) ---


def test_broken_json_splits_batch_in_half_once():
    posts = [_bill(i) for i in range(4)]

    def _broken_then_ok(requested, n):
        if n == 1:
            return _envelope('{"summaries": [{"bill_id": "PRC_0000",')
        return _reply(requested)

    ok, rec = _run(posts, reply=_broken_then_ok)
    assert ok == 4
    assert [len(c) for c in rec.calls] == [4, 2, 2]   # 4 → 2+2


def test_max_tokens_splits_batch_in_half_once():
    posts = [_bill(i) for i in range(4)]

    def _truncated_then_ok(requested, n):
        if n == 1:
            return _envelope('{"summaries": [', finish="MAX_TOKENS")
        return _reply(requested)

    ok, rec = _run(posts, reply=_truncated_then_ok)
    assert ok == 4
    assert [len(c) for c in rec.calls] == [4, 2, 2]


def test_schema_violation_splits_batch_in_half_once():
    posts = [_bill(i) for i in range(4)]

    def _wrong_shape(requested, n):
        if n == 1:
            # 단건 스키마({"summary": [...]})로 답한 경우
            return _envelope('{"summary": ["첫째임", "둘째임", "셋째임"]}')
        return _reply(requested)

    ok, rec = _run(posts, reply=_wrong_shape)
    assert ok == 4
    assert [len(c) for c in rec.calls] == [4, 2, 2]


def test_split_happens_at_most_once():
    posts = [_bill(i) for i in range(4)]
    ok, rec = _run(
        posts, reply=lambda req, n: _envelope('{"summaries": [{"bill_id":')
    )
    assert ok == 0
    assert [len(c) for c in rec.calls] == [4, 2, 2]   # 조각은 다시 나누지 않는다
    assert all(p.summary == [] for p in posts)        # 전부 발췌 폴백


def test_single_item_batch_is_not_split():
    posts = [_bill(0)]
    ok, rec = _run(posts, reply=lambda req, n: _envelope("완전히 깨진 응답 {"))
    assert ok == 0
    assert len(rec.calls) == 1


def test_safety_block_does_not_split():
    # 안전필터 차단은 배치 크기 문제가 아니다 — 나눠도 같은 결과다.
    posts = [_bill(i) for i in range(4)]
    ok, rec = _run(posts, reply=lambda req, n: _envelope("", finish="SAFETY"))
    assert ok == 0
    assert len(rec.calls) == 1


# --- HTTP 오류: 분할하지 않는다 ---


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _CountingSession:
    def __init__(self, response):
        self._response = response
        self.sent = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.sent.append(deepcopy(json))
        return self._response


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_errors_do_not_trigger_split(status):
    posts = [_bill(i) for i in range(4)]
    s = Summarizer(_cfg())
    sess = _CountingSession(
        _FakeResponse(status, {"error": {"status": "X", "message": "boom"}}, "boom")
    )
    s.session = sess
    assert s.summarize_all({_SOURCE: posts}) == 0
    # 분할했다면 4건 → 2+2 로 3회가 된다. 인증·한도·5xx 는 배치 크기 문제가 아니므로
    # 한 번 실패하면 그대로 끝난다(모델 체인도 타지 않는다).
    assert len(sess.sent) == 1
    assert all(p.summary == [] for p in posts)


def test_network_error_does_not_trigger_split():
    posts = [_bill(i) for i in range(4)]
    s = Summarizer(_cfg())
    calls = {"n": 0}

    class _Boom:
        def post(self, *a, **k):
            calls["n"] += 1
            raise ConnectionError("연결 실패")

    s.session = _Boom()
    assert s.summarize_all({_SOURCE: posts}) == 0
    assert calls["n"] == 1          # 분할했다면 3회가 된다
    assert all(p.summary == [] for p in posts)


def test_no_api_key_skips_batch_entirely():
    posts = {_SOURCE: [_bill(0), _bill(1)]}
    assert summarize_posts(_cfg(api_key=""), posts) == 0
    assert all(p.summary == [] for p in posts[_SOURCE])


def test_batch_exception_never_escapes_summarize_all():
    # 배치 경로에서 무슨 일이 나도 일반 요약 결과와 메일 발송은 유지되어야 한다.
    general = [_general(0)]
    bills = [_bill(0)]
    s = Summarizer(_cfg())

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        if schema is not None:
            raise RuntimeError("배치 경로 폭발")
        return _envelope('{"summary": ["일반 요약함"]}')

    s._generate = _generate
    ok = s.summarize_all({"금융위 · 보도자료": general, _SOURCE: bills})
    assert ok == 1
    assert general[0].summary == ["일반 요약함"]
    assert bills[0].summary == []


# --- 일반 게시물 회귀: 1건당 1회, 배치와 섞이지 않는다 ---


def test_general_posts_still_one_call_each():
    general = [_general(i) for i in range(4)]
    s = Summarizer(_cfg())
    seen = {"single": 0, "batch": 0}

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        if schema is None and max_output_tokens is None:
            seen["single"] += 1
            return _envelope('{"summary": ["첫째임", "둘째임", "셋째임"]}')
        seen["batch"] += 1
        raise AssertionError("일반 게시물은 배치로 가면 안 된다")

    s._generate = _generate
    assert s.summarize_all({"금융위 · 보도자료": general}) == 4
    assert seen == {"single": 4, "batch": 0}
    assert all(len(p.summary) == 3 for p in general)


def test_mixed_run_uses_single_calls_for_general_and_one_batch_for_bills():
    general = [_general(i) for i in range(3)]
    bills = [_bill(i) for i in range(5)]
    s = Summarizer(_cfg())
    single, batch = [], []

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        if schema is None:
            single.append(prompt)
            return _envelope('{"summary": ["첫째임", "둘째임", "셋째임"]}')
        batch.append(_ids_in(prompt, bills))
        return _reply(batch[-1])

    s._generate = _generate
    ok = s.summarize_all({"금융위 · 보도자료": general, _SOURCE: bills})

    assert ok == 8
    assert len(single) == 3            # 일반은 1건당 1회
    assert len(batch) == 1             # 의안 5건은 한 번에
    assert batch[0] == [p.post_id for p in bills]
    # 의안은 일반 단건 프롬프트에 절대 등장하지 않는다
    assert not any("PRC_" in p for p in single)


def test_bills_do_not_consume_general_max_posts():
    # 의안이 아무리 많아도 일반 게시물의 요약 상한을 잠식하지 않는다.
    general = [_general(i) for i in range(3)]
    bills = [_bill(i) for i in range(20)]
    s = Summarizer(_cfg(max_posts=3))

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        if schema is None:
            return _envelope('{"summary": ["첫째임", "둘째임", "셋째임"]}')
        return _reply(_ids_in(prompt, bills))

    s._generate = _generate
    ok = s.summarize_all({"금융위 · 보도자료": general, _SOURCE: bills})
    assert ok == 23
    assert all(p.summary for p in general)
    assert all(p.summary for p in bills)


# --- 메일 표시 ---


def test_assembly_summary_label_names_the_source_section():
    p = _bill(0)
    p.summary = _lines("PRC_0000")
    html = build_html({_SOURCE: [p]})
    text = build_text({_SOURCE: [p]})
    assert ">제안이유 및 주요내용 · AI 3줄 요약</div>" in html
    assert "[제안이유 및 주요내용 · AI 3줄 요약]" in text
    assert "생성형 AI" in text     # AI 유의사항은 그대로


def test_assembly_body_fallback_label_after_ai_failure():
    p = _bill(0)          # summary 없음 = AI 실패
    html = build_html({_SOURCE: [p]})
    text = build_text({_SOURCE: [p]})
    assert "제안이유 및 주요내용 발췌" in html
    assert "[제안이유 및 주요내용 발췌]" in text
    assert "[원문 발췌]" not in text
    # AI 생성물이 아니므로 요약 라벨도, AI 유의사항도 붙지 않는다
    assert "AI 3줄 요약" not in text and "AI 3줄 요약" not in html
    assert "생성형 AI" not in text and "생성형 AI" not in html
    assert "예치금 보호 의무" in text      # 제안이유 원문 발췌가 실린다


def test_general_post_labels_are_unchanged():
    p = _general(0)
    p.summary = ["첫째임", "둘째임", "셋째임"]
    assert ">AI 3줄 요약</div>" in build_html({p.source_name: [p]})
    assert "[AI 3줄 요약]" in build_text({p.source_name: [p]})
    assert "제안이유" not in build_text({p.source_name: [p]})

    q = _general(1)       # 요약 없음 → 기존 '원문 발췌' 그대로
    assert "[원문 발췌]" in build_text({q.source_name: [q]})
    assert "제안이유" not in build_html({q.source_name: [q]})


def test_proposal_reason_is_not_stored_in_details():
    posts = [_bill(0)]
    _run(posts)
    assert posts[0].details == []
    assert posts[0].body        # 본문에만 담긴다


# --- 호출 계층 실패는 남은 배치까지 태우지 않는다 ---


def _multi_batch_posts(n=6, batch_size=2):
    return [_bill(i) for i in range(n)], AssemblyBatchConfig(batch_size=batch_size)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_non_recoverable_failure_stops_remaining_batches(status):
    """401/403/5xx 는 다음 배치도 똑같이 실패한다 — 남은 배치를 부르지 않는다.

    회귀: 예전에는 배치마다 실패를 삼키고 계속 호출해서, 자격증명이 죽은 실행이
    (배치 수 × 타임아웃)만큼 시간예산을 태우고 메일을 그만큼 늦췄다. 일반 요약
    경로의 연속 실패 서킷 브레이커와 같은 취지다.
    """
    posts, batch = _multi_batch_posts()
    s = Summarizer(_cfg(batch=batch))
    sess = _CountingSession(
        _FakeResponse(status, {"error": {"status": "X", "message": "boom"}}, "boom")
    )
    s.session = sess

    assert s.summarize_all({_SOURCE: posts}) == 0
    # 6건 / batch_size 2 → 배치 3개. 첫 호출이 실패하면 거기서 멈춘다.
    assert len(sess.sent) == 1
    assert all(p.summary == [] for p in posts)


def test_network_failure_stops_remaining_batches():
    posts, batch = _multi_batch_posts()
    s = Summarizer(_cfg(batch=batch))
    calls = {"n": 0}

    class _Boom:
        def post(self, *a, **k):
            calls["n"] += 1
            raise ConnectionError("연결 실패")

    s.session = _Boom()
    assert s.summarize_all({_SOURCE: posts}) == 0
    assert calls["n"] == 1


def test_content_level_failure_does_not_stop_remaining_batches():
    """응답 내용이 문제인 경우는 배치마다 다를 수 있다 — 멈추면 안 된다."""
    posts = [_bill(i) for i in range(4)]
    first_batch = {"PRC_0000", "PRC_0001"}

    def _only_first_batch_broken(requested, n):
        # 첫 배치는 분할한 조각까지 계속 깨진 응답을 준다(내용 문제).
        if set(requested) & first_batch:
            return _envelope("{쓰레기")
        return _reply(requested)

    cfg = _cfg(batch=AssemblyBatchConfig(batch_size=2, retry_missing_once=False))
    ok, rec = _run(posts, cfg, reply=_only_first_batch_broken)

    # 첫 배치가 끝까지 실패해도 둘째 배치는 호출되어 요약을 받는다.
    assert ok == 2
    assert posts[0].summary == [] and posts[1].summary == []
    assert posts[2].summary == _lines("PRC_0002")
    assert posts[3].summary == _lines("PRC_0003")
    assert any(set(c) == {"PRC_0002", "PRC_0003"} for c in rec.calls)


def test_split_stops_second_half_after_call_failure():
    """분할 앞 조각에서 호출이 죽으면 뒤 조각도 같은 실패를 반복한다 — 부르지 않는다."""
    posts = [_bill(i) for i in range(4)]
    seq = {"n": 0}

    def _broken_then_dead(requested, n):
        seq["n"] = n
        if n == 1:
            return _envelope("{쓰레기")          # 분할 유발
        raise RuntimeError("HTTP 401: invalid api key")

    cfg = _cfg(batch=AssemblyBatchConfig(batch_size=4, retry_missing_once=False))
    ok, rec = _run(posts, cfg, reply=_broken_then_dead)
    assert ok == 0
    # 최초 1회 + 분할 앞 조각 1회 = 2회. 뒤 조각까지 불렀다면 3회가 된다.
    assert len(rec.calls) == 2
    assert all(p.summary == [] for p in posts)


def test_general_posts_still_summarized_after_assembly_call_failure():
    """의안 배치가 죽어도 일반 게시물 단건 요약 경로는 그대로 돈다."""
    bills = [_bill(i) for i in range(4)]
    generals = [_general(0)]
    s = Summarizer(_cfg(batch=AssemblyBatchConfig(batch_size=2)))
    calls = {"n": 0}

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        calls["n"] += 1
        if "bill_id:" in prompt:
            raise RuntimeError("HTTP 403: forbidden")
        return _envelope("첫째 문장임\n둘째 문장임\n셋째 문장임")

    s._generate = _generate
    ok = s.summarize_all({_SOURCE: bills, "금융위 · 보도자료": generals})

    assert ok == 1                                  # 일반 1건은 요약됐다
    assert generals[0].summary                      # 일반 경로 무영향
    assert all(p.summary == [] for p in bills)


def test_safety_block_does_not_stop_remaining_batches():
    """비정상 종료(안전필터 차단 등)는 이 배치 내용의 문제다 — 브레이커를 올리지 않는다.

    회귀: finishReason != STOP 은 _BatchFailed 인데 이것이 generic except 로 흘러
    call_failed=True 가 되어, 이후 배치가 통째로 취소됐다. 호출은 성공했고 응답
    내용이 문제인 상황이라 다른 의안이 담긴 다음 배치는 통과할 수 있다.
    """
    posts = [_bill(i) for i in range(4)]
    first_batch = {"PRC_0000", "PRC_0001"}

    def _first_batch_blocked(requested, n):
        if set(requested) & first_batch:
            return _envelope("", finish="SAFETY")
        return _reply(requested)

    cfg = _cfg(batch=AssemblyBatchConfig(batch_size=2, retry_missing_once=False))
    ok, rec = _run(posts, cfg, reply=_first_batch_blocked)

    assert ok == 2
    assert posts[0].summary == [] and posts[1].summary == []
    assert posts[2].summary == _lines("PRC_0002")
    assert posts[3].summary == _lines("PRC_0003")
    # 둘째 배치가 실제로 호출됐다(브레이커가 올라갔다면 호출 자체가 없다)
    assert any(set(c) == {"PRC_0002", "PRC_0003"} for c in rec.calls)


def test_safety_block_does_not_split_the_batch():
    """분할 정책은 그대로 — 배치 크기와 무관한 실패라 나눠도 같은 결과다."""
    posts = [_bill(i) for i in range(4)]

    def _blocked(requested, n):
        return _envelope("", finish="SAFETY")

    cfg = _cfg(batch=AssemblyBatchConfig(batch_size=4, retry_missing_once=False))
    ok, rec = _run(posts, cfg, reply=_blocked)
    assert ok == 0
    assert len(rec.calls) == 1          # 분할했다면 3회가 된다


# --- 프롬프트 예시는 llm.lines 를 따라야 한다 ---
#
# 예시를 3줄로 못박아 두면 lines 를 3 이외로 바꿨을 때 규칙(lines 개)과 예시(3개)가
# 어긋난다. 모델은 구체적인 예시를 따르기 쉽고, 그러면 _valid_lines 가 '정확히 lines
# 개가 아니다'라며 **모든 의안을 버려** 배치 요약이 통째로 발췌 폴백이 된다.


def _n_lines(bill_id: str, n: int) -> list[str]:
    return [f"{bill_id} {i}번째 문장임" for i in range(1, n + 1)]


@pytest.mark.parametrize("lines", [1, 2, 3, 5])
def test_prompt_example_matches_configured_lines(lines):
    posts = [_bill(0)]
    captured = {}

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        captured["prompt"] = prompt
        return _envelope(
            json.dumps(
                {"summaries": [{"bill_id": "PRC_0000",
                                "summary": _n_lines("PRC_0000", lines)}]},
                ensure_ascii=False,
            )
        )

    s = Summarizer(_cfg(lines=lines))
    s._generate = _generate
    s.summarize_all({_SOURCE: posts})

    prompt = captured["prompt"]
    assert f"정확히 {lines}개의 문장" in prompt
    # 예시 배열의 원소 수가 규칙과 같아야 한다
    found = re.search(r'"summary": (\[[^\]]*\])', prompt)
    assert found, prompt
    assert json.loads(found.group(1)) == [f"문장{i}" for i in range(1, lines + 1)]


@pytest.mark.parametrize("lines", [2, 5])
def test_batch_summary_works_with_non_default_lines(lines):
    """설정을 바꿨다고 배치 요약이 조용히 꺼지면 안 된다(끝까지 확인)."""
    posts = [_bill(i) for i in range(3)]

    def _reply_n(requested, n):
        return _envelope(
            json.dumps(
                {"summaries": [{"bill_id": b, "summary": _n_lines(b, lines)} for b in requested]},
                ensure_ascii=False,
            )
        )

    ok, _rec = _run(posts, _cfg(lines=lines), reply=_reply_n)
    assert ok == 3
    assert all(len(p.summary) == lines for p in posts)


def test_three_line_example_is_unchanged_at_the_default():
    """기본 설정(lines=3)의 프롬프트는 종전과 같아야 한다 — 동작 무변경."""
    posts = [_bill(0)]
    captured = {}

    def _generate(prompt, deadline=None, *, schema=None, max_output_tokens=None):
        captured["prompt"] = prompt
        return _reply(["PRC_0000"])

    s = Summarizer(_cfg())
    s._generate = _generate
    s.summarize_all({_SOURCE: posts})
    assert '"summary": ["문장1", "문장2", "문장3"]' in captured["prompt"]
