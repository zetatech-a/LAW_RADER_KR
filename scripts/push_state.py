#!/usr/bin/env python3
"""이미 만들어진 state 커밋을 원격에 **확실히** 반영한다(bounded retry).

왜 필요한가
-----------
모니터는 메일 발송 성공 → seen 확정 → state 저장 순서로 동작하고, 그 결과 파일을
워크플로가 커밋해 저장소에 남긴다. 이 마지막 push 가 한 번 실패하면 러너는 사라지고
원격 state 는 옛날 것으로 남는다. 다음 실행은 이미 보낸 게시글을 다시 '신규'로
판정해 **같은 메일을 또 보낸다**(2026-08-26 운영 장애: run #911/#914,
`remote: fatal error in commit_refs` → `! [remote rejected] main -> main (failure)`).

`git push` 한 번의 일시적 실패를 영구 state 유실로 만들지 않는 것이 이 스크립트의
유일한 목적이다. 커밋을 만들지 않고, 커밋을 고치지도 않으며, 강제 push 도 하지
않는다 — 이미 만들어진 로컬 커밋 하나를 원격 브랜치에 올리는 일만 한다.

안전 규칙
---------
- **force 계열 인자를 절대 쓰지 않는다.** `_run_git()` 이 명령 자체를 검사해
  `--force` / `--force-with-lease` / `reset --hard` 등이 섞이면 즉시 예외를 던진다.
- 재시도는 '원격 ref 가 아직 우리가 기반한 커밋에 있다'가 확인될 때만 한다.
  영어 오류 문구 매칭에 의존하지 않는다 — 원격 ref 가 더 강한 신호다.
- 원격이 독립적으로 전진했으면(다른 실행이 push 에 성공) **되돌리지 않고 실패**한다.
  state JSON 을 자동으로 rebase/merge 하면 seen 목록이 조용히 뒤섞일 수 있다.
- push 클라이언트가 오류를 봤지만 원격 ref 가 이미 우리 커밋이면 durable 성공이다
  (같은 커밋을 또 밀지 않는다).
- 확인할 수 없으면 **성공이라고 말하지 않는다.** 모든 실패 경로는 non-zero 다.

표준 라이브러리만 쓴다(subprocess / time / argparse).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass

# 종료 코드. 0 이외는 전부 '내구성 있는 저장 실패'다.
EXIT_OK = 0
EXIT_EXHAUSTED = 1          # 재시도 상한까지 갔지만 끝내 push 되지 않음
EXIT_FATAL = 2              # 재시도해도 달라지지 않는 실패(인증/권한/브랜치 보호)
EXIT_REMOTE_ADVANCED = 3    # 원격이 독립적으로 전진 — 자동 해결하지 않는다

# 이 스크립트가 만들어 내면 안 되는 인자들. 하나라도 섞이면 실행 자체를 거부한다
# ("실수로 추가되는" 경로를 코드 레벨에서 막는다).
FORBIDDEN_GIT_ARGS = ("--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror")
FORBIDDEN_GIT_COMBOS = (("reset", "--hard"),)

# 재시도해도 결과가 달라지지 않는 실패의 결정적 단서. 이 판정은 **fail-fast 전용**
# 이며, 재시도 여부를 정하는 근거로는 쓰지 않는다(그쪽은 원격 ref 가 판단한다).
_FATAL_PATTERNS = (
    # 인증 / 권한
    "authentication failed",
    "could not read username",
    "could not read password",
    "invalid username or password",
    "permission denied",
    "write access to repository not granted",
    "403 forbidden",
    "the requested url returned error: 403",
    # 브랜치 보호 / 저장소 규칙
    "protected branch",
    "gh006",
    "gh013",
    "repository rule violations",
    "refusing to allow",
)

# 로그에 자격증명이 섞여 나가지 않도록 지운다(러너 로그는 그대로 보관된다).
_CREDENTIAL_URL = re.compile(r"(https?://)[^/\s@]+@")
_TOKEN_LIKE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,}|x-access-token:[^\s@]+)")


@dataclass(frozen=True)
class GitResult:
    """`subprocess.CompletedProcess` 중 우리가 쓰는 부분만 추린 값."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def redact(text: str) -> str:
    """자격증명이 들어갈 수 있는 부분을 가린다."""
    if not text:
        return ""
    cleaned = _CREDENTIAL_URL.sub(r"\1***@", text)
    return _TOKEN_LIKE.sub("***", cleaned)


# 실패 원인으로 쓸 만한 줄(hint/조언 문구보다 이쪽을 먼저 고른다).
_REASON_PREFIXES = ("remote:", "error:", "fatal:", "!")


