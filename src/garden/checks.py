"""Token-free checks: pluggable scripts or Python callables that inspect a worktree or a
red CI run and return structured findings. No model is involved.

garden.yaml:

    checks:
      pre_pr:                       # run in the worktree before a PR is opened / updated
        - name: tests
          command: ".venv/bin/pytest -q -x"
        - name: lint
          command: ".venv/bin/ruff check src"
      ci:                           # run when a PR's CI goes red; results feed the revise brief
        - name: actions
          python: "garden.checks:github_actions_failures"
          flaky_patterns: ["ETIMEDOUT", "rate limit"]
          rerun: true               # rerun flaky jobs instead of dispatching a revise run
      timeout_seconds: 600

A `command` check passes on exit 0. If its stdout is a JSON object it is used as the
result; otherwise the last lines of output become the details. A `python` check is
`module:function(ctx, spec) -> dict`. Result shape:

    {"name": ..., "status": "pass" | "fail" | "flaky" | "error", "summary": "...",
     "details": "...", "retry_command": "..." (optional, for flaky)}
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

MAX_DETAILS = 4000


def run_check(spec: dict[str, Any], ctx: dict[str, Any], cwd: Path | None = None, timeout: int = 600) -> dict[str, Any]:
    name = str(spec.get("name") or spec.get("command") or spec.get("python") or "check")
    try:
        if spec.get("python"):
            mod, _, fn = str(spec["python"]).partition(":")
            func = getattr(importlib.import_module(mod), fn or "check")
            out = func(ctx, spec) or {}
            if not isinstance(out, dict):
                raise TypeError("python check must return a dict")
            out.setdefault("name", name)
            out.setdefault("status", "pass")
            return _trim(out)
        if spec.get("command"):
            env = dict(os.environ)
            env.update({f"GARDEN_{k.upper()}": (json.dumps(v) if not isinstance(v, str) else v) for k, v in ctx.items()})
            proc = subprocess.run(
                str(spec["command"]), shell=True, cwd=str(cwd) if cwd else None, env=env,
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            text = (proc.stdout or "").strip()
            if text.startswith("{"):
                try:
                    data = json.loads(text.splitlines()[-1] if "\n" in text and not text.endswith("}") else text)
                    if isinstance(data, dict):
                        data.setdefault("name", name)
                        data.setdefault("status", "pass" if proc.returncode == 0 else "fail")
                        return _trim(data)
                except json.JSONDecodeError:
                    pass
            tail = "\n".join(((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()[-40:])
            if proc.returncode == 0:
                return {"name": name, "status": "pass", "summary": "ok", "details": ""}
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


# ---- built-in: GitHub Actions failure analyser (needs the gh CLI) ------------------
ERROR_RE = re.compile(r"(error|fail|traceback|exception|assert|panic|✗|✖|FAILED)", re.IGNORECASE)
NOISE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*)?(##\[|\[command\]|Post job|Cleaning up)")


def github_actions_failures(ctx: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Pull the failed job logs for the PR's head branch and extract the lines that matter.
    Flags runs as flaky when the log matches `flaky_patterns`; with `rerun: true` returns a
    `retry_command` the scheduler runs instead of spending a revise round."""
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
    patterns = [re.compile(p, re.IGNORECASE) for p in (spec.get("flaky_patterns") or [])]
    details: list[str] = []
    flaky_ids: list[int] = []
    for r in failed:
        log = subprocess.run([gh, "run", "view", str(r["databaseId"]), "-R", slug, "--log-failed"], capture_output=True, text=True, check=False).stdout
        lines = [ln for ln in log.splitlines() if not NOISE_RE.match(ln)]
        hits = [ln[-240:] for ln in lines if ERROR_RE.search(ln)]
        picked = hits[-int(spec.get("max_lines", 40)):] if hits else lines[-20:]
        details.append(f"### {r.get('name')} (run {r['databaseId']})\n" + "\n".join(picked))
        if patterns and any(p.search(log) for p in patterns):
            flaky_ids.append(int(r["databaseId"]))
    if flaky_ids and len(flaky_ids) == len(failed):
        out: dict[str, Any] = {"status": "flaky", "summary": f"{len(flaky_ids)} run(s) matched flaky patterns", "details": "\n\n".join(details)}
        if spec.get("rerun"):
            out["retry_command"] = " && ".join(f"{gh} run rerun {i} -R {slug} --failed" for i in flaky_ids)
        return out
    return {"status": "fail", "summary": f"{len(failed)} failed workflow run(s): " + ", ".join(str(r.get("name")) for r in failed),
            "details": "\n\n".join(details)}
