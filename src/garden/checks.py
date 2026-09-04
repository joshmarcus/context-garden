"""Token-free checks: pluggable scripts or Python callables that inspect a worktree or a
red CI run and return structured findings. No model is involved.

garden.yaml:

    checks:
      pre_pr:                       # run in the worktree before a PR is opened / updated;
        - name: tests               # if omitted, defaults to the product's setup.test / setup.lint
          command: "make test"      # (run with setup.env) — nothing here assumes a venv
        - name: lint
          command: "make lint"
      ci:                           # run when a PR's CI goes red; results feed the revise brief
        - name: ci-log
          python: "garden.checks:local_command_check"
          command: "scripts/ci_failures.sh"   # your script: fetch the failing job log from your CI
          flaky_patterns: ["ETIMEDOUT", "rate limit"]
          retry_command: "scripts/ci_rerun.sh" # run instead of a revise round when the log looks flaky
      timeout_seconds: 600

A `command` check passes on exit 0. If its stdout is a JSON object it is used as the
result; otherwise the last lines of output become the details. A `python` check is
`module:function(ctx, spec) -> dict`. Result shape:

    {"name": ..., "status": "pass" | "fail" | "flaky" | "error", "summary": "...",
     "details": "...", "retry_command": "..." (optional, for flaky)}

Both kinds of check run with `GARDEN_<KEY>` env vars for every key in `ctx` (e.g.
`GARDEN_TASK_ID`, `GARDEN_BRANCH`), plus `GARDEN_EXEC_ROOT` set to the live garden's own
root — use `$GARDEN_EXEC_ROOT/.venv/bin/python` from a check that needs the garden's tools
rather than the product's. `GARDEN_ROOT` is always overridden to a non-existent sentinel:
a check command must not act on the live garden (see `garden.config.find_root`). For a
`python:` check this is enforced by overriding `GARDEN_ROOT` in the *process* environment
for the duration of the call (checks run sequentially, not concurrently), so any subprocess
your callable launches inherits the guard even if it builds its own env from `os.environ`
without going through `ctx`.

A product's tests must not depend on `GARDEN_ROOT` or `GARDEN_EXEC_ROOT`: the pre_pr
`tests` check runs them with these variables set (as above), so a suite that reads them
directly, or that calls into garden internals that do (e.g. `find_root`), passes or fails
depending on who invoked it rather than on the code under test. A product's own test suite
should clear both in an autouse fixture (see this repo's own `tests/conftest.py` for the
pattern) so it behaves the same in a developer's shell, in CI, and under this check runner.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import no_live_garden_root

MAX_DETAILS = 4000


@contextlib.contextmanager
def _guarded_process_env(cwd: Path | None):
    """Force GARDEN_ROOT to a non-existent sentinel in the process environment for the
    duration of a `python:` check, then restore it. Guards custom callables that spawn
    subprocesses without routing through `ctx`'s GARDEN_ROOT override."""
    prev = os.environ.get("GARDEN_ROOT")
    os.environ["GARDEN_ROOT"] = no_live_garden_root(cwd or Path.cwd())
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("GARDEN_ROOT", None)
        else:
            os.environ["GARDEN_ROOT"] = prev


