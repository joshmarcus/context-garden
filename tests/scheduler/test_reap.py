"""Reap: what a finished worker run turns into (retry, fail, push, pre-PR checks, the base probe, manual runs)."""

import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from garden import gitops
from garden.model import Status
from garden.preflight import PREFLIGHT_ITEMS
from garden.review import review_brief
from garden.runner.manual import ManualRunner
from garden.scheduler.report import TickReport
from garden.scheduler.snapshot import write_snapshot
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


def test_reap_persists_criteria_amendment_once_when_finalize_is_repeated(sched, fake_github, monkeypatch):
    task = sched.store.task("DM-001")
    task.body += "\n## Acceptance criteria\n\n- [ ] The original outcome works.\n"
    sched.store.save(task)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "criteria-amend")

    sched.tick()
    run = sched.runs.latest("DM-001")
    real_push = gitops.push
    monkeypatch.setattr(gitops, "push", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after amendment")))
    sched.tick()
    monkeypatch.setattr(gitops, "push", real_push)
    sched.tick()

    sched.store.invalidate()
    amended = sched.store.task("DM-001")
    assert "- [ ] The corrected outcome works." in amended.body
    assert "acceptance criterion 1 amended: The original outcome was false." in amended.body
    assert amended.extra["criteria_amended"][0]["text"] == "The corrected outcome works."
    assert len(amended.extra["criteria_amended"]) == 1
    events = [event for event in sched.events.read(task_id="DM-001", kinds=["criteria_amended"])
              if event.get("run") == run.run_id]
    assert len(events) == 1


def test_worker_amendment_round_trip_reaches_review_brief(sched, fake_github, monkeypatch):
    task = sched.store.task("DM-001")
    task.body += "\n## Acceptance criteria\n\n- [ ] The original outcome works.\n"
    sched.store.save(task)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "criteria-amend")

    sched.tick()
    sched.tick()

    sched.store.invalidate()
    amended = sched.store.task("DM-001")
    brief = review_brief(sched.store, amended, branch="garden/DM-001-first", base="main",
                         pr_title="Amended criterion", pr_body="", diff="", max_diff_chars=1000)
    assert "The corrected outcome works." in brief
    assert "amended — The original outcome was false." in brief
    assert "Judge each amended line against its stated outcome" in brief


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


def test_reap_preserves_dirty_snapshot_without_adding_it_to_the_pr_or_next_round(sched, fake_github):
    """CG-359: committed work reaches the PR while an unrelated dirty artifact remains
    recoverable by run, and a revise/reap cycle cannot sweep it back into the branch."""
    sched.cfg.data["stack"] = False
    sched.tick()  # fake worker commits worker-output.txt
    task = sched.store.task("DM-001")
    worktree = sched.worktree_for(task)
    snapshot = worktree / "docs" / "design" / "snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text('{"large": "unrelated runtime state"}\n')

    sched.tick()  # reap and open the PR
    assert statuses(sched)["DM-001"] == "in_review"
    assert not snapshot.exists()
    artifacts = sched.runs.latest("DM-001").recovery_artifacts
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["reason"] == "reap" and artifact["run"] == sched.runs.latest("DM-001").run_id
    assert "docs/design/snapshot.json" in "\n".join(artifact["files"])
    assert "git stash apply" in artifact["restore"]
    assert "docs/design/snapshot.json" not in gitops.git("diff", "--name-only", "main...HEAD", cwd=worktree)

    sched.triage(sched.store.task("DM-001"), changes="please revise")
    sched.dispatch(sched.store.task("DM-001"), mode="revise")
    sched.tick()  # reap the revise run
    assert "docs/design/snapshot.json" not in gitops.git("diff", "--name-only", "main...HEAD", cwd=worktree)
    assert len(sched.runs.latest("DM-001").recovery_artifacts) == 0


