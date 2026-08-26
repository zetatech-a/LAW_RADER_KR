"""state 커밋의 원격 반영(scripts/push_state.py) 회귀 테스트.

이 코드가 존재하는 이유는 운영 장애다: 메일은 나갔고 러너 안에서 state 도 저장됐는데
마지막 `git push` 가 `remote: fatal error in commit_refs` 로 한 번 거부되자 원격 state 가
옛날 것으로 남았고, 다음 실행이 같은 게시글을 '신규'로 다시 판정해 중복 메일을 보냈다.

여기서는 git 프로세스 경계만 스텁으로 대체한다. **네트워크도 GitHub 도 건드리지
않는다** — 따라서 이 테스트가 통과했다고 실제 GitHub push 재시도가 검증된 것은 아니다.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.push_state import (  # noqa: E402
    EXIT_EXHAUSTED,
    EXIT_FATAL,
    EXIT_OK,
    EXIT_REMOTE_ADVANCED,
    GitResult,
    StatePusher,
    _failure_reason,
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


def ls_remote_ok(sha):
    return GitResult(0, stdout=f"{sha}\trefs/heads/{BRANCH}\n")


LS_REMOTE_FAIL = GitResult(128, stderr="fatal: unable to access ...: 500\n")


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


# ── PUSH1 — 첫 push 성공 ────────────────────────────────────────────────────
def test_push1_immediate_success_is_a_single_attempt_without_sleeping():
    git = FakeGit(push_results=[OK], ls_remote_results=[])
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_OK
    assert len(git.pushes) == 1
    assert git.ls_remotes == []          # 성공했으면 원격을 다시 물어볼 이유가 없다
    assert sleeps == []                  # 잠들지 않는다
    assert "state remote push confirmed" in logs


# ── PUSH2 — 일시적 실패 뒤 성공(운영 장애 재현 클래스) ──────────────────────
def test_push2_transient_failure_then_success():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE, OK],
        ls_remote_results=[ls_remote_ok(BASE)],
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
        ls_remote_results=[ls_remote_ok(LOCAL)],
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_OK
    assert len(git.pushes) == 1          # 같은 커밋을 또 밀지 않는다
    assert sleeps == []
    assert any("remote already at the pushed commit" in m for m in logs)


# ── PUSH4 — 원격이 독립적으로 전진 ──────────────────────────────────────────
def test_push4_remote_advanced_aborts_immediately_without_force():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE],
        ls_remote_results=[ls_remote_ok(OTHER)],
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


def test_remote_branch_missing_is_also_refused():
    """rc=0 인데 ref 가 없다(브랜치 삭제) — 조회 실패와 구분해 실패로 끝낸다."""
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE],
        ls_remote_results=[GitResult(0, stdout="")],
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_REMOTE_ADVANCED
    assert len(git.pushes) == 1
    assert any("not found" in m for m in logs)


# ── PUSH5 — 계속 실패 ───────────────────────────────────────────────────────
def test_push5_repeated_transient_failure_is_bounded_and_fails_nonzero():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE] * 4,
        ls_remote_results=[ls_remote_ok(BASE)] * 4,
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
        ls_remote_results=[ls_remote_ok(BASE)] * 5,
    )
    sleeps = []

    rc = make(git, sleeps, max_attempts=5, backoff_base_sec=8.0, backoff_max_sec=15.0).run()

    assert rc == EXIT_EXHAUSTED
    assert sleeps == [8.0, 15.0, 15.0, 15.0]    # 지수 증가가 상한에서 멈춘다


# ── PUSH6 — 원격 ref 조회 자체가 실패 ───────────────────────────────────────
def test_push6_ls_remote_failure_never_reports_false_success():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE] * 2,
        # 시도마다 ls-remote 를 자체 상한(3회)까지 두드린다 → 2 * 3 = 6
        ls_remote_results=[LS_REMOTE_FAIL] * 6,
    )
    sleeps = []
    logs = []

    rc = make(git, sleeps, max_attempts=2, log=logs.append).run()

    assert rc == EXIT_EXHAUSTED
    assert rc != EXIT_OK
    assert len(git.ls_remotes) == 6              # 조회 재시도도 bounded 다
    assert len(git.pushes) == 2
    assert any("remote ref lookup failed" in m for m in logs)
    assert any("state push exhausted" in m for m in logs)
    assert not any("confirmed" in m for m in logs)


def test_ls_remote_recovers_within_its_bound():
    git = FakeGit(
        push_results=[COMMIT_REFS_FAILURE, OK],
        ls_remote_results=[LS_REMOTE_FAIL, ls_remote_ok(BASE)],
    )
    sleeps = []

    rc = make(git, sleeps).run()

    assert rc == EXIT_OK
    assert len(git.ls_remotes) == 2
    assert sleeps == [2.0, 3.0]     # ls-remote backoff → push backoff


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
    git = FakeGit(push_results=[GitResult(128, stderr=stderr)], ls_remote_results=[])
    sleeps = []
    logs = []

    rc = make(git, sleeps, log=logs.append).run()

    assert rc == EXIT_FATAL
    assert rc != EXIT_OK
    assert len(git.pushes) == 1
    assert git.ls_remotes == []
    assert sleeps == []
    assert any("not retryable" in m for m in logs)


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
        ([OK], []),
        ([COMMIT_REFS_FAILURE, OK], [ls_remote_ok(BASE)]),
        ([COMMIT_REFS_FAILURE], [ls_remote_ok(LOCAL)]),
        ([COMMIT_REFS_FAILURE], [ls_remote_ok(OTHER)]),
        ([COMMIT_REFS_FAILURE] * 4, [ls_remote_ok(BASE)] * 4),
        ([COMMIT_REFS_FAILURE] * 4, [LS_REMOTE_FAIL] * 12),
    ],
)
def test_no_code_path_uses_force_push_or_reset(push_results, ls_remote_results):
    git = FakeGit(push_results=push_results, ls_remote_results=ls_remote_results)
    make(git, []).run()
    assert_no_destructive_git(git)


def test_push_targets_the_exact_local_commit_on_the_branch():
    git = FakeGit(push_results=[OK], ls_remote_results=[])
    make(git, []).run()
    assert git.pushes == [["push", "origin", f"{LOCAL}:refs/heads/{BRANCH}"]]


def test_forbidden_arguments_are_rejected_at_the_process_boundary():
    """실수로 force 인자가 추가되는 경로를 코드가 스스로 막는지."""
    git = FakeGit(push_results=[OK], ls_remote_results=[])
    pusher = make(git, [])
    with pytest.raises(AssertionError):
        pusher._git(["push", "--force", "origin", "main"])
    with pytest.raises(AssertionError):
        pusher._git(["reset", "--hard", "origin/main"])
    assert git.calls == []


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
        ls_remote_results=[ls_remote_ok(OTHER)],
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
