"""구독자별 수신 기관 선택 — run() 통합 회귀.

보호하는 계약:
  - 기능 비활성: 구독 API 를 부르지 않고 기존과 같은 send_digest 1회 호출.
  - 구독 API 는 LLM 전에 1회. 실패하면 메일 0통·LLM 0회·state 불변(전체 발송 복귀 금지).
  - 같은 설정끼리 BCC 그룹, Post.source_key 로 신규·상세 업데이트를 거른다.
  - 보낼 것이 없는 그룹은 빈 메일을 보내지 않으며, 그래도 전역 seen·큐는 확정된다.
  - 한 그룹이라도 실패하면 seen·큐를 확정하지 않는다(앞 그룹은 중복 수신 가능).
  - dry-run 은 구독 API(운영 D1 동기화)를 부르지 않는다.
  - 로그에 이메일·식별자·토큰이 남지 않는다.

실제 HTTP·SMTP·Gemini 는 부르지 않는다.
"""
import json
import logging
import os
import sys

import pytest
import requests
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import subscription as sub_mod
from src.models import ASSEMBLY_SOURCE_KEY, Post, ProposalContentStatus as S
from src.scrapers.base import CollectResult
from src.state import State
from src.subscription import API_VERSION, subscriber_hash

KEY = "h" * 48
TOKEN = "token-very-secret-xyz"
PAGE = "https://law-rader-sub.example.workers.dev/"
INSTANCE = "a1" * 16
FSC, FSS = "fsc_press", "fss_press"
ONLY = f"{FSC},{FSS},{ASSEMBLY_SOURCE_KEY}"
OLD = "2020-01-01T00:00:00Z"

# 구독자와 Worker(D1) 에 저장된 설정. new@ 는 D1 에 행이 없는 기존 구독자 → mode=all.
PREFS = {
    "all@example.com": ("all", []),
    "asm@example.com": ("selected", ["assembly"]),
    "fscasm@example.com": ("selected", ["fsc", "assembly"]),
    "off@example.com": ("selected", []),
    "asm2@example.com": ("selected", ["assembly"]),
    "pipc@example.com": ("selected", ["pipc"]),
}
RECIPIENTS = list(PREFS) + ["new@example.com"]


def _post(key, pid, name):
    return Post(source_key=key, source_name=name, post_id=pid, title=f"{key} {pid}",
                url=f"https://e.com/{key}/{pid}?billId={pid}", date="2026-10-21")


def _bill(pid):
    return Post(
        source_key=ASSEMBLY_SOURCE_KEY, source_name="의안정보시스템 · 계류의안",
        post_id=pid, title=f"{pid} 법률안",
        url=f"https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={pid}",
        date="2026-10-21",
    )


class _FakeScraper:
    SUPPORTS_ENRICH = True

    def __init__(self, key, posts, statuses):
        self.key = key
        self.posts = list(posts)
        self.statuses = statuses

    def collect(self, limit, seen_ids, max_pages):
        posts = [p for p in self.posts if p.post_id not in seen_ids]
        return CollectResult(posts=posts, reached_boundary=True, scanned=max(len(posts), 1))

    def enrich(self, post):
        status, body = self.statuses.get(post.post_id, (S.AVAILABLE, "본문 내용"))
        post.body = body
        if post.source_key == ASSEMBLY_SOURCE_KEY:
            post.proposal_status = status


class _FakeWorker:
    """POST /internal/sync-and-resolve 대역. D1 에 없는 식별자는 mode=all 로 응답한다."""

    def __init__(self, *, status=200, error=None, mutate=None):
        self.calls = []
        self.status = status
        self.error = error
        self.mutate = mutate
        self.by_hash = {subscriber_hash(a, KEY): v for a, v in PREFS.items()}

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.error is not None:
            raise self.error
        body = {
            "api_version": API_VERSION,
            # 실제 Worker 처럼 D1 식별자를 돌려준다(요청에 있으면 같은 값).
            "instance": kw["json"].get("instance", INSTANCE),
            "subscribers": [
                {"subscriber": sid, "mode": self.by_hash.get(sid, ("all", []))[0],
                 "agencies": list(self.by_hash.get(sid, ("all", []))[1])}
                for sid in kw["json"]["subscribers"]
            ],
        }
        if self.mutate is not None:
            body = self.mutate(body)
        r = requests.Response()
        r.status_code = self.status
        r._content = json.dumps(body).encode()
        return r


