"""Hard-tier automerge (CG-191): the queue merges a hard-tier PR after two approving review
rounds and the garden's own scratch-merge check, as a config choice that defaults on."""

from __future__ import annotations

from garden import gitops
from garden.events import EventLog
from garden.model import Status, Task

BRANCH = "garden/dm-001-first-task"


def _hard_in_review(sched, fake_github, *, rounds=2, checks=True):
    """Drive DM-001 to in_review, then make it a hard-tier PR with an approving review, green
    gates, and (by default) a configured pre-PR check the scratch merge can run."""
    sched.tick()
    sched.tick()
    t = sched.store.task("DM-001")
    assert t.status == Status.IN_REVIEW
    t.difficulty = "hard"
    sched.store.save(t)
    sched.store.invalidate()
    t = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    st["last_review"] = {"verdict": "approve", "summary": "ok"}
    st["last_review_run"] = "rev-1"
    st["review_rounds"] = rounds
    st["last_diff_hash"] = gitops.diff_hash(sched.worktree_for(t), "main")
    sched.state.save()
    pr = fake_github.prs[BRANCH]
    pr.mergeable = "MERGEABLE"
    pr.checks = "SUCCESS"
    sched.cfg.data["github"]["automerge"] = True
    if checks:
        sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": "true"}], "ci": []}
    return t, st, pr


# ---- the switch --------------------------------------------------------------
def test_hard_tier_default_is_on():
    from garden.config import DEFAULTS
    assert DEFAULTS["github"]["automerge_hard_tier"] is True


def test_hard_tier_off_holds_by_tier(sched, fake_github):
    t, st, pr = _hard_in_review(sched, fake_github)
    sched.cfg.data["github"]["automerge_hard_tier"] = False
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "tier `hard`" in reason


def test_hard_tier_per_product_override(sched, fake_github):
    sched.cfg.data["github"]["automerge_hard_tier"] = False
    sched.cfg.data["products"]["demo"]["automerge_hard_tier"] = True
    t = Task(path=sched.store.root, id="X", title="", product="demo", difficulty="hard")
    assert sched._hard_tier_automerge(t) is True


def test_medium_tier_unaffected_by_hard_policy(sched, fake_github):
    t = Task(path=sched.store.root, id="X", title="", product="demo", difficulty="medium")
    assert sched._hard_tier_automerge(t) is False


# ---- each extra gate ---------------------------------------------------------
def test_hard_tier_needs_two_rounds(sched, fake_github):
    t, st, pr = _hard_in_review(sched, fake_github, rounds=1)
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "need 2" in reason


def test_hard_tier_held_until_scratch_check_passes(sched, fake_github):
    t, st, pr = _hard_in_review(sched, fake_github)
    # Two rounds and every plain gate is green, but the scratch-merge check has not run yet.
    ok, reason = sched._automerge_gate(t, pr)
    assert not ok and "scratch-merge check" in reason
    # ...and the same gate without the scratch requirement is otherwise green.
    ok2, _ = sched._automerge_gate(t, pr, require_scratch=False)
    assert ok2


# ---- end to end --------------------------------------------------------------
def test_hard_tier_scratch_check_runs_then_merges(sched, fake_github):
    t, st, pr = _hard_in_review(sched, fake_github)

    rep = sched.tick()  # poll: every other gate green -> dispatch the scratch-merge check
    assert "DM-001(check:scratch_merge)" in rep.dispatched
    assert any(r.mode == "check" for r in sched.runs.runs_for("DM-001"))
    assert fake_github.merged == []  # nothing merges while the scratch check is in flight
    assert not sched._scratch_merge_verified(sched.store.task("DM-001"))

    for _ in range(6):
        sched.tick()  # reap the scratch check -> verified -> queue rebases/merges the head
        if fake_github.prs[BRANCH].state == "MERGED":
            break
    assert fake_github.prs[BRANCH].state == "MERGED"
    auto = sched.state.get("DM-001").get("automerged")
    assert auto and auto["review_rounds"] == 2

    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["scratch_merge"])
    assert any(e.get("resolved") is True for e in evs)


def test_hard_tier_scratch_check_failure_holds_the_merge(sched, fake_github):
    t, st, pr = _hard_in_review(sched, fake_github)
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": "false"}], "ci": []}

    reps = [sched.tick() for _ in range(4)]
    # The reap of the failing scratch check holds the merge directly; it does not raise an error
    # that a later tick recovers from (the block reason must not be re-derived from a swallowed one).
    assert not any(r.errors for r in reps), [r.errors for r in reps]
    assert fake_github.merged == []
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    blocked = sched.state.get("DM-001").get("automerge_blocked")
    assert blocked and "scratch-merge check failed" in blocked
    # the reap recorded the failure and left the queue (no candidate/head lingers)
    st_now = sched.state.get("DM-001")
    assert st_now.get("scratch_merge", {}).get("ok") is False
    assert "automerge_candidate" not in st_now and "merge_head" not in st_now
    assert not sched._scratch_merge_verified(sched.store.task("DM-001"))
    # the recorded failure is keyed to the reviewed diff, so it is not re-run every tick
    assert len([r for r in sched.runs.runs_for("DM-001") if r.mode == "check"]) == 1


def test_hard_tier_with_no_checks_merges_after_two_rounds(sched, fake_github):
    """A garden with no pre-PR checks has nothing for the scratch merge to run: the check is
    vacuously satisfied and the hard-tier PR still merges after its two approving rounds."""
    t, st, pr = _hard_in_review(sched, fake_github, checks=False)

    for _ in range(4):
        sched.tick()
        if fake_github.prs[BRANCH].state == "MERGED":
            break
    assert fake_github.prs[BRANCH].state == "MERGED"


def test_hard_tier_one_round_never_dispatches_a_scratch_check(sched, fake_github):
    t, st, pr = _hard_in_review(sched, fake_github, rounds=1)
    for _ in range(3):
        sched.tick()
    assert not any(r.mode == "check" for r in sched.runs.runs_for("DM-001"))
    assert fake_github.merged == []
