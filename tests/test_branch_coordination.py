"""CG-220: two writers never race on one branch.

A revise, rebase or resume run starts from the branch's head on origin, not a stale local
copy (rule 1); every push after such a run is protected by a lease naming the head it started
from, so a rejected push recovers by rebasing onto the new head once instead of failing the
task (rule 2); and the merge queue's pre-merge rebase, the stale-base probe and a PR-conflict
rebase never touch a branch a worker-mode run is actively writing to (rule 3)."""

from __future__ import annotations

import subprocess

from garden.events import EventLog
from garden.github import Feedback
from garden.model import Status
from garden.scheduler.report import TickReport
from tests.conftest import git, write

BRANCH = "garden/dm-001-first-task"


def gitc(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _sha(cwd, ref="HEAD"):
    return gitc("rev-parse", ref, cwd=cwd)


def _clone_and_push(remote, tmp_path, name, branch, filename):
    """Push a commit straight to origin/<branch> from a fresh clone, standing in for another
    writer (the merge queue's own rebase, an earlier revise round) racing the branch."""
    other = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git("checkout", branch, cwd=other)
    write(other / filename, "raced\n")
    git("add", "-A", cwd=other)
    git("commit", "-q", "-m", f"pushed by {name}", cwd=other)
    git("push", "-q", "origin", branch, cwd=other)
    return _sha(other)


# ---- rule 1: dispatch syncs to origin's head first ---------------------------
def test_revise_dispatch_syncs_to_origin_head_and_backs_up_stray_commits(sched, fake_github, tmp_path):
    sched.tick()
    sched.tick()  # DM-001 -> in_review with a PR on BRANCH
    task = sched.store.task("DM-001")
    assert task.status == Status.IN_REVIEW
    wt = sched.worktree_for(task)
    remote = sched.repo_for(task).parent / "remote.git"

    # a killed prior run left a commit in the worktree that was never pushed
    write(wt / "partial.txt", "partial\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "partial fix from a killed run", cwd=wt)
    stray_sha = _sha(wt)

    # meanwhile origin's copy of the same branch moved (the merge queue's rebase, or an
    # earlier revise's push) — a sibling commit, not an ancestor of the stray one
    origin_sha = _clone_and_push(remote, tmp_path, "other", BRANCH, "queue-rebase.txt")

    # a review requests changes; the revise round is dispatched next tick
    st = sched.state.get("DM-001")
    task.status = Status.CHANGES_REQUESTED
    sched.store.save(task)
    st["pending_feedback"] = "- fix this"
    st["revisions"] = 0
    sched.state.save()

    rep = sched.tick()
    assert "DM-001(revise)" in rep.dispatched
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"

    # the stray commit was kept, not silently dropped
    assert _sha(wt, f"backup/{run.run_id}") == stray_sha
    body = sched.store.task("DM-001").body
    assert f"backup/{run.run_id}" in body and "partial fix from a killed run" in body

    # the run started from origin's actual head, and built on top of it (not the stray commit)
    assert run.start_head == origin_sha
    assert (wt / "queue-rebase.txt").exists()
    assert not (wt / "partial.txt").exists()


# ---- rule 2: a rejected lease push recovers instead of failing the task ------
def test_lease_rejected_push_recovers_by_rebasing_onto_the_new_head(sched, fake_github, tmp_path):
    sched.tick()
    sched.tick()  # DM-001 -> in_review with a PR
    pr = fake_github.prs[BRANCH]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{
        "kind": "comment", "author": "josh", "body": "fix this", "created": "2099-01-01T00:00:00Z"}])
    rep = sched.tick()  # poll -> changes_requested -> revise dispatched and finished synchronously
    assert rep.dispatched == ["DM-001(revise)"]
    task = sched.store.task("DM-001")
    assert task.status == Status.RUNNING
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise" and run.start_head

    # race: origin/<branch> moves past the head this run started from, before it is reaped
    remote = sched.repo_for(task).parent / "remote.git"
    interloper_sha = _clone_and_push(remote, tmp_path, "other", BRANCH, "interloper.txt")
    assert interloper_sha != run.start_head

    rep = sched.tick()  # reap: the lease push is rejected, then recovered

    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["lease_rejected"])
    assert evs and evs[0]["expected"] == run.start_head and evs[0]["actual"] == interloper_sha
    assert any(r.mode == "rebase" for r in sched.runs.runs_for("DM-001"))
    assert sched.store.task("DM-001").status != Status.FAILED
    assert "DM-001 -> failed" not in rep.transitions

    # the final branch on origin carries both the interloper's file and the revise's own work
    other2 = tmp_path / "verify"
    subprocess.run(["git", "clone", "-q", str(remote), str(other2)], check=True)
    git("checkout", BRANCH, cwd=other2)
    assert (other2 / "interloper.txt").exists()
    assert (other2 / "worker-output.txt").exists()