class _Ctx:
    def __init__(self, state_path):
        self.state_path = state_path
        self.sends = []          # (recipients, {source_key: [post_id]}, {source_key: [post_id]})
        self.summarize_calls = 0
        self.verify_calls = 0
        self.rc = None
        self.builds = []         # build_scraper 가 불린 소스 key(수집이 시작됐는지 확인용)

    @property
    def state(self):
        return State(self.state_path)

    def by_recipient(self):
        out = {}
        for recipients, news, updates in self.sends:
            for r in recipients:
                assert r not in out, f"{r} 에게 두 통 발송"
                out[r] = (news, updates)
        return out


def _ids(posts_by_source):
    out = {}
    for posts in posts_by_source.values():
        for p in posts:
            out.setdefault(p.source_key, []).append(p.post_id)
    return out


def _config(tmp_path, enabled):
    raw = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    raw["subscription"]["enabled"] = enabled
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def _seed_state(tmp_path, queue=()):
    path = tmp_path / "seen.json"
    st = State(path)
    for key in (FSC, FSS, ASSEMBLY_SOURCE_KEY):
        st.mark_seen(key, ["old"], baselined=True)
    for bill_id in queue:
        b = _bill(bill_id)
        st.queue_detail(ASSEMBLY_SOURCE_KEY, bill_id, title=b.title, url=b.url, date=b.date,
                        status="pending", note="제안이유 미등록", now=OLD)
    st.save()
    return path


def _run(tmp_path, monkeypatch, *, enabled=True, worker=None, news=None, statuses=None,
         queue=(), send_fail_on=None, argv_extra=(), env=None, verify_error=None,
         summarize_error=None):
    from src import main as main_mod

    state_path = _seed_state(tmp_path, queue)
    ctx = _Ctx(state_path)
    ctx.worker = worker or _FakeWorker()
    news = news if news is not None else {}
    statuses = statuses or {}

    base_env = {
        "SMTP_USER": "sender@example.com", "SMTP_PASSWORD": "pw",
        "MAIL_TO": ", ".join(RECIPIENTS),
        "SUBSCRIPTION_API_URL": "https://law-rader-sub.example.workers.dev/internal/sync-and-resolve",
        "SUBSCRIPTION_API_TOKEN": TOKEN,
        "SUBSCRIBER_HASH_KEY": KEY,
        "SUBSCRIPTION_PAGE_URL": PAGE,
        # 개발자 셸에 남은 값이 결과를 바꾸지 않도록 기본은 미설정.
        "SUBSCRIPTION_REQUIRED": None,
    }
    base_env.update(env or {})
    for k, v in base_env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)

    def _factory(src, fetcher):
        ctx.builds.append(src.key)
        return _FakeScraper(src.key, news.get(src.key, []), statuses)

    def _send(cfg, posts_by_source, detail_updates_by_source=None):
        ctx.sends.append((list(cfg.recipients), _ids(posts_by_source),
                          _ids(detail_updates_by_source or {})))
        ctx.last_cfg = cfg
        if send_fail_on is not None and len(ctx.sends) == send_fail_on:
            raise RuntimeError("smtp down")

    def _verify(cfg):
        ctx.verify_calls += 1
        if verify_error is not None:
            raise verify_error

    def _summarize(llm_cfg, posts_by_source):
        ctx.summarize_calls += 1
        if summarize_error is not None:
            raise summarize_error
        return 0

    monkeypatch.setattr(main_mod, "build_scraper", _factory)
    monkeypatch.setattr(main_mod, "send_digest", _send)
    monkeypatch.setattr(main_mod, "verify_smtp_login", _verify)
    monkeypatch.setattr(main_mod, "summarize_posts", _summarize)
    monkeypatch.setattr(sub_mod.requests, "post", ctx.worker)

    ctx.before = state_path.read_bytes()
    argv = ["--state", str(state_path), "--config", str(_config(tmp_path, enabled)),
            "--only", ONLY, *argv_extra]
    ctx.rc = main_mod.run(argv)
    return ctx


