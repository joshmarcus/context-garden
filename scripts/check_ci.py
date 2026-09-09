"""Push this repository's worker branch and await its exact-commit GitHub Actions run.

Uses authenticated ``gh`` where it is available.  Public github.com repositories can
read Actions metadata through the bounded unauthenticated REST API when workers only
have their repository deploy key.  This is repository tooling, not a provider
dependency of the garden package. Keep full PR CI as the merge gate.

Use this helper only for a product whose validation provider is explicitly ``actions``.
Products using another status provider, a validation command, or no external CI must use
their selected policy and must not invoke this branch-publishing helper.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

# Unauthenticated GitHub REST permits 60 requests/hour. Leave room for the initial
# lookup and unrelated metadata reads from the same worker address.
PUBLIC_API_POLL_SECONDS = 65


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
    if url.scheme == "https" and (url.username or url.port not in {None, 443}):
        raise CIError("origin HTTPS push URL must not contain credentials or a custom port")
    if url.scheme == "ssh" and url.username != "git":
        raise CIError("origin SSH push URL must use the git account")
    if url.hostname == "ssh.github.com":
        if url.scheme != "ssh" or url.port != 443:
            raise CIError("ssh.github.com must use its official SSH transport on port 443")
        return f"github.com/{path}"
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
    return max(exact, key=lambda r: (int(r["databaseId"]), int(r.get("attempt", 1))), default=None)


def authenticated_gh(repo: str) -> bool:
    """Return whether the configured gh CLI can read this repository's API host."""
    host = repo.partition("/")[0]
    try:
        command("gh", "auth", "status", "--hostname", host)
    except CIError:
        if host == "github.com":
            return False
        raise CIError(f"authenticated gh is required for non-public API host {host}") from None
    return True


def public_workflow_runs(repo: str, branch: str, sha: str) -> list[dict]:
    """Read one small, public github.com Actions listing without credentials."""
    if not repo.startswith("github.com/"):
        raise CIError("unauthenticated Actions polling is limited to public github.com repositories")
    owner_repo = repo.removeprefix("github.com/")
    query = urlencode({"head_sha": sha, "branch": branch, "event": "push", "per_page": "20"})
    request = Request(
        f"https://api.github.com/repos/{owner_repo}/actions/workflows/ci.yml/runs?{query}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "context-garden-worker-ci"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
            remaining = response.headers.get("X-RateLimit-Remaining")
    except HTTPError as exc:
        raise CIError(f"public GitHub Actions API returned HTTP {exc.code}") from exc
    except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise CIError(f"public GitHub Actions API could not provide runs: {exc}") from exc
    if remaining is not None:
        try:
            if int(remaining) <= 0:
                raise CIError("public GitHub Actions API rate limit is exhausted")
        except ValueError as exc:
            raise CIError("public GitHub Actions API returned a malformed rate-limit header") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise CIError("public GitHub Actions API returned malformed workflow runs")
    runs: list[dict] = []
    for item in payload["workflow_runs"]:
        if not isinstance(item, dict):
            raise CIError("public GitHub Actions API returned a malformed workflow run")
        required = ("id", "head_sha", "head_branch", "event", "status", "html_url", "run_attempt")
        if any(key not in item for key in required) or not isinstance(item["id"], int):
            raise CIError("public GitHub Actions API returned a malformed workflow run")
        if not all(isinstance(item[key], str) for key in ("head_sha", "head_branch", "event", "status", "html_url")):
            raise CIError("public GitHub Actions API returned a malformed workflow run")
        if item.get("conclusion") is not None and not isinstance(item["conclusion"], str):
            raise CIError("public GitHub Actions API returned a malformed workflow run")
        if not isinstance(item["run_attempt"], int):
            raise CIError("public GitHub Actions API returned a malformed workflow run")
        runs.append({"databaseId": item["id"], "headSha": item["head_sha"],
                     "headBranch": item["head_branch"], "event": item["event"],
                     "status": item["status"], "conclusion": item.get("conclusion"),
                     "url": item["html_url"], "attempt": item["run_attempt"]})
    return runs


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
    use_gh = authenticated_gh(repo)
    if not use_gh:
        print(f"Using public GitHub Actions metadata with at least {PUBLIC_API_POLL_SECONDS}s between requests", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if use_gh:
            runs = json.loads(command(
                "gh", "run", "list", "--repo", repo, "--workflow", "ci.yml", "--commit", sha,
                "--branch", branch, "--event", "push", "--limit", "20", "--json",
                "databaseId,headSha,headBranch,event,status,conclusion,url",
            ))
        else:
            runs = public_workflow_runs(repo, branch, sha)
        run = latest_run(runs, branch, sha)
        if run:
            print(f"CI {run['status']} {run.get('conclusion') or ''}: {run['url']}", flush=True)
            if run["status"] == "completed":
                if run.get("conclusion") != "success":
                    try:
                        if not use_gh:
                            raise CIError("failed public CI logs require authenticated gh")
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
        interval = max(poll, PUBLIC_API_POLL_SECONDS) if not use_gh else poll
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
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
