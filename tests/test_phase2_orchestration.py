"""Phase 2 오케스트레이션 회귀 — 의안 PENDING/ERROR 영구 재조회 + 상세 업데이트 알림.

보호하는 계약(요구된 A~S):
  최초 알림은 정확히 한 번, PENDING/ERROR/UNKNOWN 은 mail 성공 후에만 seen+queue,
  이미 seen 인 의안도 큐를 통해 상세를 다시 조회, 복구되면 '신규'가 아니라
  '의안 상세 업데이트'로 정확히 한 번, 업데이트 mail 실패 시 큐 유지,
  LLM 실패와 무관하게 완료, dry-run 은 state 불변, --only/disabled 존중,
  SMTP 불가 시 아무것도 완료 처리하지 않음.

실제 HTTP·SMTP·Gemini 는 부르지 않는다.
"""
import json
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import ASSEMBLY_SOURCE_KEY, Post, ProposalContentStatus as S
from src.scrapers.base import CollectResult
from src.state import State

ASSEMBLY_NAME = "의안정보시스템 · 계류의안"
PRESS_KEY = "fss_press"
OLD = "2020-01-01T00:00:00Z"      # 아주 오래된 시각 → 항상 due
JUST_NOW = None                   # queue_detail 기본값(현재 시각) → interval 안에 있음


# --------------------------------------------------------------------------
# 대역
# --------------------------------------------------------------------------
class _FakeScraper:
    SUPPORTS_ENRICH = True

    def __init__(self, key, name, new_posts=(), statuses=None, limits=None):
        self.key = key
        self.name = name
        self.new_posts = list(new_posts)
        self.statuses = dict(statuses or {})
        self.enrich_calls = []
        # 실제 스크래퍼처럼 config 에서 읽어 둔 재조회 상한을 노출한다.
        if limits is not None:
            self.detail_retry_interval_sec, self.detail_retry_max_per_run = limits

    def collect(self, limit, seen_ids, max_pages):
        posts = [p for p in self.new_posts if p.post_id not in seen_ids]
        # scanned>0 이어야 '목록 수집 성공'으로 세어져 all-failed 가드에 걸리지 않는다.
        return CollectResult(posts=posts, reached_boundary=True, scanned=max(len(posts), 1))

    def enrich(self, post):
        self.enrich_calls.append(post.post_id)
        status, body = self.statuses.get(post.post_id, (S.AVAILABLE, "제안이유 본문"))
        post.proposal_status = status
        post.body = body
        post.proposal_note = f"note-{status.value}"


def _bill(pid, title=None):
    return Post(
        source_key=ASSEMBLY_SOURCE_KEY,
        source_name=ASSEMBLY_NAME,
        post_id=pid,
        title=title or f"{pid} 법률안 (홍길동의원)",
        url=f"https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={pid}",
        date="2026-08-24",
    )


def _press(pid="p1"):
    return Post(
        source_key=PRESS_KEY, source_name="금감원 · 보도자료", post_id=pid,
        title=f"보도자료 {pid}", url=f"https://e.com/{pid}", date="2026-08-24",
    )


class _Ctx:
    """한 번의 run() 실행에 대한 관찰값."""

    def __init__(self, state_path):
        self.state_path = state_path
        self.scrapers = {}
        self.builds = []
        self.digests = []          # (posts_by_source, detail_updates)
        self.summarize_inputs = []
        self.verify_calls = 0
        self.rc = None

    # -- 편의 접근자 --
    @property
    def state(self):
        return State(self.state_path)

    @property
    def queue(self):
        return self.state.pending_detail(ASSEMBLY_SOURCE_KEY)

    @property
    def seen(self):
        return self.state.seen_ids(ASSEMBLY_SOURCE_KEY)

    @property
    def assembly(self):
        return self.scrapers.get(ASSEMBLY_SOURCE_KEY)

    @property
    def enriched(self):
        sc = self.assembly
        return list(sc.enrich_calls) if sc else []

    @property
    def updates(self):
        """마지막 다이제스트에 실린 상세 업데이트 Post 목록."""
        if not self.digests:
            return []
        return [p for posts in (self.digests[-1][1] or {}).values() for p in posts]

    @property
    def news(self):
        if not self.digests:
            return []
        return [p for posts in self.digests[-1][0].values() for p in posts]


