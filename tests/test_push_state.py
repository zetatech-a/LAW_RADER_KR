"""state 커밋의 원격 반영(scripts/push_state.py) 회귀 테스트.

이 코드가 존재하는 이유는 운영 장애다: 메일은 나갔고 러너 안에서 state 도 저장됐는데
마지막 `git push` 가 `remote: fatal error in commit_refs` 로 한 번 거부되자 원격 state 가
옛날 것으로 남았고, 다음 실행이 같은 게시글을 '신규'로 다시 판정해 중복 메일을 보냈다.

여기서는 git 프로세스 경계만 스텁으로 대체한다. **네트워크도 GitHub 도 건드리지
않는다** — 따라서 이 테스트가 통과했다고 실제 GitHub push 재시도가 검증된 것은 아니다.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import push_state  # noqa: E402
from scripts.push_state import (  # noqa: E402
    EXIT_EXHAUSTED,
    EXIT_FATAL,
    EXIT_OK,
    EXIT_REMOTE_ADVANCED,
    GIT_COMMAND_TIMEOUT_SEC,
    GIT_TIMEOUT_RETURNCODE,
    GitResult,
    StatePusher,
    _failure_reason,
    _is_forbidden_option,
    _subprocess_git,
    redact,
)

BASE = "a" * 40
LOCAL = "b" * 40
OTHER = "c" * 40
BRANCH = "main"

OK = GitResult(0)
# 운영에서 실제로 관측된 거부 형태.
COMMIT_REFS_FAILURE = GitResult(
    1,
    stderr=(
        "remote: fatal error in commit_refs\n"
        "To https://github.com/zetatech-a/LAW_RADER_KR\n"
        " ! [remote rejected] main -> main (failure)\n"
        "error: failed to push some refs to "
        "'https://github.com/zetatech-a/LAW_RADER_KR'\n"
    ),
)
# 클라이언트가 응답을 기다리다 끊긴 경우 — '실패 확정'이 아니라 '결과 미상'이다.
PUSH_TIMEOUT = GitResult(
    GIT_TIMEOUT_RETURNCODE, stderr="error: git push timed out after 20s"
)
LS_REMOTE_TIMEOUT = GitResult(
    GIT_TIMEOUT_RETURNCODE, stderr="error: git ls-remote timed out after 20s"
)
LS_REMOTE_FAIL = GitResult(128, stderr="fatal: unable to access ...: 500\n")


class FakeGit:
    """git 호출을 기록하고 미리 정한 응답을 돌려주는 스텁."""

    def __init__(self, *, push_results, ls_remote_results):
        self.push_results = list(push_results)
        self.ls_remote_results = list(ls_remote_results)
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "push":
            return self._next(self.push_results, "push")
        if args[0] == "ls-remote":
            return self._next(self.ls_remote_results, "ls-remote")
        raise AssertionError(f"예상하지 못한 git 호출: {args}")

    @staticmethod
    def _next(queue, what):
        if not queue:
            raise AssertionError(f"{what} 가 예상보다 많이 호출되었습니다")
        item = queue.pop(0)
        return item() if callable(item) else item

    @property
    def pushes(self):
        return [c for c in self.calls if c[0] == "push"]

    @property
    def ls_remotes(self):
        return [c for c in self.calls if c[0] == "ls-remote"]

    @property
    def kinds(self):
        return [c[0] for c in self.calls]


def ls_remote_ok(sha):
    return GitResult(0, stdout=f"{sha}\trefs/heads/{BRANCH}\n")


LS_REMOTE_NO_REF = GitResult(0, stdout="")


def make(git, sleeps, **kwargs):
    return StatePusher(
        remote="origin",
        branch=BRANCH,
        base_sha=BASE,
        local_sha=LOCAL,
        max_attempts=kwargs.pop("max_attempts", 4),
        backoff_base_sec=kwargs.pop("backoff_base_sec", 3.0),
        backoff_max_sec=kwargs.pop("backoff_max_sec", 15.0),
        ls_remote_attempts=kwargs.pop("ls_remote_attempts", 3),
        ls_remote_backoff_sec=kwargs.pop("ls_remote_backoff_sec", 2.0),
        run=git,
        sleep=sleeps.append,
        log=kwargs.pop("log", lambda _m: None),
        **kwargs,
    )


# ── 목적지 브랜치 preflight (첫 push 전 확인) ───────────────────────────────
#
# preflight 가 없으면, 브랜치가 삭제됐거나 --branch 가 틀렸을 때 평범한 push 가 ref 를
# **만들고 성공**한다. 그러면 push 실패 후에만 도는 `remote == ""` 가드에 영영 닿지
# 않는다 — 헬퍼가 '있는 브랜치를 전진시킨다'는 계약을 스스로 깨는 경로다.
def test_pre1_expected_base_allows_the_first_push():
    git = FakeGit(push_results=[OK], ls_remote_results=[ls_remote_ok(BASE)])
    sleeps = []

    rc = make(git, sleeps).run()

    assert rc == EXIT_OK
    assert git.kinds == ["ls-remote", "push"]   # 확인이 push 보다 **먼저**다
    assert sleeps == []


def test_pre2_remote_already_at_local_sha_is_success_without_pushing():
    git = FakeGit(push_results=[], ls_remote_results=[ls_remote_ok(LOCAL)])
    logs = []

    rc = make(git, [], log=logs.append).run()

    assert rc == EXIT_OK
    assert git.pushes == []                     # 밀 것이 없다
    assert any("nothing to push" in m for m in logs)
    assert any("state remote push confirmed" in m for m in logs)


def test_pre3_missing_destination_branch_is_never_created():
    git = FakeGit(push_results=[], ls_remote_results=[LS_REMOTE_NO_REF])
    logs = []

    rc = make(git, [], log=logs.append).run()

    assert rc == EXIT_REMOTE_ADVANCED
    assert rc != EXIT_OK
    assert git.pushes == []                     # ref 를 만들지 않는다
    assert any("refusing to create it" in m for m in logs)
    assert_no_destructive_git(git)


def test_pre4_advanced_destination_receives_no_first_push():
    git = FakeGit(push_results=[], ls_remote_results=[ls_remote_ok(OTHER)])
    logs = []

    rc = make(git, [], log=logs.append).run()

    assert rc == EXIT_REMOTE_ADVANCED
    assert git.pushes == []
    assert any("remote branch advanced" in m for m in logs)
    assert_no_destructive_git(git)


@pytest.mark.parametrize("lookup_result", [LS_REMOTE_FAIL, LS_REMOTE_TIMEOUT])
def test_pre5_unknown_destination_fails_closed_within_bounds(lookup_result):
    git = FakeGit(push_results=[], ls_remote_results=[lookup_result] * 3)
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_EXHAUSTED
    assert rc != EXIT_OK
    assert git.pushes == []                     # 모르는 상태로는 밀지 않는다
    assert len(git.ls_remotes) == 3             # 조회 재시도도 bounded 다
    assert sleeps == [2.0, 2.0]
    assert any("destination state unknown" in m for m in logs)
    assert not any("confirmed" in m for m in logs)


def test_preflight_does_not_replace_the_post_failure_verification():
    """preflight 통과 후에도 경쟁이 있을 수 있다 — 두 번째 확인은 남아 있어야 한다."""
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE],
        ls_remote_results=[ls_remote_ok(BASE), ls_remote_ok(LOCAL)],
    )
    logs = []

    rc = make(git, [], log=logs.append).run()

    assert rc == EXIT_OK
    assert git.kinds == ["ls-remote", "push", "ls-remote"]
    assert any("remote already at the pushed commit" in m for m in logs)


# ── PUSH1 — 첫 push 성공 ────────────────────────────────────────────────────
def test_push1_immediate_success_is_a_single_attempt_without_sleeping():
    git = FakeGit(push_results=[OK], ls_remote_results=[ls_remote_ok(BASE)])
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_OK
    assert len(git.pushes) == 1
    assert len(git.ls_remotes) == 1      # preflight 1회뿐 — 성공 후 재조회 없음
    assert sleeps == []                  # 잠들지 않는다
    assert "state remote push confirmed" in logs


# ── PUSH2 — 일시적 실패 뒤 성공(운영 장애 재현 클래스) ──────────────────────
def test_push2_transient_failure_then_success():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE, OK],
        ls_remote_results=[ls_remote_ok(BASE), ls_remote_ok(BASE)],
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_OK
    assert len(git.pushes) == 2
    assert sleeps == [3.0]               # 재시도 전에 backoff 한다
    assert any("attempt 1/4 failed" in m for m in logs)
    assert any("remote still at expected base" in m for m in logs)
    assert any("state remote push confirmed" in m for m in logs)


# ── PUSH3 — 클라이언트는 실패, 원격은 이미 우리 커밋 ────────────────────────
def test_push3_ambiguous_failure_with_remote_already_updated_is_durable_success():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE],
        ls_remote_results=[ls_remote_ok(BASE), ls_remote_ok(LOCAL)],
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_OK
    assert len(git.pushes) == 1          # 같은 커밋을 또 밀지 않는다
    assert sleeps == []
    assert any("remote already at the pushed commit" in m for m in logs)


# ── PUSH4 — 원격이 push 직후 독립적으로 전진(preflight 이후의 경쟁) ─────────
def test_push4_remote_advanced_aborts_immediately_without_force():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE],
        ls_remote_results=[ls_remote_ok(BASE), ls_remote_ok(OTHER)],
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_REMOTE_ADVANCED
    assert rc != EXIT_OK
    assert len(git.pushes) == 1          # 재시도하지 않는다
    assert sleeps == []
    assert any("remote branch advanced" in m for m in logs)
    assert_no_destructive_git(git)


def test_remote_branch_deleted_between_preflight_and_push_is_refused():
    """rc=0 인데 ref 가 없다(브랜치 삭제) — 조회 실패와 구분해 실패로 끝낸다."""
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE],
        ls_remote_results=[ls_remote_ok(BASE), LS_REMOTE_NO_REF],
    )
    logs = []

    rc = make(git, [], log=logs.append).run()

    assert rc == EXIT_REMOTE_ADVANCED
    assert len(git.pushes) == 1
    assert any("not found" in m for m in logs)


# ── PUSH5 — 계속 실패 ───────────────────────────────────────────────────────
def test_push5_repeated_transient_failure_is_bounded_and_fails_nonzero():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE] * 4,
        ls_remote_results=[ls_remote_ok(BASE)] * 5,   # preflight 1 + 실패 후 4
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_EXHAUSTED
    assert rc != EXIT_OK
    assert len(git.pushes) == 4                 # 상한을 넘지 않는다
    assert sleeps == [3.0, 6.0, 12.0]           # 마지막 시도 뒤에는 자지 않는다
    assert sum(sleeps) < 60                     # '분 단위 무한 대기'가 아니다
    assert any("state push exhausted" in m for m in logs)
    assert not any("confirmed" in m for m in logs)


def test_backoff_is_capped():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE] * 5,
        ls_remote_results=[ls_remote_ok(BASE)] * 6,
    )
    sleeps = []

    rc = make(git, sleeps, max_attempts=5, backoff_base_sec=8.0, backoff_max_sec=15.0).run()

    assert rc == EXIT_EXHAUSTED
    assert sleeps == [8.0, 15.0, 15.0, 15.0]    # 지수 증가가 상한에서 멈춘다


# ── PUSH6 — 원격 ref 조회 자체가 실패 ───────────────────────────────────────
def test_push6_ls_remote_failure_never_reports_false_success():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE] * 2,
        # preflight 1회 성공 + 시도마다 자체 상한(3회)까지 실패 = 1 + 3 + 3
        ls_remote_results=[ls_remote_ok(BASE)] + [LS_REMOTE_FAIL] * 6,
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, max_attempts=2, log=logs.append).run()

    assert rc == EXIT_EXHAUSTED
    assert rc != EXIT_OK
    assert len(git.ls_remotes) == 7              # 조회 재시도도 bounded 다
    assert len(git.pushes) == 2
    assert any("remote ref lookup failed" in m for m in logs)
    assert any("state push exhausted" in m for m in logs)
    assert not any("confirmed" in m for m in logs)


def test_ls_remote_recovers_within_its_bound():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE, OK],
        ls_remote_results=[
            LS_REMOTE_FAIL, ls_remote_ok(BASE),      # preflight: 1회 실패 후 복구
            LS_REMOTE_FAIL, ls_remote_ok(BASE),      # push 실패 후 검증도 동일
        ],
    )
    sleeps = []

    rc = make(git, sleeps).run()

    assert rc == EXIT_OK
    assert len(git.ls_remotes) == 4
    assert sleeps == [2.0, 2.0, 3.0]     # ls-remote backoff ×2 → push backoff


# ── 결정적 실패는 즉시 실패한다 ────────────────────────────────────────────
@pytest.mark.parametrize(
    "stderr",
    [
        "remote: Permission to zetatech-a/LAW_RADER_KR.git denied to github-actions[bot].\n"
        "fatal: unable to access '...': The requested URL returned error: 403\n",
        "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
        "remote: error: Required status check is expected.\n",
        "remote: error: GH013: Repository rule violations found for refs/heads/main.\n",
        "fatal: Authentication failed for 'https://github.com/zetatech-a/LAW_RADER_KR/'\n",
    ],
)
def test_deterministic_failures_fail_fast_without_retrying(stderr):
    git = FakeGit(
        push_results=[GitResult(128, stderr=stderr)],
        ls_remote_results=[ls_remote_ok(BASE)],
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_FATAL
    assert rc != EXIT_OK
    assert len(git.pushes) == 1
    assert len(git.ls_remotes) == 1      # preflight 뿐 — 실패 후 재조회하지 않는다
    assert sleeps == []
    assert any("not retryable" in m for m in logs)


# ── git 서브프로세스 벽시계 상한 ────────────────────────────────────────────
#
# 상한이 없으면 bounded 성질은 '시도 횟수 + backoff' 뿐이고 벽시계로는 무한이다.
# 멈춘 push 하나가 재시도 루프를 전진시키지 못하고, monitor 는
# cancel-in-progress: false 라 뒤따르는 15분 주기 실행까지 큐에 쌓인다.
def test_every_real_git_invocation_passes_an_explicit_timeout(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="out", stderr="")

    monkeypatch.setattr(push_state.subprocess, "run", fake_run)
    result = _subprocess_git(["ls-remote", "origin", "refs/heads/main"])

    assert seen["kwargs"]["timeout"] == GIT_COMMAND_TIMEOUT_SEC
    assert seen["kwargs"]["timeout"] > 0
    assert seen["cmd"][0] == "git"
    assert result.returncode == 0
    assert result.stdout == "out"


def test_timeout_expired_becomes_a_controlled_nonzero_result(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(push_state.subprocess, "run", fake_run)
    # 예외가 밖으로 새어나가지 않는다.
    result = _subprocess_git(
        ["push", "origin", "https://x-access-token:supersecret@github.com"]
    )

    assert result.returncode == GIT_TIMEOUT_RETURNCODE
    assert result.returncode != 0
    assert "timed out" in result.stderr
    # 타임아웃 로그에 인자(=자격증명이 섞일 수 있는 원격 URL)를 그대로 싣지 않는다.
    assert "supersecret" not in result.stderr


def test_timeout_is_not_classified_as_a_deterministic_failure():
    """타임아웃은 '실패 확정'이 아니라 '결과 미상' — fail-fast 로 빠지면 안 된다."""
    assert not push_state.is_fatal(PUSH_TIMEOUT)
    assert not push_state.is_fatal(LS_REMOTE_TIMEOUT)


def test_timed_out_push_that_actually_landed_is_durable_success():
    """클라이언트가 끊겼어도 push 는 GitHub 에 도달했을 수 있다 — 다시 밀지 않는다."""
    git = FakeGit(
        push_results=[PUSH_TIMEOUT],
        ls_remote_results=[ls_remote_ok(BASE), ls_remote_ok(LOCAL)],
    )
    logs = []

    rc = make(git, [], log=logs.append).run()

    assert rc == EXIT_OK
    assert len(git.pushes) == 1                 # 중복 push 없음
    assert any("timed out" in m for m in logs)  # 원인은 로그에 남는다


def test_timed_out_push_with_remote_still_at_base_retries_within_bounds():
    git = FakeGit(
        push_results=[PUSH_TIMEOUT, OK],
        ls_remote_results=[ls_remote_ok(BASE), ls_remote_ok(BASE)],
    )
    sleeps = []

    rc = make(git, sleeps).run()

    assert rc == EXIT_OK
    assert len(git.pushes) == 2
    assert sleeps == [3.0]


def test_timed_out_push_onto_an_advanced_remote_fails_closed():
    git = FakeGit(
        push_results=[PUSH_TIMEOUT],
        ls_remote_results=[ls_remote_ok(BASE), ls_remote_ok(OTHER)],
    )

    rc = make(git, []).run()

    assert rc == EXIT_REMOTE_ADVANCED
    assert rc != EXIT_OK


def test_ls_remote_timeout_is_a_bounded_lookup_failure():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE] * 2,
        ls_remote_results=[ls_remote_ok(BASE)] + [LS_REMOTE_TIMEOUT] * 6,
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, max_attempts=2, log=logs.append).run()

    assert rc == EXIT_EXHAUSTED
    assert len(git.ls_remotes) == 7
    assert not any("confirmed" in m for m in logs)


def test_worst_case_wall_clock_is_bounded():
    """최악 소요가 15분 monitor 주기 안에서 끝나는지(계산 회귀)."""
    attempts, ls_attempts = 4, 3
    t = GIT_COMMAND_TIMEOUT_SEC
    lookup = ls_attempts * t + (ls_attempts - 1) * 2.0        # 64초
    backoff = 3.0 + 6.0 + 12.0                                 # 21초
    worst = lookup + attempts * (t + lookup) + backoff
    assert worst == pytest.approx(421.0)
    assert worst < 900                                         # monitor cadence


# ── 강제 push 금지 회귀 ────────────────────────────────────────────────────
FORBIDDEN = (
    "--force",
    "-f",
    "--force-with-lease",
    "--force-if-includes",
    "--mirror",
    "--delete",
)


def assert_no_destructive_git(git):
    for call in git.calls:
        lowered = [a.lower() for a in call]
        for bad in FORBIDDEN:
            assert bad not in lowered, f"금지된 git 인자를 사용했습니다: {call}"
        assert call[0] not in ("reset", "rebase", "pull", "merge"), (
            f"state JSON 을 자동 병합/되감기 하면 안 됩니다: {call}"
        )


@pytest.mark.parametrize(
    "push_results,ls_remote_results",
    [
        ([OK], [ls_remote_ok(BASE)]),
        ([COMMIT_REFS_FAILURE, OK], [ls_remote_ok(BASE)] * 2),
        ([COMMIT_REFS_FAILURE], [ls_remote_ok(BASE), ls_remote_ok(LOCAL)]),
        ([COMMIT_REFS_FAILURE], [ls_remote_ok(BASE), ls_remote_ok(OTHER)]),
        ([COMMIT_REFS_FAILURE] * 4, [ls_remote_ok(BASE)] * 5),
        ([COMMIT_REFS_FAILURE] * 4, [ls_remote_ok(BASE)] + [LS_REMOTE_FAIL] * 12),
        ([PUSH_TIMEOUT] * 4, [ls_remote_ok(BASE)] * 5),
        ([], [LS_REMOTE_NO_REF]),
    ],
)
def test_no_code_path_uses_force_push_or_reset(push_results, ls_remote_results):
    git = FakeGit(push_results=push_results, ls_remote_results=ls_remote_results)
    make(git, []).run()
    assert_no_destructive_git(git)


def test_push_targets_the_exact_local_commit_on_the_branch():
    git = FakeGit(push_results=[OK], ls_remote_results=[ls_remote_ok(BASE)])
    make(git, []).run()
    assert git.pushes == [["push", "origin", f"{LOCAL}:refs/heads/{BRANCH}"]]
    assert git.ls_remotes == [["ls-remote", "origin", f"refs/heads/{BRANCH}"]]


# ── force 옵션은 '토큰'이 아니라 '형태'로 막는다 ───────────────────────────
#
# 정확한 토큰 목록만으로는 git 이 실제로 받아들이는 동등한 형태를 놓친다:
# 값 붙은 긴 옵션, 짧은 옵션 묶음 속의 -f, 모호하지 않은 긴 옵션 축약.
@pytest.mark.parametrize(
    "token",
    [
        "--force",
        "--force-with-lease",
        "--force-with-lease=refs/heads/main:" + "d" * 40,
        "--force-if-includes",
        "--force-if-includes=abc",
        "-qf",
        "-fq",
        "-f",
        "--force-w",
        "--f",
        "--mirror",
        "--mir",
        "--FORCE",
        "-Qf",
    ],
)
def test_forbidden_option_forms_are_rejected_before_git_runs(token):
    git = FakeGit(push_results=[OK], ls_remote_results=[])
    pusher = make(git, [])

    with pytest.raises(AssertionError):
        pusher._git(["push", token, "origin", "main"])

    assert git.calls == [], "거절된 인자로 git 이 실행되었습니다"


@pytest.mark.parametrize(
    "args",
    [
        ["reset", "--hard", "origin/main"],
        ["rebase", "origin/main"],
        ["merge", "origin/main"],
        ["pull", "origin", "main"],
    ],
)
def test_history_rewriting_subcommands_are_rejected(args):
    git = FakeGit(push_results=[], ls_remote_results=[])
    pusher = make(git, [])

    with pytest.raises(AssertionError):
        pusher._git(args)

    assert git.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["push", "origin", f"{LOCAL}:refs/heads/main"],
        ["ls-remote", "origin", "refs/heads/main"],
        ["rev-parse", "--verify", "HEAD^{commit}"],
    ],
)
def test_legitimate_commands_still_pass_the_guard(args):
    """과잉 거절이 정상 동작을 막지 않는지 — 거짓 양성 회귀."""
    push_state._assert_safe(args)          # 예외가 나면 안 된다


def test_the_real_subprocess_runner_is_guarded_too(monkeypatch):
    """StatePusher._git 을 거치지 않는 호출자(_resolve 등)도 가드를 지나야 한다."""
    called = []
    monkeypatch.setattr(
        push_state.subprocess, "run", lambda *a, **k: called.append(a) or None
    )
    with pytest.raises(AssertionError):
        _subprocess_git(["push", "--force-with-lease=x", "origin", "main"])
    with pytest.raises(AssertionError):
        _subprocess_git(["reset", "--hard", "origin/main"])
    assert called == [], "거절된 인자로 git 프로세스가 실행되었습니다"


def test_guard_lets_the_real_commands_reach_the_runner():
    git = FakeGit(push_results=[OK], ls_remote_results=[ls_remote_ok(BASE)])
    pusher = make(git, [])

    pusher._git(["ls-remote", "origin", f"refs/heads/{BRANCH}"])
    pusher._git(["push", "origin", f"{LOCAL}:refs/heads/{BRANCH}"])

    assert git.kinds == ["ls-remote", "push"]


def test_guard_matches_option_forms_not_just_exact_tokens():
    assert _is_forbidden_option("--force-with-lease=refs/heads/main:abc")
    assert _is_forbidden_option("-qf")
    assert _is_forbidden_option("--force-w")
    # 우리가 실제로 쓰는 옵션·인자는 걸리지 않는다.
    for safe in (
        "--verify",
        "push",
        "ls-remote",
        "rev-parse",
        "origin",
        "refs/heads/main",
        "HEAD^{commit}",
        f"{LOCAL}:refs/heads/main",
        "--",
    ):
        assert not _is_forbidden_option(safe), safe


# ── 로그 위생 ──────────────────────────────────────────────────────────────
def test_credentials_are_redacted_from_logs():
    assert "secret" not in redact("https://x-access-token:secret@github.com/o/r")
    assert "ghp_" not in redact("remote: token ghp_0123456789abcdefghij rejected")
    assert redact("") == ""


def test_failure_log_keeps_one_line_and_no_credential_url():
    git = FakeGit(
        push_results=[
            GitResult(
                1,
                stderr=(
                    "remote: fatal error in commit_refs\n"
                    "To https://x-access-token:supersecret@github.com/zetatech-a/LAW_RADER_KR\n"
                    " ! [remote rejected] main -> main (failure)\n"
                ),
            )
        ],
        ls_remote_results=[ls_remote_ok(BASE), ls_remote_ok(OTHER)],
    )
    logs = []
    make(git, [], log=logs.append).run()

    joined = "\n".join(logs)
    assert "supersecret" not in joined
    # 실패 로그는 시도별 한 줄 + 판정 한 줄이면 충분하다(로그 스팸 방지).
    assert len(logs) == 2


def test_invalid_bounds_are_rejected():
    with pytest.raises(ValueError):
        StatePusher(
            remote="origin", branch=BRANCH, base_sha=BASE, local_sha=LOCAL,
            max_attempts=0,
        )
    with pytest.raises(ValueError):
        StatePusher(
            remote="origin", branch=BRANCH, base_sha=BASE, local_sha=LOCAL,
            ls_remote_attempts=0,
        )


def test_failure_reason_prefers_the_cause_over_gits_trailing_hint():
    """git 은 원인 뒤에 hint 를 덧붙인다 — 마지막 줄만 찍으면 원인이 사라진다."""
    fast_forward = (
        "To https://github.com/zetatech-a/LAW_RADER_KR\n"
        " ! [rejected]        main -> main (fetch first)\n"
        "error: failed to push some refs\n"
        "hint: See the 'Note about fast-forwards' in 'git push --help' for details.\n"
    )
    assert _failure_reason(fast_forward).startswith("! [rejected]")

    unreachable = (
        "fatal: could not read from remote repository.\n"
        "\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"
    )
    assert _failure_reason(unreachable).startswith("fatal:")

    assert _failure_reason(COMMIT_REFS_FAILURE.stderr) == (
        "remote: fatal error in commit_refs"
    )
    assert _failure_reason("") == ""
    assert _failure_reason("something odd\nlast line") == "last line"
