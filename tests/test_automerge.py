"""Automerge: the scheduler merges a PR it opened once every loop gate is green (CG-068)."""

from __future__ import annotations

import subprocess

import pytest

from garden import gitops
from garden.events import EventLog, digest
from garden.model import Status, Task

BRANCH = "garden/dm-001-first-task"


def gitc(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _in_review(sched, fake_github, *, automerge=True):
    """Drive DM-001 to in_review with a PR, an approving automated review and green gates."""
    sched.tick()
    sched.tick()
    t = sched.store.task("DM-001")
    assert t.status == Status.IN_REVIEW
    st = sched.state.get("DM-001")
    wt = sched.worktree_for(t)
    base = sched.base_for(t)
    review = sched.runs.new_run(t.id, "local", mode="review", run_id="rev-1")
    review.status = "done"
    review.branch, review.base, review.worktree = t.branch, base, str(wt)
    review.result = {"verdict": "approve"}
    review.env_snapshot = {
        "review_head": gitops.head_sha(wt),
        "review_base_head": gitops.rev_parse(wt, gitops.base_ref(wt, base)),
        "review_diff_hash": gitops.diff_hash(wt, base),
    }
    review.save()
    st["last_review"] = {"verdict": "approve", "summary": "looks good"}
    st["last_review_run"] = review.run_id
    st["last_review_head"] = review.env_snapshot["review_head"]
    st["last_review_base_head"] = review.env_snapshot["review_base_head"]
    st["last_diff_hash"] = review.env_snapshot["review_diff_hash"]
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

    # The "merged by the garden" fact rides on the automerged event, not the log prose, so this
    # asserts on the event and the wording of the log line can change freely (CG-204).
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


def test_self_product_uses_independent_second_opinion(sched, fake_github):
    """A self-product PR gets one automated approval and an independent current-head opinion."""
    sched.cfg.data["products"]["demo"]["self"] = True
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2}
    sched.cfg.data["github"]["automerge"] = True
    sched.tick()  # dispatch the worker
    sched.tick()  # open the PR and dispatch the first automated review
    t = sched.store.task("DM-001")
    st = sched.state.get(t.id)
    pr = fake_github.prs[BRANCH]
    assert len([r for r in sched.runs.runs_for(t.id) if r.mode == "review"]) == 1, sched.state.get(t.id)
    assert [r.mode for r in sched.runs.runs_for(t.id)] == ["work", "review"], sched.state.get(t.id)
    sched.tick()  # reap the automated opinion
    st = sched.state.get(t.id)
    assert st["review_rounds"] == 1
    pr.mergeable = "MERGEABLE"
    pr.checks = "SUCCESS"
    sched.tick()  # an ordinary follow-up tick must not schedule a same-product second pass
    assert len([r for r in sched.runs.runs_for(t.id) if r.mode == "review"]) == 1
    pr.review_decision = "APPROVED"
    # Current-head human approval supplies the independent second opinion.
    ok, reason = sched._automerge_gate(t, pr)
    assert ok, reason
    pr.review_decision = ""
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "second review" in reason

    # Two automated approvals alone remain insufficient, even if an old implementation has
    # left that state behind.
    st["review_rounds"] = 2
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "second review" in reason

    # A human approval remains the other independent path when the old state has two rounds.
    pr.review_decision = "APPROVED"
    ok, reason = sched._automerge_gate(t, pr)
    assert ok, reason


