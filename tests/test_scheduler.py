"""Command-level scheduler regressions kept at the legacy test path named by task briefs."""

from __future__ import annotations

from garden.runs import Run


def test_redispatch_kills_active_run(sched, monkeypatch):
    """A replacement worker must not overlap the worker it supersedes."""
    task = sched.store.task("DM-001")
    active = sched.runs.new_run(task.id, "local")
    killed: list[str] = []
    monkeypatch.setattr(Run, "kill", lambda run: killed.append(run.run_id))

    replacement = sched.redispatch(task)

    assert killed == [active.run_id]
    assert sched.runs.runs_for(task.id)[0].status == "superseded"
    assert replacement.worktree == str(sched.worktree_for(task))