MIXED_NEWS = {
    FSC: [_post(FSC, "f1", "금융위 · 보도자료")],
    FSS: [_post(FSS, "s1", "금감원 · 보도자료")],
    ASSEMBLY_SOURCE_KEY: [_bill("B1")],
}


# ==========================================================================
# 기존 기능 호환
# ==========================================================================
def test_disabled_keeps_legacy_single_send_and_never_calls_api(tmp_path, monkeypatch):
    ctx = _run(tmp_path, monkeypatch, enabled=False, news=MIXED_NEWS)
    assert ctx.rc == 0
    assert ctx.worker.calls == []
    assert len(ctx.sends) == 1
    recipients, news, updates = ctx.sends[0]
    assert recipients == RECIPIENTS                       # MAIL_TO 전원 한 통
    assert news == {FSC: ["f1"], FSS: ["s1"], ASSEMBLY_SOURCE_KEY: ["B1"]}
    assert ctx.last_cfg.preferences_url == ""             # 꺼져 있으면 링크 없음
    assert ctx.state.seen_ids(FSC) == {"old", "f1"}


# ==========================================================================
# 기관 선택
# ==========================================================================
def test_groups_receive_only_selected_agencies(tmp_path, monkeypatch):
    ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS)
    assert ctx.rc == 0
    assert len(ctx.worker.calls) == 1
    got = ctx.by_recipient()
    everything = {FSC: ["f1"], FSS: ["s1"], ASSEMBLY_SOURCE_KEY: ["B1"]}
    # D1 에 행이 없는 기존 구독자(new@)는 mode=all → 전체 수신 그룹과 같은 한 통.
    assert got["all@example.com"][0] == everything
    assert got["new@example.com"][0] == everything
    assert got["asm@example.com"][0] == {ASSEMBLY_SOURCE_KEY: ["B1"]}
    assert got["asm2@example.com"][0] == {ASSEMBLY_SOURCE_KEY: ["B1"]}
    assert got["fscasm@example.com"][0] == {FSC: ["f1"], ASSEMBLY_SOURCE_KEY: ["B1"]}
    # 일시중지(0개 선택)·이번에 글이 없는 기관만 고른 사용자에게는 빈 메일을 보내지 않는다.
    assert "off@example.com" not in got
    assert "pipc@example.com" not in got
    # 같은 설정은 BCC 한 통, 다른 설정은 다른 통.
    assert sorted(len(r) for r, _, _ in ctx.sends) == [1, 2, 2]
    assert ["asm@example.com", "asm2@example.com"] in [r for r, _, _ in ctx.sends]
    # 그룹 메일에는 설정 링크가 실린다.
    assert ctx.last_cfg.preferences_url == PAGE
    # 전역 seen 의미는 그대로(수신자와 무관하게 발송 트랜잭션 완료 = seen).
    assert ctx.state.seen_ids(FSS) == {"old", "s1"}
    assert ctx.state.seen_ids(ASSEMBLY_SOURCE_KEY) == {"old", "B1"}


def test_detail_updates_follow_assembly_selection(tmp_path, monkeypatch):
    ctx = _run(
        tmp_path, monkeypatch,
        news={FSS: [_post(FSS, "s1", "금감원 · 보도자료")]},
        queue=["PRC_Q"],
        statuses={"PRC_Q": (S.AVAILABLE, "제안이유 본문")},
    )
    assert ctx.rc == 0
    got = ctx.by_recipient()
    assert got["asm@example.com"] == ({}, {ASSEMBLY_SOURCE_KEY: ["PRC_Q"]})
    assert got["fscasm@example.com"] == ({}, {ASSEMBLY_SOURCE_KEY: ["PRC_Q"]})
    assert got["all@example.com"] == ({FSS: ["s1"]}, {ASSEMBLY_SOURCE_KEY: ["PRC_Q"]})
    assert "pipc@example.com" not in got and "off@example.com" not in got
    # 큐 처리 의미는 그대로 — 발송 성공 후 복구분은 큐에서 빠지고 seen 에 들어간다.
    assert ctx.state.pending_detail(ASSEMBLY_SOURCE_KEY) == {}
    assert "PRC_Q" in ctx.state.seen_ids(ASSEMBLY_SOURCE_KEY)