def _seed_state(tmp_path, *, seen=(), queue=(), press_seen=("old",)):
    """운영 중(baselined)인 state 를 만든다. queue 는 (bill_id, last_attempt_at) 목록."""
    path = tmp_path / "seen.json"
    st = State(path)
    st.mark_seen(ASSEMBLY_SOURCE_KEY, list(seen), baselined=True)
    st.mark_seen(PRESS_KEY, list(press_seen), baselined=True)
    for entry in queue:
        bill_id, last = entry if isinstance(entry, tuple) else (entry, OLD)
        b = _bill(bill_id)
        st.queue_detail(
            ASSEMBLY_SOURCE_KEY, bill_id, title=b.title, url=b.url, date=b.date,
            status="pending", note="제안이유 미등록", now=last,
        )
    st.save()
    return path


def _config_without_assembly(tmp_path):
    """assembly_bill 만 enabled:false 로 바꾼 config 사본."""
    raw = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    for s in raw["sources"]:
        if s["key"] == ASSEMBLY_SOURCE_KEY:
            s["enabled"] = False
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def _run(
    tmp_path,
    monkeypatch,
    *,
    state_path,
    new_assembly=(),
    statuses=None,
    new_press=(),
    argv_extra=(),
    only="assembly_bill",
    config=None,
    send_error=None,
    smtp_missing=False,
    verify_error=None,
    summarize_error=None,
    fill_summaries=False,
    limits=None,
):
    from src import main as main_mod

    ctx = _Ctx(state_path)

    if smtp_missing:
        for var in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO", "MAIL_FROM"):
            monkeypatch.delenv(var, raising=False)
    else:
        monkeypatch.setenv("SMTP_USER", "s@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        monkeypatch.setenv("MAIL_TO", "to@example.com")

    def _factory(src, fetcher):
        posts = new_assembly if src.key == ASSEMBLY_SOURCE_KEY else new_press
        sc = _FakeScraper(src.key, src.name, posts, statuses, limits=limits)
        ctx.builds.append(src.key)
        ctx.scrapers[src.key] = sc
        return sc

    def _send(cfg, posts_by_source, detail_updates_by_source=None):
        # main 이 넘긴 그대로를 기록한다(리스트는 이후 변형되지 않도록 복사).
        ctx.digests.append(
            ({k: list(v) for k, v in posts_by_source.items()},
             {k: list(v) for k, v in (detail_updates_by_source or {}).items()})
        )
        if send_error is not None:
            raise send_error

    def _verify(cfg):
        ctx.verify_calls += 1
        if verify_error is not None:
            raise verify_error

    def _summarize(llm_cfg, posts_by_source):
        ctx.summarize_inputs.append({k: list(v) for k, v in posts_by_source.items()})
        if summarize_error is not None:
            raise summarize_error
        if fill_summaries:
            for posts in posts_by_source.values():
                for p in posts:
                    if p.body:
                        p.summary = ["요약1", "요약2", "요약3"]
        return 0

    monkeypatch.setattr(main_mod, "build_scraper", _factory)
    monkeypatch.setattr(main_mod, "send_digest", _send)
    monkeypatch.setattr(main_mod, "verify_smtp_login", _verify)
    monkeypatch.setattr(main_mod, "summarize_posts", _summarize)

    argv = ["--state", str(state_path)]
    if config:
        argv += ["--config", str(config)]
    if only:
        argv += ["--only", only]
    argv += list(argv_extra)
    ctx.rc = main_mod.run(argv)
    return ctx


# ==========================================================================
# A. 신규 AVAILABLE — 기존 동작 유지, 큐에 넣지 않는다
# ==========================================================================
def test_A_new_available_is_seen_without_queue(tmp_path, monkeypatch):
    path = _seed_state(tmp_path)
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_A")],
        statuses={"PRC_A": (S.AVAILABLE, "제안이유 본문")},
    )
    assert ctx.rc == 0
    assert ctx.seen == {"PRC_A"}
    assert ctx.queue == {}                       # 상세를 확보했으므로 큐 대상이 아니다
    assert len(ctx.digests) == 1
    assert [p.post_id for p in ctx.news] == ["PRC_A"]
    assert ctx.updates == []


