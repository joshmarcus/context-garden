"""The orphan sweep (CG-116): finished verdict runs whose task moved on are closed; nothing else is touched."""


from garden.github import Feedback
from garden.model import Status
from garden.scheduler import TickReport
from tests.conftest import wait_for_runs
from tests.scheduler.conftest import statuses, stub_finished_run


def test_orphan_sweep_leaves_finished_revise_run_of_running_task(sched, fake_github):
    """CG-098: a revise run that finishes in the same tick as the sweep — after its
    task's reap has already run and seen it unfinished — must be left for the next
    reap, never swept out from under the task."""
    sched.tick()
    wait_for_runs(sched)
    sched.tick()  # DM-001 -> in_review, PR opened
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "fix this", "created": "2099-01-01T00:00:00Z"}])
    sched.tick()  # -> changes_requested -> revise dispatched in the same tick
    assert statuses(sched)["DM-001"] == "running"
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise" and run.status == "running"

    # the revise run finishes; the sweep now fires while the task is still running
    wait_for_runs(sched)
    rep = TickReport()
    sched.reap_orphaned(rep)
    assert not any("orphaned" in t for t in rep.transitions)
    assert sched.runs.latest("DM-001").status == "running"  # untouched by the sweep

    # the normal reap finalises it on the next tick; the task never bounced to ready
    rep = sched.tick()
    assert not any("orphaned" in t for t in rep.transitions)
    assert statuses(sched)["DM-001"] == "in_review"
    assert "no active run found" not in sched.store.task("DM-001").body


def test_orphan_sweep_closes_verdict_runs_of_moved_on_tasks(sched):
    """The sweep closes a finished review/persona/compare run once its task has moved
    on to a status where no verdict can be applied."""
    for status in (Status.DONE, Status.CANCELLED, Status.FAILED):
        for mode in ("review", "persona", "compare"):
            task = sched.store.task("DM-001")
            task.status = status
            sched.store.save(task)
            run = stub_finished_run(sched, "DM-001", mode)
            rep = TickReport()
            sched.reap_orphaned(rep)
            assert any(f"{run.run_id} closed (orphaned)" in t for t in rep.transitions), (status, mode)
            closed = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run.run_id)
            assert closed.status in ("done", "failed"), (status, mode)
            assert closed.cost_usd == 0.02  # usage/cost still recorded


def test_orphan_sweep_closes_verdict_run_when_pr_closed(sched):
    """A PR that is closed or merged also moots the verdict even if the task status
    has not caught up yet."""
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    sched.state.get("DM-001")["pr_state"] = "MERGED"
    run = stub_finished_run(sched, "DM-001", "review")
    rep = TickReport()
    sched.reap_orphaned(rep)
    assert any(f"{run.run_id} closed (orphaned)" in t for t in rep.transitions)


def test_orphan_sweep_leaves_verdict_runs_of_tasks_still_in_review(sched):
    """A task still in review can still receive a verdict, so its finished
    review/persona/compare run is reaped by the normal path, not swept."""
    for mode in ("review", "persona", "compare"):
        task = sched.store.task("DM-001")
        task.status = Status.IN_REVIEW
        sched.store.save(task)
        run = stub_finished_run(sched, "DM-001", mode)
        rep = TickReport()
        sched.reap_orphaned(rep)
        assert not any("orphaned" in t for t in rep.transitions), mode
        still = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run.run_id)
        assert still.status == "running", mode


def test_orphan_sweep_never_touches_work_runs(sched):
    """Even for a task that has moved on, the sweep never closes a work/resume/trial
    run: those belong to the task's own reap path."""
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    sched.store.save(task)
    for mode in ("work", "revise", "resume", "trial"):
        run = stub_finished_run(sched, "DM-001", mode)
        rep = TickReport()
        sched.reap_orphaned(rep)
        assert not any("orphaned" in t for t in rep.transitions), mode
        still = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run.run_id)
        assert still.status == "running", mode
