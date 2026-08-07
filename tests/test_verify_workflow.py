"""Verify 워크플로의 false-green 방지 구조 회귀 테스트.

라이브 워크플로는 진단 자료를 남기려고 대부분의 단계를 continue-on-error 로 둔다.
그러면 단계가 실패해도 job 은 초록불이 될 수 있다(false-green). 그래서 마지막 게이트가
각 단계의 outcome 을 **반드시** 확인해야 하는데, 이건 YAML 구조라 실행 없이도 검증할 수
있다. 여기서는 그 구조를 못박는다.
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "verify.yml"


@pytest.fixture(scope="module")
def steps():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["verify"]["steps"]


@pytest.fixture(scope="module")
def by_id(steps):
    return {s["id"]: s for s in steps if "id" in s}


@pytest.fixture(scope="module")
def raw():
    return WORKFLOW.read_text(encoding="utf-8")


def _index(steps, predicate):
    for i, s in enumerate(steps):
        if predicate(s):
            return i
    raise AssertionError("해당 단계를 찾지 못했습니다")


# --- 단계 식별자 ---


@pytest.mark.parametrize(
    "step_id",
    ["verify", "netcapture", "capture", "capturepending", "pendingcheck",
     "availablecheck", "fixturetest", "pytest"],
)
def test_diagnostic_steps_have_ids(by_id, step_id):
    """게이트가 outcome 을 참조하려면 id 가 있어야 한다."""
    assert step_id in by_id


@pytest.mark.parametrize(
    "step_id",
    ["verify", "netcapture", "capture", "capturepending", "pendingcheck",
     "availablecheck", "fixturetest", "pytest"],
)
def test_diagnostic_steps_continue_on_error(by_id, step_id):
    """실패해도 아티팩트 업로드까지 진행해야 한다 — 대신 게이트가 잡는다."""
    assert by_id[step_id].get("continue-on-error") is True


# --- 아티팩트가 게이트보다 먼저 ---


def test_artifact_upload_runs_always_and_before_gates(steps):
    upload = _index(steps, lambda s: (s.get("name") or "").startswith("Upload results"))
    assert steps[upload]["if"] == "always()"
    gates = [
        i for i, s in enumerate(steps) if (s.get("name") or "").startswith("Gate on")
    ]
    assert gates, "게이트 단계가 없습니다"
    assert upload < min(gates), "아티팩트 업로드가 게이트보다 뒤에 있으면 실패 시 자료를 잃는다"


def test_all_diagnostic_reports_are_uploaded(steps):
    upload = steps[
        _index(steps, lambda s: (s.get("name") or "").startswith("Upload results"))
    ]
    paths = upload["with"]["path"]
    for name in (
        "verify_report.txt",
        "network_capture_report.txt",
        "capture_report.txt",
        "capture_pending_report.txt",
        "available_check_report.txt",
        "pending_check_report.txt",
        "fixture_test_report.txt",
        "pytest_report.txt",
        "tests/fixtures/",
    ):
        assert name in paths, name


# --- capture mode 종합 게이트 ---


def test_capture_gate_checks_all_four_outcomes(steps):
    """production AVAILABLE / Playwright / requests capture / fixture replay 전부."""
    gate = steps[
        _index(steps, lambda s: s.get("name") == "Gate on capture mode results")
    ]
    env = gate["env"]
    assert env["AVAILABLE"] == "${{ steps.availablecheck.outcome }}"
    assert env["NET"] == "${{ steps.netcapture.outcome }}"
    assert env["CAPTURE"] == "${{ steps.capture.outcome }}"
    assert env["FIXTURETEST"] == "${{ steps.fixturetest.outcome }}"

    run = gate["run"]
    # 넷 다 success 인지 확인하고, 아니면 exit
    for var in ("AVAILABLE", "NET", "CAPTURE", "FIXTURETEST"):
        assert f'"${var}"' in run and "success" in run, var
    assert "exit 2" in run


def test_requests_capture_failure_fails_the_gate(steps):
    """requests fixture capture 실패가 게이트를 실패시켜야 한다."""
    gate = steps[
        _index(steps, lambda s: s.get("name") == "Gate on capture mode results")
    ]
    assert gate["env"]["CAPTURE"] == "${{ steps.capture.outcome }}"
    assert 'requests fixture 캡처 실패' in gate["run"]


def test_fixture_replay_failure_fails_the_gate(steps):
    """fixture replay 테스트 실패(또는 skip 잔존)가 게이트를 실패시켜야 한다."""
    gate = steps[
        _index(steps, lambda s: s.get("name") == "Gate on capture mode results")
    ]
    assert gate["env"]["FIXTURETEST"] == "${{ steps.fixturetest.outcome }}"
    assert "fixture 재생 테스트 실패" in gate["run"]


def test_fixture_test_fails_when_skips_remain(by_id):
    """캡처했는데도 skip 이 남으면 fixture 가 안 만들어진 것 — 실패시켜야 한다."""
    run = by_id["fixturetest"]["run"]
    assert 'grep -q "skipped"' in run
    assert "exit 1" in run


# --- 전체 pytest ---


def test_full_pytest_step_writes_report(by_id):
    run = by_id["pytest"]["run"]
    assert "python -m pytest -q" in run
    assert "tee pytest_report.txt" in run


def test_pytest_gate_checks_outcome(steps):
    gate = steps[_index(steps, lambda s: s.get("name") == "Gate on full pytest")]
    assert gate["env"]["PYTEST"] == "${{ steps.pytest.outcome }}"
    assert "exit 1" in gate["run"]


# --- pending 게이트 ---


def test_pending_gate_checks_both_check_and_capture(steps):
    gate = steps[
        _index(steps, lambda s: s.get("name") == "Gate on PENDING classification")
    ]
    assert gate["env"]["PEND"] == "${{ steps.pendingcheck.outcome }}"
    assert gate["env"]["PENDCAP"] == "${{ steps.capturepending.outcome }}"


# --- 캡처 단계가 --expect 를 쓰는지 ---


def test_available_capture_uses_expect_available(by_id):
    assert "--expect available" in by_id["capture"]["run"]


def test_pending_capture_uses_expect_pending(by_id):
    assert "--expect pending" in by_id["capturepending"]["run"]


def test_pending_capture_only_runs_with_pending_bill_id(by_id):
    """available fixture 를 덮어쓰지 않도록, pending 캡처는 별도 입력이 있을 때만 돈다."""
    assert "inputs.pending_bill_id != ''" in by_id["capturepending"]["if"]


# --- 입력 주입 방지 ---


def test_inputs_are_passed_through_env_not_interpolated(by_id):
    """run 블록에 ${{ inputs.* }} 를 직접 끼우면 스크립트 인젝션 경로가 된다."""
    for step_id, step in by_id.items():
        run = step.get("run", "")
        assert "${{ inputs." not in run, step_id
        assert "${{ github.event.inputs." not in run, step_id
