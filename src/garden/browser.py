"""Bounded browser-runtime readiness checks for capture-producing child processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .runner.base import scrubbed_env


def classify_browser_failure(detail: str) -> tuple[str, str]:
    """Turn Chromium's launch diagnostic into an actionable infrastructure category."""
    lower = detail.lower()
    if any(text in lower for text in ("executable doesn't exist", "executable not found", "browser executable missing")):
        return "missing_executable", "Chromium is not installed; install the configured Playwright Chromium browser without changing host privileges."
    if any(text in lower for text in ("error while loading shared libraries", "cannot open shared object file", "libnss3", "libnspr4")):
        return "missing_libraries", "Chromium cannot load its shared libraries; provide the runtime libraries in the capture environment (for example through an unprivileged library directory and worker_env.pass), without granting privileges."
    return "launch_failure", "Chromium was found but could not launch; inspect sandbox permissions and the child-process diagnostic."


def _probe_child() -> dict[str, str | bool]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return {"ready": False, "kind": "missing_executable", "diagnostic": f"Playwright is not installed: {exc}"}
    try:
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
            browser = playwright.chromium.launch()
            browser.close()
        return {"ready": True, "kind": "ready", "diagnostic": f"launched {executable}"}
    except Exception as exc:  # noqa: BLE001 - Playwright exposes launch failures as several types
        kind, action = classify_browser_failure(str(exc))
        return {"ready": False, "kind": kind, "diagnostic": f"{action} Launch detail: {exc}"}


def probe_browser_runtime(config: dict[str, Any], *, setup: dict[str, Any] | None = None,
                          worktree: Path | None = None, timeout: int = 20) -> dict[str, Any]:
    """Launch Chromium once in the final scrubbed environment used by capture checks.

    A comparison with the service environment makes an allowlist mismatch explicit. This
    probe never installs a browser, changes packages, or elevates privileges.
    """
    child_env = scrubbed_env(config, setup, worktree=worktree)
    command = [sys.executable, "-m", "garden.browser", "--probe-child"]

    def launch(env: dict[str, str]) -> dict[str, Any]:
        try:
            proc = subprocess.run(command, env=env, cwd=str(worktree) if worktree else None,
                                  capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"ready": False, "kind": "launch_failure", "diagnostic": f"Chromium launch timed out after {timeout}s."}
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            detail = (proc.stderr or proc.stdout or f"probe exited {proc.returncode}").strip()[-2000:]
            kind, action = classify_browser_failure(detail)
            return {"ready": False, "kind": kind, "diagnostic": f"{action} Launch detail: {detail}"}

    result = launch(child_env)
    if result.get("ready"):
        return result
    direct = launch(dict(os.environ))
    if direct.get("ready"):
        result["kind"] = "environment_mismatch"
        result["diagnostic"] = (str(result.get("diagnostic") or "") +
                                " Chromium launches in the service environment but not in the scrubbed capture child; pass the required variable through worker_env.pass or product setup.env.")
    return result


if __name__ == "__main__" and sys.argv[1:] == ["--probe-child"]:
    print(json.dumps(_probe_child()))
