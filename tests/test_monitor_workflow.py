"""monitor 워크플로가 Gemini 모델 설정을 실제로 프로세스에 넘기는지 구조 검증.

`MODEL` 은 GitHub repository Variable(Settings → Secrets and variables → Actions →
Variables)이다. Variable 은 자동으로 프로세스 환경에 들어오지 않는다 — 워크플로가
명시적으로 env 에 넣어 줘야 한다. 이 연결이 빠져 있어서 운영자가 Variable 을 바꿔도
코드는 계속 config.yaml 의 모델을 불렀다(실제 장애). YAML 구조라 실행 없이 확인할 수
있으므로 여기서 못박는다.
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "monitor.yml"


@pytest.fixture(scope="module")
def steps():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["monitor"]["steps"]


@pytest.fixture(scope="module")
def run_step(steps):
    for s in steps:
        if str(s.get("run", "")).strip() == "python -m src.main":
            return s
    raise AssertionError("모니터 실행 단계(python -m src.main)를 찾지 못했습니다")


def test_model_variable_is_passed_as_gemini_model(run_step):
    env = run_step.get("env") or {}
    assert env.get("GEMINI_MODEL") == "${{ vars.MODEL }}", (
        "repository Variable MODEL 이 GEMINI_MODEL 로 전달되어야 합니다 — "
        "그러지 않으면 Variable 을 바꿔도 코드가 config.yaml 모델을 계속 부릅니다."
    )


def test_gemini_api_key_is_still_passed(run_step):
    # 모델 연결을 추가하면서 기존 주입이 깨지지 않았는지.
    env = run_step.get("env") or {}
    assert env.get("GEMINI_API_KEY") == "${{ secrets.GEMINI_API_KEY }}"


def test_model_is_a_variable_not_a_secret(run_step):
    """모델명은 비밀이 아니다 — secrets 로 넣으면 로그에서 마스킹되어 진단이 어려워진다."""
    raw = str((run_step.get("env") or {}).get("GEMINI_MODEL", ""))
    assert "vars." in raw and "secrets." not in raw


def test_no_gemini_model_name_is_hardcoded_in_the_workflow():
    """모델명 문자열을 워크플로에 박아 두면 Variable 이 source of truth 가 아니게 된다."""
    raw = WORKFLOW.read_text(encoding="utf-8")
    # 주석의 설명 문구까지 걸리지 않도록 'gemini-<무언가>' 형태만 본다.
    import re

    hits = re.findall(r"gemini-[a-z0-9.\-]+", raw, flags=re.IGNORECASE)
    assert hits == [], f"워크플로에 모델명이 하드코딩되어 있습니다: {hits}"


def test_state_is_still_committed_always(steps):
    """장애 격리 변경이 기존 state 커밋 단계를 건드리지 않았는지."""
    commit = [s for s in steps if s.get("name") == "Commit updated state"]
    assert len(commit) == 1
    assert commit[0].get("if") == "always()"


# ── state 원격 반영(2026-08-26 중복 메일 장애) ──────────────────────────────
@pytest.fixture(scope="module")
def commit_step(steps):
    for s in steps:
        if s.get("name") == "Commit updated state":
            return s
    raise AssertionError("'Commit updated state' 단계를 찾지 못했습니다")


def test_state_push_goes_through_the_bounded_retry_helper(commit_step):
    """맨 `git push` 한 번으로 돌아가면 장애가 그대로 재발한다."""
    run = str(commit_step.get("run", ""))
    assert "scripts/push_state.py" in run
    import re

    # `git push` 를 직접 부르는 줄이 남아 있으면 안 된다(헬퍼 호출만 허용).
    bare_push = [
        ln.strip()
        for ln in run.splitlines()
        if re.match(r"^\s*git\s+push\b", ln)
    ]
    assert bare_push == [], f"헬퍼를 우회하는 git push 가 남아 있습니다: {bare_push}"


def test_state_commit_is_still_created_exactly_once(commit_step):
    run = str(commit_step.get("run", ""))
    assert run.count("git commit") == 1
    assert "git add state/" in run


def test_durable_success_is_logged_only_after_the_helper_succeeds(commit_step):
    """'state remote push 완료' 는 헬퍼 호출 **뒤**에 있어야 한다."""
    run = str(commit_step.get("run", ""))
    assert "state remote push 완료" in run
    assert run.index("scripts/push_state.py") < run.index(
        'echo "state remote push 완료"'
    )


def test_workflow_never_force_pushes_state():
    raw = WORKFLOW.read_text(encoding="utf-8")
    for bad in ("--force", "--force-with-lease", "reset --hard"):
        assert bad not in raw, f"워크플로에 금지된 조작이 있습니다: {bad}"


def test_monitor_concurrency_protection_is_unchanged():
    """cancel-in-progress:false 가 유지되어야 실행이 서로를 자르지 않는다."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    concurrency = doc["concurrency"]
    assert concurrency["group"] == "law-rader-monitor"
    assert concurrency["cancel-in-progress"] is False
