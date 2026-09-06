"""The dead-run sweep (CG-144): a `running` record whose process has already exited
is closed on the next tick even when no pointer in state leads a reap to it any more —
the generalisation of the orphan sweep (CG-116) to every run mode."""

from garden.model import Status
from garden.scheduler import TickReport
from tests.scheduler.conftest import stub_finished_run


def test_dead_run_closed_when_nothing_points_at_it_any_more(sched):
    """A finished `revise` run left behind by a task that has since moved on to a
    terminal status (nothing will ever call `reap()` for it again) is closed on the
    next tick, with its cost recorded and a transition logged."""
    task = sched.store.task("DM-001")
    task.status = Status.FAILED
    sched.store.save(task)
    run = stub_finished_run(sched, "DM-001", "revise")

    rep = sched.tick()

    assert any(f"{run.run_id} closed (dangling)" in t for t in rep.transitions)
    closed = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run.run_id)
    assert closed.status == "done"
    assert closed.cost_usd == 0.02
    assert closed.finished_at


def test_dead_run_never_closed_while_its_task_is_running(sched):
    """The single active run a `running` task's own `reap()` still follows (via
    `RunStore.latest`) is never swept by the dead-run pass, even after its process has
    finished — that run belongs to the normal reap path, not this safety net."""
    sched.tick()  # DM-001 dispatched, work run running
    run = sched.runs.latest("DM-001")
    assert run.status == "running"

    rep = TickReport()
    sched.reap_dead_runs(rep)

    assert not any("dangling" in t for t in rep.transitions)
    assert sched.runs.latest("DM-001").status == "running"


def test_dead_run_sweep_closes_a_run_that_never_started(sched):
    """A pid-less running record has no worker process and is failed immediately."""
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    task.attempts = 1
    sched.store.save(task)
    run = sched.runs.new_run("DM-001", "local", mode="revise")  # no exit_code written
    (run.path / "stdout.json").write_text("worker output before disappearing\n")

    rep = TickReport()
    sched.reap_dead_runs(rep)

    assert any("process never started" in t for t in rep.transitions)
    still = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run.run_id)
    assert still.status == "failed" and still.error == "process never started"
    assert still.finished_at
    assert sched.store.task("DM-001").status == Status.FAILED


def test_dead_run_sweep_closes_a_vanished_process(sched):
    """A run whose pid died before writing an exit code follows worker-crash handling."""
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    task.attempts = 1
    sched.store.save(task)
    run = sched.runs.new_run("DM-001", "local", mode="work")
    run.pid = 999999
    run.save()

    rep = TickReport()
    sched.reap_dead_runs(rep)

    assert any("process vanished" in t for t in rep.transitions)
    assert sched.runs.latest("DM-001").status == "failed"
    assert sched.store.task("DM-001").status == Status.READY


def test_dead_run_sweep_never_touches_manual_runs(sched):
    """A manual run's record is only ever finalised by `garden finish`; a dead-looking
    record it left behind (exit_code written, task moved on) is not this sweep's to
    close."""
    task = sched.store.task("DM-001")
    task.status = Status.FAILED
    sched.store.save(task)
    run = stub_finished_run(sched, "DM-001", "work", cost=0.03)
    run.runner = "manual"
    run.save()

    rep = TickReport()
    sched.reap_dead_runs(rep)

    assert not any("dangling" in t for t in rep.transitions)
    still = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run.run_id)
    assert still.status == "running"