def run_check(spec: dict[str, Any], ctx: dict[str, Any], cwd: Path | None = None, timeout: int = 600) -> dict[str, Any]:
    name = str(spec.get("name") or spec.get("command") or spec.get("python") or "check")
    try:
        if spec.get("python"):
            mod, _, fn = str(spec["python"]).partition(":")
            func = getattr(importlib.import_module(mod), fn or "check")
            with _guarded_process_env(cwd):
                out = func(ctx, spec) or {}
            if not isinstance(out, dict):
                raise TypeError("python check must return a dict")
            out.setdefault("name", name)
            out.setdefault("status", "pass")
            return _trim(out)
        if spec.get("command"):
            env = dict(os.environ)
            env.update({f"GARDEN_{k.upper()}": (json.dumps(v) if not isinstance(v, str) else v) for k, v in ctx.items()})
            for k, v in (spec.get("env") or {}).items():  # the product's prepared environment
                env[str(k)] = str(v)
            # The sentinel wins over any product env: a check must never act on the live garden.
            env["GARDEN_ROOT"] = no_live_garden_root(Path(cwd) if cwd else Path.cwd())
            proc = subprocess.run(
                str(spec["command"]), shell=True, cwd=str(cwd) if cwd else None, env=env,
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            text = (proc.stdout or "").strip()
            tail = "\n".join(((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()[-40:])
            # A signalled (negative return code, e.g. killed with the server) result did not
            # run to a verdict — even if it printed JSON before it died. Classify the signal
            # before trusting any structured output, so a killed check is never mistaken for a
            # pass (or a clean failure with text to revise against).
            if proc.returncode < 0:
                import signal

                try:
                    why = f"killed by {signal.Signals(-proc.returncode).name}"
                except ValueError:
                    why = f"killed by signal {-proc.returncode}"
                return _trim({"name": name, "status": "error", "summary": f"check did not finish ({why})", "details": tail})
            if text.startswith("{"):
                try:
                    data = json.loads(text.splitlines()[-1] if "\n" in text and not text.endswith("}") else text)
                    if isinstance(data, dict):
                        data.setdefault("name", name)
                        data.setdefault("status", "pass" if proc.returncode == 0 else "fail")
                        return _trim(data)
                except json.JSONDecodeError:
                    pass
            if proc.returncode == 0:
                return {"name": name, "status": "pass", "summary": "ok", "details": ""}
            # An output-less non-zero exit did not run to a verdict either: nothing to revise
            # against, so record it as "check did not finish" rather than an empty failure.
            if not tail.strip():
                return _trim({"name": name, "status": "error", "summary": f"check did not finish (exit {proc.returncode}, no output)", "details": tail})
            return _trim({"name": name, "status": "fail", "summary": f"exit {proc.returncode}", "details": tail})
        return {"name": name, "status": "error", "summary": "check has neither command nor python", "details": ""}
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "error", "summary": f"timed out after {timeout}s", "details": ""}
    except Exception as e:  # noqa: BLE001
        return {"name": name, "status": "error", "summary": f"{type(e).__name__}: {e}", "details": ""}


def run_checks(specs: list[dict[str, Any]], ctx: dict[str, Any], cwd: Path | None = None, timeout: int = 600) -> list[dict[str, Any]]:
    return [run_check(s, ctx, cwd, timeout) for s in specs or []]


def failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in results if r.get("status") in ("fail", "error")]


def to_feedback(results: list[dict[str, Any]], heading: str) -> str:
    """Markdown for the revise brief."""
    lines = []
    for r in results:
        if r.get("status") == "pass":
            continue
        lines.append(f"- **{heading}** `{r.get('name')}` {r.get('status')}: {r.get('summary', '')}")
        if r.get("details"):
            fence = "````" if "```" in r["details"] else "```"
            lines.append(f"\n  {fence}\n" + "\n".join("  " + ln for ln in str(r["details"]).splitlines()) + f"\n  {fence}")
    return "\n".join(lines)


def _trim(r: dict[str, Any]) -> dict[str, Any]:
    d = str(r.get("details") or "")
    if len(d) > MAX_DETAILS:
        r["details"] = "…\n" + d[-MAX_DETAILS:]
    return r


# ---- built-in helpers for writing CI analysers ------------------------------------
ERROR_RE = re.compile(r"(error|fail|traceback|exception|assert|panic|✗|✖|FAILED)", re.IGNORECASE)


def interesting_lines(log: str, max_lines: int = 40, tail: int = 20) -> list[str]:
    """Keep the lines of a CI log that look like failures (or the tail when none match).
    Use from your own `python:` analyser after fetching the log from whatever CI you run."""
    lines = [ln.rstrip() for ln in log.splitlines() if ln.strip()]
    hits = [ln[-240:] for ln in lines if ERROR_RE.search(ln)]
    return hits[-max_lines:] if hits else lines[-tail:]


def classify_log(log: str, flaky_patterns: list[str] | None = None) -> str:
    """'flaky' when the log matches any pattern, else 'fail'."""
    for p in flaky_patterns or []:
        if re.search(p, log, re.IGNORECASE):
            return "flaky"
    return "fail"


def local_command_check(ctx: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Example `python:` analyser: run `spec['command']` (e.g. a script that queries your CI
    system) and turn its output into a result using the helpers above."""
    worktree = ctx.get("worktree") or None
    env = {**os.environ, **{f"GARDEN_{k.upper()}": str(v) for k, v in ctx.items()}}
    env["GARDEN_ROOT"] = no_live_garden_root(Path(worktree) if worktree else Path.cwd())
    proc = subprocess.run(str(spec.get("command") or "true"), shell=True, capture_output=True, text=True, check=False,
                          cwd=worktree, env=env)
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode == 0:
        return {"status": "pass", "summary": "ok", "details": ""}
    status = classify_log(log, spec.get("flaky_patterns"))
    out: dict[str, Any] = {"status": status, "summary": f"exit {proc.returncode}", "details": "\n".join(interesting_lines(log))}
    if status == "flaky" and spec.get("retry_command"):
        out["retry_command"] = str(spec["retry_command"])
    return out


# ---- optional: GitHub Actions analyser (needs the gh CLI; enable per environment) ----
NOISE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*)?(##\[|\[command\]|Post job|Cleaning up)")


def github_actions_failures(ctx: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Fetch failed GitHub Actions job logs for the PR head and keep the lines that matter.
    Opt in from garden.yaml (or a per-environment overlay) with
    `python: "garden.checks:github_actions_failures"`; the garden itself never assumes
    Actions is available."""
    import shutil

    gh = shutil.which("gh")
    slug, branch = ctx.get("repo_slug", ""), ctx.get("branch", "")
    if not gh or not slug or not branch:
        return {"status": "error", "summary": "gh CLI or repo/branch context missing", "details": ""}
    proc = subprocess.run([gh, "run", "list", "-R", slug, "--branch", branch, "--limit", "10",
                           "--json", "databaseId,name,conclusion,headSha,status"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return {"status": "error", "summary": proc.stderr.strip()[-300:], "details": ""}
    runs = json.loads(proc.stdout or "[]")
    head = ctx.get("head_sha", "")
    failed = [r for r in runs if r.get("conclusion") in ("failure", "timed_out", "cancelled") and (not head or r.get("headSha") == head)]
    if not failed:
        return {"status": "pass", "summary": "no failed workflow runs on this head", "details": ""}
    details: list[str] = []
    flaky_ids: list[int] = []
    for r in failed:
        log = subprocess.run([gh, "run", "view", str(r["databaseId"]), "-R", slug, "--log-failed"], capture_output=True, text=True, check=False).stdout
        clean = "\n".join(ln for ln in log.splitlines() if not NOISE_RE.match(ln))
        details.append(f"### {r.get('name')} (run {r['databaseId']})\n" + "\n".join(interesting_lines(clean, int(spec.get("max_lines", 40)))))
        if classify_log(clean, spec.get("flaky_patterns")) == "flaky":
            flaky_ids.append(int(r["databaseId"]))
    if flaky_ids and len(flaky_ids) == len(failed):
        out: dict[str, Any] = {"status": "flaky", "summary": f"{len(flaky_ids)} run(s) matched flaky patterns", "details": "\n\n".join(details)}
        if spec.get("rerun"):
            out["retry_command"] = " && ".join(f"{gh} run rerun {i} -R {slug} --failed" for i in flaky_ids)
        return out
    return {"status": "fail", "summary": f"{len(failed)} failed workflow run(s): " + ", ".join(str(r.get("name")) for r in failed),
            "details": "\n\n".join(details)}