# ==========================================================================
# B/C/D. 신규 PENDING / ERROR / UNKNOWN — 발송 성공 후 seen + 큐 등록
# ==========================================================================
@pytest.mark.parametrize("status", [S.PENDING, S.ERROR, S.UNKNOWN])
def test_BCD_new_unavailable_is_seen_and_queued(tmp_path, monkeypatch, status):
    path = _seed_state(tmp_path)
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_A")],
        statuses={"PRC_A": (status, "")},
    )
    assert ctx.rc == 0
    assert ctx.seen == {"PRC_A"}                 # 신규 알림은 정확히 한 번
    entry = ctx.queue["PRC_A"]
    assert entry["status"] == status.value
    assert entry["title"] == "PRC_A 법률안 (홍길동의원)"
    assert entry["url"].endswith("billId=PRC_A")
    assert entry["date"] == "2026-08-24"
    assert entry["attempts"] == 1
    assert entry["first_seen_at"] and entry["last_attempt_at"]
    assert entry["note"] == f"note-{status.value}"


def test_D_unknown_is_not_dropped(tmp_path, monkeypatch):
    """UNKNOWN 을 버리면 예상 못 한 예외로 판정되지 않은 의안이 영구 누락된다."""
    path = _seed_state(tmp_path)
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_A")], statuses={"PRC_A": (S.UNKNOWN, "")},
    )
    assert set(ctx.queue) == {"PRC_A"}


def test_new_pending_is_not_retried_in_the_same_run(tmp_path, monkeypatch):
    """§7 — 이번 실행에서 새로 큐에 들어온 항목을 같은 실행에서 다시 조회하지 않는다."""
    path = _seed_state(tmp_path)
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_A")], statuses={"PRC_A": (S.PENDING, "")},
    )
    assert ctx.enriched == ["PRC_A"]             # 신규 상세 1회뿐 (재조회 없음)


# ==========================================================================
# E. 최초 메일 실패 — seen 도 큐도 확정하지 않는다
# ==========================================================================
def test_E_initial_mail_failure_confirms_nothing(tmp_path, monkeypatch):
    path = _seed_state(tmp_path)
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_A")], statuses={"PRC_A": (S.PENDING, "")},
        send_error=RuntimeError("smtp down"),
    )
    assert ctx.rc == 1
    assert ctx.seen == set()                     # 다음 실행에서 다시 신규로 알린다
    assert ctx.queue == {}


# ==========================================================================
# F. 기존 PENDING 이 아직 due 가 아님 — 조회하지 않고 큐 유지
# ==========================================================================
def test_F_not_due_is_not_enriched(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", JUST_NOW)])
    before = json.loads(path.read_text(encoding="utf-8"))
    ctx = _run(tmp_path, monkeypatch, state_path=path)
    assert ctx.rc == 0
    assert ctx.enriched == []                    # 간격이 안 지났으면 요청하지 않는다
    assert set(ctx.queue) == {"PRC_Q"}
    assert ctx.digests == []                     # 보낼 것이 없으면 메일도 없다
    assert json.loads(path.read_text(encoding="utf-8")) == before


# ==========================================================================
# G/H. 기존 PENDING/ERROR 가 due → 여전히 미확보. 조회 1회, 메일 없음, 메타 갱신
# ==========================================================================
@pytest.mark.parametrize("status", [S.PENDING, S.ERROR, S.UNKNOWN])
def test_GH_due_but_still_unavailable(tmp_path, monkeypatch, status):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path, statuses={"PRC_Q": (status, "")}
    )
    assert ctx.rc == 0
    assert ctx.enriched == ["PRC_Q"]             # 정확히 1회
    assert ctx.digests == []                     # 중복 신규 발송도, 업데이트 발송도 없음
    entry = ctx.queue["PRC_Q"]                   # 큐 유지 + 메타데이터만 갱신
    assert entry["status"] == status.value
    assert entry["attempts"] == 2
    assert entry["last_attempt_at"] != OLD
    assert entry["first_seen_at"] == OLD         # 언제부터 미확보인지는 보존
    assert ctx.seen == {"PRC_Q"}


