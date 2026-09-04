import os
import subprocess

from garden.github import Feedback
from garden.model import Status
from garden.runner.manual import ManualRunner
from tests.conftest import wait_for_runs


def statuses(sched):
    sched.store.invalidate()
    return {tid: t.status.value for tid, t in sched.store.tasks().items()}


def test_happy_path_dispatch_reap_pr_merge(sched, fake_github):
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]  # DM-002 is blocked
    assert statuses(sched)["DM-001"] == "running"
    wait_for_runs(sched)
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
    # merge -> done, and DM-002 unblocks and dispatches
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    s = statuses(sched)
    assert s["DM-001"] == "done" and s["DM-002"] == "running"
    assert not sched.worktree_for(sched.store.task("DM-001")).exists()


def test_feedback_triggers_revise_round(sched, fake_github):
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "line comment", "author": "josh", "body": "rename this", "path": "a.py", "line": 3, "created": "2099-01-01T00:00:00Z"}])
    rep = sched.tick()  # poll -> changes_requested -> revise dispatched in the same tick
    assert "DM-001 -> changes_requested" in rep.transitions
    assert rep.dispatched == ["DM-001(revise)"]
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"
    brief = (run.path / "brief.md").read_text()
    assert "Revision round" in brief and "rename this" in brief and "`a.py`:3" in brief
    wait_for_runs(sched)
    fake_github.feedback.clear()
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    assert len(fake_github.created) == 1  # same PR, no second one
    assert fake_github.comments and "revised per feedback" in fake_github.comments[0]
    assert sched.state.get("DM-001")["revisions"] == 1


def test_bot_notice_does_not_trigger_revise_but_is_logged(sched, fake_github):
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(
        ignored=[{"author": "chatgpt-codex-connector[bot]", "body": "You have reached your Codex usage limits for code reviews", "created": "2099-01-01T00:00:00Z"}]
    )
    rep = sched.tick()
    assert not any("changes_requested" in t for t in rep.transitions)
    assert statuses(sched)["DM-001"] == "in_review"
    t = sched.store.task("DM-001")
    assert "bot notice ignored: chatgpt-codex-connector[bot]" in t.body
    assert "usage limits" in t.body


def test_revision_cap(sched, fake_github):
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    for i in range(3):
        pr.updated_at = f"t{i + 2}"
        fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": f"round {i}", "created": "2099-01-01T00:00:00Z"}])
        sched.tick()
        if statuses(sched)["DM-001"] == "running":
            wait_for_runs(sched)
            sched.tick()
    assert statuses(sched)["DM-001"] == "changes_requested"
    assert sched.state.get("DM-001")["revisions"] == 2
    assert "round 2" in sched.state.get("DM-001")["pending_feedback"]


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


def test_pre_pr_check_failure_at_cap_needs_human(sched, fake_github):
    """A pre-PR check that fails once the revision cap is reached hands off to a human,
    exactly like the review path — it does not leave the task queued-but-skipped."""
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": "echo boom; exit 1"}], "ci": []}
    sched.cfg.data["max_revisions"] = 2
    sched.tick()
    wait_for_runs(sched)
    sched.state.get("DM-001")["revisions"] = 2  # pretend two revise rounds were already used
    sched.state.save()
    rep = sched.tick()  # reap -> pre-PR check fails at the cap
    assert statuses(sched)["DM-001"] == "changes_requested"
    st = sched.state.get("DM-001")
    assert st.get("needs_human") and "revision rounds already used" in st["needs_human"]
    assert any("cap" in tr for tr in rep.transitions)


def test_retry_grants_one_more_round_past_cap(sched, fake_github):
    """Resuming a capped task rolls the revision counter back one so a revise run runs."""
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    t.pr = "https://example.com/pull/101"
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["revisions"] = 2  # == max_revisions (2) in the test garden
    st["needs_human"] = "2 revision rounds already used"
    sched.retry(t)
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert int(st["revisions"]) == 1  # cap (2) minus one -> one more round dispatchable


def test_retry_of_changes_requested_without_pr_is_a_revise(sched, fake_github):
    """A pre-PR check that failed at the cap leaves the task in changes_requested with no
    PR. `garden retry` must continue the revise loop (keep the feedback, roll the cap back),
    not reset to a fresh work run that would drop both."""
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    t.pr = ""  # capped before any PR was opened
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["revisions"] = 2  # == max_revisions in the test garden
    st["pending_feedback"] = "- **pre-PR check** `unit` fail: exit 1"
    st["needs_human"] = "pre-PR checks failed and 2 revision rounds already used"
    sched.retry(sched.store.task("DM-001"))
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert int(st["revisions"]) == 1  # one more round dispatchable, counter not lost
    assert st["pending_feedback"]  # feedback preserved, not dropped for a fresh work run
    assert statuses(sched)["DM-001"] == "changes_requested"
    # the revise round is dispatchable now
    rep = sched.tick()
    assert "DM-001(revise)" in rep.dispatched


