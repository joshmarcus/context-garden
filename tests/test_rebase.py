"""Rebase as its own mode (CG-141): a diff-unchanged rebase keeps the verdict and dispatches
no review; automerge is a queue that rebases and merges only the head, one PR per tick; and
`garden metrics` reports rebases per merge and rebase cost."""

from __future__ import annotations

import subprocess

from garden.events import EventLog
from garden.events import metrics as _metrics
from garden.model import Status

BRANCH = "garden/dm-001-first-task"


def gitc(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


# ---- rule 2: a diff-unchanged rebase keeps the verdict, no review ------------
def test_clean_rebase_keeps_verdict_and_dispatches_no_review(sched, fake_github, tmp_path):
    sched.tick()
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

    # DM-001's rebase dispatched no review, and its caps did not move (a stacked sibling
    # reaching in_review may get its own first review; that is not the rebase path).
    assert not any(r.mode == "review" for r in sched.runs.runs_for("DM-001"))
    assert "DM-001(review)" not in rep.dispatched
    st = sched.state.get("DM-001")
    assert int(st.get("review_rounds", 0)) == 1  # unchanged
    assert st["last_review"]["verdict"] == "approve"  # verdict kept
    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["rebase"])
    assert any(e.get("diff_unchanged") is True and e.get("verdict_kept") is True for e in evs)
    assert any("rebased; diff unchanged; verdict kept" in ln for ln in sched.store.task("DM-001").body.splitlines())


# ---- rule 3: the merge queue keeps its head, one merge per PR ----------------
def _approve(sched, fake_github, task_id, branch, ready_at):
    st = sched.state.get(task_id)
    st["last_review"] = {"verdict": "approve", "summary": "ok"}
    st["last_review_run"] = f"rev-{task_id}"
    st["review_rounds"] = 1
    st["automerge_ready_at"] = ready_at
    pr = fake_github.prs[branch]
    pr.mergeable = "MERGEABLE"
    pr.checks = "SUCCESS"


def _advance_main(tmp_path, name):
    """Push an unrelated commit onto origin/main so every open branch falls one commit behind
    and needs exactly one rebase to come forward (mirrors the incident: eight stale PRs)."""
    repo = tmp_path / "repo"
    (repo / f"{name}.txt").write_text("unrelated\n")
    gitc("add", f"{name}.txt", cwd=repo)
    gitc("commit", "-q", "-m", f"main moves: {name}", cwd=repo)
    gitc("push", "-q", "origin", "main", cwd=repo)


def _independent_tasks(sched, n):
    """Write DM-001..DM-00n as independent (no deps) tasks and turn stacking off, so each PR
    targets main on its own."""
    sched.cfg.data["stack"] = False
    tasks_dir = sched.store.root / "demo" / "p1" / "tasks"
    for p in tasks_dir.glob("*.md"):
        p.unlink()
    for i in range(1, n + 1):
        (tasks_dir / f"DM-{i:03d}-task.md").write_text(
            f"---\nid: DM-{i:03d}\ntitle: Task {i}\nstatus: ready\ndepends_on: []\n"
            f"priority: {i}\nreading: []\ncreated: '2026-01-01T00:00:00+00:00'\n"
            f"updated: '2026-01-01T00:00:00+00:00'\n---\n\n## Goal\n\nDo thing {i}.\n")
    sched.store.invalidate()


def test_merge_queue_merges_eight_prs_each_rebased_once(sched, fake_github, tmp_path):
    """Eight approved, mergeable, green PRs merge one after another, each rebased exactly once
    (the incident: eight stale PRs the operator had to merge by hand)."""
    _independent_tasks(sched, 8)
    sched.cfg.data["max_parallel"] = 8
    sched.cfg.data["github"]["automerge"] = True
    ids = [f"DM-{i:03d}" for i in range(1, 9)]

    sched.tick()  # dispatch all eight
    sched.tick()  # all -> in_review with PRs on main
    for tid in ids:
        assert sched.store.task(tid).status == Status.IN_REVIEW, tid
    branches = {tid: sched.store.task(tid).branch for tid in ids}

    # every branch is one commit behind main, so each needs exactly one rebase.
    _advance_main(tmp_path, "moved")
    for i, tid in enumerate(ids):
        _approve(sched, fake_github, tid, branches[tid], f"2026-09-05T03:{i:02d}:00+00:00")
    sched.state.save()
    numbers = {tid: fake_github.prs[branches[tid]].number for tid in ids}

    for _ in range(40):
        sched.tick()
        if all(sched.store.task(tid).status == Status.DONE for tid in ids):
            break

    # all eight merged, oldest-approved first, each rebased exactly once.
    assert [m["number"] for m in fake_github.merged] == [numbers[tid] for tid in ids]
    for tid in ids:
        rebases = [r for r in sched.runs.runs_for(tid) if r.mode == "rebase"]
        assert len(rebases) == 1, (tid, len(rebases))
        assert sched.store.task(tid).status == Status.DONE, tid


