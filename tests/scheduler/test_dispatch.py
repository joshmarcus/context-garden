"""Dispatch: the queue, slots and the live max_parallel override, the pause, and the stuck-task audit."""

import subprocess

from garden.model import Status
from tests.scheduler.conftest import statuses


def test_happy_path_dispatch_reap_pr_merge(sched, fake_github):
    # Exercise the plain dependency gate (dep must be *done*), not stacking: with stacking on,
    # DM-002 would stack-dispatch onto DM-001's open PR at tick 2, and this test's final
    # "DM-002 running after merge" assertion would then depend on whether that stacked worker
    # happened to still be running at the merge tick — a timing race that flaked in CI. Stacking
    # has its own coverage (see test_feedback_triggers_revise_round); here we want the merge to
    # be what unblocks and dispatches DM-002. See CG-119.
    sched.cfg.data["stack"] = False
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]  # DM-002 is blocked
    assert statuses(sched)["DM-001"] == "running"
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    assert fake_github.created and fake_github.created[0]["title"] == "Fake: implemented the thing"
    assert fake_github.created[0]["head"] == "garden/dm-001-first-task"
    t = sched.store.task("DM-001")
    assert t.pr.endswith("/pull/101") and t.attempts == 1
    # the branch was pushed to origin
    remote = sched.repo_for(t).parent / "remote.git"
    out = subprocess.run(["git", "branch", "--list", "garden/*"], cwd=remote, capture_output=True, text=True).stdout
    assert "garden/dm-001-first-task" in out
    run = sched.runs.latest("DM-001")
    assert run.status == "done" and run.cost_usd == 0.05 and run.usage["input_tokens"] == 1234
    assert (run.path / "brief.md").exists() and "Do the first thing" in (run.path / "brief.md").read_text()

    # nothing new on the PR -> no change
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    # merge -> done, and DM-002 (blocked until now) unblocks and dispatches in the same tick
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    s = statuses(sched)
    assert s["DM-001"] == "done" and s["DM-002"] == "running"
    assert not sched.worktree_for(sched.store.task("DM-001")).exists()


def test_brief_inlines_reading_from_the_target_checkout(sched, tmp_path):
    """A reading-list file that exists on the task's branch but not on the product base must be
    inlined from the worktree the worker actually gets — the target checkout — and not marked
    'not found' because the base repo does not have it yet. The worktree is prepared before the
    brief is built for exactly this reason (a stacked task's parent-created files, too)."""
    from tests.conftest import git, write

    repo = tmp_path / "repo"
    branch = "garden/dm-001-first-task"  # DM-001's default branch
    # Commit a file only on the task's branch; main never gets it.
    git("checkout", "-q", "-b", branch, cwd=repo)
    write(repo / "src" / "onbranch.py", "ANSWER = 42\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add onbranch", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)

    t = sched.store.task("DM-001")
    t.reading = ["src/onbranch.py"]
    sched.store.save(t)
    sched.store.invalidate()

    sched.tick()  # dispatch DM-001 (the in-process worker runs synchronously)
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "### src/onbranch.py" in brief and "ANSWER = 42" in brief
    assert "not found when the brief was built" not in brief


def test_max_parallel(sched, garden):
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    for i in range(3, 6):
        (garden / "demo" / "p1" / "tasks" / f"DM-00{i}.md").write_text(
            f"---\nid: DM-00{i}\ntitle: t{i}\nstatus: ready\ndepends_on: []\npriority: 3\nreading: []\ncreated: ''\nupdated: ''\n---\n\n## Goal\n\nx\n")
    rep = sched.tick()
    assert len(rep.dispatched) == 2
    rep = sched.tick()
    assert len(rep.dispatched) == 2


def test_max_parallel_override_and_clear(sched):
    from garden.scheduler import State

    assert sched.effective_max_parallel() == 2  # garden.yaml's max_parallel, no override yet
    assert sched.overrides().get("max_parallel") is None
    sched.set_override("max_parallel", 7, by="test")
    assert sched.effective_max_parallel() == 7
    on_disk = State(sched.state.path).get("_control")
    assert on_disk["overrides"]["max_parallel"] == 7
    sched.clear_override("max_parallel", by="test")
    assert sched.effective_max_parallel() == 2  # back to the garden.yaml value
    on_disk = State(sched.state.path).get("_control")
    assert "max_parallel" not in on_disk.get("overrides", {})


def test_tick_uses_max_parallel_override(sched, garden):
    """The override takes effect on the very next tick, no restart, and running workers
    are never stopped by lowering it (dispatch just skips them until the count drops)."""
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    for i in range(3, 6):
        (garden / "demo" / "p1" / "tasks" / f"DM-00{i}.md").write_text(
            f"---\nid: DM-00{i}\ntitle: t{i}\nstatus: ready\ndepends_on: []\npriority: 3\nreading: []\ncreated: ''\nupdated: ''\n---\n\n## Goal\n\nx\n")
    sched.set_override("max_parallel", 1)
    rep = sched.tick()
    assert len(rep.dispatched) == 1  # override (1) wins over garden.yaml's max_parallel (2)
    sched.clear_override("max_parallel")
    rep = sched.tick()  # reaps the finished run, then dispatches up to garden.yaml's 2 again
    assert len(rep.dispatched) == 2