# ---- rule 3: the mechanical rebase paths skip a task with a worker run in flight ----
def test_merge_queue_skips_a_task_with_a_worker_run_in_flight(sched, fake_github):
    """A revise round leaves `automerge_candidate` set (it can resume its queue place once the
    round reaps — see `_queue_drop_head`), so the queue's own candidate bookkeeping alone would
    still consider this task eligible. The explicit in-flight check must still hold it back."""
    sched.tick()
    sched.tick()
    task = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    st["automerge_candidate"] = True
    st["automerge_ready_at"] = "2026-01-01T00:00:00+00:00"
    st["last_review"] = {"verdict": "approve", "summary": "ok"}
    st["review_rounds"] = 1
    sched.cfg.data["github"]["automerge"] = True
    pr = fake_github.prs[task.branch]
    pr.mergeable = "MERGEABLE"
    pr.checks = "SUCCESS"
    sched.state.save()
    run = sched.runs.new_run("DM-001", "local", mode="revise")
    run.status = "running"
    run.save()

    sched._merge_candidate(task, TickReport())

    assert not [r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]
    assert fake_github.merged == []


def test_conflict_rebase_skips_a_task_with_a_worker_run_in_flight(sched, fake_github):
    sched.tick()
    sched.tick()
    task = sched.store.task("DM-001")
    fake_github.prs[task.branch].mergeable = "CONFLICTING"
    run = sched.runs.new_run("DM-001", "local", mode="revise")
    run.status = "running"
    run.save()

    sched._handle_pr_conflict(task, TickReport())

    assert not [r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]


def test_stale_base_probe_skips_a_task_with_a_worker_run_in_flight(sched, fake_github, tmp_path):
    sched.tick()
    sched.tick()
    task = sched.store.task("DM-001")
    repo = tmp_path / "repo"
    parked_sha = _sha(repo, "main")
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "base_broken", "base": "main", "base_sha": parked_sha}
    sched.state.save()

    # the base recovers: without the fence, the probe would rebase onto it right now
    write(repo / "fix.txt", "green again\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base recovered", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)

    run = sched.runs.new_run("DM-001", "local", mode="revise")
    run.status = "running"
    run.save()

    acted = sched._reprobe_base_broken(task, TickReport())

    assert acted is False
    assert not [r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]
    assert sched.state.get("DM-001").get("needs_human", {}).get("kind") == "base_broken"


def test_no_rebase_while_a_revise_is_in_flight_across_ticks(sched, fake_github, tmp_path, monkeypatch):
    """End to end: a revise is dispatched and held open (a real worker takes time); main moves
    and the PR looks conflicting; several ticks must not touch the branch until the revise
    reaps, at which point the task moves on normally."""
    from tests.inprocess import InProcessRunner

    sched.tick()
    sched.tick()  # DM-001 -> in_review with a PR
    st = sched.state.get("DM-001")
    st["automerge_candidate"] = True
    st["automerge_ready_at"] = "2026-01-01T00:00:00+00:00"
    st["last_review"] = {"verdict": "approve", "summary": "ok"}
    st["review_rounds"] = 1
    sched.cfg.data["github"]["automerge"] = True
    sched.state.save()

    orig = InProcessRunner.launch

    def start_but_dont_finish(self, run, worktree, brief_path, env):
        orig(self, run, worktree, brief_path, env)
        (run.path / "exit_code").unlink(missing_ok=True)

    monkeypatch.setattr(InProcessRunner, "launch", start_but_dont_finish)

    pr = fake_github.prs[BRANCH]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{
        "kind": "comment", "author": "josh", "body": "fix this", "created": "2099-01-01T00:00:00Z"}])
    rep = sched.tick()  # poll -> changes_requested -> revise dispatched, held open (still "running")
    assert rep.dispatched == ["DM-001(revise)"]
    assert sched.store.task("DM-001").status == Status.RUNNING

    repo = tmp_path / "repo"
    write(repo / "other.txt", "unrelated\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "main moves", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    pr.mergeable = "CONFLICTING"

    for _ in range(3):
        sched.tick()
        assert not [r for r in sched.runs.runs_for("DM-001") if r.mode == "rebase"]
        assert sched.store.task("DM-001").status == Status.RUNNING
        assert fake_github.merged == []

    # the held run finishes; the task reaps and moves on normally
    monkeypatch.setattr(InProcessRunner, "launch", orig)
    run = sched.runs.latest("DM-001")
    (run.path / "exit_code").write_text("0\n")
    for _ in range(4):
        sched.tick()
        if sched.store.task("DM-001").status != Status.RUNNING:
            break
    assert sched.store.task("DM-001").status != Status.RUNNING
