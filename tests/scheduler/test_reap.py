"""Reap: what a finished worker run turns into (retry, fail, push, pre-PR checks, the base probe, manual runs)."""

import os
import shutil
import subprocess
import sys

from garden.model import Status
from garden.runner.manual import ManualRunner
from tests.conftest import FAKE_CLAUDE, git, wait_for_runs, write
from tests.scheduler.conftest import make_idle, statuses, wait_for_stdout


def test_interrupted_reap_finalizes_on_next_tick_instead_of_redispatching(sched, fake_github, monkeypatch):
    """CG-083: a crash between the run record's final-status write and the task
    transition / push / PR step must not strand the finished run. Simulate the
    crash by making the push step (which runs right after `run.status = "done"`
    is saved, but before the PR is opened and the task is transitioned) blow up
    with an unhandled error, then tick again."""
    from garden import gitops

    sched.tick()
    wait_for_runs(sched)

    real_push = gitops.push
    calls = {"n": 0}

    def flaky_push(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash mid-reap")
        return real_push(*a, **k)

    monkeypatch.setattr(gitops, "push", flaky_push)

    rep = sched.tick()
    assert any("DM-001" in e for e in rep.errors)
    assert statuses(sched)["DM-001"] == "running"  # never transitioned
    run = sched.runs.latest("DM-001")
    assert run.status == "done"  # the run record was already finalized on disk
    assert not fake_github.created  # no PR was opened yet

    # `garden runs` must surface this as finished-but-unreaped, not just "done"
    assert run.run_id in sched.unreaped_run_ids()

    monkeypatch.setattr(gitops, "push", real_push)
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    assert len(fake_github.created) == 1  # the finished run was reaped, not redispatched
    assert "DM-001(work)" not in rep.dispatched  # DM-001 itself was not redispatched
    all_runs = sched.runs.runs_for("DM-001")
    assert len(all_runs) == 1 and all_runs[0].run_id == run.run_id  # still the same single run
    assert not sched.unreaped_run_ids()

    # CG-153: the resumed reap must not emit run_finished a second time (which would
    # double-count the run's cost). Exactly one run_finished for this single run.
    finished = [e for e in sched.events.read(task_id="DM-001", kinds=["run_finished"])
                if e.get("run") == run.run_id]
    assert len(finished) == 1


def _run_fake_claude(cwd, task_id, run_id, when):
    env = dict(os.environ, GARDEN_TASK_ID=task_id, GARDEN_RUN_ID=run_id, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    env.pop("FAKE_CLAUDE_MODE", None)
    subprocess.run([sys.executable, str(FAKE_CLAUDE)], cwd=cwd, input="brief", capture_output=True, text=True, env=env, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_stacked_runs_never_collide_into_the_same_commit(tmp_path):
    """A stack child's own work run and its parent's revise round both branch from the
    parent's tip, write the same counter value to the same file, and can finish in the
    same wall-clock second. If the fake worker's commit is otherwise identical (same
    tree, parent, author, message and timestamp), git dedupes the two into one object:
    the child's branch ends up pointing at the parent's revise commit, its own commit
    silently vanishes, and `commits_ahead()` reports 0 -- the scheduler then discards the
    child's real work as "worker finished with no commits" (this is what actually caused
    the intermittent DM-002 PR seen in test_feedback_triggers_revise_round, not a race in
    finalize()'s PR lookup). Mixing task/run identity into the commit message keeps every
    run's commit distinct even when timestamps and content otherwise collide."""
    base = tmp_path / "base"
    base.mkdir()
    git("init", "-q", "-b", "main", cwd=base)
    (base / "worker-output.txt").write_text("1\n")
    git("add", "-A", cwd=base)
    git("commit", "-q", "-m", "parent work", cwd=base)

    a, b = tmp_path / "a", tmp_path / "b"
    shutil.copytree(base, a)
    shutil.copytree(base, b)
    same_instant = "2024-01-01T00:00:00"

    sha_a = _run_fake_claude(a, "DM-001", "20260101T000000Z-revise", same_instant)
    sha_b = _run_fake_claude(b, "DM-002", "20260101T000000Z-work", same_instant)
    assert sha_a != sha_b


def test_pre_pr_check_failure_at_cap_needs_human(sched, fake_github):
    """A pre-PR check that fails once the revision cap is reached hands off to a human,
    exactly like the review path — it does not leave the task queued-but-skipped."""
    # A branch-owned failure (passes at the base, where worker-output.txt does not exist, so the
    # CG-131 base probe does not divert it) that still fails once the revision cap is reached.
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": "test ! -f worker-output.txt"}], "ci": []}
    sched.cfg.data["max_revisions"] = 2
    sched.tick()
    wait_for_runs(sched)
    sched.state.get("DM-001")["revisions"] = 2  # pretend two revise rounds were already used
    sched.state.save()
    rep = sched.tick()  # reap -> pre-PR check fails at the cap
    assert statuses(sched)["DM-001"] == "changes_requested"
    st = sched.state.get("DM-001")
    assert st.get("needs_human") and "revision rounds already used" in st["needs_human"]
    assert any("cap" in tr for tr in rep.transitions)


def _seed_base_guard(sched, content: str) -> str:
    """Commit a sentinel file to the product's base and push it. The pre-PR `guard` check
    passes only when the sentinel reads `ok`. Returns the new base commit sha."""
    repo = sched.repo_for(sched.store.task("DM-001"))
    write(repo / "sentinel.txt", content + "\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", f"base: sentinel={content}", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def test_check_failing_at_moved_base_is_rebased_not_revised(sched, fake_github):
    """CG-131: a pre-PR check that fails on the branch and at its (stale) base does not spend a
    revise round. When the base branch has moved and gone green, the loop rebases onto it and
    re-runs the checks without a worker; the branch reaches review with no revision used."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "guard", "command": "grep -qx ok sentinel.txt"}], "ci": []}
    _seed_base_guard(sched, "bad")  # red base: guard fails here and on any branch cut from it

    sched.tick()  # dispatch DM-001 from the red base
    wait_for_runs(sched)
    _seed_base_guard(sched, "ok")  # main goes green before the branch is reaped

    rep = sched.tick()  # reap: guard fails on the branch; base moved + green -> rebase, pass, PR
    assert statuses(sched)["DM-001"] == "in_review"
    assert not any("revise" in d for d in rep.dispatched)
    assert sched.state.get("DM-001").get("revisions", 0) == 0
    # the rebased branch picked up the now-green base file
    wt = sched.worktree_for(sched.store.task("DM-001"))
    assert (wt / "sentinel.txt").read_text().strip() == "ok"


def test_check_failing_at_unmoved_base_parks_without_revise(sched, fake_github):
    """CG-131: when the base branch itself is red (it has not moved), a pre-PR check that also
    fails there parks the task on a card that names the check and the base commit — no revise
    round, no worker, no spend."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "guard", "command": "grep -qx ok sentinel.txt"}], "ci": []}
    base_sha = _seed_base_guard(sched, "bad")  # red base that stays put

    sched.tick()
    wait_for_runs(sched)
    runs_before = len(sched.runs.runs_for("DM-001"))
    rep = sched.tick()  # reap: guard fails on the branch and at the unmoved base -> card

    assert statuses(sched)["DM-001"] == "changes_requested"
    info = sched.state.get("DM-001").get("needs_human")
    assert isinstance(info, dict) and info["kind"] == "base_broken"
    assert "guard" in info["reason"] and base_sha[:12] in info["reason"]
    assert not any("revise" in d for d in rep.dispatched)
    # no revise worker dispatched now or on the next tick: the task waits without spending
    rep2 = sched.tick()
    assert not any("DM-001" in d for d in rep.dispatched + rep2.dispatched)
    assert len(sched.runs.runs_for("DM-001")) == runs_before
    # the Inbox surfaces it as a base-broken card
    from garden.inbox import build_inbox
    cards = [it for it in build_inbox(sched.store, sched) if it["task"] == "DM-001"]
    assert any(c["group"] == "attention" for c in cards)


def test_crash_retries_then_fails(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    sched.tick()
    wait_for_runs(sched)
    rep = sched.tick()
    assert "DM-001 -> ready (retry)" in rep.transitions
    assert rep.dispatched == ["DM-001(work)"]  # retried immediately
    wait_for_runs(sched)
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    t = sched.store.task("DM-001")
    assert t.attempts == 2 and "giving up" in t.body


def test_no_commits_is_a_failure(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "blocked")
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    assert "Which database?" in sched.store.task("DM-001").body


def test_noresult_retries(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult")
    sched.tick()
    wait_for_runs(sched)
    rep = sched.tick()
    assert "DM-001 -> ready (retry)" in rep.transitions


def test_idle_worker_is_stopped_before_timeout(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    monkeypatch.setenv("FAKE_CLAUDE_STALL_SECONDS", "30")
    sched.cfg.data["idle_kill_minutes"] = 5
    sched.cfg.data["max_attempts"] = 1  # terminal on first failure: no second stall worker
    sched.tick()  # dispatch DM-001; the worker starts sleeping
    run = sched.runs.latest("DM-001")
    assert run.status == "running" and run.pid
    wait_for_stdout(run)
    # nothing has changed for 12 minutes, past idle_kill_minutes but well under timeout_minutes
    make_idle(run, 12)
    rep = sched.tick()
    assert "DM-001 -> failed" in rep.transitions
    assert statuses(sched)["DM-001"] == "failed"
    run = sched.runs.latest("DM-001")
    assert run.status == "timeout" and "idle" in run.error


def test_running_card_shows_idle_time(sched, monkeypatch):
    from garden.inbox import running_now
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    monkeypatch.setenv("FAKE_CLAUDE_STALL_SECONDS", "30")
    sched.cfg.data["idle_minutes"] = 5
    sched.tick()
    run = sched.runs.latest("DM-001")
    wait_for_stdout(run)
    # a fresh worker is not flagged
    assert all(r["idle"] is None for r in running_now(sched.store))
    make_idle(run, 8)
    row = next(r for r in running_now(sched.store) if r["task"] == "DM-001")
    assert row["idle"] is not None and row["idle"] >= 5
    run.kill()


def test_no_github_still_pushes(sched, fake_github):
    fake_github.available = False
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    t = sched.store.task("DM-001")
    assert t.status == Status.IN_REVIEW and not t.pr and "GitHub unavailable" in t.body


def test_manual_take_and_finish(sched, fake_github):
    t = sched.store.task("DM-001")
    run = sched.dispatch(t, runner=ManualRunner({}), worktree=True)
    assert statuses(sched)["DM-001"] == "running"
    assert sched.slots_free() == 2  # manual runs don't occupy slots
    sched.tick()
    assert statuses(sched)["DM-001"] == "running"  # not reaped: no exit_code yet
    wt = run.worktree
    with open(os.path.join(wt, "hello.txt"), "w") as f:
        f.write("hi\n")
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "manual work"], cwd=wt, check=True)
    sched.finish_manual(sched.store.task("DM-001"), {"status": "done", "summary": "by hand", "pr_title": "manual PR"})
    assert statuses(sched)["DM-001"] == "in_review"
    assert fake_github.created[-1]["title"] == "manual PR"


def test_tick_does_not_race_manual_finish(sched, fake_github):
    """A tick fired between ManualRunner.finish() and finalize() must leave the task alone."""
    t = sched.store.task("DM-001")
    run = sched.dispatch(t, runner=ManualRunner({}), worktree=True)
    wt = run.worktree

    # simulate the human doing work and committing
    with open(os.path.join(wt, "hello.txt"), "w") as f:
        f.write("hi\n")
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "manual work"], cwd=wt, check=True)

    # ManualRunner.finish() writes result.json + exit_code — this is the race window start
    ManualRunner.finish(run, {"status": "done", "summary": "by hand", "pr_title": "manual PR"})
    assert (run.path / "exit_code").exists()
    assert sched.runs.latest("DM-001").status == "running"  # run.json still says running on disk

    # tick fires inside the window: must not transition the task
    rep = sched.tick()
    assert rep.transitions == [], f"tick must not transition mid-finish: {rep.transitions}"
    assert statuses(sched)["DM-001"] == "running"

    # finalize() completes the single clean transition
    sched.finalize(t, run, sched.runner_for(t, run.runner), rep)
    sched.state.save()
    sched.store.invalidate()
    assert statuses(sched)["DM-001"] == "in_review"
    # the tick must not have inserted a spurious "back to ready" revert
    t = sched.store.task("DM-001")
    assert "back to ready" not in t.body, "tick must not have reverted the task"


def test_running_without_run_record_resets(sched):
    t = sched.store.task("DM-001")
    t.status = Status.RUNNING
    sched.store.save(t)
    rep = sched.tick()
    assert "no run" in rep.transitions[0]


def test_no_active_run_logs_run_id_and_closer(sched):
    """When reap finds a task running but its expected run already finished, the log
    names the run and its closer so the disappearance is traceable."""
    sched.pause(by="test")  # keep the reset task from being re-dispatched this tick
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    sched.store.save(task)
    run = sched.runs.new_run("DM-001", "local", mode="work")
    run.status = "done"
    run.error = "closed by orphan sweep: task moved on before this run's verdict was read"
    run.save()
    rep = sched.tick()
    assert f"{task.id} running -> ready (no run)" in rep.transitions
    body = sched.store.task("DM-001").body
    assert run.run_id in body and "closed by orphan sweep" in body
