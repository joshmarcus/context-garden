"""Rebase as its own mode (CG-141): a diff-unchanged rebase keeps the verdict and dispatches
no review; automerge is a queue that rebases and merges only the head, one PR per tick; and
`garden metrics` reports rebases per merge and rebase cost."""

from __future__ import annotations

import subprocess

from garden.events import EventLog
from garden.events import metrics as _metrics
from garden.model import Status
from tests.conftest import wait_for_runs

BRANCH = "garden/dm-001-first-task"


def gitc(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


# ---- rule 2: a diff-unchanged rebase keeps the verdict, no review ------------
def test_clean_rebase_keeps_verdict_and_dispatches_no_review(sched, fake_github, tmp_path):
    sched.tick()
    wait_for_runs(sched)
    sched.tick()  # DM-001 -> in_review with a PR (review disabled so far: no review run)
    assert sched.store.task("DM-001").status == Status.IN_REVIEW

    # a standing approving verdict from the reviewed push
    st = sched.state.get("DM-001")
    st["last_review"] = {"verdict": "approve", "summary": "looks good"}
    st["last_review_run"] = "rev-1"
    st["review_rounds"] = 1
    sched.state.save()

    # main advances with a change the branch never touched: the rebase is clean and does not
    # alter this branch's diff against the new base.
    repo = tmp_path / "repo"
    (repo / "other.txt").write_text("unrelated\n")
    gitc("add", "other.txt", cwd=repo)
    gitc("commit", "-q", "-m", "main moves", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)
    fake_github.prs[BRANCH].mergeable = "CONFLICTING"

    # review is on for this tick: only the rebase path could dispatch one now, and it must not.
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2}
    rep = sched.tick()

    # no review run was dispatched, and the caps did not move
    assert not any(r.mode == "review" for r in sched.runs.all_runs())
    assert "DM-001(review)" not in rep.dispatched
    st = sched.state.get("DM-001")
    assert int(st.get("review_rounds", 0)) == 1  # unchanged
    assert st["last_review"]["verdict"] == "approve"  # verdict kept
    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["rebase"])
    assert any(e.get("diff_unchanged") is True and e.get("verdict_kept") is True for e in evs)
    assert any("rebased; diff unchanged; verdict kept" in ln for ln in sched.store.task("DM-001").body.splitlines())


# ---- rule 3: the merge queue merges only the head, one per tick --------------
def _independent_two_task_garden(sched):
    """Drop DM-002's dependency and turn off stacking so both PRs target main independently."""
    sched.cfg.data["stack"] = False
    p = sched.store.root / "demo" / "p1" / "tasks" / "DM-002-second.md"
    p.write_text(p.read_text().replace("depends_on: [DM-001]", "depends_on: []"))
    sched.store.invalidate()


def _approve(sched, fake_github, task_id, branch, ready_at):
    st = sched.state.get(task_id)
    st["last_review"] = {"verdict": "approve", "summary": "ok"}
    st["last_review_run"] = f"rev-{task_id}"
    st["review_rounds"] = 1
    st["automerge_ready_at"] = ready_at
    pr = fake_github.prs[branch]
    pr.mergeable = "MERGEABLE"
    pr.checks = "SUCCESS"


def test_merge_queue_merges_head_only_each_rebased_once(sched, fake_github, tmp_path):
    _independent_two_task_garden(sched)
    sched.cfg.data["github"]["automerge"] = True
    b1, b2 = "garden/dm-001-first-task", "garden/dm-002-second-task"

    sched.tick()  # dispatch both (max_parallel=2)
    wait_for_runs(sched)
    sched.tick()  # both -> in_review with PRs on main
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    assert sched.store.task("DM-002").status == Status.IN_REVIEW

    _approve(sched, fake_github, "DM-001", b1, "2026-09-05T03:00:00+00:00")  # older -> head
    _approve(sched, fake_github, "DM-002", b2, "2026-09-05T03:05:00+00:00")
    sched.state.save()

    # first poll: only the head of the queue (DM-001) is rebased and merged
    sched.tick()
    assert [m["number"] for m in fake_github.merged] == [fake_github.prs[b1].number]
    assert fake_github.prs[b1].state == "MERGED" and fake_github.prs[b2].state == "OPEN"
    rb1 = [r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]
    assert len(rb1) == 1  # rebased once, right before it merged
    assert not [r for r in sched.runs.runs_for("DM-002") if r.mode == "rebase"]  # not yet touched

    # next poll: DM-001 reaches done, and the next candidate (DM-002) is rebased and merged
    sched.tick()
    assert sched.store.task("DM-001").status == Status.DONE
    assert fake_github.prs[b2].state == "MERGED"
    rb2 = [r for r in sched.runs.runs_for("DM-002") if r.mode == "rebase"]
    assert len(rb2) == 1  # rebased exactly once


# ---- metrics ----------------------------------------------------------------
def test_metrics_reports_rebases_per_merge_and_cost(sched, fake_github, tmp_path):
    events = [
        {"at": "2026-09-05T03:00:00+00:00", "kind": "dispatch", "task": "DM-001", "mode": "work"},
        {"at": "2026-09-05T03:01:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "rebase", "cost_usd": 0.0},
        {"at": "2026-09-05T03:02:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "rebase", "cost_usd": 0.5},
        {"at": "2026-09-05T03:03:00+00:00", "kind": "transition", "task": "DM-001", "to": "done"},
    ]
    m = _metrics(events, {})
    rb = m["rebase"]
    assert rb["rebases"] == 2
    assert rb["merges"] == 1
    assert rb["per_merge"] == 2.0
    assert rb["cost_usd"] == 0.5
