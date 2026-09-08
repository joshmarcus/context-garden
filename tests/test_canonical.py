from __future__ import annotations

import json
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


def test_ssh_in_place_script_uses_remote_lease_and_never_hard_resets(garden, tmp_path):
    store = _enable(garden, (garden / "../repo").resolve(), reconcile_command="true")
    scheduler = Scheduler(store)
    task = scheduler.store.task("DM-001")
    runner = scheduler.runner_for(task, "ssh")
    assert isinstance(runner, SSHRunner)
    run = scheduler.runs.new_run(task.id, "ssh")
    run.host, run.branch, run.base = "boxA", task.default_branch(), "main"
    # Render only: the fake transport is exercised elsewhere; this asserts that wrapped SSH
    # receives the same canonical contract without touching the fixture clone.
    import garden.runner.ssh as ssh_mod

    class Process:
        pid = 123

    original = ssh_mod.subprocess.Popen
    try:
        ssh_mod.subprocess.Popen = lambda *args, **kwargs: Process()
        runner.start(run, tmp_path, "brief")
    finally:
        ssh_mod.subprocess.Popen = original
    script = (run.path / "remote.sh").read_text()
    assert "mkdir \"$GARDEN_CANONICAL_LEASE\"" in script
    assert "canonical checkout has uncommitted work" in script
    assert "GARDEN_RECONCILE_CMD=true" in script
    assert 'if [ "$GARDEN_CHECKOUT_STRATEGY" != in_place ] && git show-ref' in script


def test_default_checkout_still_uses_linked_worktree(garden, fake_github):
    scheduler = Scheduler(Store(garden), github=fake_github)
    task = scheduler.store.task("DM-001")
    run = scheduler.dispatch(task)
    assert Path(run.worktree) == scheduler.cfg.worktree_path(task.id)
    assert Path(run.worktree) != scheduler.repo_for(task)
