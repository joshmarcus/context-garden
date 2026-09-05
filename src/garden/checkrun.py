"""Run a set of token-free checks as a detached job.

A check run is a run record like a review: the tick starts it (a detached process, or the
in-process runner in tests) and reaps it on a later tick, so a product's test suite never
runs inside `tick()` and never blocks the web UI (CG-182). The job prepares the worktree's
environment (marker-guarded, so a worktree the worker already set up pays nothing), runs
the check specs and writes the results to `checks.json` beside the run record.

`main()` is the entry point the local runner launches as `python -m garden.checkrun <dir>`;
`run_check_job()` is the same work as a callable, used by the in-process test runner.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .checks import run_checks
from .runner.base import RunnerError, run_setup, scrubbed_env


def run_check_job(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Prepare the environment, run the specs and return the results. `payload` carries the
    specs, the check context, the worktree, the product's setup block, the timeout and the
    garden config (for the scrubbed environment). An empty spec list or a missing worktree is
    a no-op."""
    specs = payload.get("specs") or []
    cwd = Path(payload["cwd"]) if payload.get("cwd") else None
    if not specs or cwd is None or not cwd.exists():
        return []
    setup = payload.get("setup") or {}
    config = payload.get("config") or {}
    try:
        run_setup(cwd, setup, log_path=cwd.parent / f".garden-setup-{cwd.name}.log",
                  env=scrubbed_env(config, setup))
    except RunnerError as e:
        return [{"name": "setup", "status": "fail", "summary": "setup command failed", "details": str(e)}]
    return run_checks(specs, payload.get("ctx") or {}, cwd=cwd,
                      timeout=int(payload.get("timeout") or 600), config=config)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    run_dir = Path(argv[0])
    payload = json.loads((run_dir / "checks_input.json").read_text())
    results = run_check_job(payload)
    (run_dir / "checks.json").write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