def _failure_reason(text: str) -> str:
    """오류 원인 한 줄만 남긴다(로그 스팸 방지).

    git 은 마지막 줄에 조언(hint/`and the repository exists.`)을 붙이는 경우가 많아
    그냥 마지막 줄을 쓰면 정작 원인이 로그에서 사라진다. `remote:`/`error:`/`fatal:`/
    거부 표시(`!`) 로 시작하는 첫 줄을 우선 고르고, 없을 때만 마지막 줄로 내려간다.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    for line in lines:
        if line.lower().startswith(_REASON_PREFIXES):
            return redact(line)
    return redact(lines[-1])


def _assert_safe(args: list[str]) -> None:
    lowered = [a.lower() for a in args]
    for bad in FORBIDDEN_GIT_ARGS:
        if bad in lowered:
            raise AssertionError(f"금지된 git 인자입니다: {bad}")
    for combo in FORBIDDEN_GIT_COMBOS:
        if all(part in lowered for part in combo):
            raise AssertionError(f"금지된 git 조합입니다: {' '.join(combo)}")


def _subprocess_git(args: list[str]) -> GitResult:
    proc = subprocess.run(  # noqa: S603 - 인자는 아래에서 우리가 조립한 것뿐이다
        ["git", *args],
        capture_output=True,
        text=True,
    )
    return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def is_fatal(result: GitResult) -> bool:
    """재시도가 의미 없는 실패인지(인증/권한/브랜치 보호)."""
    blob = f"{result.stdout}\n{result.stderr}".lower()
    return any(p in blob for p in _FATAL_PATTERNS)


class StatePusher:
    """이미 만들어진 커밋 하나를 원격 브랜치에 올린다.

    git 프로세스 경계(`run`)와 시계(`sleep`)를 주입받는다 — 단위 테스트가 네트워크
    없이 모든 분기를 결정적으로 재현할 수 있게 하기 위함이다.
    """

    def __init__(
        self,
        *,
        remote: str,
        branch: str,
        base_sha: str,
        local_sha: str,
        max_attempts: int = 4,
        backoff_base_sec: float = 3.0,
        backoff_max_sec: float = 15.0,
        ls_remote_attempts: int = 3,
        ls_remote_backoff_sec: float = 2.0,
        run=_subprocess_git,
        sleep=time.sleep,
        log=None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts 는 1 이상이어야 합니다")
        if ls_remote_attempts < 1:
            raise ValueError("ls_remote_attempts 는 1 이상이어야 합니다")
        self.remote = remote
        self.branch = branch
        self.base_sha = base_sha
        self.local_sha = local_sha
        self.max_attempts = max_attempts
        self.backoff_base_sec = backoff_base_sec
        self.backoff_max_sec = backoff_max_sec
        self.ls_remote_attempts = ls_remote_attempts
        self.ls_remote_backoff_sec = ls_remote_backoff_sec
        self._run = run
        self._sleep = sleep
        self._log = log or (lambda msg: print(msg, flush=True))

    # --- git 경계 ---
    def _git(self, args: list[str]) -> GitResult:
        _assert_safe(args)
        return self._run(args)

    def _push(self) -> GitResult:
        # 커밋 SHA 를 명시해 '지금 만든 그 커밋'만 올린다. force 아님(fast-forward 만).
        return self._git(
            ["push", self.remote, f"{self.local_sha}:refs/heads/{self.branch}"]
        )

    def remote_sha(self) -> str | None:
        """원격 브랜치의 현재 SHA. 조회 자체가 실패하면 None(=모름).

        '모름'은 성공도 실패도 아니다 — 호출자는 이를 근거로 성공을 선언하지 않는다.
        """
        for attempt in range(1, self.ls_remote_attempts + 1):
            result = self._git(
                ["ls-remote", self.remote, f"refs/heads/{self.branch}"]
            )
            if result.returncode == 0:
                for line in (result.stdout or "").splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == f"refs/heads/{self.branch}":
                        return parts[0].strip()
                # rc=0 인데 ref 가 없다 = 원격 브랜치 없음. 조회 실패와 구분한다.
                return ""
            if attempt < self.ls_remote_attempts:
                self._sleep(self.ls_remote_backoff_sec)
        return None

    def _backoff_sec(self, attempt: int) -> float:
        delay = self.backoff_base_sec * (2 ** (attempt - 1))
        return min(delay, self.backoff_max_sec)

    # --- 본체 ---
    def run(self) -> int:
        for attempt in range(1, self.max_attempts + 1):
            result = self._push()
            if result.returncode == 0:
                self._log("state remote push confirmed")
                return EXIT_OK

            reason = _failure_reason(result.stderr) or _failure_reason(result.stdout)
            self._log(
                f"state push attempt {attempt}/{self.max_attempts} failed"
                + (f": {reason}" if reason else "")
            )

            if is_fatal(result):
                self._log(
                    "push rejected by authentication/permission/branch protection; "
                    "not retryable"
                )
                return EXIT_FATAL

            # 여기서부터는 원격 ref 가 판단한다(오류 문구가 아니라).
            remote = self.remote_sha()

            if remote == self.local_sha:
                # 클라이언트는 실패를 봤지만 원격에는 이미 반영됐다 — durable 성공이다.
                self._log("remote already at the pushed commit; treating as success")
                self._log("state remote push confirmed")
                return EXIT_OK

            if remote is None:
                # 원격 상태를 모른다. 성공이라고 말하지 않는다. 남은 시도가 있으면
                # 다시 밀어 본다(force 가 아니므로 원격이 전진했다면 그냥 거부된다).
                if attempt < self.max_attempts:
                    delay = self._backoff_sec(attempt)
                    self._log(
                        f"remote ref lookup failed; state unknown, retrying in {delay:g} sec"
                    )
                    self._sleep(delay)
                    continue
                break

            if remote == "":
                # 원격에 브랜치가 없다. 이 스크립트는 '기존 브랜치를 전진시키는' 도구다
                # — 사라진 브랜치를 되살리는 판단은 사람이 해야 한다.
                self._log(
                    f"remote branch {self.branch} not found; refusing unsafe retry"
                )
                return EXIT_REMOTE_ADVANCED

            if remote != self.base_sha:
                self._log(
                    "remote branch advanced; refusing unsafe retry "
                    f"(expected base {self.base_sha[:12]}, remote {remote[:12]})"
                )
                return EXIT_REMOTE_ADVANCED

            # 원격은 우리가 기반한 커밋 그대로다 — 같은 커밋을 다시 미는 것이 안전하다.
            if attempt < self.max_attempts:
                delay = self._backoff_sec(attempt)
                self._log(
                    f"remote still at expected base; retrying in {delay:g} sec"
                )
                self._sleep(delay)
                continue
            break

        self._log("state push exhausted; durable state NOT persisted")
        return EXIT_EXHAUSTED


def _resolve(rev: str, run=_subprocess_git) -> str:
    result = run(["rev-parse", "--verify", f"{rev}^{{commit}}"])
    if result.returncode != 0:
        raise SystemExit(f"{rev} 를 확인할 수 없습니다: {_failure_reason(result.stderr)}")
    return (result.stdout or "").strip()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="이미 만들어진 state 커밋을 원격 브랜치에 안전하게 반영한다."
    )
    p.add_argument("--remote", default="origin", help="원격 이름(기본 origin)")
    p.add_argument(
        "--branch",
        required=True,
        help="state 를 유지하는 브랜치 이름(예: main)",
    )
    p.add_argument(
        "--local-sha",
        default=None,
        help="올릴 커밋(기본: HEAD). 지정하지 않으면 HEAD 를 확인해 쓴다.",
    )
    p.add_argument(
        "--base-sha",
        default=None,
        help="state 커밋이 기반한 커밋(기본: HEAD^). 원격 전진 여부 판정 기준.",
    )
    p.add_argument("--max-attempts", type=int, default=4, help="push 최대 시도 횟수")
    p.add_argument(
        "--backoff-base-sec", type=float, default=3.0, help="첫 재시도 대기(초)"
    )
    p.add_argument(
        "--backoff-max-sec", type=float, default=15.0, help="재시도 대기 상한(초)"
    )
    p.add_argument(
        "--ls-remote-attempts", type=int, default=3, help="원격 ref 조회 최대 횟수"
    )
    p.add_argument(
        "--ls-remote-backoff-sec", type=float, default=2.0, help="원격 ref 조회 대기(초)"
    )
    return p


def main(argv: list[str] | None = None, *, run=_subprocess_git, sleep=time.sleep) -> int:
    args = build_parser().parse_args(argv)
    local_sha = args.local_sha or _resolve("HEAD", run=run)
    base_sha = args.base_sha or _resolve("HEAD^", run=run)
    pusher = StatePusher(
        remote=args.remote,
        branch=args.branch,
        base_sha=base_sha,
        local_sha=local_sha,
        max_attempts=args.max_attempts,
        backoff_base_sec=args.backoff_base_sec,
        backoff_max_sec=args.backoff_max_sec,
        ls_remote_attempts=args.ls_remote_attempts,
        ls_remote_backoff_sec=args.ls_remote_backoff_sec,
        run=run,
        sleep=sleep,
    )
    return pusher.run()


if __name__ == "__main__":  # pragma: no cover - 실행 진입점
    sys.exit(main())