def test_gate_provides_tool_product_needs_two_rounds_by_default(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["products"]["demo"]["provides_tool"] = True
    st["review_rounds"] = 1
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "review round" in reason


def test_gate_self_product_two_round_default_is_overridable_per_product(sched, fake_github):
    """An explicit per-product `automerge_min_review_rounds` overrides the self/tool default."""
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["products"]["demo"]["self"] = True
    sched.cfg.data["products"]["demo"]["automerge_min_review_rounds"] = 1
    st["review_rounds"] = 1
    ok, reason = sched._automerge_gate(t, pr)
    assert ok, reason


def test_gate_self_product_honours_a_stricter_global(sched, fake_github):
    """A global setting above the self/tool floor still wins (max, not replace)."""
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["products"]["demo"]["self"] = True
    sched.cfg.data["github"]["automerge_min_review_rounds"] = 3
    st["review_rounds"] = 2
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "need 3" in reason


def test_gate_normal_product_still_needs_one_round(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    st["review_rounds"] = 1
    ok, reason = sched._automerge_gate(t, pr)
    assert ok, reason


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


def test_review_cap_hit_by_rebase_holds_automerge_instead_of_merging_stale(sched, fake_github, tmp_path):
    """CG-175: `_run_merge_queue` rebases the head of the queue right before merging it (rule
    2 in rebase.py). When that rebase changes the diff and a fresh review is due but the
    review cap is already used up, the cap sets a needs-human stop instead of dispatching a
    review — the merge must not go through on the stale, pre-rebase verdict."""
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["review"]["enabled"] = True
    sched.cfg.data["review"]["max_rounds"] = 1
    st["review_rounds"] = 1  # already at the cap
    sched.state.save()

    # main independently carries the identical change the branch's own commit made: the
    # pre-merge rebase folds the branch's commit away as already-applied, a real change to its
    # patch (CG-210: a stale `last_diff_hash` alone no longer forces a re-review).
    repo = tmp_path / "repo"
    gitc("checkout", "main", cwd=repo)
    (repo / "worker-output.txt").write_text("1\n")
    gitc("add", "worker-output.txt", cwd=repo)
    gitc("commit", "-q", "-m", "main makes the identical change", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)

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


def test_product_protected_path_holds_automerge_without_relaxing_defaults(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["products"]["demo"]["protected_paths"] = ["deployment/**"]
    _commit_in_worktree(sched.worktree_for(t), "deployment/live.yaml", "enabled: true\n")
    guarded = sched._guarded_diff_paths(t)
    assert "deployment/live.yaml" in guarded
    assert not sched._automerge_gate(t, pr)[0]


def test_invalid_branch_ownership_and_protected_paths_fail_config_load(tmp_path):
    from garden.config import Config

    (tmp_path / "garden.yaml").write_text(
        "products:\n  demo:\n    stack_owner: another-tool\n    protected_paths: deployment/**\n"
    )
    with pytest.raises(ValueError, match="stack_owner"):
        Config.load(tmp_path)

    (tmp_path / "garden.yaml").write_text(
        "products:\n  demo:\n    stack_owner: external\n    protected_paths: deployment/**\n"
    )
    with pytest.raises(ValueError, match="protected_paths"):
        Config.load(tmp_path)


def test_worker_ci_requires_a_pr_result_before_merge(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["products"]["demo"]["setup"] = {"worker_push": True}
    pr.checks = ""
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "no CI result" in reason
    pr.checks = "PENDING"
    assert not sched._automerge_gate(t, pr)[0]
    pr.checks = "FAILURE"
    assert not sched._automerge_gate(t, pr)[0]
    pr.checks = "SUCCESS"
    assert sched._automerge_gate(t, pr)[0]


def test_explicit_actions_and_status_require_rollup_but_none_does_not(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    pr.checks = ""
    for provider in ("actions", "status"):
        sched.cfg.data["products"]["demo"]["validation"] = provider
        ok, reason = sched._automerge_gate(t, pr)
        assert not ok and "no result" in reason
    sched.cfg.data["products"]["demo"]["validation"] = "none"
    assert sched._automerge_gate(t, pr)[0]


def test_command_validation_must_match_exact_pr_head(sched, fake_github):
    t, st, pr = _in_review(sched, fake_github)
    sched.cfg.data["products"]["demo"]["validation"] = {
        "provider": "command", "command": "make validate"
    }
    pr.head_sha = gitops.head_sha(sched.worktree_for(t))
    pr.checks = "FAILURE"
    st["validation_head"] = "old"
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "exact PR head" in reason
    st["validation_head"] = pr.head_sha
    assert sched._automerge_gate(t, pr)[0]
