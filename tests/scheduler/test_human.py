"""What a person does to a task: retry past the cap, retry a capped pre-PR round."""


import pytest

from garden.model import Status
from tests.scheduler.conftest import statuses


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


# ---- CG-142: a done or cancelled task is terminal; no action reopens it -----
@pytest.mark.parametrize("action", ["triage", "retry", "cancel", "answer", "accept_decision", "reject_decision", "resume_task", "dispatch", "dispatch_review", "review_again", "dispatch_persona_pr", "integrate_now"])
def test_state_changing_actions_refuse_a_merged_done_task(sched, fake_github, action):
    """A task whose PR was merged (poll sets `done`) must not be moved back into the loop
    by any of the actions a stale page or a race could still fire."""
    sched.tick()
    sched.tick()  # DM-001 -> in_review with an open PR
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    task.log("PR merged: https://github.com/test/demo/pull/71")
    task.status = Status.DONE
    sched.store.save(task)

    kwargs = {}
    if action == "triage":
        kwargs = {"ready": True}
    elif action == "answer":
        kwargs = {"text": "hi"}
    elif action in ("accept_decision", "reject_decision"):
        kwargs = {"note": "ok"} if action == "accept_decision" else {"note": "no"}
    elif action == "dispatch_persona_pr":
        kwargs = {"name": "security"}

    with pytest.raises(RuntimeError, match=r"DM-001 is done: #71 was merged"):
        getattr(sched, action)(sched.store.task("DM-001"), **kwargs)

    assert statuses(sched)["DM-001"] == "done"  # untouched by the refused action


def test_cancel_refuses_an_already_cancelled_task(sched):
    task = sched.store.task("DM-001")
    sched.cancel(task, "cancelled by hand")
    assert statuses(sched)["DM-001"] == "cancelled"
    with pytest.raises(RuntimeError, match=r"DM-001 is cancelled: cancelled by hand"):
        sched.retry(sched.store.task("DM-001"))
    assert statuses(sched)["DM-001"] == "cancelled"  # still cancelled, not reopened
