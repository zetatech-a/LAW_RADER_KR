"""핫픽스 회귀 — 상세 재조회를 수백 회 반복하던 의안 3건의 큐 영구 제외(2026-10-12).

조치는 state/seen.json 에서만 했다: 3건은 seen 에 그대로 두고 pending_detail 에서만
뺐다. 코드는 바꾸지 않았다. 재등록 경로가 _queue_new_pending() 하나뿐이고, 그 입력은
collect() 가 seen 에 없는 의안만 돌려준 결과이기 때문이다. 이 테스트는 그 전제가
실제 저장소 state 와 실제 main.run() 흐름에서 성립하는지 확인한다.

실제 HTTP·SMTP·Gemini 는 부르지 않는다(대역은 test_phase2_orchestration 것을 재사용).
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import ASSEMBLY_SOURCE_KEY, ProposalContentStatus as S
from src.state import PENDING_DETAIL_KEY, State
from test_phase2_orchestration import _bill, _run

REPO_STATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "seen.json"
)
STUCK = (
    "PRC_G2H6G0G8E0F5N1L4M4K6L2J5H5I0G1",
    "ARC_R2T6K0S9Y0Y3H1F1T1F3F2D6S8A5J1",
    "PRC_D2D6L0J8K2I8J1H3I4Q3Q0O4N2O3M9",
)


def test_repo_state_keeps_stuck_bills_seen_but_not_queued():
    st = State(REPO_STATE)
    assert st.is_baselined(ASSEMBLY_SOURCE_KEY)
    queue = st.pending_detail(ASSEMBLY_SOURCE_KEY)
    for bill_id in STUCK:
        assert not st.is_new(ASSEMBLY_SOURCE_KEY, bill_id)
        assert bill_id not in queue


def test_stuck_bills_are_not_requeued_but_other_bills_still_are(tmp_path, monkeypatch):
    """실제 state 사본으로 두 번 실행해도 3건은 재조회·재등록되지 않는다.

    목록에 3건이 다시 나타나고 상세가 여전히 PENDING 이어도 신규로 잡히지 않아야 하며,
    같은 실행의 다른 신규 PENDING 의안은 기존처럼 알림 후 큐에 등록되어야 한다.
    """
    path = tmp_path / "seen.json"
    shutil.copyfile(REPO_STATE, path)
    before = json.loads(path.read_text(encoding="utf-8"))

    statuses = {bill_id: (S.PENDING, "") for bill_id in STUCK}
    statuses["PRC_NEW"] = (S.PENDING, "")
    listed = [_bill(bill_id) for bill_id in STUCK] + [_bill("PRC_NEW")]

    for _ in range(2):  # 두 번째 실행 = 저장된 state 를 다시 로드한 뒤의 동작
        ctx = _run(
            tmp_path, monkeypatch, state_path=path,
            new_assembly=listed, statuses=statuses,
        )
        assert ctx.rc == 0
        assert not set(STUCK) & set(ctx.enriched)
        assert not set(STUCK) & set(ctx.queue)
        assert set(STUCK) <= ctx.seen

    # 정상 의안의 신규 감지·큐 등록은 그대로다(첫 실행에서 한 번만 알림).
    assert set(ctx.queue) == {"PRC_NEW"}
    assert "PRC_NEW" in ctx.seen

    # 의안 외 소스 상태는 전혀 바뀌지 않는다.
    after = json.loads(path.read_text(encoding="utf-8"))
    assert set(after) == set(before)
    for key in before:
        if key == ASSEMBLY_SOURCE_KEY:
            continue
        assert after[key] == before[key]
    assembly_after = after[ASSEMBLY_SOURCE_KEY]
    assert assembly_after["baselined"] is True
    assert set(before[ASSEMBLY_SOURCE_KEY]["seen"]) <= set(assembly_after["seen"])
    assert set(assembly_after[PENDING_DETAIL_KEY]) == {"PRC_NEW"}