def test_G_retry_does_not_depend_on_list_visibility(tmp_path, monkeypatch):
    """이미 seen 인 의안은 목록에 다시 나오지 않는다 — 저장된 스냅샷만으로 조회한다."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_Q")],           # 목록에 있어도 seen 이라 신규가 아니다
        statuses={"PRC_Q": (S.PENDING, "")},
    )
    assert ctx.news == [] and ctx.digests == []
    assert ctx.enriched == ["PRC_Q"]


# ==========================================================================
# I/J. PENDING/ERROR → AVAILABLE 복구 → 상세 업데이트 알림 → 큐 제거
# ==========================================================================
def test_IJ_recovered_is_notified_as_update_and_dequeued(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        statuses={"PRC_Q": (S.AVAILABLE, "제안이유 본문")},
        fill_summaries=True,
    )
    assert ctx.rc == 0
    assert len(ctx.digests) == 1                 # 한 실행에 메일 한 통
    assert ctx.news == []                        # '신규'로 재분류하지 않는다
    assert [p.post_id for p in ctx.updates] == ["PRC_Q"]
    assert ctx.updates[0].body == "제안이유 본문"
    assert ctx.updates[0].summary == ["요약1", "요약2", "요약3"]
    assert ctx.queue == {}                       # 전달 완료 → 큐에서 제거
    assert ctx.seen == {"PRC_Q"}                 # seen 은 그대로(중복 신규 없음)


def test_I_recovered_post_keeps_original_snapshot(tmp_path, monkeypatch):
    """복구 알림에는 원래 제목·날짜·상세 URL 이 그대로 쓰인다."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        statuses={"PRC_Q": (S.AVAILABLE, "본문")},
    )
    p = ctx.updates[0]
    assert p.title == "PRC_Q 법률안 (홍길동의원)"
    assert p.date == "2026-08-24"
    assert p.url.endswith("billId=PRC_Q")
    assert p.source_name == ASSEMBLY_NAME


# ==========================================================================
# K. 업데이트 메일 실패 — 큐를 지우지 않는다
# ==========================================================================
def test_K_update_mail_failure_keeps_queue(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        statuses={"PRC_Q": (S.AVAILABLE, "본문")},
        send_error=RuntimeError("smtp refused"),
    )
    assert ctx.rc == 1
    assert set(ctx.queue) == {"PRC_Q"}           # 다음 실행에서 다시 시도 가능
    entry = ctx.queue["PRC_Q"]
    # 복구했지만 전달하지 못했으므로 '시도 완료'로 덮어쓰지 않는다 → 곧바로 다시 due.
    assert entry["last_attempt_at"] == OLD
    assert entry["attempts"] == 1


