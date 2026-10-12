"""핫픽스 회귀 — 상세 재조회를 수백 회 반복하던 의안 3건의 영구 제외(2026-10-12, PR #36).

보호하는 계약:
  - 의안(assembly_bill) seen 은 상한 없이 누적한다. 다른 소스는 기존 5000건 상한 그대로.
  - excluded_detail_ids 에 등록된 의안은 seen 여부와 무관하게 상세 재조회·신규 재알림·
    큐 재등록이 일어나지 않는다. 등록은 State.exclude_detail() 로 큐 제거와 함께 한다.
  - 다른 의안의 신규 알림·큐 등록·재조회, 메일 실패 시 미확정 계약은 그대로다.

운영 state(state/seen.json)는 계속 바뀌므로 동작 검증은 격리된 state 로만 하고,
운영 state 는 '3건이 제외돼 있는지'만 별도 테스트로 확인한다.
실제 HTTP·SMTP·Gemini 는 부르지 않는다(대역은 test_phase2_orchestration 것을 재사용).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import ASSEMBLY_SOURCE_KEY, ProposalContentStatus as S
from src.state import EXCLUDED_DETAIL_KEY, MAX_PER_SOURCE, PENDING_DETAIL_KEY, State
from test_phase2_orchestration import OLD, PRESS_KEY, _bill, _run

REPO_STATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "seen.json"
)
STUCK = [
    "PRC_G2H6G0G8E0F5N1L4M4K6L2J5H5I0G1",
    "ARC_R2T6K0S9Y0Y3H1F1T1F3F2D6S8A5J1",
    "PRC_D2D6L0J8K2I8J1H3I4Q3Q0O4N2O3M9",
]


def _queue(st, bill_id, last):
    b = _bill(bill_id)
    st.queue_detail(
        ASSEMBLY_SOURCE_KEY, bill_id, title=b.title, url=b.url, date=b.date,
        status="pending", note="제안이유 미등록", now=last,
    )


def _seed(tmp_path, *, stuck_in_seen=True):
    """운영과 같은 모양의 격리 state.

    - STUCK 3건: 큐에 있던 상태에서 exclude_detail() 로 제외(seen 포함 여부는 인자)
    - PRC_KEEP:   무관한 기존 pending, due(오래전 시도) → 정상 재조회 대상
    - PRC_RECENT: 무관한 기존 pending, 방금 시도 → interval 로 이번엔 건너뜀
    """
    path = tmp_path / "seen.json"
    st = State(path)
    seen = ["PRC_KEEP", "PRC_RECENT", "PRC_OTHER"] + (STUCK if stuck_in_seen else [])
    st.mark_seen(ASSEMBLY_SOURCE_KEY, seen, baselined=True)
    st.mark_seen(PRESS_KEY, ["old"], baselined=True)
    for bill_id in STUCK + ["PRC_KEEP"]:
        _queue(st, bill_id, OLD)
    _queue(st, "PRC_RECENT", None)
    st.exclude_detail(ASSEMBLY_SOURCE_KEY, STUCK)
    st.save()
    return path


def _raw(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ==========================================================================
# 운영 state — 3건이 실제로 제외돼 있는지(동작 검증과 분리)
# ==========================================================================
def test_repo_state_excludes_stuck_bills():
    st = State(REPO_STATE)
    assert st.is_baselined(ASSEMBLY_SOURCE_KEY)
    assert set(STUCK) <= st.excluded_detail_ids(ASSEMBLY_SOURCE_KEY)
    raw_queue = _raw_repo_assembly().get(PENDING_DETAIL_KEY, {})
    for bill_id in STUCK:
        assert not st.is_new(ASSEMBLY_SOURCE_KEY, bill_id)   # 기존 seen 보존
        assert bill_id not in raw_queue                       # 파일에서도 큐 제거


def _raw_repo_assembly():
    with open(REPO_STATE, encoding="utf-8") as f:
        return json.load(f)[ASSEMBLY_SOURCE_KEY]


# ==========================================================================
# A/B. seen 보관 정책
# ==========================================================================
def test_A_assembly_seen_is_never_truncated(tmp_path):
    path = tmp_path / "seen.json"
    st = State(path)
    old = [f"OLD_{i}" for i in range(10)]
    st.mark_seen(ASSEMBLY_SOURCE_KEY, old, baselined=True)
    new = [f"NEW_{i:05d}" for i in range(MAX_PER_SOURCE + 1)]
    st.mark_seen(ASSEMBLY_SOURCE_KEY, new)
    st.save()

    seen = _raw(path)[ASSEMBLY_SOURCE_KEY]["seen"]
    assert seen == new + old                 # 기존 ID·순서 보존, 새 ID 는 앞쪽
    reloaded = State(path)
    assert all(not reloaded.is_new(ASSEMBLY_SOURCE_KEY, i) for i in old)


def test_B_other_sources_keep_5000_cap(tmp_path):
    st = State(tmp_path / "seen.json")
    st.mark_seen(PRESS_KEY, ["oldest"], baselined=True)
    st.mark_seen(PRESS_KEY, [f"P_{i}" for i in range(MAX_PER_SOURCE)])
    assert len(st.seen_ids(PRESS_KEY)) == MAX_PER_SOURCE
    assert st.is_new(PRESS_KEY, "oldest")    # 기존대로 가장 오래된 것부터 밀려난다


# ==========================================================================
# C/F/G. 제외 3건은 재조회되지 않고, 다른 의안은 기존대로
# ==========================================================================
def test_CFG_excluded_not_retried_others_unchanged(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    before = _raw(path)[ASSEMBLY_SOURCE_KEY]
    statuses = {i: (S.PENDING, "") for i in STUCK + ["PRC_KEEP", "PRC_RECENT", "PRC_NEW"]}

    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill(i) for i in STUCK] + [_bill("PRC_NEW")],
        statuses=statuses,
    )
    assert ctx.rc == 0
    # C. 제외 3건: 재조회 없음, 신규 아님, 큐 재등록 없음
    assert not set(STUCK) & set(ctx.enriched)
    assert not set(STUCK) & set(ctx.queue)
    assert not set(STUCK) & set(_raw(path)[ASSEMBLY_SOURCE_KEY].get(PENDING_DETAIL_KEY, {}))
    # F. 정상 신규 PENDING: 알림 후 큐 등록
    assert [p.post_id for p in ctx.news] == ["PRC_NEW"]
    assert ctx.queue["PRC_NEW"]["attempts"] == 1
    # G. 무관한 기존 pending: due 는 정상 재조회되어 큐 유지, 미도래는 그대로 보존
    assert "PRC_KEEP" in ctx.enriched
    assert ctx.queue["PRC_KEEP"]["attempts"] == before[PENDING_DETAIL_KEY]["PRC_KEEP"]["attempts"] + 1
    assert "PRC_RECENT" not in ctx.enriched
    assert ctx.queue["PRC_RECENT"] == before[PENDING_DETAIL_KEY]["PRC_RECENT"]
    # 제외 목록·seen 은 그대로
    after = _raw(path)[ASSEMBLY_SOURCE_KEY]
    assert after[EXCLUDED_DETAIL_KEY] == STUCK
    assert set(before["seen"]) <= set(after["seen"])


def test_C_excluded_id_left_in_queue_by_hand_is_not_retried(tmp_path, monkeypatch):
    """수동 편집 등으로 제외 ID 가 큐에 남아 있어도 재조회 대상이 아니다."""
    path = tmp_path / "seen.json"
    st = State(path)
    st.mark_seen(ASSEMBLY_SOURCE_KEY, list(STUCK), baselined=True)
    for bill_id in STUCK:
        _queue(st, bill_id, OLD)
    st.save()
    data = _raw(path)
    data[ASSEMBLY_SOURCE_KEY][EXCLUDED_DETAIL_KEY] = list(STUCK)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        statuses={i: (S.PENDING, "") for i in STUCK},
    )
    assert ctx.rc == 0
    assert ctx.enriched == []
    assert ctx.queue == {}


# ==========================================================================
# D. seen 에 없어도 재알림·재등록되지 않는다
# ==========================================================================
def test_D_excluded_but_not_seen_is_not_renotified(tmp_path, monkeypatch):
    path = _seed(tmp_path, stuck_in_seen=False)
    assert all(State(path).is_new(ASSEMBLY_SOURCE_KEY, i) for i in STUCK)

    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill(i) for i in STUCK],
        statuses={i: (S.PENDING, "") for i in STUCK},
    )
    assert ctx.rc == 0
    assert set(STUCK) <= ctx.assembly.collect_known
    assert not set(STUCK) & {p.post_id for p in ctx.news}
    assert not set(STUCK) & set(ctx.enriched)
    assert not set(STUCK) & set(ctx.queue)


# ==========================================================================
# E. queue_detail() 은 제외 ID 의 큐를 만들지 않는다
# ==========================================================================
def test_E_queue_detail_ignores_excluded_id(tmp_path):
    path = tmp_path / "seen.json"
    st = State(path)
    st.mark_seen(ASSEMBLY_SOURCE_KEY, [], baselined=True)
    st.exclude_detail(ASSEMBLY_SOURCE_KEY, [STUCK[0]])
    _queue(st, STUCK[0], OLD)
    _queue(st, "PRC_OK", OLD)                 # 다른 의안은 그대로 등록된다
    st.save()

    assert set(State(path).pending_detail(ASSEMBLY_SOURCE_KEY)) == {"PRC_OK"}
    assert set(_raw(path)[ASSEMBLY_SOURCE_KEY][PENDING_DETAIL_KEY]) == {"PRC_OK"}


def test_E_exclude_detail_removes_queue_and_is_idempotent(tmp_path):
    st = State(tmp_path / "seen.json")
    st.mark_seen(ASSEMBLY_SOURCE_KEY, list(STUCK), baselined=True)
    for bill_id in STUCK:
        _queue(st, bill_id, OLD)
    st.exclude_detail(ASSEMBLY_SOURCE_KEY, STUCK[:1])
    st.exclude_detail(ASSEMBLY_SOURCE_KEY, STUCK)          # 중복 등록은 무시, 순서 유지
    assert st.excluded_detail_ids(ASSEMBLY_SOURCE_KEY) == set(STUCK)
    assert st.pending_detail(ASSEMBLY_SOURCE_KEY) == {}
    assert st.seen_ids(ASSEMBLY_SOURCE_KEY) == set(STUCK)  # seen 은 건드리지 않는다


# ==========================================================================
# H. 저장·재로드 후에도 유지되고, 다음 실행에서도 그대로다
# ==========================================================================
def test_H_state_round_trip_and_second_run(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    first = _raw(path)
    State(path).save()                         # 재로드 → 재저장
    assert _raw(path) == first

    for _ in range(2):                         # 두 번째 실행 = 저장된 state 재로드 후
        ctx = _run(
            tmp_path, monkeypatch, state_path=path,
            new_assembly=[_bill(i) for i in STUCK],
            statuses={i: (S.PENDING, "") for i in STUCK + ["PRC_KEEP"]},
        )
        assert ctx.rc == 0
        assert not set(STUCK) & set(ctx.enriched)

    after = _raw(path)
    assert after[ASSEMBLY_SOURCE_KEY][EXCLUDED_DETAIL_KEY] == STUCK
    assert after[ASSEMBLY_SOURCE_KEY]["baselined"] is True
    assert after[PRESS_KEY] == first[PRESS_KEY]
    assert {"PRC_KEEP", "PRC_RECENT"} <= set(after[ASSEMBLY_SOURCE_KEY][PENDING_DETAIL_KEY])


# ==========================================================================
# I. 새 필드가 없는 기존 state 는 마이그레이션 없이 읽힌다
# ==========================================================================
def test_I_legacy_state_without_new_field(tmp_path):
    path = tmp_path / "seen.json"
    legacy = {
        ASSEMBLY_SOURCE_KEY: {
            "seen": ["PRC_A", "PRC_B"],
            "baselined": True,
            PENDING_DETAIL_KEY: {"PRC_A": {"title": "t", "attempts": 2}},
        },
        PRESS_KEY: {"seen": ["old"], "baselined": True},
    }
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    st = State(path)
    assert st.excluded_detail_ids(ASSEMBLY_SOURCE_KEY) == set()
    assert set(st.pending_detail(ASSEMBLY_SOURCE_KEY)) == {"PRC_A"}
    st.save()
    assert _raw(path) == legacy               # 쓰지 않은 새 키를 만들지 않는다


def test_I_malformed_exclusion_list_is_treated_as_empty(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({
        ASSEMBLY_SOURCE_KEY: {"seen": [], "baselined": True, EXCLUDED_DETAIL_KEY: "oops"},
    }), encoding="utf-8")
    st = State(path)
    assert st.excluded_detail_ids(ASSEMBLY_SOURCE_KEY) == set()
    st.exclude_detail(ASSEMBLY_SOURCE_KEY, ["PRC_X"])
    assert st.excluded_detail_ids(ASSEMBLY_SOURCE_KEY) == {"PRC_X"}


# ==========================================================================
# J. 메일 실패 시 신규 seen 확정·큐 등록 없음(기존 계약)
# ==========================================================================
def test_J_mail_failure_confirms_nothing(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    ctx = _run(
        tmp_path, monkeypatch, state_path=path,
        new_assembly=[_bill("PRC_NEW")],
        statuses={"PRC_NEW": (S.PENDING, ""), "PRC_KEEP": (S.PENDING, "")},
        send_error=RuntimeError("smtp down"),
    )
    assert ctx.rc == 1
    assert "PRC_NEW" not in ctx.seen
    assert "PRC_NEW" not in ctx.queue
    after = _raw(path)[ASSEMBLY_SOURCE_KEY]
    assert after[EXCLUDED_DETAIL_KEY] == STUCK
    assert not set(STUCK) & set(after.get(PENDING_DETAIL_KEY, {}))