def test_all_groups_empty_sends_nothing_but_confirms_state(tmp_path, monkeypatch):
    # 아무도 금감원·의안을 고르지 않은 상황: 모든 그룹이 전송 대상 없음.
    only_pipc = {sid: ("selected", ["pipc"]) for sid in
                 (subscriber_hash(a, KEY) for a in RECIPIENTS)}
    worker = _FakeWorker()
    worker.by_hash = only_pipc
    ctx = _run(
        tmp_path, monkeypatch, worker=worker,
        news={FSS: [_post(FSS, "s1", "금감원 · 보도자료")], ASSEMBLY_SOURCE_KEY: [_bill("B1")]},
        statuses={"B1": (S.PENDING, "")},
    )
    assert ctx.rc == 0
    assert ctx.sends == []
    assert ctx.state.seen_ids(FSS) == {"old", "s1"}
    # 최초 알림 처리(빈 그룹 = 의도적 미발송)가 끝났으므로 미확보 의안은 큐에 등록된다.
    assert set(ctx.state.pending_detail(ASSEMBLY_SOURCE_KEY)) == {"B1"}


# ==========================================================================
# 장애 — fail-closed
# ==========================================================================
@pytest.mark.parametrize(
    "worker",
    [
        _FakeWorker(status=500),
        _FakeWorker(status=401),
        _FakeWorker(error=requests.Timeout("t")),
        _FakeWorker(error=requests.ConnectionError("c")),
        _FakeWorker(mutate=lambda b: {**b, "subscribers": b["subscribers"][1:]}),   # 누락
        _FakeWorker(mutate=lambda b: {**b, "api_version": 99}),
        _FakeWorker(mutate=lambda b: {**b, "subscribers": [
            {**s, "mode": "selected", "agencies": ["unknown_agency"]} for s in b["subscribers"]]}),
    ],
    ids=["http500", "http401", "timeout", "conn", "missing", "version", "unknown-agency"],
)
def test_api_failure_sends_nothing_skips_llm_and_keeps_state(tmp_path, monkeypatch, worker):
    ctx = _run(tmp_path, monkeypatch, worker=worker, news=MIXED_NEWS,
               queue=["PRC_Q"], statuses={"PRC_Q": (S.AVAILABLE, "본문")})
    assert ctx.rc == 1
    assert len(ctx.worker.calls) == 1               # 실제로 API 단계에서 실패했다
    assert ctx.sends == []                          # 전체 발송으로 되돌아가지 않는다
    assert ctx.summarize_calls == 0                 # Gemini 호출 없음
    assert ctx.verify_calls == 1                    # SMTP 선점검은 했다
    assert ctx.state_path.read_bytes() == ctx.before  # seen·큐·재조회 메타 모두 불변


def test_missing_subscription_settings_fail_closed(tmp_path, monkeypatch):
    ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS, env={"SUBSCRIPTION_API_TOKEN": None})
    assert ctx.rc == 1
    assert ctx.worker.calls == [] and ctx.sends == [] and ctx.summarize_calls == 0
    assert ctx.state_path.read_bytes() == ctx.before


def test_smtp_precheck_failure_does_not_call_api(tmp_path, monkeypatch):
    ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS, verify_error=OSError("down"))
    assert ctx.rc == 1
    assert ctx.worker.calls == [] and ctx.sends == []
    assert ctx.state_path.read_bytes() == ctx.before


def test_partial_group_failure_confirms_nothing(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS, send_fail_on=2,
                   statuses={"B1": (S.PENDING, "")})
    assert ctx.rc == 1
    assert len(ctx.sends) == 2                      # 1번 그룹 성공, 2번 그룹에서 실패
    st = ctx.state
    assert st.seen_ids(FSC) == {"old"} and st.seen_ids(FSS) == {"old"}
    assert st.seen_ids(ASSEMBLY_SOURCE_KEY) == {"old"}
    assert st.pending_detail(ASSEMBLY_SOURCE_KEY) == {}   # 신규 큐 등록도 하지 않는다
    assert "중복 수신" in caplog.text               # 운영자가 볼 진단 로그


def test_llm_failure_still_sends_groups(tmp_path, monkeypatch):
    ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS, summarize_error=RuntimeError("gemini"))
    assert ctx.rc == 0
    assert len(ctx.sends) == 3
    assert ctx.state.seen_ids(FSC) == {"old", "f1"}


