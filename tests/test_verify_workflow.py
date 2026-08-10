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


# --- 소스 검증 게이트: 의안 예외가 다른 소스의 실패를 삼키면 안 된다 ---
#
# 문자열이 아니라 스크립트를 **실제로 실행**해 판정을 확인한다. 이 게이트의 위험은
# 문구가 빠지는 것이 아니라 판정이 틀리는 것이기 때문이다.


def _source_gate(steps) -> str:
    return steps[
        _index(steps, lambda s: s.get("name") == "Gate on source verification")
    ]["run"]


def _report(*, n_fail: int, n_total: int, partials: list[str]) -> str:
    """verify_sources.py 요약부와 같은 모양의 리포트."""
    body = [
        "=" * 70,
        "요약",
        "=" * 70,
        "  🟠 assembly_bill        목록 10건  [제안이유 available 0 / pending 3 / failed 0]",
        "",
        f"실패(수정 필요) 소스: {n_fail}/{n_total}",
    ]
    if partials:
        body.append(f"부분 실패(목록 성공 / 상세 실패) 소스: {len(partials)}/{n_total}")
        body.extend(partials)
    return "\n".join(body) + "\n"


_ASSEMBLY_PENDING = (
    "  🟠 assembly_bill: 목록 수집 성공(10건) / 표본 3건이 모두 등록 대기 — "
    "제안이유 추출 자체는 확인하지 못했습니다."
)
_OTHER_PARTIAL = "  🟠 fsc_press: 목록 수집 성공(10건) / 상세 본문 0자"


def _run_gate(steps, tmp_path, report: str, *, verify: str, available: str) -> int:
    import subprocess

    (tmp_path / "verify_report.txt").write_text(report, encoding="utf-8")
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
         _source_gate(steps)],
        cwd=tmp_path,
        env={**os.environ, "VERIFY": verify, "AVAILABLE": available},
        capture_output=True,
        text=True,
    )
    return proc.returncode


def test_source_gate_passes_when_verify_succeeded(steps, tmp_path):
    rc = _run_gate(
        steps, tmp_path, _report(n_fail=0, n_total=2, partials=[]),
        verify="success", available="success",
    )
    assert rc == 0


def test_source_gate_allows_assembly_only_pending(steps, tmp_path):
    """표본이 전부 등록 대기이고 FAIL 소스가 없으면 통과(원래 의도한 예외)."""
    rc = _run_gate(
        steps, tmp_path,
        _report(n_fail=0, n_total=2, partials=[_ASSEMBLY_PENDING]),
        verify="failure", available="success",
    )
    assert rc == 0


def test_source_gate_fails_when_another_source_failed(steps, tmp_path):
    """핵심 회귀: 무관한 소스의 FAIL 이 의안의 '모두 등록 대기' 문구에 가리면 안 된다."""
    rc = _run_gate(
        steps, tmp_path,
        _report(n_fail=1, n_total=2, partials=[_ASSEMBLY_PENDING]),
        verify="failure", available="success",
    )
    assert rc == 1


def test_source_gate_fails_when_another_source_is_partial(steps, tmp_path):
    """의안 외의 부분 실패도 예외 대상이 아니다."""
    rc = _run_gate(
        steps, tmp_path,
        _report(n_fail=0, n_total=2, partials=[_ASSEMBLY_PENDING, _OTHER_PARTIAL]),
        verify="failure", available="success",
    )
    assert rc == 1


def test_source_gate_fails_without_explicit_available_check(steps, tmp_path):
    """available 검증이 없으면 '추출이 되긴 하는지'를 확인하지 못한 것이다."""
    rc = _run_gate(
        steps, tmp_path,
        _report(n_fail=0, n_total=2, partials=[_ASSEMBLY_PENDING]),
        verify="failure", available="failure",
    )
    assert rc == 1


def test_source_gate_fails_when_assembly_collection_failed(steps, tmp_path):
    """failed>0 이면 assembly 는 FAIL 로 집계되므로 예외가 적용되지 않는다."""
    rc = _run_gate(
        steps, tmp_path, _report(n_fail=1, n_total=2, partials=[]),
        verify="failure", available="success",
    )
    assert rc == 1