def test_ci_failure_triggers_revise(sched, fake_github):
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at, pr.checks = "t2", "FAILURE"
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(revise)"]
    assert "**CI** is failing" in (sched.runs.latest("DM-001").path / "brief.md").read_text()


def test_crash_retries_then_fails(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    sched.tick()
    wait_for_runs(sched)
    rep = sched.tick()
    assert "DM-001 -> ready (retry)" in rep.transitions
    assert rep.dispatched == ["DM-001(work)"]  # retried immediately
    wait_for_runs(sched)
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    t = sched.store.task("DM-001")
    assert t.attempts == 2 and "giving up" in t.body


def test_no_commits_is_a_failure(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "blocked")
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    assert "Which database?" in sched.store.task("DM-001").body


def test_noresult_retries(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult")
    sched.tick()
    wait_for_runs(sched)
    rep = sched.tick()
    assert "DM-001 -> ready (retry)" in rep.transitions


def test_pr_closed_fails(sched, fake_github):
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    fake_github.prs["garden/dm-001-first-task"].state = "CLOSED"
    sched.tick()
    assert statuses(sched)["DM-001"] == "failed"


def test_max_parallel(sched, garden):
    for t in sched.store.tasks().values():
        t.depends_on = []
        sched.store.save(t)
    for i in range(3, 6):
        (garden / "demo" / "p1" / "tasks" / f"DM-00{i}.md").write_text(
            f"---\nid: DM-00{i}\ntitle: t{i}\nstatus: ready\ndepends_on: []\npriority: 3\nreading: []\ncreated: ''\nupdated: ''\n---\n\n## Goal\n\nx\n")
    rep = sched.tick()
    assert len(rep.dispatched) == 2
    wait_for_runs(sched)
    rep = sched.tick()
    assert len(rep.dispatched) == 2


def test_no_github_still_pushes(sched, fake_github):
    fake_github.available = False
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    t = sched.store.task("DM-001")
    assert t.status == Status.IN_REVIEW and not t.pr and "GitHub unavailable" in t.body


def test_manual_take_and_finish(sched, fake_github):
    t = sched.store.task("DM-001")
    run = sched.dispatch(t, runner=ManualRunner({}), worktree=True)
    assert statuses(sched)["DM-001"] == "running"
    assert sched.slots_free() == 2  # manual runs don't occupy slots
    sched.tick()
    assert statuses(sched)["DM-001"] == "running"  # not reaped: no exit_code yet
    wt = run.worktree
    with open(os.path.join(wt, "hello.txt"), "w") as f:
        f.write("hi\n")
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "manual work"], cwd=wt, check=True)
    sched.finish_manual(sched.store.task("DM-001"), {"status": "done", "summary": "by hand", "pr_title": "manual PR"})
    assert statuses(sched)["DM-001"] == "in_review"
    assert fake_github.created[-1]["title"] == "manual PR"


def test_tick_does_not_race_manual_finish(sched, fake_github):
    """A tick fired between ManualRunner.finish() and finalize() must leave the task alone."""
    t = sched.store.task("DM-001")
    run = sched.dispatch(t, runner=ManualRunner({}), worktree=True)
    wt = run.worktree

    # simulate the human doing work and committing
    with open(os.path.join(wt, "hello.txt"), "w") as f:
        f.write("hi\n")
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "manual work"], cwd=wt, check=True)

    # ManualRunner.finish() writes result.json + exit_code — this is the race window start
    ManualRunner.finish(run, {"status": "done", "summary": "by hand", "pr_title": "manual PR"})
    assert (run.path / "exit_code").exists()
    assert sched.runs.latest("DM-001").status == "running"  # run.json still says running on disk

    # tick fires inside the window: must not transition the task
    rep = sched.tick()
    assert rep.transitions == [], f"tick must not transition mid-finish: {rep.transitions}"
    assert statuses(sched)["DM-001"] == "running"

    # finalize() completes the single clean transition
    sched.finalize(t, run, sched.runner_for(t, run.runner), rep)
    sched.state.save()
    sched.store.invalidate()
    assert statuses(sched)["DM-001"] == "in_review"
    # the tick must not have inserted a spurious "back to ready" revert
    t = sched.store.task("DM-001")
    assert "back to ready" not in t.body, "tick must not have reverted the task"


def test_running_without_run_record_resets(sched):
    t = sched.store.task("DM-001")
    t.status = Status.RUNNING
    sched.store.save(t)
    rep = sched.tick()
    assert "no run" in rep.transitions[0]


