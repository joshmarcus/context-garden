"""Reap: what a finished worker run turns into (retry, fail, push, pre-PR checks, the base probe, manual runs)."""

import os
import shutil
import subprocess

from garden.model import Status
from garden.runner.manual import ManualRunner
from garden.scheduler.report import TickReport
from tests import fake_claude
from tests.conftest import git, write
from tests.scheduler.conftest import make_idle, statuses


def drive(sched, until, n=8):
    """Tick until `until(sched)` holds (or `n` ticks pass), accumulating what was dispatched
    and transitioned. Checks are detached run records now (CG-182): a pre-PR check, a base
    probe and a stale-base rebase re-check each take their own tick, so a test drives the loop
    to a stable state instead of asserting a single tick's outcome."""
    dispatched: set[str] = set()
    transitions: set[str] = set()
    for _ in range(n):
        rep = sched.tick()
        dispatched |= set(rep.dispatched)
        transitions |= set(rep.transitions)
        if until(sched):
            break
    return dispatched, transitions


def test_interrupted_reap_finalizes_on_next_tick_instead_of_redispatching(sched, fake_github, monkeypatch):
    """CG-083: a crash between the run record's final-status write and the task
    transition / push / PR step must not strand the finished run. Simulate the
    crash by making the push step (which runs right after `run.status = "done"`
    is saved, but before the PR is opened and the task is transitioned) blow up
    with an unhandled error, then tick again."""
    from garden import gitops

    sched.tick()

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
    _, _, code = fake_claude.run([], "brief", cwd, env)
    assert code == 0
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
    sched.state.get("DM-001")["revisions"] = 2  # pretend two revise rounds were already used
    sched.state.save()
    _, transitions = drive(sched, lambda s: statuses(s)["DM-001"] == "changes_requested")
    assert statuses(sched)["DM-001"] == "changes_requested"
    st = sched.state.get("DM-001")
    assert st.get("needs_human") and "revision rounds already used" in st["needs_human"]
    assert any("cap" in tr for tr in transitions)


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
    _seed_base_guard(sched, "ok")  # main goes green before the branch is reaped

    # reap: guard fails on the branch; base probe finds it red-and-moved -> rebase + re-check -> PR
    dispatched, _ = drive(sched, lambda s: statuses(s)["DM-001"] == "in_review")
    assert statuses(sched)["DM-001"] == "in_review"
    assert not any("revise" in d for d in dispatched)
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
    # reap: guard fails on the branch and at the unmoved base -> base_broken card
    dispatched, _ = drive(sched, lambda s: statuses(s)["DM-001"] == "changes_requested")
    assert statuses(sched)["DM-001"] == "changes_requested"
    info = sched.state.get("DM-001").get("needs_human")
    assert isinstance(info, dict) and info["kind"] == "base_broken"
    assert "guard" in info["reason"] and base_sha[:12] in info["reason"]
    assert not any("revise" in d for d in dispatched)
    # no revise worker or check run dispatched while parked: the task waits without spending
    runs_before = len(sched.runs.runs_for("DM-001"))
    rep2 = sched.tick()
    assert not any("DM-001" in d for d in rep2.dispatched)
    assert len(sched.runs.runs_for("DM-001")) == runs_before
    # the Inbox surfaces it as a base-broken card
    from garden.inbox import build_inbox
    cards = [it for it in build_inbox(sched.store, sched) if it["task"] == "DM-001"]
    assert any(c["group"] == "attention" for c in cards)


