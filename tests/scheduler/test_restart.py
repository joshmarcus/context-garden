"""CG-198: a restart reaps finished-but-unreaped runs of every mode before its first tick, a
dispatch onto a dirty worktree stashes and continues, and run_finished is emitted once per run."""

from garden import gitops
from garden.scheduler import Scheduler
from garden.store import Store
from tests.scheduler.conftest import statuses


def _restart(sched, fake_github) -> Scheduler:
    """A brand-new scheduler over the same garden, as a fresh process would build on restart."""
    return Scheduler(Store(sched.store.root), github=fake_github, log=print)


def test_reap_on_start_applies_a_finished_but_unreaped_review(sched, fake_github, monkeypatch):
    """A scheduler is killed after a review worker finished but before its verdict was reaped.
    A new process reaps on start — before it ticks — and applies the verdict, so the review is
    not lost and no fresh review is needed."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    sched.tick()  # dispatch work
    sched.tick()  # reap work -> PR opened -> review dispatched (finishes in-process, not yet reaped)

    st = sched.state.get("DM-001")
    run_id = st["review_run"]
    assert run_id
    run = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run_id)
    assert run.status == "running" and run.process_finished()  # finished, not yet reaped
    assert statuses(sched)["DM-001"] == "in_review"

    fresh = _restart(sched, fake_github)
    fresh.reap_on_start()

    assert statuses(fresh)["DM-001"] == "changes_requested"  # the verdict was applied
    reaped = next(r for r in fresh.runs.runs_for("DM-001") if r.run_id == run_id)
    assert reaped.status == "done"
    assert fresh.state.get("DM-001").get("pending_feedback")
    assert not fresh.state.get("DM-001").get("review_run")


def test_reap_on_start_reapplies_a_verdict_lost_before_state_was_saved(sched, fake_github, monkeypatch):
    """The harder case: the previous process *did* reap the review (the run record is terminal
    with its verdict) but was killed before state.json recorded the effect, so the task is still
    in_review and no revise round is pending. The restart re-applies the stored verdict once,
    without emitting run_finished a second time."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    sched.tick()  # DM-001 in_review, PR opened, review dispatched

    st = sched.state.get("DM-001")
    run_id = st["review_run"]
    run = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run_id)
    # Simulate a reap the old process finished on disk but never persisted to state: the run
    # record is terminal with its verdict, and run_finished was already emitted for it, but the
    # transition / pending_feedback never reached state.json.
    run.status = "done"
    run.result = {"verdict": "request_changes", "summary": "criteria not met",
                  "description_ok": True, "findings": [
                      {"severity": "blocking", "file": "a.py", "line": 1, "summary": "bug"}]}
    run.cost_usd = 0.02
    run.save()
    sched.events.emit("run_finished", "DM-001", run=run_id, mode="review", cost_usd=0.02, usage={}, status="request_changes")
    sched.state.save()
    assert statuses(sched)["DM-001"] == "in_review"

    fresh = _restart(sched, fake_github)
    fresh.reap_on_start()

    assert statuses(fresh)["DM-001"] == "changes_requested"  # the lost verdict was re-applied
    assert fresh.state.get("DM-001").get("pending_feedback")
    assert fresh.state.get("DM-001").get("last_review_run") == run_id
    finished = [e for e in fresh.events.read(task_id="DM-001", kinds=["run_finished"]) if e.get("run") == run_id]
    assert len(finished) == 1  # not re-emitted: cost is counted once


def test_dirty_worktree_is_stashed_on_dispatch(sched, fake_github):
    """A dispatch that lands on a worktree a killed worker left dirty stashes the leftover edits
    under a named stash, records it on the task, and starts the new run from a clean tree."""
    sched.cfg.data["stack"] = False
    sched.tick()  # dispatch work
    sched.tick()  # reap -> in_review, PR opened

    t = sched.store.task("DM-001")
    wt = sched.worktree_for(t)
    (wt / "leftover.txt").write_text("half-done edits from a killed worker\n")
    assert gitops.has_uncommitted_changes(wt)

    sched.triage(t, changes="please revisit")  # -> changes_requested with pending feedback
    sched.dispatch(sched.store.task("DM-001"), mode="revise")

    assert not gitops.has_uncommitted_changes(wt)  # the tree is clean for the new run
    stashes = sched.state.get("DM-001").get("stashes")
    assert stashes and stashes[0]["sha"]
    assert stashes[0]["name"].startswith("garden:DM-001:")
    assert not (wt / "leftover.txt").exists()  # set aside, not left in the tree
    # the stash sha resolves to a real commit, and its untracked-files parent still holds the edit
    assert gitops.git("cat-file", "-t", stashes[0]["sha"], cwd=wt).strip() == "commit"
    assert "leftover.txt" in gitops.git("show", "--name-only", "--format=", f"{stashes[0]['sha']}^3", cwd=wt)
    assert "stashed leftover changes" in sched.store.task("DM-001").body


def test_run_finished_emitted_once_when_finalize_interrupted_during_fence(sched, fake_github, monkeypatch):
    """A kill during the fence check — after run_finished is emitted but before the task
    transition — must not re-emit run_finished on the resumed finalize (which would double-count
    the run's cost). The interrupted finalize is recognised as unreaped and resumed once."""
    sched.tick()  # dispatch work

    real_fence = sched._fence_check
    calls = {"n": 0}

    def flaky_fence(task, run=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash during fence check")
        return real_fence(task, run)

    monkeypatch.setattr(sched, "_fence_check", flaky_fence)
    rep = sched.tick()  # reap: finalize persists the outcome, emits run_finished, then the fence crashes
    assert any("DM-001" in e for e in rep.errors)
    assert statuses(sched)["DM-001"] == "running"  # never transitioned
    run = sched.runs.latest("DM-001")
    assert run.finished_at  # finalize got far enough to persist the collected outcome
    assert run.run_id in sched.unreaped_run_ids()  # surfaced as finished-but-unreaped

    monkeypatch.setattr(sched, "_fence_check", real_fence)
    sched.tick()  # the resumed finalize completes the reap
    assert statuses(sched)["DM-001"] == "in_review"
    finished = [e for e in sched.events.read(task_id="DM-001", kinds=["run_finished"]) if e.get("run") == run.run_id]
    assert len(finished) == 1  # emitted exactly once despite the interrupted first finalize
