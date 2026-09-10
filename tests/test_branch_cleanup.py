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
    assert rows[first.branch].classification == "removable"
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