def test_nothing_new_syncs_subscribers_without_sending(tmp_path, monkeypatch):
    """Phase 2: 보낼 것이 없는 실행도 MAIL_TO 를 동기화한다(삭제 사용자 접근 차단 지연 해소).

    Phase 1 에서는 이 경우 API 를 부르지 않았다 — 요구사항 10 에 따라 의도적으로 바뀐 계약이다.
    """
    ctx = _run(tmp_path, monkeypatch, news={})
    assert ctx.rc == 0
    assert len(ctx.worker.calls) == 1
    sent_ids = ctx.worker.calls[0][1]["json"]["subscribers"]
    assert sent_ids == sorted(subscriber_hash(a, KEY) for a in RECIPIENTS)
    assert ctx.sends == [] and ctx.summarize_calls == 0
    assert ctx.state_path.read_bytes() == ctx.before


def test_dry_run_never_calls_api_or_changes_state(tmp_path, monkeypatch):
    ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS, argv_extra=["--dry-run"])
    assert ctx.rc == 0
    assert ctx.worker.calls == []                   # 운영 D1 동기화 없음
    assert ctx.sends == []
    assert ctx.state_path.read_bytes() == ctx.before


# ==========================================================================
# 개인정보
# ==========================================================================
def test_logs_never_contain_addresses_identifiers_or_secrets(tmp_path, monkeypatch, caplog):
    fail_dir = tmp_path / "fail"
    fail_dir.mkdir()
    with caplog.at_level(logging.DEBUG):
        # 성공 경로(그룹 발송·상세 업데이트) + 부분 실패 경로를 모두 지난다.
        _run(tmp_path, monkeypatch, news=MIXED_NEWS, queue=["PRC_Q"],
             statuses={"PRC_Q": (S.AVAILABLE, "본문")})
        _run(fail_dir, monkeypatch, news=MIXED_NEWS, send_fail_on=2)
    text = caplog.text
    for addr in RECIPIENTS:
        assert addr not in text
        assert subscriber_hash(addr, KEY) not in text
    assert TOKEN not in text and KEY not in text


def test_state_file_holds_no_subscription_data(tmp_path, monkeypatch):
    ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS)
    raw = ctx.state_path.read_text(encoding="utf-8")
    assert "@" not in raw
    assert "selected" not in raw and "subscri" not in raw
    for addr in RECIPIENTS:
        assert subscriber_hash(addr, KEY) not in raw


# ==========================================================================
# Phase 2 — 발송 없는 실행의 동기화
# ==========================================================================
@pytest.mark.parametrize(
    "worker",
    [_FakeWorker(status=503), _FakeWorker(error=requests.Timeout("t")),
     _FakeWorker(mutate=lambda b: {**b, "api_version": 9})],
    ids=["http503", "timeout", "contract"],
)
def test_quiet_sync_failure_changes_nothing(tmp_path, monkeypatch, caplog, worker):
    with caplog.at_level(logging.INFO):
        ctx = _run(tmp_path, monkeypatch, news={}, worker=worker)
    assert ctx.rc == 0                               # 기존 '신규 없음' 실행 결과 그대로
    assert ctx.sends == []
    assert ctx.state_path.read_bytes() == ctx.before
    assert "구독자 목록 동기화 실패" in caplog.text
    for addr in RECIPIENTS:
        assert addr not in caplog.text and subscriber_hash(addr, KEY) not in caplog.text


def test_quiet_sync_unexpected_exception_is_contained(tmp_path, monkeypatch, caplog):
    worker = _FakeWorker(error=RecursionError("deep"))
    with caplog.at_level(logging.INFO):
        ctx = _run(tmp_path, monkeypatch, news={}, worker=worker)
    assert ctx.rc == 0 and ctx.sends == []
    assert "RecursionError" in caplog.text


def test_quiet_sync_skipped_in_dry_run_and_when_disabled(tmp_path, monkeypatch):
    dry = _run(tmp_path, monkeypatch, news={}, argv_extra=["--dry-run"])
    assert dry.worker.calls == []
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = _run(off_dir, monkeypatch, news={}, enabled=False)
    assert off.worker.calls == []