def test_source_gate_fails_on_missing_report(steps, tmp_path):
    """리포트 자체가 없으면(단계가 죽었으면) 통과시키지 않는다."""
    import subprocess

    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
         _source_gate(steps)],
        cwd=tmp_path,
        env={**os.environ, "VERIFY": "failure", "AVAILABLE": "success"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1


# --- 리포트 형식 계약: verify_sources.py 출력과 게이트 패턴이 어긋나면 안 된다 ---


def _real_report(monkeypatch, capsys, statuses: dict[str, str]) -> str:
    """verify_sources.main() 을 실제로 돌려 요약부까지 찍힌 리포트를 받는다.

    게이트는 이 출력의 모양(요약 집계 줄, '  🟠 키: 사유' 목록)에 의존한다. 형식이
    바뀌면 게이트가 조용히 오작동하므로, 문자열을 손으로 흉내 내지 않고 진짜로 찍는다.
    """
    import scripts.verify_sources as vs

    class _Src:
        def __init__(self, key):
            self.key = key
            self.name = f"{key} 소스"
            self.enabled = True

    class _Cfg:
        sources = [_Src(k) for k in statuses]

        class fetch:
            timeout_sec = 1
            delay_sec = 0
            list_limit = 10

    details = {
        vs.PARTIAL: "목록 수집 성공(10건) / 표본 3건이 모두 등록 대기 — 재확인 필요",
        vs.FAIL: "목록 수집 성공(10건) / 제안이유 수집 실패 2건 — 구조 확인 필요",
    }
    keys = list(statuses)
    idx = {"i": 0}

    def _verify_source(scraper, limit, enrich, sample):
        key = keys[idx["i"]]
        idx["i"] += 1
        report = {"key": key, "status": statuses[key], "page1_count": 10}
        if statuses[key] in details:
            report["detail"] = details[statuses[key]]
        return report, []

    monkeypatch.setattr(vs, "load_config", lambda path: _Cfg())
    monkeypatch.setattr(vs, "Fetcher", lambda **kw: None)
    monkeypatch.setattr(vs, "build_scraper", lambda src, fetcher: None)
    monkeypatch.setattr(vs, "verify_source", _verify_source)

    vs.main([])
    return capsys.readouterr().out


def test_gate_matches_real_report_when_only_assembly_is_pending(
    steps, tmp_path, monkeypatch, capsys
):
    report = _real_report(
        monkeypatch, capsys,
        {"assembly_bill": "🟠", "fsc_press": "✅"},
    )
    assert _run_gate(steps, tmp_path, report, verify="failure", available="success") == 0


def test_gate_matches_real_report_when_another_source_failed(
    steps, tmp_path, monkeypatch, capsys
):
    """핵심 회귀를 실제 출력으로 확인한다."""
    report = _real_report(
        monkeypatch, capsys,
        {"assembly_bill": "🟠", "fsc_press": "❌"},
    )
    assert _run_gate(steps, tmp_path, report, verify="failure", available="success") == 1


def test_gate_matches_real_report_when_another_source_is_partial(
    steps, tmp_path, monkeypatch, capsys
):
    report = _real_report(
        monkeypatch, capsys,
        {"assembly_bill": "🟠", "fsc_press": "🟠"},
    )
    assert _run_gate(steps, tmp_path, report, verify="failure", available="success") == 1


# --- 파이프라인 실패가 게이트까지 전달되는지 ---
#
# `python ... | tee report.txt` 는 pipefail 이 없으면 tee 의 0 을 돌려주어, 생산자가
# 죽어도 단계 outcome 이 success 가 되고 게이트가 false-green 이 된다.
#
# GitHub Actions 의 `shell: bash` 기본값은 `bash --noprofile --norc -e -o pipefail {0}`
# 라 pipefail 이 **이미 켜져 있다**(2026-08-10 라이브 실행 로그로 확인). 다만 그건
# 암묵적 의존이므로, 셸을 약한 형태로 바꾸면 조용히 깨진다. 여기서 못박는다.


def _piped_steps(steps):
    return [s for s in steps if "| tee " in (s.get("run") or "")]


def test_piped_steps_exist(steps):
    """전제 확인 — 파이프라인을 쓰는 단계가 실제로 있다."""
    assert len(_piped_steps(steps)) >= 4


def test_piped_steps_use_pipefail_shell(steps):
    """`shell: bash` 여야 한다. `bash -e {0}` 등으로 바꾸면 pipefail 이 사라진다.

    GitHub Actions 는 `shell: bash` 일 때만 -e -o pipefail 을 붙인다. 명시적 인자를
    준 커스텀 셸(`bash -e {0}`, `bash {0}`)이나 `sh` 는 pipefail 이 없다.
    """
    for step in _piped_steps(steps):
        shell = step.get("shell")
        name = step.get("name") or step.get("id") or "(이름 없음)"
        assert shell == "bash", f"{name}: shell={shell!r} 는 pipefail 을 보장하지 않는다"


def test_no_step_overrides_shell_to_a_weaker_form(steps):
    """어떤 단계도 pipefail 없는 셸로 내려가면 안 된다."""
    for step in steps:
        shell = step.get("shell")
        if shell is None:
            continue
        assert shell == "bash", f"예상치 못한 shell: {shell!r}"


def test_pipefail_actually_propagates_producer_failure(tmp_path):
    """문서가 아니라 동작으로 확인 — 실패한 생산자가 tee 를 지나 종료코드를 남긴다."""
    import subprocess

    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
         "python3 -c 'import sys; sys.exit(3)' | tee out.txt"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc.returncode == 3, "pipefail 셸에서 생산자 실패가 전달되지 않았다"

    # 반대로 pipefail 이 없으면 0 이 된다 — 이것이 막으려는 상황이다.
    proc2 = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c",
         "python3 -c 'import sys; sys.exit(3)' | tee out.txt"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc2.returncode == 0
