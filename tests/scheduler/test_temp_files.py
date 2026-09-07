"""Disk-backed temporary files and conservative terminal-worktree cleanup."""

from __future__ import annotations

import os
import time

from garden.model import Status


def test_local_worker_and_check_temp_dirs_are_reaped(sched, monkeypatch):
    """Workers, setup and detached checks share one private disk temp directory per run."""
    seen = sched.worktree_for(sched.store.task("DM-001")) / "worker-env.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ENV_DUMP", str(seen))
    sched.tick()  # dispatch worker
    worker = sched.runs.latest("DM-001")
    worker_tmp = sched.cfg.work_dir / "tmp" / worker.run_id
    assert worker_tmp.is_dir()
    worker_env = dict(line.split("=", 1) for line in seen.read_text().splitlines() if "=" in line)
    assert worker_env["TMPDIR"] == str(worker_tmp)
    assert worker_env["PYTEST_DEBUG_TEMPROOT"] == str(worker_tmp)

    sched.tick()  # reap worker
    assert not worker_tmp.exists()

    task = sched.store.task("DM-001")
    check = sched.runs.new_run(task.id, "local", mode="check")
    check.worktree = str(sched.worktree_for(task))
    check.save()
    runner = sched.runner_for(task, "local")
    runner.start_checks(check, sched.worktree_for(task), {
        "specs": [{"name": "env", "command": "printf '%s|%s' \"$TMPDIR\" \"$PYTEST_DEBUG_TEMPROOT\" > check-env.txt"}],
        "cwd": str(sched.worktree_for(task)), "config": sched.cfg.data,
    })
    check_tmp = sched.cfg.work_dir / "tmp" / check.run_id
    assert check_tmp.is_dir()
    assert (sched.worktree_for(task) / "check-env.txt").read_text() == f"{check_tmp}|{check_tmp}"
    check.status = "done"
    check.save()
    sched._cleanup_reaped_temp_dirs()
    assert not check_tmp.exists()


def test_terminal_worktree_sweep_prunes_caches_but_skips_active_runs(sched):
    task = sched.store.task("DM-001")
    sched.tick()  # materialise its linked worktree
    worktree = sched.worktree_for(task)
    for cache in (worktree / ".venv", worktree / ".pytest_cache", worktree / "pkg" / "__pycache__"):
        cache.mkdir(parents=True)
        (cache / "artifact").write_text("x")
    task.status = Status.DONE
    sched.store.save(task)
    active = sched.runs.new_run(task.id, "local")

    sched.tick(dispatch=False)
    assert (worktree / ".venv").exists()  # never remove a worktree a run still uses

    active.status = "done"
    active.save()
    sched.tick(dispatch=False)
    assert not (worktree / ".venv").exists()
    assert not (worktree / ".pytest_cache").exists()
    assert not (worktree / "pkg" / "__pycache__").exists()

    old = time.time() - 3 * 86400
    os.utime(worktree, (old, old))
    sched.tick(dispatch=False)
    assert not worktree.exists()


def test_temp_cleanup_requires_terminal_record_and_inactive_process(sched, monkeypatch):
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local")
    temp = sched.cfg.work_dir / "tmp" / run.run_id
    temp.mkdir(parents=True)
    run.status = "done"
    run.pid = 424242
    run.save()
    monkeypatch.setattr(type(run), "process_finished", lambda self: False)

    sched._cleanup_reaped_temp_dirs()
    assert temp.exists()

    monkeypatch.setattr(type(run), "process_finished", lambda self: True)
    sched._cleanup_reaped_temp_dirs()
    assert not temp.exists()
