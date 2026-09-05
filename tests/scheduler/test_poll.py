"""Poll: what GitHub says about an open PR (feedback, bot notices, the revision cap, CI, merged, closed)."""


from garden.github import Feedback
from garden.model import Status
from tests.conftest import wait_for_runs
from tests.scheduler.conftest import statuses


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
    # DM-002 stacks on DM-001's open PR and gets its own PR once its work run lands (see
    # test_happy_path_dispatch_reap_pr_merge) -- that is unrelated to this test, so scope
    # the "no duplicate PR" check to DM-001's own branch rather than the whole fake.
    dm001_prs = [c for c in fake_github.created if c["head"] == "garden/dm-001-first-task"]
    assert len(dm001_prs) == 1  # same PR, no second one
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


def test_ci_failure_triggers_revise(sched, fake_github):
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at, pr.checks = "t2", "FAILURE"
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(revise)"]
    assert "**CI** is failing" in (sched.runs.latest("DM-001").path / "brief.md").read_text()


def test_pr_closed_fails(sched, fake_github):
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    fake_github.prs["garden/dm-001-first-task"].state = "CLOSED"
    sched.tick()
    assert statuses(sched)["DM-001"] == "failed"


def test_failed_task_with_merged_pr_becomes_done(sched, fake_github):
    """CG-046/CG-039: a revise round (or a retry) can die with the task's PR still open.
    A human merging that PR on GitHub must still resolve the task to done, worktree cleaned
    up and dependants unblocked, even though the task fell out of the review flow."""
    sched.tick()
    wait_for_runs(sched)
    sched.tick()  # DM-001 -> in_review, PR opened
    t = sched.store.task("DM-001")
    t.status = Status.FAILED  # e.g. a revise run died while the PR was still open
    sched.store.save(t)
    fake_github.prs["garden/dm-001-first-task"].state = "MERGED"
    rep = sched.tick()
    s = statuses(sched)
    assert s["DM-001"] == "done" and s["DM-002"] == "running"
    assert "DM-001 -> done" in rep.transitions
    assert not sched.worktree_for(sched.store.task("DM-001")).exists()


def test_failed_task_with_closed_pr_stays_failed(sched, fake_github):
    """A failed task whose PR is closed unmerged stays failed, with the close noted."""
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    t = sched.store.task("DM-001")
    t.status = Status.FAILED
    sched.store.save(t)
    fake_github.prs["garden/dm-001-first-task"].state = "CLOSED"
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    assert "PR closed without merging" in sched.store.task("DM-001").body
    assert "DM-001 -> failed (PR closed)" in rep.transitions