def test_design_snapshot_includes_the_real_merge_and_dispatch_queues(sched, tmp_path):
    """CG-318: queue state belongs to task records, never a nonexistent `_queue` entry."""
    sched.state.get("DM-001").update(automerge_candidate=True, merge_head=True,
                                       automerge_ready_at="2026-09-06T12:00:00+00:00")
    output = tmp_path / "worktree"
    task = sched.store.task("DM-001")
    task.title = "Design the queue"
    write_snapshot(sched, task, output)

    queue = json.loads((output / "docs" / "design" / "snapshot.json").read_text())["queue"]
    assert queue["merge"] == [{"task": "DM-001", "candidate": True, "head": True,
                                "ready_at": "2026-09-06T12:00:00+00:00", "blocked": ""}]
    assert queue["dispatch"] == [{"task": "DM-001", "mode": "work", "reason": "priority 1"}]


def test_missing_result_preserves_dirty_new_file_without_discarding_committed_work(sched, fake_github):
    """CG-359: a crashed/missing result keeps both the committed salvage and the separate
    uncommitted recovery artifact."""
    sched.cfg.data["stack"] = False
    sched.tick()
    run = sched.runs.latest("DM-001")
    worktree = Path(run.worktree)
    (worktree / "interrupted.txt").write_text("keep me\n")
    (run.path / "stdout.json").unlink()

    sched.tick()
    assert int(gitops.git("rev-list", "--count", "main..HEAD", cwd=worktree).strip()) >= 1
    completed = next(item for item in sched.runs.runs_for("DM-001") if item.run_id == run.run_id)
    artifact = completed.recovery_artifacts[0]
    assert artifact["reason"] == "reap"
    assert "interrupted.txt" in "\n".join(artifact["files"])
    assert not (worktree / "interrupted.txt").exists()


def test_missing_result_with_a_preflight_contract_enters_a_revise_round(sched):
    """A current brief cannot reach review without the checklist it required."""
    sched.cfg.data["stack"] = False
    sched.tick()
    run = sched.runs.latest("DM-001")
    assert run.env_snapshot["requires_preflight"] is True
    frozen = list(run.env_snapshot["criteria"])
    committed_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=run.worktree,
                                    capture_output=True, text=True, check=True).stdout.strip()
    (run.path / "stdout.json").unlink()

    report = sched.tick()

    assert "DM-001 -> changes_requested (checks)" in report.transitions
    revise = sched.runs.latest("DM-001")
    assert revise.mode == "revise"
    brief = (revise.path / "brief.md").read_text()
    assert "missing items: result block and review pre-flight checklist" in brief
    assert "Criteria frozen for the interrupted dispatch" in brief
    for criterion in frozen:
        assert criterion in brief
    assert subprocess.run(["git", "merge-base", "--is-ancestor", committed_head, "HEAD"], cwd=revise.worktree,
                          check=False).returncode == 0


def test_missing_result_without_a_preflight_contract_uses_legacy_recovery(sched):
    """Saved runs from before the rubric retain missing-result commit salvage."""
    sched.cfg.data["stack"] = False
    sched.tick()
    run = sched.runs.latest("DM-001")
    run.env_snapshot.pop("requires_preflight")
    run.save()
    (run.path / "stdout.json").unlink()

    report = sched.tick()

    assert "DM-001 -> changes_requested (checks)" not in report.transitions
    assert "DM-001 -> in_review" in report.transitions[0]
    assert sched.runs.latest("DM-001").run_id == run.run_id


def test_pre_pr_check_failure_at_cap_needs_human(sched, fake_github):
    """A pre-PR check that fails once the revision cap is reached hands off to a human,
    exactly like the review path — it does not leave the task queued-but-skipped."""
    # A branch-owned failure (passes at the base, where worker-output.txt does not exist, so the
    # CG-131 base probe does not divert it) that still fails once the revision cap is reached.
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": "test ! -f worker-output.txt || { echo check-failed; exit 1; }"}], "ci": []}
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
    round to resolve the mechanical rebase by hand once `gitops.rebase_onto_capture` can't apply
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
    monkeypatch.setattr(gitops, "rebase_onto_capture", lambda worktree, onto, **_kwargs: (False, ["sentinel.txt"], {}))

    for i in range(3):
        # each cycle reaps the running round, runs the pre-PR check and base probe as detached
        # runs (guard fails on the branch and at the moved, still-red base), flags it as a rebase
        # round when `rebase_onto_capture` can't apply, and redispatches the exempt revise despite
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