def test_base_broken_task_continues_itself_when_base_goes_green(sched, fake_github):
    """CG-170: a task parked with the base_broken stop re-probes its base every tick and, the
    moment the base branch goes green, rebases mechanically and re-runs the checks by itself —
    the PR opens with no worker run dispatched, no person, and no revise round spent."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "guard", "command": "grep -qx ok sentinel.txt"}], "ci": []}
    _seed_base_guard(sched, "bad")  # red base the branch is cut from

    sched.tick()  # dispatch DM-001 from the red base (the in-process worker finishes here)
    # reap: guard fails on the branch and at the unmoved base -> parked base_broken
    drive(sched, lambda s: statuses(s)["DM-001"] == "changes_requested")
    assert statuses(sched)["DM-001"] == "changes_requested"
    info = sched.state.get("DM-001").get("needs_human")
    assert isinstance(info, dict) and info["kind"] == "base_broken"
    runs_before = len(sched.runs.runs_for("DM-001"))

    _seed_base_guard(sched, "ok")  # the base branch is fixed and goes green
    # re-probe: base moved + green -> mechanical rebase, re-check run, open PR, no worker
    dispatched, _ = drive(sched, lambda s: statuses(s)["DM-001"] == "in_review")

    assert statuses(sched)["DM-001"] == "in_review"
    assert not sched.state.get("DM-001").get("needs_human")  # the stop is cleared
    assert not any(d.startswith("DM-001(work") or "revise" in d for d in dispatched)  # no worker run
    assert len(fake_github.created) == 1  # the PR is open
    assert sched.state.get("DM-001").get("revisions", 0) == 0  # no revise round spent
    # the runs added are the no-cost mechanical rebase and the detached re-check, not a worker run
    added = sched.runs.runs_for("DM-001")[runs_before:]
    modes = [r.mode for r in added]
    assert "rebase" in modes and "work" not in modes and "revise" not in modes
    assert next(r for r in added if r.mode == "rebase").cost_usd == 0.0
    # the rebased branch picked up the now-green base file
    wt = sched.worktree_for(sched.store.task("DM-001"))
    assert (wt / "sentinel.txt").read_text().strip() == "ok"
    # a rebased_stale_base event records the automatic continuation
    assert any(e.get("resolved") for e in sched.events.read(task_id="DM-001", kinds=["rebased_stale_base"]))


def test_base_broken_task_stays_parked_while_base_stays_red(sched, fake_github):
    """CG-170: while the base has not moved, the parked task re-probes cheaply and waits — no
    rebase run, no worker, no spend — until the base actually changes."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "guard", "command": "grep -qx ok sentinel.txt"}], "ci": []}
    _seed_base_guard(sched, "bad")

    sched.tick()
    drive(sched, lambda s: statuses(s)["DM-001"] == "changes_requested")  # parked base_broken
    runs_before = len(sched.runs.runs_for("DM-001"))

    rep = sched.tick()  # base unchanged: the re-probe waits
    assert statuses(sched)["DM-001"] == "changes_requested"
    assert sched.state.get("DM-001").get("needs_human", {}).get("kind") == "base_broken"
    assert not any("DM-001" in d for d in rep.dispatched)
    assert len(sched.runs.runs_for("DM-001")) == runs_before  # no rebase/check run created while waiting


def test_stale_base_rebase_conflict_does_not_count_toward_revision_cap(sched, fake_github, monkeypatch):
    """CG-139: a stale base (CG-131) that has moved but is still red hands the branch a revise
    round to resolve the mechanical rebase by hand once `gitops.rebase_onto` can't apply
    cleanly. That round is bookkeeping, not a fix the worker was asked to make: it must keep
    its own `rebases` counter and never burn through max_revisions (0 in this fixture) or flag
    needs_human, however many times in a row it recurs."""
    from garden import gitops

    sched.cfg.data["stack"] = False
    sched.cfg.data["max_revisions"] = 0  # any ordinary revise round would need_human immediately
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "guard", "command": "grep -qx ok sentinel.txt"}], "ci": []}
    _seed_base_guard(sched, "bad")  # red base: guard fails here and on any branch cut from it

    sched.tick()  # dispatch DM-001 from the red base
    _seed_base_guard(sched, "still-bad")  # base moves, but stays red

    # the mechanical rebase onto the moved base never applies cleanly
    monkeypatch.setattr(gitops, "rebase_onto", lambda worktree, onto: (False, ["sentinel.txt"]))

    for i in range(3):
        # each cycle reaps the running round, runs the pre-PR check and base probe as detached
        # runs (guard fails on the branch and at the moved, still-red base), flags it as a rebase
        # round when `rebase_onto` can't apply, and redispatches the exempt revise despite
        # max_revisions=0 — never counting a revision or flagging needs_human
        got_revise = False
        for _ in range(6):
            rep = sched.tick()
            if "DM-001(revise)" in rep.dispatched:
                got_revise = True
                break
        assert got_revise
        st = sched.state.get("DM-001")
        assert st["rebases"] == i + 1
        assert st.get("revisions", 0) == 0
        assert not st.get("needs_human")


