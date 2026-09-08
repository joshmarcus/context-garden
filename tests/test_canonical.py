from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from garden import gitops
from garden.canonical import (
    CanonicalCheckoutError,
    claim,
    configured_root,
    lease_path,
    preflight,
    reconcile,
)
from garden.runner.ssh import SSHRunner
from garden.scheduler import Scheduler
from garden.store import Store


def _enable(garden: Path, root: Path, **checkout: object) -> Store:
    data = yaml.safe_load((garden / "garden.yaml").read_text())
    data["products"]["demo"]["checkout"] = {"strategy": "in_place", "root": str(root), **checkout}
    (garden / "garden.yaml").write_text(yaml.safe_dump(data))
    return Store(garden)


def test_canonical_local_run_reconciles_every_warm_use(garden, fake_github, tmp_path):
    repo = (garden / "../repo").resolve()
    tally = tmp_path / "reconciled"
    store = _enable(garden, repo, reconcile_command=f"echo run >> {tally}")
    scheduler = Scheduler(store, github=fake_github)
    task = scheduler.store.task("DM-001")

    first = scheduler.dispatch(task)
    assert first.worktree == str(repo)
    assert tally.read_text() == "run\n"
    # Simulate terminal collection; a stale durable lease is deliberately reclaimable on
    # the next use, while the accepted task commit and branch remain in the checkout.
    first.status = "done"
    first.save()
    second = scheduler.dispatch(task, mode="resume")
    assert second.worktree == str(repo)
    assert tally.read_text() == "run\nrun\n"


def test_canonical_claim_survives_restart_and_refuses_competitor(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    claim(root, "run-a", set())
    with pytest.raises(CanonicalCheckoutError, match="active run run-a"):
        claim(root, "run-b", {"run-a"})
    assert json.loads(lease_path(root).read_text())["run_id"] == "run-a"


def test_interrupted_reconciliation_is_bounded_and_stale_lease_recovers(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    claim(root, "interrupted", set())
    with pytest.raises(CanonicalCheckoutError, match="timed out"):
        reconcile(root, {"reconcile_command": "sleep 2", "reconcile_timeout_seconds": 1}, {}, tmp_path / "log")
    claim(root, "replacement", set())
    assert json.loads(lease_path(root).read_text())["run_id"] == "replacement"


def test_canonical_dirty_and_branch_drift_are_non_destructive(garden):
    repo = (garden / "../repo").resolve()
    (repo / "mine.txt").write_text("keep me")
    with pytest.raises(CanonicalCheckoutError, match="uncommitted work"):
        preflight(repo, "garden/dm-001-first-task", "main")
    assert (repo / "mine.txt").read_text() == "keep me"
    gitops.git("add", "mine.txt", cwd=repo)
    gitops.git("commit", "-m", "mine", cwd=repo)
    gitops.git("checkout", "-b", "operator/topic", cwd=repo)
    before = gitops.head_sha(repo)
    with pytest.raises(CanonicalCheckoutError, match="branch drift"):
        preflight(repo, "garden/dm-001-first-task", "main")
    assert gitops.head_sha(repo) == before


def test_canonical_root_rejects_symlink_and_controller(garden, tmp_path):
    repo = (garden / "../repo").resolve()
    alias = tmp_path / "alias"
    alias.symlink_to(repo)
    with pytest.raises(CanonicalCheckoutError, match="symlinks"):
        configured_root({"strategy": "in_place", "root": str(alias)}, garden)
    with pytest.raises(CanonicalCheckoutError, match="controller"):
        configured_root({"strategy": "in_place", "root": str(garden)}, garden)


def _remote_clone(garden: Path) -> Path:
    data = yaml.safe_load((garden / "garden.yaml").read_text())
    root = Path(data["ssh"]["hosts"][0]["repos"]["demo"])
    subprocess.run(["git", "clone", "-q", "-b", "main",
                    str((garden / "../remote.git").resolve()), str(root)], check=True)
    return root


def _wait_run(run) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (run.path / "exit_code").exists():
            return int((run.path / "exit_code").read_text())
        time.sleep(0.05)
    raise AssertionError("wrapped SSH run did not finish")


def test_ssh_in_place_executes_reconciliation_and_recovers_named_stale_lease(garden, tmp_path):
    remote = _remote_clone(garden)
    tally = tmp_path / "remote-reconciled"
    store = _enable(garden, (garden / "../repo").resolve(),
                    reconcile_command=f"echo run >> {tally}")
    scheduler = Scheduler(store)
    task = scheduler.store.task("DM-001")
    runner = scheduler.runner_for(task, "ssh")
    runner.config["timeout_minutes"] = 0
    assert isinstance(runner, SSHRunner)
    lease = remote / ".git" / "garden-canonical-lease"
    lease.mkdir()
    (lease / "run-id").write_text("killed-run\n")
    run = scheduler.runs.new_run(task.id, "ssh")
    run.host, run.branch, run.base = "boxA", task.default_branch(), "main"
    runner.start(run, tmp_path, "brief")
    assert _wait_run(run) == 0
    assert tally.read_text() == "run\n"
    assert not lease.exists()


def test_ssh_in_place_refuses_reconciliation_without_working_timeout(garden, tmp_path, monkeypatch):
    _remote_clone(garden)
    store = _enable(garden, (garden / "../repo").resolve(), reconcile_command="true")
    scheduler = Scheduler(store)
    task = scheduler.store.task("DM-001")
    runner = scheduler.runner_for(task, "ssh")
    runner.config["timeout_minutes"] = 0
    run = scheduler.runs.new_run(task.id, "ssh")
    run.host, run.branch, run.base = "boxA", task.default_branch(), "main"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    timeout = fake_bin / "timeout"
    timeout.write_text("#!/bin/sh\nexit 1\n")
    timeout.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    runner.start(run, tmp_path, "brief")
    assert _wait_run(run) == 4
    assert "requires working timeout(1)" in (run.path / "stderr.log").read_text()


@pytest.mark.parametrize("consumer", ["review", "persona", "rebase"])
@pytest.mark.parametrize("unsafe", ["dirty", "drift", "competing"])
def test_every_canonical_consumer_claims_before_git_operations(garden, fake_github, consumer, unsafe):
    repo = (garden / "../repo").resolve()
    scheduler = Scheduler(_enable(garden, repo), github=fake_github)
    task = scheduler.store.task("DM-001")
    task.branch = task.default_branch()
    scheduler.store.save(task)
    if unsafe == "dirty":
        (repo / "operator.txt").write_text("preserve\n")
        expected = "uncommitted work"
    elif unsafe == "drift":
        gitops.git("checkout", "-b", "operator/topic", cwd=repo)
        expected = "branch drift"
    else:
        active = scheduler.runs.new_run("DM-002", "local", mode="check")
        active.worktree = str(repo)
        active.save()
        expected = "leased by active run"
    with pytest.raises(CanonicalCheckoutError, match=expected):
        if consumer == "review":
            scheduler.dispatch_review(task)
        elif consumer == "persona":
            scheduler.dispatch_persona_pr(task, "security")
        else:
            scheduler._rebase_and_record(task, "main")
    if unsafe == "dirty":
        assert (repo / "operator.txt").read_text() == "preserve\n"


def test_default_checkout_still_uses_linked_worktree(garden, fake_github):
    scheduler = Scheduler(Store(garden), github=fake_github)
    task = scheduler.store.task("DM-001")
    run = scheduler.dispatch(task)
    assert Path(run.worktree) == scheduler.cfg.worktree_path(task.id)
    assert Path(run.worktree) != scheduler.repo_for(task)
