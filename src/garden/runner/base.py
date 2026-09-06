from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
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
# commits in its worktree and the scheduler does the pushing. `HOME` is *not* on this list:
# a worker (and a branch's own test suite) runs under an isolated scratch home instead of the
# operator's, so it cannot read `~/.config/gh`, `~/.git-credentials`, `~/.ssh` or the like
# (see `worker_home` and `scrubbed_env`). A trailing `*` matches a prefix. `worker_env.pass`
# in garden.yaml adds names (`AWS_*` for a Bedrock-backed harness, a private registry token
# for `setup.command`, or `HOME` to restore the operator's home); `"*"` restores full
# inheritance.
PASS_ENV: tuple[str, ...] = (
    "PATH", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM", "COLUMNS", "LINES",
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


def worker_home(worktree: Path | str | None) -> str:
    """An isolated `HOME` for a worker or check: a scratch directory, never the operator's
    real home, so a worker (or a branch's test suite) cannot read the operator's gh token,
    git credentials or ssh keys out of `~`. Merely unsetting HOME is not enough: glibc and
    `os.path.expanduser` fall back to the passwd entry, which is the operator's real home, so
    HOME must be *set* to somewhere empty. It sits beside the worktree (not inside it, so
    `git add -A` cannot commit it) and persists per task, so tool caches (npm, uv, pip)
    survive across runs. With no worktree known, a shared throwaway directory under TMPDIR."""
    if worktree is not None:
        wt = Path(worktree)
        home = wt.parent / f".garden-home-{wt.name}"
    else:
        import tempfile

        home = Path(tempfile.gettempdir()) / "garden-worker-home"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(home)


# The config-dir variable each built-in harness reads, and where it points by default (relative
# to the *operator's* real home, not the worker's isolated one): CLAUDE_CONFIG_DIR is where
# claude keeps `.credentials.json`; CODEX_HOME is codex's whole state directory. Neither is on
# PASS_ENV by name (only the `CLAUDE_*` / `CODEX_*` prefixes are), so an isolated worker HOME
# does not silently hide either harness's saved login — see `scrubbed_env`. A custom harness
# reads whatever variable its own CLI defines; name it as a key under `worker_env.config_dirs`
# in garden.yaml to have its value passed the same way (`scrubbed_env` has no default for it).
DEFAULT_CONFIG_DIRS: dict[str, str] = {
    "CLAUDE_CONFIG_DIR": ".claude",
    "CODEX_HOME": ".codex",
}

# A harness gets its login token, not its whole mutable configuration.  In particular,
# settings files can contain permission policy or instructions that a worker must not inherit.
CONFIG_CREDENTIAL_FILES: dict[str, str] = {
    "CLAUDE_CONFIG_DIR": ".credentials.json",
    "CODEX_HOME": "auth.json",
}


def config_dir_env(config: dict[str, Any] | None) -> dict[str, str]:
    """The `CLAUDE_CONFIG_DIR` / `CODEX_HOME` defaults `scrubbed_env` applies — each built-in
    harness's config-dir variable, defaulting to the *operator's* real home, overridden by
    `worker_env.config_dirs` — keyed by variable name. Factored out so the ssh runner's remote
    script (`runner/ssh.py`) can thread the same resolved values in as `export` lines: the
    remote login environment cannot compute "the operator's real home" itself, and a custom
    `worker_env.config_dirs` path is exactly what a remote host needs when its credentials
    live somewhere other than the default."""
    overrides = {str(k): str(v) for k, v in (((config or {}).get("worker_env") or {}).get("config_dirs") or {}).items()}
    real_home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    env = {var: overrides.get(var, str(real_home / rel)) for var, rel in DEFAULT_CONFIG_DIRS.items()}
    for var, val in overrides.items():  # a custom harness's own key, named explicitly
        env.setdefault(var, val)
    return env


def private_config_dir_env(config: dict[str, Any] | None, scratch_home: Path | str) -> dict[str, str]:
    """Build fresh harness homes below ``scratch_home`` and copy only each login file.

    ``config_dir_env`` identifies the operator-side source.  The returned paths are always
    private destinations, including when a caller explicitly passes HOME through.  Rebuilding
    them for every dispatch prevents settings written by one worker from reaching the next.
    """
    sources = config_dir_env(config)
    home = Path(scratch_home)
    destinations: dict[str, str] = {}
    for variable, credential in CONFIG_CREDENTIAL_FILES.items():
        destination = home / DEFAULT_CONFIG_DIRS[variable]
        try:
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir(parents=True, exist_ok=True)
            source = Path(sources[variable]) / credential
            if source.is_file():
                shutil.copyfile(source, destination / credential)
        except OSError:
            # The harness will report a normal authentication failure if its credential cannot
            # be read; a scrubbed environment must still be available for runners and checks.
            pass
        destinations[variable] = str(destination)
    for variable, source in sources.items():
        destinations.setdefault(variable, source)
    return destinations


def scrubbed_env(config: dict[str, Any] | None, setup: dict[str, Any] | None = None, *,
                 worktree: Path | str | None = None) -> dict[str, str]:
    """The scrubbed environment a worker (and its setup command) runs in: `PASS_ENV` plus
    the names or globs under `config['worker_env']['pass']`, then `setup['env']` on top.
    `CLAUDECODE` is always dropped so a garden can be driven from inside a Claude Code session.
    `HOME` is not inherited: unless `worker_env.pass` restores it, it is set to an isolated
    scratch home (`worker_home`), so neither the worker nor a branch's own test suite can read
    the operator's gh token, git credentials or ssh keys.

    The private HOME would also hide each harness's own saved login (claude's
    `~/.claude/.credentials.json`, codex's `~/.codex`), so `CLAUDE_CONFIG_DIR` and `CODEX_HOME`
    are set to the operator's real home by default, unless the operator already set them (they
    pass straight through the `CLAUDE_*` / `CODEX_*` allowlist) or `config['worker_env']
    ['config_dirs']` overrides them — keyed by the variable name, e.g. `{CLAUDE_CONFIG_DIR:
    /srv/claude-creds}`. A worker's isolated HOME then carries each harness's own config without
    exposing the rest of the operator's home."""
    patterns = pass_env_patterns(config)
    env = {k: v for k, v in os.environ.items() if any(fnmatch.fnmatchcase(k, p) for p in patterns)}
    env.pop("CLAUDECODE", None)
    if "HOME" not in env:  # dropped from PASS_ENV; give an isolated scratch home, not the operator's
        env["HOME"] = worker_home(worktree)
    # Do not let the CLAUDE_* / CODEX_* allowlist turn into a whole-home capability: replace
    # those source paths with fresh, credential-only directories for this dispatch.
    scratch_home = worker_home(worktree)
    env.update(private_config_dir_env(config, scratch_home))
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
    env = dict(env) if env is not None else scrubbed_env({}, setup, worktree=worktree)
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

    def probe(self, cwd: Path) -> dict[str, Any]:
        """A cheap, synchronous health check for a paused harness: the same minimal,
        no-permissions, no-tools invocation `garden doctor`'s login check uses
        (`Harness.login_probe`) — no run record, no worktree setup — just whether it
        responds and how (Harness.parse's env_error classification tells the caller whether
        it is still down). Not every runner supports this; the default says so instead of
        raising."""
        return {"final_text": "", "usage": {}, "cost_usd": None, "session_id": "", "result": {},
                "error": f"the {self.name} runner does not support probing", "env_error": False, "env_kind": ""}

    def doctor(self) -> list[str]:
        return []