def test_K_next_run_can_resend_after_failure(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    _run(tmp_path, monkeypatch, state_path=path,
         statuses={"PRC_Q": (S.AVAILABLE, "본문")}, send_error=RuntimeError("boom"))
    again = _run(tmp_path, monkeypatch, state_path=path,
                 statuses={"PRC_Q": (S.AVAILABLE, "본문")})
    assert again.rc == 0
    assert [p.post_id for p in again.updates] == ["PRC_Q"]
    assert again.queue == {}


# ==========================================================================
# L. idempotency — 완료된 업데이트는 다음 실행에서 재발송되지 않는다
# ==========================================================================
def test_L_update_is_sent_exactly_once(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    first = _run(tmp_path, monkeypatch, state_path=path,
                 statuses={"PRC_Q": (S.AVAILABLE, "본문")})
    assert [p.post_id for p in first.updates] == ["PRC_Q"]

    second = _run(tmp_path, monkeypatch, state_path=path,
                  statuses={"PRC_Q": (S.AVAILABLE, "본문")})
    assert second.rc == 0
    assert second.enriched == []                 # 큐에 없으므로 조회조차 하지 않는다
    assert second.digests == []                  # 재발송 없음
    assert second.queue == {}


# ==========================================================================
# M. 업데이트만 있는 실행 — 메일 1통, '신규'라고 표시하지 않는다
# ==========================================================================
def test_M_update_only_run_sends_one_update_mail(tmp_path, monkeypatch):
    from src.notifier import build_html, build_subject, build_text

    class _Cfg:
        subject_prefix = "[LAW RADER]"

    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(tmp_path, monkeypatch, state_path=path,
               statuses={"PRC_Q": (S.AVAILABLE, "본문")})
    assert len(ctx.digests) == 1
    posts, updates = ctx.digests[0]
    assert posts == {} and len(updates[ASSEMBLY_NAME]) == 1
    # 실제 렌더링에서도 '신규'라고 말하지 않는다.
    assert build_subject(_Cfg(), posts, updates) == "[LAW RADER] 의안 상세 업데이트 1건"
    assert "의안 상세 업데이트 1건" in build_html(posts, updates)
    assert build_text(posts, updates).startswith("의안 상세 업데이트 1건")


# ==========================================================================
# N. 신규 + 복구 혼합 — 메일 1통, 두 섹션, state 정확
# ==========================================================================
def test_N_mixed_run(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_N1"), _bill("PRC_N2")],
        statuses={
            "PRC_N1": (S.AVAILABLE, "신규 본문"),
            "PRC_N2": (S.PENDING, ""),
            "PRC_Q": (S.AVAILABLE, "복구 본문"),
        },
    )
    assert ctx.rc == 0
    assert len(ctx.digests) == 1                              # 메일 총 1통
    assert sorted(p.post_id for p in ctx.news) == ["PRC_N1", "PRC_N2"]
    assert [p.post_id for p in ctx.updates] == ["PRC_Q"]      # 두 섹션 모두 존재
    assert ctx.seen == {"PRC_Q", "PRC_N1", "PRC_N2"}
    assert set(ctx.queue) == {"PRC_N2"}                       # 복구분 제거 + 신규 미확보 등록
    assert ctx.queue["PRC_N2"]["status"] == "pending"


def test_N_shares_one_scraper_instance_for_new_and_retry(tmp_path, monkeypatch):
    """§9 — 신규 상세와 재조회가 같은 인스턴스를 써야 budget/breaker 가 합산된다."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_N1")],
        statuses={"PRC_N1": (S.AVAILABLE, "본문"), "PRC_Q": (S.AVAILABLE, "본문")},
    )
    assert ctx.builds.count(ASSEMBLY_SOURCE_KEY) == 1
    # 신규와 재조회가 같은 대역의 호출 목록에 함께 기록된다.
    assert ctx.enriched == ["PRC_N1", "PRC_Q"]


def test_N_llm_gets_new_and_recovered_in_one_pass(tmp_path, monkeypatch):
    """§16 — 불필요한 Gemini 요청 증가를 막기 위해 한 요약 경로로 합쳐 넘긴다."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_N1")],
        statuses={"PRC_N1": (S.AVAILABLE, "신규 본문"), "PRC_Q": (S.AVAILABLE, "복구 본문")},
    )
    assert len(ctx.summarize_inputs) == 1
    ids = [p.post_id for posts in ctx.summarize_inputs[0].values() for p in posts]
    assert ids == ["PRC_N1", "PRC_Q"]            # 신규가 먼저(배치 우선순위)