def test_review_runs_do_not_consume_worker_slots(sched):
    """max_parallel=2 (fixture). A review run must not count against it, and review_parallel
    (defaulting to max_parallel) must be tracked independently."""
    t = sched.store.task("DM-001")
    review_run = sched.runs.new_run(t.id, "local", mode="review")
    review_run.status = "running"
    review_run.save()
    assert sched.slots_free() == 2  # the review run holds a review slot, not a worker slot
    assert sched.review_parallel_limit() == 2  # defaults to max_parallel
    assert sched.review_slots_free() == 1

    work_run = sched.runs.new_run(t.id, "local", mode="work")
    work_run.status = "running"
    work_run.save()
    assert sched.slots_free() == 1
    assert sched.review_slots_free() == 1  # unaffected by the work run

    sched.cfg.data["review_parallel"] = 1
    assert sched.review_parallel_limit() == 1
    assert sched.review_slots_free() == 0


def test_pause_stops_dispatch(sched, fake_github):
    sched.pause(by="cli", reason="testing")
    assert sched.is_dispatch_paused()
    rep = sched.tick()
    # nothing dispatched while paused
    assert rep.dispatched == []
    assert statuses(sched)["DM-001"] == "ready"


def test_resume_restarts_dispatch(sched, fake_github):
    sched.pause(by="cli")
    sched.tick()
    assert statuses(sched)["DM-001"] == "ready"
    sched.resume(by="cli")
    assert not sched.is_dispatch_paused()
    rep = sched.tick()
    assert "DM-001(work)" in rep.dispatched


def test_paused_tick_still_reaps_and_polls(sched, fake_github):
    # dispatch, let worker finish, then pause before reaping
    sched.tick()
    sched.pause(by="cli")
    rep = sched.tick()
    # should reap the finished worker and push the PR even while paused
    assert "DM-001" in rep.reaped
    assert statuses(sched)["DM-001"] == "in_review"
    assert fake_github.created  # PR was opened
    # poll should also run: merge the PR -> task becomes done
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "done"
    # DM-002 remains ready (not dispatched because paused)
    assert statuses(sched)["DM-002"] == "ready"


def test_pause_state_persists_in_state_json(sched, fake_github):
    sched.pause(by="cli", reason="hold")
    path = sched.state.path
    from garden.scheduler import State
    fresh = State(path).get("_control")
    assert fresh["dispatch"] == "paused" and fresh["reason"] == "hold" and fresh["by"] == "cli"


def test_pause_overrides_auto_dispatch_true(sched, fake_github):
    sched.cfg.data["auto_dispatch"] = True
    sched.pause(by="web")
    rep = sched.tick()
    assert rep.dispatched == []


def test_audit_flags_stuck_changes_requested(sched, fake_github):
    """A task hand-left in changes_requested with no feedback, no run and no needs_human
    is stuck: the tick audit must give it an attention card, not let it sit silent."""
    from garden.inbox import build_inbox

    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = ""  # nothing to revise against -> not dispatchable
    st["revisions"] = 0
    sched.state.save()

    rep = sched.tick()
    st = sched.state.get("DM-001")
    assert str(st.get("needs_human", "")).startswith("stuck:")
    assert "no feedback" in st["needs_human"]
    assert any("stuck" in tr for tr in rep.transitions)
    # it now appears on the Inbox attention group
    cards = [it for it in build_inbox(sched.store, sched) if it["task"] == "DM-001"]
    assert any(c["group"] == "attention" and "stuck" in c["why"] for c in cards)


def test_audit_flags_pending_feedback_stuck_on_in_review(sched, fake_github):
    """CG-140: a stored pending_feedback always comes with the changes_requested
    transition; if it ever appears on an in_review task instead (no run dispatches from
    there), the tick audit must flag it rather than let automerge hold on it silently."""
    t = sched.store.task("DM-001")
    t.status = Status.IN_REVIEW
    t.pr = "https://example.com/pr/1"
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- **automated review** PR description: needs work"
    sched.state.save()

    rep = sched.tick()
    st = sched.state.get("DM-001")
    assert str(st.get("needs_human", "")).startswith("stuck:")
    assert "in_review" in st["needs_human"]
    assert any("stuck" in tr for tr in rep.transitions)


def test_audit_does_not_flag_dispatchable_changes_requested(sched, fake_github):
    """A changes_requested task with feedback under the cap is dispatchable; not stuck."""
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- fix this"
    st["revisions"] = 0
    sched.state.save()
    sched.tick(dispatch=False)  # audit runs even with dispatch off
    assert not sched.state.get("DM-001").get("needs_human")
