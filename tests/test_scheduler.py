"""Command-level scheduler regressions kept at the legacy test path named by task briefs."""

from __future__ import annotations

import subprocess


def test_redispatch_kills_active_run(sched, monkeypatch):
    """A replacement worker must not overlap the worker it supersedes."""
    task = sched.store.task("DM-001")
    active = sched.runs.new_run(task.id, "local")
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    active.pid = proc.pid
    active.save()
    started_after: list[bool] = []
    original_dispatch = sched.dispatch

    def dispatch_after_stop(*args, **kwargs):
        started_after.append(proc.poll() is not None)
        return original_dispatch(*args, **kwargs)

    monkeypatch.setattr(sched, "dispatch", dispatch_after_stop)

    replacement = sched.redispatch(task)

    assert proc.poll() is not None
    assert started_after == [True]
    assert sched.runs.runs_for(task.id)[0].status == "superseded"
    assert replacement.worktree == str(sched.worktree_for(task))


def test_redispatch_refuses_when_worker_cannot_be_confirmed_dead(sched, monkeypatch):
    """An uncertain kill preserves the active record and never starts an overlapping run."""
    task = sched.store.task("DM-001")
    active = sched.runs.new_run(task.id, "local")
    monkeypatch.setattr(active, "stop", lambda: False)
    monkeypatch.setattr(sched.runs, "active", lambda: [active])
    monkeypatch.setattr(sched, "dispatch", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not dispatch")))

    import pytest

    with pytest.raises(RuntimeError, match="could not confirm"):
        sched.redispatch(task)
    assert active.status == "running"


def test_stop_does_not_trust_stale_exit_file(sched, monkeypatch):
    active = sched.runs.new_run("DM-001", "local")
    active.pid = 987654
    (active.path / "exit_code").write_text("0")
    monkeypatch.setattr("garden.runs._pid_alive", lambda _pid: True)
    monkeypatch.setattr("garden.runs._process_group_alive", lambda _pid: True)
    monkeypatch.setattr("garden.runs.os.killpg", lambda *_args: None)
    monkeypatch.setattr("garden.runs.os.kill", lambda *_args: None)
    assert active.stop(timeout=0) is False
