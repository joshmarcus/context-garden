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


def test_gate_run_in_flight_for_a_real_running_review(sched, fake_github):
    """A `review_run` pointer to a run that really is still `running` still blocks."""
    t, st, pr = _in_review(sched, fake_github)
    run = sched.runs.new_run("DM-001", "local", mode="review")
    st["review_run"] = run.run_id
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "in flight" in reason


def test_gate_not_held_by_a_superseded_review_run(sched, fake_github):
    """CG-144: a `review_run` pointer left over from a run that has since been closed
    (superseded by a newer review, or reaped by the dead-run sweep) must not hold
    automerge forever."""
    t, st, pr = _in_review(sched, fake_github)
    for status in ("superseded", "done", "failed"):
        run = sched.runs.new_run("DM-001", "local", mode="review")
        run.status = status
        run.save()
        st["review_run"] = run.run_id
        ok, reason = sched._automerge_gate(t, pr)
        assert ok, (status, reason)


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


def test_gate_needs_human(sched, fake_github):
    """CG-175: a needs-human stop (e.g. a review cap hit by a rebase right before the merge)
    must hold automerge rather than let it merge on a verdict recorded before the stop."""
    t, st, pr = _in_review(sched, fake_github)
    st["needs_human"] = {"kind": "review_cap", "reason": "1 automated review round(s) used", "at": "t"}
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "needs-human" in reason


def test_review_cap_hit_by_rebase_holds_automerge_instead_of_merging_stale(sched, fake_github):
    """CG-175: `_run_merge_queue` rebases the head of the queue right before merging it (rule
    2 in rebase.py). When that rebase changes the diff and a fresh review is due but the
    review cap is already used up, the cap sets a needs-human stop instead of dispatching a
    review — the merge must not go through on the stale, pre-rebase verdict."""
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["review"]["enabled"] = True
    sched.cfg.data["review"]["max_rounds"] = 1
    st["review_rounds"] = 1  # already at the cap
    st["last_diff_hash"] = "stale-hash-that-will-not-match-the-real-diff"
    sched.state.save()
    sched.tick()
    assert fake_github.merged == []
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    st = sched.state.get("DM-001")
    assert st.get("needs_human", {}).get("kind") == "review_cap"


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


# ---- check latency: the rollup is PENDING for a poll or two after a push -----
def test_automerge_waits_out_a_pending_rollup(sched, fake_github):
    """A freshly-pushed rollup is PENDING for a poll before it turns green (the fake models
    real GitHub's latency, N >= 1): the merge holds until the rollup settles, then goes."""
    t, st, pr = _in_review(sched, fake_github)
    fake_github.set_checks(BRANCH, "SUCCESS", latency=1)  # one poll PENDING, then green
    sched.tick()  # poll sees PENDING -> held, not merged
    assert fake_github.merged == []
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    assert "pending" in (sched.state.get("DM-001").get("automerge_blocked") or "")
    sched.tick()  # poll sees the rollup settle to green -> merge
    assert fake_github.merged == [{"number": pr.number, "method": "squash", "delete_branch": True}]
    assert pr.state == "MERGED"


# ---- a failing gate records the reason and leaves the PR in review -----------
def test_red_ci_holds_the_merge_with_reason_on_the_task(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    pr.checks = "FAILURE"
    sched.tick()
    assert fake_github.merged == []
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    blocked = sched.state.get("DM-001").get("automerge_blocked")
    assert blocked and "checks" in blocked


# ---- guarded-path hold (CG-194) ---------------------------------------------
def _commit_in_worktree(wt, rel, content="x\n"):
    import subprocess
    p = wt / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    for args in (["add", "-A"], ["commit", "-q", "-m", f"touch {rel}"]):
        subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t", *args],
                       cwd=wt, check=True, capture_output=True, text=True)


def test_automerge_holds_when_diff_touches_guarded_paths(sched, fake_github):
    """A PR whose diff touches garden*.yaml, **/tasks/, .github/ or principles/ is too
    sensitive to merge unattended: automerge holds it for a person even with every gate green."""
    t, st, pr = _in_review(sched, fake_github)
    _commit_in_worktree(sched.worktree_for(t), ".github/workflows/ci.yml", "on: push\n")
    sched.tick()
    assert fake_github.merged == []
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    blocked = sched.state.get("DM-001").get("automerge_blocked")
    assert blocked and "guarded paths" in blocked and ".github/workflows/ci.yml" in blocked


def test_touches_guarded_path_predicate():
    from garden.scheduler.poll import _touches_guarded_path
    for p in ("garden.yaml", "garden.local.yaml", "sub/garden.work.yaml",
              "demo/p1/tasks/x.md", ".github/workflows/ci.yml", "principles/00-index.md"):
        assert _touches_guarded_path(p), p
    for p in ("src/garden/foo.py", "README.md", "docs/tasks.md", "principles.md",
              "not_garden.yaml.txt"):
        assert not _touches_guarded_path(p), p
