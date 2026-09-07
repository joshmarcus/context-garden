"""Push this repository's worker branch and await its exact-commit GitHub CI.

No dependencies beyond Python, git and authenticated gh. This is repository tooling,
not a provider dependency of the garden package. Keep full PR CI as the merge gate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit


class CIError(RuntimeError):
    pass


def command(*args: str) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=90,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CIError(f"{args[0]} could not finish: {exc}") from exc
    if result.returncode:
        raise CIError(f"{args[0]} exited {result.returncode}: {(result.stderr or result.stdout)[-4000:]}")
    return result.stdout.strip()


def repository(remote: str) -> str:
    # Use the push destination explicitly; gh's implicit repo can be overridden by GH_REPO.
    if remote.startswith("git@"):
        remote = "ssh://" + remote.replace(":", "/", 1)
    url = urlsplit(remote)
    path = url.path.strip("/").removesuffix(".git")
    if (url.scheme not in {"https", "ssh"} or not url.hostname or url.password
            or url.query or url.fragment or len(path.split("/")) != 2):
        raise CIError("origin must have one HTTPS or SSH GitHub repository push URL")
    return f"{url.hostname}/{path}"


def checkout() -> tuple[str, str]:
    if command("git", "status", "--porcelain"):
        raise CIError("Commit or remove uncommitted/untracked changes before checking CI")
    branch = command("git", "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch.startswith(("garden/", "codex/")):
        raise CIError("CI pushes are limited to assigned garden/ or codex/ branches")
    expected = os.environ.get("GARDEN_BRANCH")
    if expected and branch != expected:
        raise CIError(f"Checked-out branch {branch} is not assigned branch {expected}")
    return branch, command("git", "rev-parse", "HEAD")


def latest_run(runs: list[dict], branch: str, sha: str) -> dict | None:
    exact = [r for r in runs if r.get("headSha") == sha and r.get("headBranch") == branch
             and r.get("event") == "push"]
    return max(exact, key=lambda r: int(r["databaseId"]), default=None)


def check_ci(timeout: float = 1200, poll: float = 15) -> None:
    branch, sha = checkout()
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise CIError("HEAD is not a commit SHA")
    remote = command("git", "remote", "get-url", "--push", "--all", "origin")
    if len(remote.splitlines()) != 1:
        raise CIError("origin must have exactly one push destination")
    repo = repository(remote)
    git_auth = ("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")
    # Ephemeral credential helper: works with an isolated HOME and explicitly granted
    # GH_CONFIG_DIR/GH_TOKEN without writing shared git config or changing tracking refs.
    command(*git_auth, "push", "origin", f"{sha}:refs/heads/{branch}")
    print(f"CI commit {sha} on {repo}:{branch}", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = json.loads(command(
            "gh", "run", "list", "--repo", repo, "--workflow", "ci.yml", "--commit", sha,
            "--branch", branch, "--event", "push", "--limit", "20", "--json",
            "databaseId,headSha,headBranch,event,status,conclusion,url",
        ))
        run = latest_run(runs, branch, sha)
        if run:
            print(f"CI {run['status']} {run.get('conclusion') or ''}: {run['url']}", flush=True)
            if run["status"] == "completed":
                if run.get("conclusion") != "success":
                    try:
                        log = command("gh", "run", "view", str(run["databaseId"]),
                                      "--repo", repo, "--log-failed")
                        print("\n".join(log.splitlines()[-100:]), file=sys.stderr)
                    except CIError as exc:
                        print(str(exc), file=sys.stderr)
                    raise CIError(f"CI did not pass: {run.get('conclusion')} {run['url']}")
                # A successful old commit cannot validate edits made while CI was running.
                if checkout() != (branch, sha):
                    raise CIError("Checkout changed while CI ran; check the final commit again")
                remote_tip = command(*git_auth, "ls-remote", "origin", f"refs/heads/{branch}")
                if not remote_tip or remote_tip.split()[0] != sha:
                    raise CIError("Remote branch changed while CI ran; check the final commit again")
                print(f"PASS {sha} {run['url']}", flush=True)
                return
        else:
            print("Waiting for this commit's push CI to register (no result yet)", flush=True)
        time.sleep(min(poll, max(0, deadline - time.monotonic())))
    raise CIError(f"Timed out awaiting CI for {sha}; missing or pending is not a pass")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, default=1200)
    parser.add_argument("--poll-seconds", type=float, default=15)
    args = parser.parse_args()
    if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
        parser.error("timeout and poll interval must be positive")
    try:
        check_ci(args.timeout_seconds, args.poll_seconds)
    except (CIError, ValueError, KeyError, TypeError) as exc:
        print(f"CI ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
