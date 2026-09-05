from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..harness import Harness
from ..runs import Run


class RunnerError(Exception):
    pass


# The scheduler's environment variables a worker keeps, and the setup command that prepares
# its worktree with it. Everything else is dropped: a worker inherits no GitHub token, no
# cloud credentials, no ssh agent and no `GARDEN_*` of the live garden, because it only
# commits in its worktree and the scheduler does the pushing. A trailing `*` matches a
# prefix. `worker_env.pass` in garden.yaml adds names (`AWS_*` for a Bedrock-backed
# harness, a private registry token for `setup.command`); `"*"` restores full inheritance.
PASS_ENV: tuple[str, ...] = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM", "COLUMNS", "LINES",
    "LANG", "LANGUAGE", "LC_*", "TZ", "TMPDIR", "TMP", "TEMP", "XDG_*",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "ANTHROPIC_*", "CLAUDE_*",   # the claude harness's own credentials and settings
    "OPENAI_*", "CODEX_*",       # the codex harness's
)


def pass_env_patterns(config: dict[str, Any] | None) -> list[str]:
    """The environment-variable allowlist: `PASS_ENV` plus the names or globs under
    `config['worker_env']['pass']`. Shared by `scrubbed_env` (the local runner and checks,
    which filter this process's `os.environ`) and the ssh runner's remote scrub (which filters
    the remote login environment the same way, in shell)."""
    extra = [str(p) for p in (((config or {}).get("worker_env") or {}).get("pass") or []) if str(p)]
    return [*PASS_ENV, *extra]


def scrubbed_env(config: dict[str, Any] | None, setup: dict[str, Any] | None = None) -> dict[str, str]:
    """The scrubbed environment a worker (and its setup command) runs in: `PASS_ENV` plus
    the names or globs under `config['worker_env']['pass']`, then `setup['env']` on top.
    `CLAUDECODE` is always dropped so a garden can be driven from inside a Claude Code session."""
    patterns = pass_env_patterns(config)
    env = {k: v for k, v in os.environ.items() if any(fnmatch.fnmatchcase(k, p) for p in patterns)}
    env.pop("CLAUDECODE", None)
    for k, v in ((setup or {}).get("env") or {}).items():
        env[str(k)] = str(v)
    return env


def setup_stamp(command: str) -> str:
    """A fingerprint of the setup command; the marker holds this so a changed command re-runs."""
    return hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()


def setup_marker(worktree: Path) -> Path:
    """A per-worktree marker kept beside the worktree, never inside the checkout, so the
    worker's leftover-commit step (`git add -A`) cannot pick it up."""
    return worktree.parent / f".garden-setup-{worktree.name}"


def run_setup(worktree: Path, setup: dict[str, Any] | None, *, log_path: Path | None = None,
              env: dict[str, str] | None = None) -> None:
    """Prepare a fresh worktree's environment: run `setup['command']` once (again only when the
    command changes, tracked by a marker file) in `env` (default: `scrubbed_env`)
    with `setup['env']` added. A non-zero exit raises RunnerError with the log tail — a run
    failure, not a worker fault. An empty or missing command is a no-op, so products that
    need no setup pay nothing."""
    command = str((setup or {}).get("command") or "").strip()
    if not command:
        return
    marker = setup_marker(worktree)
    stamp = setup_stamp(command)
    if marker.exists() and marker.read_text().strip() == stamp:
        return
    env = dict(env) if env is not None else scrubbed_env({}, setup)
    for k, v in ((setup or {}).get("env") or {}).items():
        env[str(k)] = str(v)
    timeout = int((setup or {}).get("timeout_seconds") or 600)
    try:
        proc = subprocess.run(command, shell=True, cwd=str(worktree), env=env,
                              capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise RunnerError(f"setup command timed out after {timeout}s: {command}") from e
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if log_path is not None:
        try:
            log_path.write_text(out)
        except OSError:
            pass
    if proc.returncode != 0:
        tail = "\n".join(out.splitlines()[-40:])
        raise RunnerError(f"setup command failed (exit {proc.returncode}): {command}\n{tail}")
    marker.write_text(stamp)


class Runner(ABC):
    name: str = "base"
    detached: bool = True  # False = a human drives the session; completion comes via `garden finish`
    remote: bool = False  # True = the worker pushes the branch itself; no local worktree during the run

    def __init__(self, config: dict[str, Any], harness: Harness | None = None):
        self.config = config
        self.harness = harness

    def assign(self, run: Run, active: list[Run]) -> None:  # noqa: B027
        """Optional: pick a host / slot before start (ssh runner)."""

    @abstractmethod
    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        """Launch the worker. Must return immediately; must arrange for run.dir/exit_code."""

    @abstractmethod
    def collect(self, run: Run) -> dict[str, Any]:
        """After the process finished: {"result": {...}, "usage": {...}, "cost_usd": float|None,
        "final_text": str, "error": str}."""

    def harness_shell(self, run: Run, final_path: Path | None) -> str:
        """The harness command for this run: a resume when the run carries a session id."""
        assert self.harness is not None
        if run.mode == "resume" and run.session_id:
            return self.harness.shell_resume_command(run.session_id, run.model, final_path, run.difficulty)
        return self.harness.shell_command(run.model, final_path, run.difficulty)

    def doctor(self) -> list[str]:
        return []
