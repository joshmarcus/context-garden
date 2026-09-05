"""notify.command: which transitions fire it and what it receives."""


from garden.github import Feedback
from tests.scheduler.conftest import statuses


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
    sched.tick()  # DM-001 -> in_review; DM-002 stacks and dispatches
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