# ==========================================================================
# O. --no-llm — 복구분도 원문 발췌로 알리고 큐에서 제거한다
# ==========================================================================
def test_O_no_llm_still_completes_the_update(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        statuses={"PRC_Q": (S.AVAILABLE, "제안이유 원문 " * 20)},
        argv_extra=["--no-llm"],
    )
    assert ctx.rc == 0
    assert ctx.summarize_inputs == []            # LLM 은 부르지 않는다
    p = ctx.updates[0]
    assert p.summary == [] and p.body.startswith("제안이유 원문")
    assert ctx.queue == {}                       # 완료 기준은 '원문을 전달했는가'


def test_O_llm_failure_does_not_block_completion(tmp_path, monkeypatch):
    """§32 — 'AI 성공'은 큐 완료 조건이 아니다."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        statuses={"PRC_Q": (S.AVAILABLE, "본문")},
        summarize_error=RuntimeError("gemini 503"),
    )
    assert ctx.rc == 0
    assert [p.post_id for p in ctx.updates] == ["PRC_Q"]
    assert ctx.updates[0].summary == []
    assert ctx.queue == {}


# ==========================================================================
# P. --dry-run — state 를 건드리지 않는다
# ==========================================================================
def test_P_dry_run_mutates_no_state(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    before = path.read_text(encoding="utf-8")
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_N1")],
        statuses={"PRC_N1": (S.PENDING, ""), "PRC_Q": (S.AVAILABLE, "본문")},
        argv_extra=["--dry-run"],
    )
    assert ctx.rc == 0
    assert path.read_text(encoding="utf-8") == before   # seen/큐 모두 그대로
    assert ctx.digests == []                            # 발송 없음
    assert ctx.verify_calls == 0                        # SMTP 접속도 하지 않는다
    # 사이트 조회 자체는 허용된다(결과를 보여주기 위해).
    assert ctx.enriched == ["PRC_N1", "PRC_Q"]


def test_P_dry_run_prints_new_and_update_separately(tmp_path, monkeypatch, capsys):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_N1")],
        statuses={"PRC_N1": (S.AVAILABLE, "신규"), "PRC_Q": (S.AVAILABLE, "복구")},
        argv_extra=["--dry-run"],
    )
    out = capsys.readouterr().out
    assert "[NEW]" in out and "[DETAIL UPDATE]" in out
    assert out.index("[NEW]") < out.index("[DETAIL UPDATE]")


# ==========================================================================
# Q/R. --only 제외 / 소스 비활성 — 큐를 처리하지 않는다
# ==========================================================================
def test_Q_only_excluding_assembly_skips_the_queue(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    before = path.read_text(encoding="utf-8")
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        only=PRESS_KEY, statuses={"PRC_Q": (S.AVAILABLE, "본문")},
    )
    assert ctx.rc == 0
    assert ASSEMBLY_SOURCE_KEY not in ctx.scrapers   # 의안 스크래퍼조차 만들지 않는다
    assert ctx.digests == []
    assert path.read_text(encoding="utf-8") == before


def test_R_disabled_assembly_skips_the_queue(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    before = path.read_text(encoding="utf-8")
    entry_before = json.loads(before)[ASSEMBLY_SOURCE_KEY]
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        only="", config=_config_without_assembly(tmp_path),
        statuses={"PRC_Q": (S.AVAILABLE, "본문")},
    )
    assert ctx.rc == 0
    assert ASSEMBLY_SOURCE_KEY not in ctx.scrapers
    # 다른 소스는 정상 동작(기준선 저장)하되, 의안 entry 는 손대지 않는다.
    assert json.loads(path.read_text(encoding="utf-8"))[ASSEMBLY_SOURCE_KEY] == entry_before


# ==========================================================================
# S. SMTP 불가 — 요약도 재조회도 하지 않고, 큐도 건드리지 않는다
# ==========================================================================
def test_S_missing_smtp_settings_completes_nothing(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    before = path.read_text(encoding="utf-8")
    ctx = _run(
        tmp_path, monkeypatch, state_path=path, smtp_missing=True,
        statuses={"PRC_Q": (S.AVAILABLE, "본문")},
    )
    assert ctx.rc == 1
    assert ctx.verify_calls == 0                 # 접속 시도조차 하지 않는다
    assert ctx.enriched == []                    # 재조회도 하지 않는다
    assert ctx.summarize_inputs == []            # Gemini 할당량도 쓰지 않는다
    assert ctx.digests == []
    assert path.read_text(encoding="utf-8") == before


def test_S_smtp_login_failure_completes_nothing(tmp_path, monkeypatch):
    import smtplib

    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    before = path.read_text(encoding="utf-8")
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        verify_error=smtplib.SMTPAuthenticationError(535, b"bad"),
        statuses={"PRC_Q": (S.AVAILABLE, "본문")},
    )
    assert ctx.rc == 1
    assert ctx.verify_calls == 1
    assert ctx.enriched == []
    assert ctx.summarize_inputs == []
    assert path.read_text(encoding="utf-8") == before


def test_smtp_preflight_runs_even_without_new_posts(tmp_path, monkeypatch):
    """§18 — 신규가 없어도 due 큐가 있으면 발송 가능성이 있으므로 선점검한다."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(tmp_path, monkeypatch, state_path=path,
               statuses={"PRC_Q": (S.PENDING, "")})
    assert ctx.verify_calls == 1