def test_crash_retries_then_fails(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    sched.tick()
    rep = sched.tick()
    assert "DM-001 -> ready (retry)" in rep.transitions
    assert rep.dispatched == ["DM-001(work)"]  # retried immediately
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    t = sched.store.task("DM-001")
    assert t.attempts == 2 and "giving up" in t.body


def test_no_commits_is_a_failure(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "blocked")
    sched.tick()
    sched.tick()
    assert statuses(sched)["DM-001"] == "failed"
    assert "Which database?" in sched.store.task("DM-001").body


def test_noresult_retries(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult")
    sched.tick()
    rep = sched.tick()
    assert "DM-001 -> ready (retry)" in rep.transitions


def test_idle_worker_is_stopped_before_timeout(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    sched.cfg.data["idle_kill_minutes"] = 5
    sched.cfg.data["max_attempts"] = 1  # terminal on first failure: no second stall worker
    sched.tick()  # dispatch DM-001; the worker goes silent and never writes exit_code
    run = sched.runs.latest("DM-001")
    assert run.status == "running" and not run.process_finished()
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
    sched.cfg.data["idle_minutes"] = 5
    sched.tick()
    run = sched.runs.latest("DM-001")
    # a fresh worker is not flagged
    assert all(r["idle"] is None for r in running_now(sched.store))
    make_idle(run, 8)
    row = next(r for r in running_now(sched.store) if r["task"] == "DM-001")
    assert row["idle"] is not None and row["idle"] >= 5


def test_no_github_still_pushes(sched, fake_github):
    fake_github.available = False
    sched.tick()
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


def _seed_prior_progress(sched, task_id="DM-001", n=2):
    """Leave `n` commits from an interrupted attempt on the task's branch worktree, without
    a live run: set the task RUNNING and drop a single done run record (no finished_at) in
    front of reap, so the next tick takes the "no active run" path with real progress present."""
    from garden import gitops

    task = sched.store.task(task_id)
    branch = task.default_branch()
    wt = sched.worktree_for(task)
    gitops.prepare_worktree(sched.repo_for(task), wt, branch, "main")
    for i in range(n):
        write(wt / f"progress-{i}.txt", "partial\n")
        git("add", "-A", cwd=wt)
        git("commit", "-q", "-m", f"{task_id}: partial fix {i}", cwd=wt)
    task.status = Status.RUNNING
    sched.store.save(task)
    run = sched.runs.new_run(task_id, "local", mode="work")
    run.status = "done"
    run.save()
    return wt


def test_no_active_run_distinguishes_prior_progress(sched):
    """CG-125: when a run disappears but the worktree already holds commits from the
    interrupted attempt, the 'back to ready' log names that real, unreported progress
    (and the event carries the commit count) — distinct from a clean restart."""
    from garden import gitops

    sched.pause(by="test")  # keep the reset task from being re-dispatched this tick
    wt = _seed_prior_progress(sched, n=2)
    assert gitops.commits_ahead(wt, "main") == 2

    rep = sched.tick()
    assert "DM-001 running -> ready (no run)" in rep.transitions
    body = sched.store.task("DM-001").body
    assert "prior attempt made real progress" in body and "2 commits" in body
    event = sched.events.read(task_id="DM-001", kinds=["no_active_run"])[-1]
    assert event.get("prior_commits", 0) == 2


def test_no_active_run_clean_restart_has_no_progress_note(sched):
    """A run that vanished before committing anything is a clean restart: the 'back to
    ready' log carries no progress note and the event reports zero prior commits."""
    sched.pause(by="test")
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    sched.store.save(task)
    run = sched.runs.new_run("DM-001", "local", mode="work")
    run.status = "done"
    run.save()

    rep = sched.tick()
    assert f"{task.id} running -> ready (no run)" in rep.transitions
    body = sched.store.task("DM-001").body
    assert "prior attempt made real progress" not in body
    event = sched.events.read(task_id="DM-001", kinds=["no_active_run"])[-1]
    assert event.get("prior_commits", 0) == 0


def _revise_in_flight(sched, monkeypatch):
    """Drive DM-001 to a stalled revise run: work -> PR -> triage back for changes -> a revise
    round that goes silent and stays `running` across the next tick. Returns (task, revise_run)."""
    sched.cfg.data["stack"] = False
    sched.tick()  # dispatch work
    sched.tick()  # reap work -> in_review, PR opened
    sched.triage(sched.store.task("DM-001"), changes="please revisit")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    sched.tick()  # dispatch a revise run; the worker goes silent and stays running
    task = sched.store.task("DM-001")
    revise_run = sched.runs.latest("DM-001")
    assert revise_run.mode == "revise" and revise_run.status == "running"
    assert statuses(sched)["DM-001"] == "running"
    return task, revise_run


def test_a_review_run_never_sends_a_running_task_back_to_ready(sched, fake_github, monkeypatch):
    """CG-177: a revise round is in flight and a review run is dispatched for the same task
    (as the poll re-reviewing a fresh push once did). Reap must reap the task's own worker run
    — the newer review record must never be read as the task's active run and drive it back to
    `ready` with its PR still open."""
    task, revise_run = _revise_in_flight(sched, monkeypatch)

    # A review is dispatched on top of the in-flight revise (the CG-177 incident). It is the
    # newest run, so the naive `latest()` would hand reap the review record, not the revise.
    review_run = sched.dispatch_review(task)
    assert review_run.mode == "review"
    assert sched.runs.latest("DM-001").run_id == review_run.run_id

    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "running"  # the revise is still in flight, not reaped
    assert not any("ready" in tr for tr in rep.transitions)
    assert "no active run found" not in sched.store.task("DM-001").body

    # When the revise finishes it is reaped exactly as usual.
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    sched.runner_for(task).wake(revise_run)
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    reaped = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == revise_run.run_id)
    assert reaped.status == "done"


def test_review_dispatch_is_deferred_while_a_worker_run_is_in_flight(sched, fake_github, monkeypatch):
    """CG-177: while a task has a worker-mode run in flight, a review dispatch is deferred to
    `pending_reviews` (and logged once) instead of starting a review run that could be mistaken
    for the task's own run. The deferred round drains once the worker finishes."""
    logs: list[str] = []
    sched.log = logs.append
    task, revise_run = _revise_in_flight(sched, monkeypatch)

    rep = TickReport()
    item = {"kind": "review", "count_round": True}
    sched._dispatch_or_defer_reviews(task, [item], rep)
    sched._dispatch_or_defer_reviews(task, [item], rep)  # a second attempt while still in flight

    st = sched.state.get("DM-001")
    assert not any(r.mode == "review" for r in sched.runs.runs_for("DM-001"))  # nothing dispatched
    assert len(st.get("pending_reviews") or []) == 2  # both deferred, not lost
    assert sum(1 for m in logs if "review deferred while a worker run is in flight" in m) == 1  # logged once
    assert statuses(sched)["DM-001"] == "running"
    sched.state.save()  # a real deferral happens inside a tick, which persists state at its end

    # The worker finishes; the deferred reviews drain now that no worker run is in flight.
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    sched.runner_for(task).wake(revise_run)
    sched.tick()
    assert next(r for r in sched.runs.runs_for("DM-001") if r.run_id == revise_run.run_id).status == "done"
    assert any(r.mode == "review" for r in sched.runs.runs_for("DM-001"))
    assert not sched.state.get("DM-001").get("pending_reviews")
