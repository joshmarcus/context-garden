"""Automerge: the scheduler merges a PR it opened once every loop gate is green (CG-068)."""

from __future__ import annotations

from garden.events import EventLog, digest
from garden.model import Status, Task

BRANCH = "garden/dm-001-first-task"


def _in_review(sched, fake_github, *, automerge=True):
    """Drive DM-001 to in_review with a PR, an approving automated review and green gates."""
    sched.tick()
    sched.tick()
    t = sched.store.task("DM-001")
    assert t.status == Status.IN_REVIEW
    st = sched.state.get("DM-001")
    st["last_review"] = {"verdict": "approve", "summary": "looks good"}
    st["last_review_run"] = "rev-1"
    st["review_rounds"] = 1
    sched.state.save()
    pr = fake_github.prs[BRANCH]
    pr.mergeable = "MERGEABLE"
    pr.checks = "SUCCESS"
    if automerge:
        sched.cfg.data["github"]["automerge"] = True
    return t, st, pr


# ---- the switch --------------------------------------------------------------
def test_off_by_default_leaves_pr_in_review(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github, automerge=False)
    sched.tick()
    assert fake_github.merged == []
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    assert not sched.state.get("DM-001").get("automerge_blocked")


def test_all_gates_green_merges_and_reaches_done(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    sched.tick()  # poll -> every gate green -> merge
    assert fake_github.merged == [{"number": pr.number, "method": "squash", "delete_branch": True}]
    assert pr.state == "MERGED"
    auto = sched.state.get("DM-001").get("automerged")
    assert auto and auto["review_run"] == "rev-1" and auto["method"] == "squash"
    # the garden posted a merge comment carrying the verdict run id
    assert any("Merged by the garden" in c and "rev-1" in c for c in fake_github.comments)

    sched.tick()  # the existing poll now sees MERGED and finishes the task
    done = sched.store.task("DM-001")
    assert done.status == Status.DONE
    assert "merged by the garden" in done.body.lower()

    events = EventLog(sched.cfg.garden_dir / "events.jsonl").read()
    d = digest(events)
    assert [e["task"] for e in d["automerged"]] == ["DM-001"]
    assert "DM-001" in [e["task"] for e in d["merged"]]


def test_method_and_min_rounds_are_configurable(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["github"]["automerge_method"] = "rebase"
    sched.tick()
    assert fake_github.merged == [{"number": pr.number, "method": "rebase", "delete_branch": True}]


# ---- per-task and per-product resolution -------------------------------------
def test_task_level_opt_out(sched, fake_github):
    sched.cfg.data["github"]["automerge"] = True
    t = Task(path=sched.store.root, id="X", title="", product="demo")
    assert sched._automerge_enabled(t) is True
    t.extra["automerge"] = False
    assert sched._automerge_enabled(t) is False


def test_per_product_override(sched, fake_github):
    sched.cfg.data["github"]["automerge"] = False
    sched.cfg.data["products"]["demo"]["automerge"] = True
    t = Task(path=sched.store.root, id="X", title="", product="demo")
    assert sched._automerge_enabled(t) is True


# ---- each gate ---------------------------------------------------------------
def test_gate_tier_not_allowed(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["github"]["automerge_tiers"] = ["easy"]
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "tier" in reason


def test_gate_review_not_approve(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    st["last_review"] = {"verdict": "request_changes"}
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "approve" in reason


def test_gate_min_review_rounds(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    st["review_rounds"] = 1
    sched.cfg.data["github"]["automerge_min_review_rounds"] = 2
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "review round" in reason


def test_gate_pending_feedback(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    st["pending_feedback"] = "- please fix the thing"
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "feedback" in reason


def test_gate_run_in_flight(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    st["review_run"] = "rev-2"
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "in flight" in reason


def test_gate_red_ci(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    pr.checks = "FAILURE"
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "checks" in reason


def test_gate_conflicting(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    pr.mergeable = "CONFLICTING"
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "conflicting" in reason.lower()


def test_gate_human_changes_requested(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    pr.review_decision = "CHANGES_REQUESTED"
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "human" in reason


def test_gate_over_budget(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["budgets"] = {"demo/p1": 0.01}
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "budget" in reason


def test_gate_stacked_on_parent_branch(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    pr.base = "garden/dm-000-parent-task"
    st["stack_parent"] = "DM-000"
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and reason == "stacked on DM-000; waits for the restack"


def test_stacked_pr_is_not_automerged(sched, fake_github):
    """Every other gate green, but the PR targets the parent's branch: held, not merged."""
    t, st, pr = _in_review(sched, fake_github)
    pr.base = "garden/dm-000-parent-task"
    st["stack_parent"] = "DM-000"
    sched.state.save()
    sched.tick()
    assert fake_github.merged == []
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    blocked = sched.state.get("DM-001").get("automerge_blocked")
    assert blocked == "stacked on DM-000; waits for the restack"


# ---- a failing gate records the reason and leaves the PR in review -----------
def test_red_ci_holds_the_merge_with_reason_on_the_task(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    pr.checks = "FAILURE"
    sched.tick()
    assert fake_github.merged == []
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    blocked = sched.state.get("DM-001").get("automerge_blocked")
    assert blocked and "checks" in blocked