def test_pending_rollup_keeps_head_and_does_not_rotate(sched, fake_github, tmp_path):
    """After the pre-merge rebase, a still-running rollup keeps the same task as the queue head:
    the queue does not rebase another PR, and it merges the head once the rollup goes green."""
    _independent_tasks(sched, 2)
    sched.cfg.data["max_parallel"] = 2
    sched.cfg.data["github"]["automerge"] = True

    sched.tick()
    sched.tick()
    b1, b2 = sched.store.task("DM-001").branch, sched.store.task("DM-002").branch
    _advance_main(tmp_path, "moved")  # both branches behind: the head will need a rebase
    _approve(sched, fake_github, "DM-001", b1, "2026-09-05T03:00:00+00:00")  # older -> head
    _approve(sched, fake_github, "DM-002", b2, "2026-09-05T03:05:00+00:00")
    sched.state.save()

    # first tick: the head (DM-001) is rebased and force-pushed; it is now in flight.
    sched.tick()
    st = sched.state.get("DM-001")
    assert st.get("merge_head") is True
    assert fake_github.merged == []  # not merged yet: waits for its rollup
    ready_at = st.get("automerge_ready_at")
    assert len([r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]) == 1

    # the force-push restarted CI: the rollup is pending. Several ticks must NOT rotate the head
    # or touch any other PR, and DM-001's ready_at is preserved.
    fake_github.prs[b1].checks = "PENDING"
    for _ in range(3):
        sched.tick()
        assert sched.state.get("DM-001").get("merge_head") is True
        assert sched.state.get("DM-001").get("automerge_ready_at") == ready_at
        assert fake_github.merged == []
        assert not [r for r in sched.runs.runs_for("DM-002") if r.mode == "rebase"]

    # the rollup goes green: the head merges on the next poll, without another rebase.
    fake_github.prs[b1].checks = "SUCCESS"
    sched.tick()
    assert fake_github.prs[b1].state == "MERGED"
    assert len([r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]) == 1

    # DM-002 becomes the head next and merges in turn.
    for _ in range(4):
        sched.tick()
        if fake_github.prs[b2].state == "MERGED":
            break
    assert fake_github.prs[b2].state == "MERGED"
    assert len([r for r in sched.runs.runs_for("DM-002") if r.mode == "rebase"]) == 1


def test_pre_merge_rebase_runs_checks_as_a_detached_run(sched, fake_github, tmp_path):
    """CG-182: the pre-merge rebase's checks run as a detached check run, not in the tick — the
    incident that grew ticks to a minute. The head is held until the check reaps, then merges."""
    _independent_tasks(sched, 1)
    sched.cfg.data["max_parallel"] = 1
    sched.cfg.data["github"]["automerge"] = True
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": "true"}], "ci": []}
    for _ in range(6):  # worker -> pre-PR check run -> PR (the check gates it, so an extra tick)
        sched.tick()
        if sched.store.task("DM-001").status == Status.IN_REVIEW:
            break
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    b1 = sched.store.task("DM-001").branch
    _advance_main(tmp_path, "moved")  # the branch is behind: the merge needs a rebase first
    _approve(sched, fake_github, "DM-001", b1, "2026-09-05T03:00:00+00:00")
    sched.state.save()

    # merge queue: rebase + force-push, then start the pre-PR check as a detached check run.
    rep = sched.tick()
    assert "DM-001(check:merge_rebase)" in rep.dispatched
    assert any(r.mode == "check" for r in sched.runs.runs_for("DM-001"))
    assert fake_github.prs[b1].state != "MERGED"  # not merged: the check has not been reaped yet
    assert not sched.state.get("DM-001").get("merge_head")  # the head is not held until checks pass

    for _ in range(5):
        sched.tick()  # reap the check -> hold the head -> merge on the green rollup
        if fake_github.prs[b1].state == "MERGED":
            break
    assert fake_github.prs[b1].state == "MERGED"
    assert len([r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]) == 1


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