def test_notify_on_waiting_human_transition(sched, fake_github, tmp_path):
    notify_file = tmp_path / "notify.txt"
    # Use env vars in a script to test the notification hook
    sched.cfg.data["notify"] = {
        "command": f"bash -c 'echo \"task=$GARDEN_TASK_ID status=$GARDEN_STATUS\" >> {notify_file}'",
        "timeout_seconds": 5,
    }
    # Set max_revisions to 0 to trigger needs_human immediately
    sched.cfg.data["max_revisions"] = 0
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    # PR opened, task is in_review
    assert statuses(sched)["DM-001"] == "in_review"
    # Add feedback to trigger revise, but immediately hit revision cap
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "change this", "created": "2099-01-01T00:00:00Z"}])
    sched.tick()
    # Should transition to changes_requested with needs_human flag (max_revisions=0 means revision cap hit immediately)
    assert statuses(sched)["DM-001"] == "changes_requested"
    # Check that the notify command was called
    assert notify_file.exists()
    content = notify_file.read_text()
    assert "task=DM-001" in content and "status=changes_requested" in content


def test_notify_on_failed_status(sched, fake_github, tmp_path):
    notify_file = tmp_path / "notify.txt"
    sched.cfg.data["notify"] = {
        "command": f"bash -c 'echo \"task=$GARDEN_TASK_ID status=$GARDEN_STATUS\" >> {notify_file}'",
        "timeout_seconds": 5,
    }
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    # Close the PR -> task fails
    fake_github.prs["garden/dm-001-first-task"].state = "CLOSED"
    sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    # Check that the notify command was called for failed status
    assert notify_file.exists()
    content = notify_file.read_text()
    assert "task=DM-001" in content and "status=failed" in content


def test_notify_receives_all_env_vars(sched, fake_github, tmp_path):
    notify_file = tmp_path / "notify.txt"
    # Write all environment variables to the file to verify they're passed correctly
    sched.cfg.data["notify"] = {
        "command": f"bash -c 'echo \"task=$GARDEN_TASK_ID status=$GARDEN_STATUS message=$GARDEN_MESSAGE pr=$GARDEN_PR\" >> {notify_file}'",
        "timeout_seconds": 5,
    }
    # Trigger a failed status transition to verify environment variables
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    # PR opened
    assert statuses(sched)["DM-001"] == "in_review"
    # Close the PR to trigger failed transition with notification
    fake_github.prs["garden/dm-001-first-task"].state = "CLOSED"
    sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    # Check that the notify command received all environment variables
    assert notify_file.exists()
    content = notify_file.read_text()
    assert "task=DM-001" in content and "status=failed" in content
    assert "message=" in content  # GARDEN_MESSAGE should be present
    assert "pr=" in content and "pull/101" in content  # GARDEN_PR should contain the PR URL


def test_no_notify_on_auto_revise_changes_requested(sched, fake_github, tmp_path):
    """Auto-revise changes_requested (scheduler handles it) must not trigger a notification."""
    notify_file = tmp_path / "notify.txt"
    sched.cfg.data["notify"] = {
        "command": f"bash -c 'echo \"task=$GARDEN_TASK_ID status=$GARDEN_STATUS\" >> {notify_file}'",
        "timeout_seconds": 5,
    }
    # auto_revise=True (default) and max_revisions=3 (default), so the scheduler will
    # queue a revise run without any human action
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "tweak this", "created": "2099-01-01T00:00:00Z"}])
    sched.tick()
    # Auto-revise immediately re-dispatches in the same tick, so the task ends up running.
    # The notify file must not exist because changes_requested was auto-handled.
    assert statuses(sched)["DM-001"] in ("changes_requested", "running")
    assert not notify_file.exists()


def test_notify_on_parent_closed(sched, fake_github, tmp_path):
    """Closing a parent PR without merging must notify stacked children."""
    notify_file = tmp_path / "notify.txt"
    sched.cfg.data["notify"] = {
        "command": f"bash -c 'echo \"task=$GARDEN_TASK_ID status=$GARDEN_STATUS\" >> {notify_file}'",
        "timeout_seconds": 5,
    }
    # DM-001 dispatched and reaches in_review; DM-002 stacks on it
    sched.tick()
    wait_for_runs(sched)
    sched.tick()  # DM-001 -> in_review; DM-002 stacks and dispatches
    wait_for_runs(sched)
    sched.tick()  # DM-002 -> in_review (stacked on DM-001's branch)
    assert statuses(sched)["DM-002"] == "in_review"
    assert sched.state.get("DM-002").get("stack_parent") == "DM-001"
    # notify_file may have entries from awaiting_triage/in_review transitions already;
    # clear it so we can isolate the parent-closed notification
    notify_file.unlink(missing_ok=True)
    # close the parent PR without merging
    fake_github.prs["garden/dm-001-first-task"].state = "CLOSED"
    sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    # DM-002's stack parent closed -> notification must have fired for DM-002
    assert notify_file.exists(), "notify hook must fire when a stack parent is closed"
    content = notify_file.read_text()
    assert "task=DM-002" in content and "status=needs_human" in content


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
    wait_for_runs(sched)
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
