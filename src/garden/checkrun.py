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
import subprocess
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
    if not specs:
        return []
    cwd = Path(payload["cwd"]) if payload.get("cwd") else None
    setup = payload.get("setup") or {}
    config = payload.get("config") or {}
    temp_dir = str(payload.get("temp_dir") or "")
    temp_env = {"TMPDIR": temp_dir, "PYTEST_DEBUG_TEMPROOT": temp_dir} if temp_dir else {}
    # A pre-PR / base-probe check runs in a worktree that exists (its caller guards that); a CI
    # analyser (`checks.ci`) may run with no worktree at all. Only prepare an env when there is a
    # worktree to prepare.
    if cwd is not None and cwd.exists():
        try:
            setup_env = scrubbed_env(config, setup, worktree=cwd)
            setup_env.update(temp_env)
            run_setup(cwd, setup, log_path=cwd.parent / f".garden-setup-{cwd.name}.log", env=setup_env)
        except RunnerError as e:
            return [{"name": "setup", "status": "fail", "summary": "setup command failed", "details": str(e)}]
    elif cwd is not None and not cwd.exists():
        cwd = None  # do not run command checks in a worktree that isn't there
    specs = [{**spec, "env": {**(spec.get("env") or {}), **temp_env}} for spec in specs]
    results = run_checks(specs, payload.get("ctx") or {}, cwd=cwd,
                         timeout=int(payload.get("timeout") or 600), config=config)
    if payload.get("ci_rerun"):
        # A wholly-flaky CI verdict reruns CI here, in the detached job — not in the tick — so
        # the scheduler only reads the outcome (`reran`) on the reap (CG-182).
        nonpass = [r for r in results if r.get("status") != "pass"]
        flaky_results = [r for r in results if r.get("status") == "flaky"]
        if flaky_results and len(flaky_results) == len(nonpass):
            # The retry command comes only from config (checks.run_check strips any that a
            # check's output injected) and runs scrubbed — no GitHub token, cloud credentials
            # or the operator's HOME unless `worker_env.pass` names them — so a flaky rerun
            # cannot become a channel for a branch to run a privileged shell command.
            retry_env = scrubbed_env(config, worktree=cwd)
            retry_env.update(temp_env)
            for r in flaky_results:
                if r.get("retry_command"):
                    subprocess.run(str(r["retry_command"]), shell=True, check=False, capture_output=True,
                                   timeout=120, cwd=str(cwd) if cwd else None, env=retry_env)
                    r["reran"] = True
    return results


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    run_dir = Path(argv[0])
    try:
        payload = json.loads((run_dir / "checks_input.json").read_text())
        results = run_check_job(payload)
    except Exception as e:  # noqa: BLE001
        # Always leave a structured result for the scheduler to reap.  A detached check
        # process can fail before run_check_job gets to its normal result handling (for
        # example, while importing a check plugin); without this record reap reports the
        # less useful "check run produced no results" and loses the actual exception.
        results = [{"name": "checks", "status": "error",
                    "summary": f"check runner failed: {type(e).__name__}: {e}", "details": ""}]
    (run_dir / "checks.json").write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
