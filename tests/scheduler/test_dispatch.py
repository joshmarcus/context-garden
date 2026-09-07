"""Dispatch: the queue, slots and the live max_parallel override, the pause, and the stuck-task audit."""

import os
import subprocess
import textwrap

import pytest
from typer.testing import CliRunner

from garden.cli import app
from garden.model import Status
from tests.scheduler.conftest import statuses


def test_duplicate_task_id_quarantined_the_tick_survives_and_dispatch_continues(sched, garden, fake_github):
    """CG-244: two files claiming one id used to make store.tasks() raise, which took down every
    page and tick. Now the ambiguous id is quarantined out of dispatch, the tick runs to
    completion, unrelated ready tasks still dispatch, and the collision is surfaced on the tick
    report and by `garden validate`."""
    for name in ("clash-a", "clash-b"):
        (garden / "demo" / "p1" / "tasks" / f"DM-050-{name}.md").write_text(textwrap.dedent(f"""
            ---
            id: DM-050
            title: {name}
            status: ready
            depends_on: []
            priority: 1
            reading: []
            created: '2026-01-01T00:00:00+00:00'
            updated: '2026-01-01T00:00:00+00:00'
            ---

            ## Goal

            {name}
            """).lstrip())
    sched.cfg.data["stack"] = False
    rep = sched.tick()  # does not raise
    assert rep.dispatched == ["DM-001(work)"]  # the healthy ready task still dispatches
    assert "DM-050" not in statuses(sched)  # the ambiguous id never entered the task map
    assert any("duplicate task id DM-050" in e for e in rep.errors)

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        result = CliRunner().invoke(app, ["validate"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 1
    assert "duplicate task id DM-050" in result.output


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


def test_brief_drops_reading_not_present_at_the_task_base(sched, tmp_path):
    """A branch-only file is not context at the task's base and must not leak into its brief."""
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
    assert "### src/onbranch.py" not in brief and "ANSWER = 42" not in brief
    assert "## Brief gaps" in brief and "- `src/onbranch.py`" in brief


def test_revise_brief_names_rebase_conflict_without_github_feedback(sched):
    from garden.model import Status

    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.test/pull/1"
    sched.store.save(task)
    state = sched.state.get(task.id)
    state["pending_feedback_rebase"] = True
    sched.dispatch(task, mode="revise")
    brief = (sched.runs.latest(task.id).path / "brief.md").read_text()
    assert "## Concrete blocker" in brief
    assert "GitHub has no open review comments" in brief
    assert "rebase conflict" in brief


def test_redispatched_work_brief_lists_prior_commits(sched):
    """CG-125: a fresh work round that lands on a worktree an interrupted attempt left with
    commits gets those commits listed in its brief ("Already on this branch"), so it builds
    on the prior progress instead of reverse-engineering it from git log/diff."""
    from garden import gitops
    from tests.conftest import git, write

    sched.cfg.data["stack"] = False
    # Leave commits from an interrupted attempt on the branch worktree, with no live run.
    task = sched.store.task("DM-001")
    wt = sched.worktree_for(task)
    gitops.prepare_worktree(sched.repo_for(task), wt, task.default_branch(), "main")
    write(wt / "progress.txt", "partial\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "DM-001: partial fix", cwd=wt)
    assert gitops.commits_ahead(wt, "main") == 1

    rep = sched.tick()  # dispatch a fresh work round onto the worktree with prior commits
    assert "DM-001(work)" in rep.dispatched
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "## Already on this branch" in brief and "partial fix" in brief


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


def test_garden_yaml_reloaded_between_ticks(sched, garden):
    """CG-192: editing garden.yaml is honoured within one tick, no restart. The scheduler
    re-reads store.config each pass when the file's mtime changed, so a raised max_parallel
    dispatches more work on the very next tick and the change is logged."""
    import os

    import yaml

    logs = []
    sched.log = logs.append
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    for i in range(3, 6):
        (garden / "demo" / "p1" / "tasks" / f"DM-00{i}.md").write_text(
            f"---\nid: DM-00{i}\ntitle: t{i}\nstatus: ready\ndepends_on: []\npriority: 3\nreading: []\ncreated: ''\nupdated: ''\n---\n\n## Goal\n\nx\n")
    rep = sched.tick()
    assert len(rep.dispatched) == 2  # garden.yaml's max_parallel

    # raise the limit in garden.yaml (and bump the mtime so the reload is deterministic)
    p = garden / "garden.yaml"
    data = yaml.safe_load(p.read_text())
    data["max_parallel"] = 4
    p.write_text(yaml.safe_dump(data))
    future = os.stat(p).st_mtime + 10
    os.utime(p, (future, future))

    rep = sched.tick()  # reaps the 2 finished runs, re-reads the config, dispatches up to 4
    assert sched.cfg.get("max_parallel") == 4
    assert len(rep.dispatched) == 3  # the 3 remaining ready tasks, without a restart
    assert any("garden.yaml reloaded" in m and "max_parallel" in m for m in logs)


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


def test_audit_flags_manual_task_with_a_revise_round_waiting(sched, fake_github):
    """CG-158: a `runner: manual` task sent to changes_requested is never auto-dispatched
    (dispatch_ready skips non-detached runners) — the stuck audit must not mistake its
    waiting feedback for something the queue will pick up, or it rots silently with no
    Inbox card and nothing telling anyone to `garden take` it."""
    t = sched.store.task("DM-001")
    t.runner = "manual"
    t.status = Status.CHANGES_REQUESTED
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- fix this"
    st["revisions"] = 0
    sched.state.save()

    rep = sched.tick()
    assert rep.dispatched == []  # never auto-dispatched
    st = sched.state.get("DM-001")
    assert str(st.get("needs_human", "")).startswith("stuck:")
    assert "manual" in st["needs_human"]


def test_take_clears_a_stale_needs_human_flag(sched, fake_github):
    """Taking a manual task that the stuck audit flagged is the human resolving the flag,
    the same way retry/resume/answer already do: the Inbox card must not linger once a
    fresh run is actually in flight."""
    from garden.runner.manual import ManualRunner

    t = sched.store.task("DM-001")
    t.runner = "manual"
    t.status = Status.CHANGES_REQUESTED
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- fix this"
    st["revisions"] = 0
    sched.state.save()
    sched.tick()
    assert sched.state.get("DM-001").get("needs_human")

    sched.dispatch(sched.store.task("DM-001"), mode="revise", runner=ManualRunner({}), worktree=False)
    assert not sched.state.get("DM-001").get("needs_human")


def test_dispatch_ready_failure_does_not_abort_the_tick(sched, fake_github, monkeypatch):
    """CG-203: dispatch_ready is wrapped in the same guard as every other tick phase — an
    exception is logged with the phase name and the tick still runs the phases after it
    (audit), instead of the exception escaping tick() and skipping everything past it."""
    sched.cfg.data["stack"] = False

    def boom(rep):
        raise RuntimeError("boom")

    monkeypatch.setattr(sched, "dispatch_ready", boom)
    rep = sched.tick()
    assert any("dispatch ready failed" in e and "boom" in e for e in rep.errors)
    assert "audit" in rep.steps  # the tick kept going past the failing phase


def test_exception_in_dispatch_does_not_lose_an_earlier_transition(sched, fake_github, monkeypatch):
    """CG-203: state.save() runs in a `finally`, so a state.json field an earlier phase in the
    same tick wrote — here, the PR-open reap's pr_number cache — is not lost when a later
    phase (dispatch) blows up before the tick would otherwise have saved it."""
    from garden.scheduler import State

    sched.cfg.data["stack"] = False
    sched.tick()  # dispatch DM-001 (the in-process worker finishes synchronously)

    def boom(rep):
        raise RuntimeError("boom")

    monkeypatch.setattr(sched, "dispatch_ready", boom)
    rep = sched.tick()  # reaps the finished run (opens the PR, caches pr_number), then dispatch blows up
    assert statuses(sched)["DM-001"] == "in_review"
    assert any("dispatch ready failed" in e for e in rep.errors)

    fresh = State(sched.state.path).get("DM-001")  # reload from disk, not the in-memory copy
    assert fresh.get("pr_number")  # persisted despite the exception in dispatch


def test_dispatch_preparation_failure_closes_created_run(sched, monkeypatch):
    """Any preparation error after run creation closes the run in the same dispatch."""
    task = sched.store.task("DM-001")

    def boom(_task):
        raise RuntimeError("broken setup")

    monkeypatch.setattr(sched, "_stack_for", boom)
    with pytest.raises(RuntimeError, match="broken setup"):
        sched.dispatch(task)

    run = sched.runs.latest(task.id)
    assert run is not None
    assert run.status == "failed" and run.finished_at and run.error == "broken setup"
    finished = [e for e in sched.events.read(task_id=task.id, kinds=["run_finished"])
                if e.get("run") == run.run_id]
    assert len(finished) == 1 and finished[0]["status"] == "failed"


def test_dispatch_start_failure_after_launch_keeps_run_running(sched, monkeypatch):
    """A runner that launches a process before reporting a startup error still owns the run."""
    task = sched.store.task("DM-001")
    runner = sched.runner_for(task)

    def launched_then_failed(run, _worktree, _brief):
        run.pid = os.getpid()
        run.save()
        raise RuntimeError("failed while recording startup")

    monkeypatch.setattr(runner, "start", launched_then_failed)
    with pytest.raises(RuntimeError, match="failed while recording startup"):
        sched.dispatch(task, runner=runner)

    run = sched.runs.latest(task.id)
    assert run is not None
    assert run.status == "running" and run.pid == os.getpid()
