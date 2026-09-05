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


def test_cancel_clears_needs_human_and_automerge_blocked(sched, fake_github):
    """CG-175: cancelling a task with a stop recorded (a review cap, feedback waiting for a
    revise run, an automerge hold) must drop them so the cancelled task never shows as a
    decision on the Inbox."""
    task = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.cancel(task, "cancelled by hand")
    assert statuses(sched)["DM-001"] == "cancelled"
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


def test_wont_do_clears_needs_human_and_automerge_blocked(sched, fake_github):
    """CG-195: `wont_do` is terminal alongside done and cancelled, so it must clear the same
    stale stops on transition — it was missing from the CG-175 fix, which only checked
    Status.DONE/CANCELLED."""
    task = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.mark_wont_do(task, reason="not worth doing")
    assert statuses(sched)["DM-001"] == "wont_do"
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


def test_tick_sweeps_stale_state_off_a_task_already_terminal(sched, fake_github):
    """CG-195: a task that reached done/cancelled/wont_do before `_transition` cleared these
    fields (or through a path that bypassed it, e.g. a hand-edited state.json) must not keep
    showing a decision forever — a plain tick sweeps every terminal task's stale needs_human,
    pending feedback and automerge stop, not just the moment of transition."""
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.tick()
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


def test_cancel_refuses_an_already_cancelled_task(sched):
    task = sched.store.task("DM-001")
    sched.cancel(task, "cancelled by hand")
    assert statuses(sched)["DM-001"] == "cancelled"
    with pytest.raises(RuntimeError, match=r"DM-001 is cancelled: cancelled by hand"):
        sched.retry(sched.store.task("DM-001"))
    assert statuses(sched)["DM-001"] == "cancelled"  # still cancelled, not reopened


# ---- approve refuses an incomplete brief (CG-193) ----


def _draft(sched, body, reading=None):
    t = sched.store.create_task("demo", "p1", "Needs a real brief", body,
                                reading=reading or [], status="draft")
    return sched.store.task(t.id)


def test_approve_refuses_placeholder_criteria(sched, fake_github):
    t = _draft(sched, "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] ...\n")
    ph = sched.store.phase("demo", "p1")
    with pytest.raises(RuntimeError, match="incomplete brief"):
        sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.DRAFT


def test_approve_refuses_unresolved_reading_path(sched, fake_github):
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _draft(sched, body, reading=["demo/p1/specs/nope.md"])
    ph = sched.store.phase("demo", "p1")
    with pytest.raises(RuntimeError, match="reading-list path not found"):
        sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.DRAFT


def test_approve_accepts_a_complete_brief(sched, fake_github):
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _draft(sched, body, reading=["demo/p1/specs/spec.md"])
    ph = sched.store.phase("demo", "p1")
    sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.READY


def test_inbox_approve_card_shows_the_gap(sched, fake_github):
    from garden.inbox import build_inbox

    _draft(sched, "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] ...\n")
    items = build_inbox(sched.store, sched)
    card = next(i for i in items if i["group"] == "approve" and i["title"] == "Needs a real brief")
    assert card["gaps"]
    assert "brief incomplete" in card["why"]