def test_missing_result_with_commits_is_reaped_and_sent_to_review(sched, fake_github, monkeypatch):
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult")
    sched.tick()
    run = sched.runs.latest("DM-001")
    run.env_snapshot.pop("requires_preflight")
    run.save()
    rep = sched.tick()
    task = sched.store.task("DM-001")
    run = sched.latest_worker_run("DM-001")
    assert task.status == Status.IN_REVIEW
    assert task.pr
    assert run.status == "done"
    assert "result missing; 1 commit reaped from the worktree" in task.body
    assert "worker's last message: 'I did some things but forgot the result line.'" in task.body
    assert any(event["run"] == run.run_id for event in sched.events.read(task_id="DM-001", kinds=["result_missing_reaped"]))
    assert any(item.startswith("DM-001(review)") for item in rep.dispatched)


def test_statusless_result_with_commits_is_reaped(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "statusless")
    sched.tick()
    run = sched.runs.latest("DM-001")
    run.env_snapshot.pop("requires_preflight")
    run.save()
    sched.tick()
    task = sched.store.task("DM-001")
    assert task.status == Status.IN_REVIEW
    assert task.pr
    assert "result missing; 1 commit reaped from the worktree" in task.body
    assert "Finished the change." in task.body


def test_missing_result_without_commits_retries(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult-nocommit")
    sched.tick()
    run = sched.runs.latest("DM-001")
    run.env_snapshot.pop("requires_preflight")
    run.save()
    rep = sched.tick()
    assert "DM-001 -> ready (retry)" in rep.transitions


def test_missing_result_revise_pushes_with_lease_and_keeps_revision_count(sched, fake_github, monkeypatch):
    sched.tick()
    sched.tick()
    task = sched.store.task("DM-001")
    sched.triage(task, changes="please revise")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult")
    sched.dispatch(sched.store.task("DM-001"), mode="revise")
    run = sched.runs.latest("DM-001")
    run.env_snapshot.pop("requires_preflight")
    run.save()
    assert run.start_head
    real_push = gitops.push
    leases: list[str] = []

    def capture_push(*args, **kwargs):
        leases.append(kwargs.get("lease", ""))
        return real_push(*args, **kwargs)

    monkeypatch.setattr(gitops, "push", capture_push)

    sched.tick()

    task = sched.store.task("DM-001")
    assert task.status == Status.IN_REVIEW
    assert sched.state.get("DM-001")["revisions"] == 1
    assert "result missing; 1 commit reaped from the worktree" in task.body
    assert fake_github.created[0]["head"] == task.branch
    assert run.start_head in leases


def test_failed_rebase_retries_then_parks_without_restarting_work(sched, monkeypatch):
    """CG-330: a lost conflict-resolution run belongs to its open PR, not a new work round."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult")
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.test/acme/widget/pull/7"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st.update({"rebase_pending": True, "rebase_base": "main", "rebase_files": ["widget.py"]})
    sched.state.save()
    sched.dispatch(task, mode="rebase")
    branch = task.branch

    rep = sched.tick()
    assert "DM-001(rebase)" in rep.dispatched
    assert "DM-001(work)" not in rep.dispatched
    assert task.attempts == 0
    assert task.pr.endswith("/7")

    rep = sched.tick()
    task = sched.store.task("DM-001")
    stop = sched.state.get(task.id)["needs_human"]
    assert task.status == Status.IN_REVIEW and task.pr.endswith("/7")
    assert task.attempts == 0
    assert task.branch == branch
    assert stop["kind"] == "rebase_failed" and "rebase conflict" in stop["reason"]
    assert not any(item.startswith("DM-001(work)") for item in rep.dispatched)
    assert "rebase run" in task.body and "will retry" in task.body


def test_killed_check_retries_then_parks_without_using_revision_cap(sched):
    """CG-330: no-output checks retry their detached continuation, never a revise run."""
    from garden import gitops

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.test/acme/widget/pull/7"
    sched.store.save(task)
    wt = gitops.prepare_worktree(sched.repo_for(task), sched.worktree_for(task), task.default_branch(), "main")
    specs = [{"name": "unit", "command": (
        "printf 'Traceback (most recent call last):\\n  File \\\"check.py\\\", line 7\\n"
        "RuntimeError: contention\\n' >&2; kill -TERM $$"
    )}]
    cont = sched._pre_pr_cont(None, wt, task.default_branch(), "main", "")
    sched._dispatch_check_run(task, worktree=wt, branch=task.default_branch(), base="main", specs=specs,
                              stage="merge_rebase", cont=cont, rep=TickReport())

    rep = sched.tick(dispatch=False)
    assert "DM-001(check:merge_rebase)" in rep.dispatched
    assert sched.state.get(task.id).get("revisions", 0) == 0
    assert not sched.state.get(task.id).get("pending_feedback")
    assert "SIGTERM" in sched.store.task("DM-001").body

    sched.tick(dispatch=False)
    task = sched.store.task("DM-001")
    stop = sched.state.get(task.id)["needs_human"]
    assert task.status == Status.IN_REVIEW
    assert stop["kind"] == "check_did_not_run" and "check did not run" in stop["reason"]
    assert "SIGTERM" in stop["reason"]
    assert "Traceback (most recent call last):" in stop["reason"]
    assert "RuntimeError: contention" in stop["reason"]
    assert sched.state.get(task.id).get("revisions", 0) == 0


def test_empty_collected_check_parks_once_without_fabricating_success(sched):
    """A collected check with no results produces one durable stop, not a replay per tick."""
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.result = {"checks": []}
    run.save()
    sched.state.get(task.id)["check_run"] = {
        "run_id": run.run_id, "stage": "base_probe", "cont": {}, "specs": [],
        "retries": 0, "collected": True,
    }
    sched.state.save()

    first = TickReport()
    assert sched.reap_check(task, first) is True
    stop = sched.state.get(task.id)["needs_human"]
    assert stop["kind"] == "check_did_not_run"
    assert not sched.state.get(task.id).get("check_run")
    assert not any(event.get("status") in ("pass", "passed")
                   for event in sched.events.read(task_id=task.id, kinds=["check"]))

    event_count = len(sched.events.read(task_id=task.id, kinds=["needs_human"]))
    for _ in range(3):
        assert sched.reap_check(sched.store.task(task.id), TickReport()) is False
    assert len(sched.events.read(task_id=task.id, kinds=["needs_human"])) == event_count == 1


def test_auxiliary_reapers_do_not_dispatch_work_directly():
    """CG-330: only the work/revise reap path may put a task back on the work queue."""
    import inspect

    from garden.scheduler.checkruns import CheckRunMixin
    from garden.scheduler.edits import EditsMixin
    from garden.scheduler.reap import ReapMixin

    assert "Status.READY" not in inspect.getsource(ReapMixin._retry_or_park_rebase)
    assert "dispatch(task, mode=\"work\")" not in inspect.getsource(CheckRunMixin._retry_or_park_check)
    assert "dispatch(task, mode=\"work\")" not in inspect.getsource(EditsMixin.reap_edit)


def test_idle_worker_is_stopped_before_timeout(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    sched.cfg.data["idle_kill_minutes"] = 5
    sched.cfg.data["timeout_minutes"] = 0
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


def test_check_admission_wait_does_not_inherit_old_checkout_idle_time(sched, tmp_path):
    """A queued heavy check remains live, then resumes the ordinary idle policy on release."""
    sched.cfg.data["idle_kill_minutes"] = 5
    sched.cfg.data["timeout_minutes"] = 0
    sched.cfg.data["resources"]["admission_wait_minutes"] = 30
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    checkout = tmp_path / "old-checkout"
    checkout.mkdir()
    (checkout / "unchanged.py").write_text("# pre-existing checkout\n")
    run.worktree = str(checkout)
    run.pid = os.getpid()  # the in-process liveness sentinel, with no exit_code
    run.save()
    old = datetime.now(UTC).timestamp() - 22 * 60
    for path in Path(run.worktree).rglob("*"):
        try:
            os.utime(path, (old, old))
        except OSError:
            pass
    (run.path / "execution.json").write_text(json.dumps({
        "state": "waiting", "reason": "heavy-test budget full (limit 1)", "limit": 1,
    }))

    runner = sched.runner_for(task, run.runner)
    assert not sched._finished_or_timed_out(run, runner)
    assert run.status == "running"  # admission waiting neither retries nor creates another check

    # Capacity becomes available.  The supervisor no longer reports waiting, and a genuinely
    # silent run is still stopped by the normal idle safeguard.
    (run.path / "execution.json").write_text(json.dumps({"state": "running", "limit": 1}))
    make_idle(run, 8)
    assert sched._finished_or_timed_out(run, runner)
    assert run.status == "timeout"
    assert "idle 8 min" in run.error


def test_check_admission_wait_has_a_bounded_truthful_timeout(sched):
    sched.cfg.data["timeout_minutes"] = 0
    sched.cfg.data["resources"]["admission_wait_minutes"] = 30
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.pid = os.getpid()
    run.started_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    run.save()
    (run.path / "execution.json").write_text(json.dumps({
        "state": "waiting", "reason": "heavy-test budget full (limit 1)", "limit": 1,
        "waiting_since": (datetime.now(UTC) - timedelta(minutes=31)).isoformat(),
    }))

    assert sched._finished_or_timed_out(run, sched.runner_for(task, run.runner))
    assert run.status == "timeout"
    assert "admission wait 31 min (heavy-test budget full (limit 1))" == run.error


def test_late_admission_wait_gets_its_full_window(sched):
    """A long-running check is charged only from its published admission wait."""
    sched.cfg.data["timeout_minutes"] = 0
    sched.cfg.data["resources"]["admission_wait_minutes"] = 30
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.pid = os.getpid()
    run.started_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    run.save()
    (run.path / "execution.json").write_text(json.dumps({
        "state": "waiting", "reason": "heavy-test budget full (limit 1)", "limit": 1,
        "waiting_since": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }))

    assert not sched._finished_or_timed_out(run, sched.runner_for(task, run.runner))
    assert run.status == "running"


def test_real_local_check_hard_timeout_preserves_exact_recovery_cause(sched, tmp_path, monkeypatch):
    """A supervisor deadline is a distinct retry cause, not a generic empty result."""
    from garden.runner.local import LocalRunner

    runtime = tmp_path / "timeout-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(
        "garden.runner.local.bounded_validation_timeout_seconds", lambda _configured: 0.1,
    )
    checkout = tmp_path / "timeout-checkout"
    checkout.mkdir()
    task = sched.store.task("DM-001")
    specs = [{"name": "silent", "command": "sleep 5"}]
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.worktree, run.branch, run.base = str(checkout), "main", "main"
    LocalRunner(sched.cfg.data).start_checks(run, checkout, {
        "specs": specs, "cwd": str(checkout), "setup": {},
        "config": sched.cfg.data, "timeout": 30,
    })
    sched.state.get(task.id)["check_run"] = {
        "run_id": run.run_id, "stage": "ci", "cont": {}, "specs": specs,
        "retries": 1, "backend": "local", "provenance": "timeout fixture",
    }
    sched.state.save()
    try:
        # The nested supervisor must first acquire and release its own validation slot.
        # Keep the assertion bounded, but allow a saturated serial suite enough time to
        # observe the 0.1-second execution deadline and reap the process group.
        deadline = time.monotonic() + 10
        while not run.process_finished() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert run.process_finished() and run.read_exit_code() == 124
        assert sched.reap_check(task, TickReport())
    finally:
        if not run.process_finished():
            run.stop(timeout=2)

    saved = sched._run_by_id(task, run.run_id)
    assert saved is not None and saved.status == "done"
    assert saved.result["checks"] == [{
        "name": "checks", "status": "error", "summary": "check execution timed out",
        "details": "validation execution exceeded 0.1 seconds",
    }]
    recovery = sched.state.get(task.id)["recovery_check"]
    assert recovery["cause"] == (
        "check execution timed out\n\nvalidation execution exceeded 0.1 seconds"
    )


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


def test_running_card_omits_a_dead_run(sched):
    from garden.inbox import running_now

    run = sched.runs.new_run("DM-001", "local", mode="work")
    run.pid = 999999
    run.save()
    assert all(r["task"] != "DM-001" for r in running_now(sched.store))


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
    sched.finish_manual(sched.store.task("DM-001"), {
        "status": "done", "summary": "by hand", "pr_title": "manual PR",
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked by hand"} for item in PREFLIGHT_ITEMS],
    })
    assert statuses(sched)["DM-001"] == "in_review"
    assert fake_github.created[-1]["title"] == "manual PR"


def test_manual_finish_without_worktree_still_dispatches_review(sched, fake_github, tmp_path):
    """CG-158: `garden take` without --worktree is the common manual path — the human works
    in their own clone and finishes with just a PR URL. finalize() has no garden-managed
    worktree to push from or run pre-PR checks against, but the automated reviewer builds
    its own worktree from the pushed branch, so it must still see this PR."""
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    t = sched.store.task("DM-001")
    run = sched.dispatch(t, runner=ManualRunner({}), worktree=False)
    assert not run.worktree
    branch = t.default_branch()

    # the human's own clone, pushed straight to the product's origin
    clone = tmp_path / "manual-clone"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", branch], check=True)
    with open(clone / "hello.txt", "w") as f:
        f.write("hi\n")
    git("add", "-A", cwd=clone)
    git("commit", "-q", "-m", "manual work", cwd=clone)
    git("push", "-q", "-u", "origin", branch, cwd=clone)

    # the human already opened the PR on GitHub themselves
    pr = fake_github.create_pr("test/demo", branch, "main", "manual PR", "body")

    sched.finish_manual(sched.store.task("DM-001"), {
        "status": "done", "summary": "by hand", "pr": pr.url,
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked by hand"} for item in PREFLIGHT_ITEMS],
    })
    assert statuses(sched)["DM-001"] == "in_review"
    st = sched.state.get("DM-001")
    assert st.get("review_run"), "the automated reviewer must still be dispatched"


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
    ManualRunner.finish(run, {
        "status": "done", "summary": "by hand", "pr_title": "manual PR",
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked by hand"} for item in PREFLIGHT_ITEMS],
    })
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
    assert sched.state.get("DM-001").get("last_review_run") == review_run.run_id
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
    for the task's own run. The deferred round drains once the worker finishes. CG-203: a
    second deferral attempt for the same round is deduplicated, not appended again."""
    logs: list[str] = []
    sched.log = logs.append
    task, revise_run = _revise_in_flight(sched, monkeypatch)

    rep = TickReport()
    item = {"kind": "review", "count_round": True}
    sched._dispatch_or_defer_reviews(task, [item], rep)
    sched._dispatch_or_defer_reviews(task, [item], rep)  # a second attempt while still in flight

    st = sched.state.get("DM-001")
    assert not any(r.mode == "review" for r in sched.runs.runs_for("DM-001"))  # nothing dispatched
    assert len(st.get("pending_reviews") or []) == 1  # deduplicated, not doubled
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


def test_tick_report_carries_duration_and_slowest_step(garden, fake_github):
    """CG-182: every pass records its own duration and the slowest step, and warns when it
    runs over the tick.warn_seconds budget, naming the slow step."""
    from garden.scheduler import Scheduler
    from garden.store import Store

    logs: list[str] = []
    sched = Scheduler(Store(garden), github=fake_github, log=logs.append)
    sched.cfg.data["tick"] = {"warn_seconds": 0.001}  # any real pass exceeds this tiny budget
    rep = sched.tick()
    assert rep.duration_s > 0
    assert rep.slowest_step in {"reap", "poll", "base_reprobe", "merge_queue", "dispatch", "audit"}
    assert "took" in rep.timing() and "slowest" in rep.timing()
    assert rep.timing() in rep.summary()
    assert any("exceeded" in m and "budget" in m for m in logs)
