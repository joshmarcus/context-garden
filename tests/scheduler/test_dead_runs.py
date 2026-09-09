"""The dead-run sweep (CG-144): a `running` record whose process has already exited
is closed on the next tick even when no pointer in state leads a reap to it any more —
the generalisation of the orphan sweep (CG-116) to every run mode."""

import hashlib
import json

import pytest

from garden.model import Status
from garden.scheduler import TickReport
from tests.scheduler.conftest import stub_finished_run


def _finished_remote_run(sched, *, task_status=Status.DONE, exit_code=0):
    task = sched.store.task("DM-001")
    task.status = task_status
    sched.store.save(task)
    run = sched.runs.new_run(task.id, "remote", mode="revise")
    run.host = "worker-1"
    run.lease_token = "accepted-generation"
    run.pushed_ref = f"refs/heads/garden-worker/{run.run_id}/accepted"
    run.final_received_at = "2026-09-09T01:00:20+00:00"
    run.claim_history = [{
        "claimed_at": "2026-09-09T01:00:10+00:00",
        "host": run.host,
        "lease_token_sha256": hashlib.sha256(run.lease_token.encode()).hexdigest(),
        "pushed_ref": run.pushed_ref,
    }]
    run.save()
    (run.path / "remote_result.json").write_text(json.dumps({
        "result": {"status": "no_change", "summary": "already merged"},
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "cost_usd": 0.04,
        "final_text": "No source change was needed.",
        "error": "",
        "session_id": "remote-session",
    }))
    (run.path / "exit_code").write_text(str(exit_code))
    return task, run


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


def test_dead_run_failure_is_parked_during_manual_reservation(sched):
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    task.attempts = 1
    sched.store.save(task)
    run = sched.runs.new_run(task.id, "local", mode="work")
    run.pid = 999999
    run.save()
    reservation = sched.reserve_manual(task)

    rep = TickReport()
    sched.reap_dead_runs(rep)

    parked = sched.runs.latest(task.id)
    assert parked.status == "failed"
    assert parked.env_snapshot["parked_dead_run_reason"] == "process vanished"
    assert sched.store.task(task.id).status == Status.RUNNING
    assert sched.store.task(task.id).attempts == 1
    assert not sched.runs.active()

    sched.return_to_automation(
        sched.store.task(task.id), reservation_id=reservation["id"],
        expected=sched.manual_return_guard(sched.store.task(task.id)),
    )
    rep = TickReport()
    assert sched.reap(sched.store.task(task.id), rep)
    assert sched.store.task(task.id).status == Status.READY
    assert f"{task.id} -> ready (retry)" in rep.transitions


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


@pytest.mark.parametrize(
    ("task_status", "exit_code", "run_status"),
    [(Status.DONE, 0, "done"), (Status.CANCELLED, 0, "done"),
     (Status.WONT_DO, 1, "failed")],
)
def test_terminal_task_collects_accepted_remote_result_once(
    sched, task_status, exit_code, run_status
):
    task, run = _finished_remote_run(
        sched, task_status=task_status, exit_code=exit_code
    )
    task_before = task.path.read_text()
    state_before = json.dumps(sched.state.get(task.id), sort_keys=True)

    first = TickReport()
    sched.reap_dead_runs(first)
    second = TickReport()
    sched.reap_dead_runs(second)

    closed = next(r for r in sched.runs.runs_for(task.id) if r.run_id == run.run_id)
    assert closed.status == run_status
    assert closed.exit_code == exit_code and closed.finished_at
    assert closed.result == {"status": "no_change", "summary": "already merged"}
    assert closed.usage == {"input_tokens": 12, "output_tokens": 3}
    assert closed.cost_usd == 0.04 and closed.session_id == "remote-session"
    assert "accepted remote result was collected" in closed.error
    assert any(f"{run.run_id} closed (dangling)" in row for row in first.transitions)
    assert not second.transitions
    assert task.path.read_text() == task_before
    assert json.dumps(sched.state.get(task.id), sort_keys=True) == state_before
    events = [json.loads(line) for line in
              (sched.store.config.garden_dir / "events.jsonl").read_text().splitlines()]
    finished = [event for event in events
                if event.get("kind") == "run_finished" and event.get("run") == run.run_id]
    assert len(finished) == 1
    assert finished[0]["status"] == run_status and finished[0]["terminal_task"] is True


@pytest.mark.parametrize(
    "partial",
    [
        "missing_exit",
        "empty_exit",
        "malformed_exit",
        "stale_generation",
        "owned",
        "malformed_result",
    ],
)
def test_terminal_remote_sweep_rejects_partial_stale_or_owned_result(sched, partial):
    task, run = _finished_remote_run(sched)
    if partial == "missing_exit":
        (run.path / "exit_code").unlink()
    elif partial == "empty_exit":
        (run.path / "exit_code").write_text("")
    elif partial == "malformed_exit":
        (run.path / "exit_code").write_text("not-an-exit-code")
    elif partial == "stale_generation":
        run.lease_token = "replacement-generation"
        run.save()
    elif partial == "owned":
        sched.state.get(task.id)["review_run"] = run.run_id
        sched.state.save()
    else:
        (run.path / "remote_result.json").write_text("{not-json")

    rep = TickReport()
    sched.reap_dead_runs(rep)

    current = next(r for r in sched.runs.runs_for(task.id) if r.run_id == run.run_id)
    assert current.status == "running"
    assert not rep.transitions
    assert bool(rep.errors) is (partial == "malformed_result")


def test_remote_result_for_running_task_stays_with_normal_reap(sched):
    task, run = _finished_remote_run(sched, task_status=Status.RUNNING)

    rep = TickReport()
    sched.reap_dead_runs(rep)

    current = next(r for r in sched.runs.runs_for(task.id) if r.run_id == run.run_id)
    assert current.status == "running"
    assert not rep.transitions


def test_remote_result_for_recoverable_failed_task_is_not_terminally_collected(sched):
    task, run = _finished_remote_run(
        sched, task_status=Status.FAILED, exit_code=1
    )

    rep = TickReport()
    sched.reap_dead_runs(rep)

    current = next(r for r in sched.runs.runs_for(task.id) if r.run_id == run.run_id)
    assert current.status == "running"
    assert not rep.transitions