# ==========================================================================
# Phase 2 — SUBSCRIPTION_REQUIRED 운영 보호장치
# ==========================================================================
def test_required_but_disabled_stops_before_collection(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        ctx = _run(tmp_path, monkeypatch, enabled=False, news=MIXED_NEWS,
                   env={"SUBSCRIPTION_REQUIRED": "true"})
    assert ctx.rc == 1
    assert ctx.builds == []                          # 수집조차 시작하지 않음
    assert ctx.sends == [] and ctx.worker.calls == [] and ctx.summarize_calls == 0
    assert ctx.state_path.read_bytes() == ctx.before
    assert "SUBSCRIPTION_REQUIRED=true" in caplog.text


def test_required_but_disabled_dry_run_only_warns(tmp_path, monkeypatch):
    ctx = _run(tmp_path, monkeypatch, enabled=False, news=MIXED_NEWS, argv_extra=["--dry-run"],
               env={"SUBSCRIPTION_REQUIRED": "true"})
    assert ctx.rc == 0 and ctx.sends == []
    assert ctx.state_path.read_bytes() == ctx.before


@pytest.mark.parametrize("value", ["true", "TRUE", " true "])
def test_required_and_enabled_runs_normally(tmp_path, monkeypatch, value):
    ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS,
               env={"SUBSCRIPTION_REQUIRED": value, "SUBSCRIPTION_DB_INSTANCE": INSTANCE})
    assert ctx.rc == 0 and len(ctx.sends) == 3
    assert ctx.worker.calls[0][1]["json"]["instance"] == INSTANCE


@pytest.mark.parametrize("value", [None, "", "false", "FALSE"])
def test_not_required_keeps_legacy_disabled_behaviour(tmp_path, monkeypatch, value):
    ctx = _run(tmp_path, monkeypatch, enabled=False, news=MIXED_NEWS,
               env={"SUBSCRIPTION_REQUIRED": value})
    assert ctx.rc == 0
    assert len(ctx.sends) == 1 and ctx.sends[0][0] == RECIPIENTS


@pytest.mark.parametrize("value", ["ture", "yes", "1", "on", "enabled"])
def test_invalid_required_value_is_a_config_error_not_false(tmp_path, monkeypatch, value):
    with pytest.raises(ValueError, match="SUBSCRIPTION_REQUIRED"):
        _run(tmp_path, monkeypatch, enabled=False, news=MIXED_NEWS,
             env={"SUBSCRIPTION_REQUIRED": value})


def test_required_without_db_instance_fails_closed(tmp_path, monkeypatch):
    """Phase 2.6: 정식 운영(REQUIRED=true)에서는 D1 교체 감지 없이 돌지 않는다."""
    ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS, env={"SUBSCRIPTION_REQUIRED": "true"})
    assert ctx.rc == 1
    assert ctx.worker.calls == [] and ctx.sends == [] and ctx.summarize_calls == 0
    assert ctx.state_path.read_bytes() == ctx.before


@pytest.mark.parametrize(
    "worker",
    [
        # D1 재생성(식별자 없음)·카탈로그 불일치·설정 유실 — Worker 가 409 로 거절
        _FakeWorker(status=409, mutate=lambda b: {"error": "instance_missing"}),
        _FakeWorker(status=409, mutate=lambda b: {"error": "catalog_mismatch"}),
        _FakeWorker(status=409, mutate=lambda b: {"error": "preference_missing"}),
        # Worker 가 200 이어도 식별자가 다르거나 없으면 Python 이 스스로 거절
        _FakeWorker(mutate=lambda b: {**b, "instance": "b2" * 16}),
        _FakeWorker(mutate=lambda b: {k: v for k, v in b.items() if k != "instance"}),
    ],
    ids=["instance_missing", "catalog_mismatch", "preference_missing", "other-db", "no-instance"],
)
def test_integrity_failures_never_fall_back_to_full_send(tmp_path, monkeypatch, caplog, worker):
    with caplog.at_level(logging.INFO):
        ctx = _run(tmp_path, monkeypatch, news=MIXED_NEWS, worker=worker,
                   env={"SUBSCRIPTION_REQUIRED": "true", "SUBSCRIPTION_DB_INSTANCE": INSTANCE})
    assert ctx.rc == 1
    assert ctx.sends == [] and ctx.summarize_calls == 0
    assert ctx.state_path.read_bytes() == ctx.before
    assert "구독 설정 조회 실패" in caplog.text