def test_no_preflight_when_nothing_is_due(tmp_path, monkeypatch):
    """할 일이 없으면 SMTP 에 접속하지 않는다(기존 동작 유지)."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", JUST_NOW)])
    ctx = _run(tmp_path, monkeypatch, state_path=path)
    assert ctx.verify_calls == 0


# ==========================================================================
# §20. 메일이 없는 재조회도 진단 메타데이터는 저장할 수 있다
# ==========================================================================
def test_retry_metadata_saved_without_any_mail(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=["PRC_1", "PRC_2"], queue=[("PRC_1", OLD), ("PRC_2", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        statuses={"PRC_1": (S.PENDING, ""), "PRC_2": (S.PENDING, "")},
    )
    assert ctx.digests == []                     # 메일을 억지로 보내지 않는다
    for bill_id in ("PRC_1", "PRC_2"):
        assert ctx.queue[bill_id]["attempts"] == 2
        assert ctx.queue[bill_id]["last_attempt_at"] != OLD


def test_retry_metadata_saved_even_when_mail_fails(tmp_path, monkeypatch):
    """발송이 막힌 동안 같은 의안을 매 실행 무제한으로 다시 두드리지 않도록."""
    path = _seed_state(tmp_path, seen=["PRC_N", "PRC_Q"], queue=[("PRC_Q", OLD)])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_NEW")],
        statuses={"PRC_NEW": (S.PENDING, ""), "PRC_Q": (S.PENDING, "")},
        send_error=RuntimeError("boom"),
    )
    assert ctx.rc == 1
    assert "PRC_NEW" not in ctx.queue            # 알리지 못한 신규는 큐에 넣지 않는다
    assert "PRC_NEW" not in ctx.seen
    assert ctx.queue["PRC_Q"]["attempts"] == 2   # 기존 항목의 재시도 기록만 남는다


# ==========================================================================
# 상한 / 순서 (§30 의 오케스트레이션 측면)
# ==========================================================================
def test_per_run_cap_limits_enrich_calls(tmp_path, monkeypatch):
    ids = [f"PRC_{i}" for i in range(5)]
    path = _seed_state(tmp_path, seen=ids, queue=[(b, OLD) for b in ids])
    ctx = _run(tmp_path, monkeypatch, state_path=path, limits=(0.0, 2),
               statuses={b: (S.PENDING, "") for b in ids})
    assert ctx.enriched == ["PRC_0", "PRC_1"]    # 결정적 순서로 상한까지만
    assert len(ctx.queue) == 5                   # 나머지는 다음 실행으로(삭제 없음)


def test_interval_setting_defers_recent_attempts(tmp_path, monkeypatch):
    """detail_retry_interval_sec 가 실제 실행 경로에서 지켜지는지."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", JUST_NOW)])
    deferred = _run(tmp_path, monkeypatch, state_path=path, limits=(1800.0, 0),
                    statuses={"PRC_Q": (S.AVAILABLE, "본문")})
    assert deferred.enriched == []

    # interval 0 = 매 실행 허용(config 계약).
    allowed = _run(tmp_path, monkeypatch, state_path=path, limits=(0.0, 0),
                   statuses={"PRC_Q": (S.AVAILABLE, "본문")})
    assert allowed.enriched == ["PRC_Q"]


