"""Helpers shared by the scheduler tests. The `garden`, `sched` and `fake_github` fixtures
and `wait_for_runs` come from tests/conftest.py."""

import json
import os


def statuses(sched):
    sched.store.invalidate()
    return {tid: t.status.value for tid, t in sched.store.tasks().items()}


def wait_for_stdout(run, timeout=5.0):
    import time
    from pathlib import Path
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (Path(run.dir) / "stdout.json").exists():
            return
        time.sleep(0.02)


def make_idle(run, minutes):
    """Backdate every file the idle check looks at so the run appears silent for `minutes`."""
    import time
    from pathlib import Path
    old = time.time() - minutes * 60
    for base in (Path(run.dir), Path(run.worktree)):
        for p in [base, *base.rglob("*")]:
            try:
                os.utime(p, (old, old))
            except OSError:
                pass


def stub_finished_run(sched, task_id, mode, cost=0.02):
    """A run whose process has already finished, written straight to disk without a
    live worker. Used to place a finished run in front of the orphan sweep."""
    run = sched.runs.new_run(task_id, "local", mode=mode)
    (run.path / "stdout.json").write_text(json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "done",
         "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": cost}))
    (run.path / "exit_code").write_text("0")
    return run
