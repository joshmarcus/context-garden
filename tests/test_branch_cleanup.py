from __future__ import annotations

import subprocess

from garden import gitops
from garden.branch_cleanup import BranchDisposition, delete_disposition
from garden.model import Status


def _git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _record_branch(sched, task_id: str, branch: str, *, active: bool = False):
    run = sched.runs.new_run(task_id, "local")
    run.branch = branch
    run.base = "main"
    run.status = "running" if active else "done"
    run.save()
    return run


def _make_branch(repo, branch: str, *, unique: bool = False):
    head = _git(repo, "rev-parse", "main")
    if unique:
        tree = _git(repo, "rev-parse", "main^{tree}")
        head = _git(repo, "commit-tree", tree, "-p", head, "-m", "unique abandoned work")
    subprocess.run(["git", "branch", branch, head], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", f"{branch}:{branch}"], cwd=repo, check=True)
    return head


def test_inventory_classifies_complete_active_and_unique_cancelled_work(sched):
    tasks = sched.store.tasks()
    first, second = tasks["DM-001"], tasks["DM-002"]
    first.status = Status.DONE
    first.branch = "garden/complete"
    sched.store.save(first)
    second.branch = "garden/active"
    sched.store.save(second)
    repo = sched.repo_for(first)
    _make_branch(repo, first.branch)
    _make_branch(repo, second.branch)
    _record_branch(sched, first.id, first.branch)
    _record_branch(sched, second.id, second.branch, active=True)

    rows = {row.branch: row for row in sched.branch_cleanup_inventory()}
    assert rows[first.branch].classification == "removable", rows[first.branch]
    assert rows[second.branch].classification == "needed"
    assert "active or queued run" in rows[second.branch].reason

    second.status = Status.CANCELLED
    second.branch = "garden/cancelled-unique"
    sched.store.save(second)
    _make_branch(repo, second.branch, unique=True)
    run = _record_branch(sched, second.id, second.branch)
    run.status = "cancelled"
    run.save()
    row = next(row for row in sched.branch_cleanup_inventory() if row.branch == second.branch)
    assert row.classification == "uncertain"
    assert "not proven preserved" in row.reason


def test_inventory_preserves_open_pr_stack_and_default_branch_claims(sched):
    task = sched.store.tasks()["DM-001"]
    task.status = Status.DONE
    repo = sched.repo_for(task)
    cases = {
        "garden/open": "an open PR uses the branch",
        "garden/stack-base": "stack base",
        "main": "protected branch",
    }
    for branch in cases:
        task.branch = branch
        sched.store.save(task)
        if branch != "main":
            _make_branch(repo, branch)
        _record_branch(sched, task.id, branch)
    sched.state.get("__open_prs__")["demo"] = {
        "prs": [{"head": "garden/open", "state": "OPEN"}],
    }
    sched.state.get("DM-002")["pr_base"] = "garden/stack-base"

    rows = {row.branch: row for row in sched.branch_cleanup_inventory()}
    for branch, reason in cases.items():
        assert rows[branch].classification == "needed"
        assert reason in rows[branch].reason


def test_inventory_preserves_external_and_recovery_owned_branches(sched):
    task = sched.store.tasks()["DM-001"]
    task.status = Status.DONE
    repo = sched.repo_for(task)
    for branch in ("garden/external", "garden/recovery"):
        _make_branch(repo, branch)
        _record_branch(sched, task.id, branch)
    external = _record_branch(sched, task.id, "garden/external")
    external.completion_mode = "external"
    external.save()
    recovery = _record_branch(sched, task.id, "garden/recovery")
    sched.state.get(task.id)["recovery_run"] = recovery.run_id

    rows = {row.branch: row for row in sched.branch_cleanup_inventory()}
    assert rows["garden/external"].classification == "needed"
    assert "external ownership" in rows["garden/external"].reason
    assert rows["garden/recovery"].classification == "needed"
    assert "recovery state" in rows["garden/recovery"].reason


def test_superseded_attempt_branch_is_removable_after_task_completes_when_preserved(sched):
    task = sched.store.tasks()["DM-001"]
    task.status = Status.DONE
    task.branch = "garden/current-attempt"
    sched.store.save(task)
    repo = sched.repo_for(task)
    _make_branch(repo, "garden/superseded-attempt")
    run = _record_branch(sched, task.id, "garden/superseded-attempt")
    run.status = "superseded"
    run.save()

    row = next(row for row in sched.branch_cleanup_inventory()
               if row.branch == "garden/superseded-attempt")
    assert row.classification == "removable", row


def test_unique_superseded_attempt_is_uncertain_after_task_completes(sched):
    task = sched.store.tasks()["DM-001"]
    task.status = Status.DONE
    task.branch = "garden/current-attempt"
    sched.store.save(task)
    repo = sched.repo_for(task)
    _make_branch(repo, "garden/superseded-attempt", unique=True)
    run = _record_branch(sched, task.id, "garden/superseded-attempt")
    run.status = "superseded"
    run.save()

    row = next(row for row in sched.branch_cleanup_inventory()
               if row.branch == "garden/superseded-attempt")

    assert row.classification == "uncertain"
    assert "not proven preserved" in row.reason


def test_merged_pr_record_preserves_exact_current_branch_head(sched):
    task = sched.store.tasks()["DM-001"]
    task.status = Status.DONE
    task.branch = "garden/squash-merged"
    sched.store.save(task)
    repo = sched.repo_for(task)
    head = _make_branch(repo, task.branch, unique=True)
    _record_branch(sched, task.id, task.branch)
    sched.state.get(task.id).update({
        "pr_state": "MERGED",
        "head_sha": _git(repo, "rev-parse", "main"),
    })

    row = next(row for row in sched.branch_cleanup_inventory() if row.branch == task.branch)
    assert row.classification == "uncertain"

    sched.state.get(task.id)["head_sha"] = head

    row = next(row for row in sched.branch_cleanup_inventory() if row.branch == task.branch)

    assert row.classification == "removable", row
    assert "merged PR record" in row.reason


def test_guarded_delete_removes_remote_and_local_and_is_idempotent(sched):
    task = sched.store.tasks()["DM-001"]
    repo = sched.repo_for(task)
    head = _make_branch(repo, "garden/remove-me")
    item = BranchDisposition("demo", "garden/remove-me", "removable", "complete", head, head, (task.id,))

    result = delete_disposition(item, repo)
    assert result["outcome"] == "removed"
    assert set(result["removed"]) == {"local", "remote"}
    assert delete_disposition(item, repo)["outcome"] == "absent"


def test_guarded_delete_rejects_a_concurrent_remote_push(sched):
    task = sched.store.tasks()["DM-001"]
    repo = sched.repo_for(task)
    old = _make_branch(repo, "garden/raced")
    tree = _git(repo, "rev-parse", "main^{tree}")
    new = _git(repo, "commit-tree", tree, "-p", old, "-m", "concurrent")
    subprocess.run(["git", "push", "-q", "--force", "origin", f"{new}:refs/heads/garden/raced"], cwd=repo, check=True)
    item = BranchDisposition("demo", "garden/raced", "removable", "complete", old, old, (task.id,))

    result = delete_disposition(item, repo)
    assert result["outcome"] == "failed"
    assert result["removed"] == []
    assert "lease rejected" in result["errors"][0]
    assert gitops.local_head(repo, "garden/raced") == old
    assert _git(repo, "ls-remote", "--heads", "origin", "refs/heads/garden/raced").startswith(new)


def test_sweep_retains_candidate_claimed_by_unrelated_pr_after_inventory(sched, monkeypatch):
    task = sched.store.tasks()["DM-001"]
    task.status = Status.DONE
    task.branch = "garden/new-pr-claim"
    sched.store.save(task)
    repo = sched.repo_for(task)
    head = _make_branch(repo, task.branch)
    _record_branch(sched, task.id, task.branch)
    inventory = sched.branch_cleanup_inventory()
    assert next(row for row in inventory if row.branch == task.branch).classification == "removable"

    pr = sched.github.create_pr("example/demo", task.branch, "main", "Claim", "body")
    pr.author = "unrelated-collaborator"
    monkeypatch.setattr(sched, "branch_cleanup_inventory", lambda **_kwargs: inventory)

    result = sched.sweep_worker_branches(type("Report", (), {"transitions": []})(), limit=20)

    assert result[0]["outcome"] == "retained"
    assert result[0]["reason"] == f"PR #{pr.number} is open"
    assert gitops.local_head(repo, task.branch) == head
    assert _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{task.branch}").startswith(head)

    sched.github.close_pr("example/demo", pr.number)
    dependent = sched.github.create_pr(
        "example/demo", "garden/new-dependent", task.branch, "Dependent", "body",
    )
    dependent.author = "unrelated-collaborator"
    result = sched.sweep_worker_branches(type("Report", (), {"transitions": []})(), limit=20)

    assert result[0]["outcome"] == "retained"
    assert result[0]["reason"] == f"PR #{dependent.number} depends on the branch"


def test_partial_failures_are_reported_without_hiding_success(sched, monkeypatch):
    task = sched.store.tasks()["DM-001"]
    repo = sched.repo_for(task)
    head = _make_branch(repo, "garden/partial")
    item = BranchDisposition("demo", "garden/partial", "removable", "complete", head, head, (task.id,))
    monkeypatch.setattr(gitops, "delete_local_branch", lambda *args, **kwargs: (_ for _ in ()).throw(gitops.GitError("locked")))

    result = delete_disposition(item, repo)
    assert result["outcome"] == "partial"
    assert result["removed"] == ["remote"]
    assert result["errors"] == ["local: locked"]


def test_large_history_uses_one_remote_snapshot_per_repository_per_tick(sched, monkeypatch):
    task = sched.store.tasks()["DM-001"]
    for number in range(600):
        _record_branch(sched, task.id, f"garden/historical-{number:03d}")
    calls = 0
    original = gitops.git

    def counted(*args, **kwargs):
        nonlocal calls
        if args[:2] == ("ls-remote", "--heads"):
            calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(gitops, "git", counted)
    with gitops.tick_read_cache():
        first = sched.branch_cleanup_inventory()
        second = sched.branch_cleanup_inventory()

    assert len(first) == len(second) == 600
    assert calls == 1


def test_failed_remote_snapshot_preserves_every_historical_branch(sched, monkeypatch):
    task = sched.store.tasks()["DM-001"]
    for number in range(20):
        _record_branch(sched, task.id, f"garden/historical-{number:03d}")
    monkeypatch.setattr(
        gitops, "remote_branch_heads",
        lambda *args, **kwargs: (_ for _ in ()).throw(gitops.GitError("timed out")),
    )

    rows = sched.branch_cleanup_inventory()

    assert len(rows) == 20
    assert all(row.classification == "uncertain" for row in rows)
    assert all("timed out" in row.reason for row in rows)


def test_zero_cleanup_limits_skip_inventory_before_automatic_work(sched, monkeypatch):
    sched.cfg.data["branches"] = {"cleanup_limit": 0}
    sched.cfg.data["storage_cleanup"] = {"limit": 0}
    monkeypatch.setattr(
        sched, "branch_cleanup_inventory",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("inventory must not run")),
    )
    report = type("Report", (), {"transitions": []})()

    assert sched.sweep_worker_branches(report) == []
    assert sched.sweep_storage(report)["status"] == "disabled"
    sched._sweep_terminal_worktrees(report)


def test_sweep_bounds_authoritative_remote_delete(sched, monkeypatch):
    task = sched.store.tasks()["DM-001"]
    item = BranchDisposition(
        task.product, "garden/remove-me", "removable", "preserved",
        remote_head="a" * 40, task_ids=(task.id,),
    )
    observed: list[float | None] = []
    monkeypatch.setattr(sched, "branch_cleanup_inventory", lambda **_kwargs: [item])
    monkeypatch.setattr(sched, "_branch_delete_recheck", lambda _item: "")
    monkeypatch.setattr(
        gitops, "delete_remote_branch",
        lambda *_args, timeout=None, **_kwargs: observed.append(timeout) or False,
    )

    result = sched.sweep_worker_branches(type("Report", (), {"transitions": []})())

    assert result[0]["outcome"] == "absent"
    assert observed == [5.0]