def test_cap_zero_processes_every_due_item(tmp_path, monkeypatch):
    ids = [f"PRC_{i}" for i in range(12)]
    path = _seed_state(tmp_path, seen=ids, queue=[(b, OLD) for b in ids])
    ctx = _run(tmp_path, monkeypatch, state_path=path, limits=(0.0, 0),
               statuses={b: (S.PENDING, "") for b in ids})
    assert len(ctx.enriched) == 12


def test_queue_is_never_silently_expired(tmp_path, monkeypatch):
    """§11 — 시도 횟수가 쌓여도 조용히 삭제하지 않는다."""
    path = _seed_state(tmp_path, seen=["PRC_Q"], queue=[("PRC_Q", OLD)])
    for _ in range(4):
        # 매번 오래된 시각으로 되돌려 계속 due 상태로 만든다.
        st = State(path)
        st.record_detail_attempt(ASSEMBLY_SOURCE_KEY, "PRC_Q", status="pending", now=OLD)
        st.save()
        ctx = _run(tmp_path, monkeypatch, state_path=path,
                   statuses={"PRC_Q": (S.PENDING, "")})
    assert set(ctx.queue) == {"PRC_Q"}
    assert ctx.queue["PRC_Q"]["attempts"] >= 4


# ==========================================================================
# §24. 과거 seen 의안을 추측으로 backfill 하지 않는다
# ==========================================================================
def test_historical_seen_bills_are_not_backfilled(tmp_path, monkeypatch):
    path = _seed_state(tmp_path, seen=[f"PRC_OLD{i}" for i in range(10)])
    ctx = _run(tmp_path, monkeypatch, state_path=path)
    assert ctx.queue == {}
    assert ctx.enriched == []


def test_baseline_run_does_not_queue_anything(tmp_path, monkeypatch):
    """§5 — 기준선으로 seen 처리된 과거 의안은 후속 알림 대상이 아니다."""
    path = tmp_path / "seen.json"                # 비어 있음 → 최초 실행(기준선)
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_A"), _bill("PRC_B")],
    )
    assert ctx.rc == 0
    assert ctx.digests == []                     # 기준선은 발송하지 않는다
    assert ctx.queue == {}
    assert ctx.enriched == []


# ==========================================================================
# §5. max_new_per_source 초과분은 이번 Phase 의 큐 대상이 아니다
# ==========================================================================
def test_overflow_items_are_not_queued(tmp_path, monkeypatch):
    from src import main as main_mod

    bills = [_bill(f"PRC_{i:03d}") for i in range(60)]
    path = _seed_state(tmp_path, seen=["seed"])
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=bills,
        statuses={b.post_id: (S.PENDING, "") for b in bills},
    )
    assert ctx.rc == 0
    # 상한(50)까지만 발송·상세수집되고, 그 50건만 큐에 들어간다.
    assert len(ctx.news) == 50
    assert len(ctx.queue) == 50
    assert len(ctx.seen) == 61                   # seed + 60건 전부 seen 처리(기존 동작)
    overflow = {b.post_id for b in bills[50:]}
    assert overflow.isdisjoint(set(ctx.queue))
