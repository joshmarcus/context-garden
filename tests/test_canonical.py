from __future__ import annotations

import json
import os
import shlex
import shutil
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
from garden.model import Status
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


def test_local_preparing_owner_is_durable_before_competing_claim(garden):
    repo = (garden / "../repo").resolve()
    first_scheduler = Scheduler(_enable(garden, repo))
    task = first_scheduler.store.task("DM-001")
    first = first_scheduler.runs.new_run(task.id, "local", initial_status="preparing")
    first_scheduler.prepare_canonical_run(
        task, first, first_scheduler.runner_for(task, "local"), task.default_branch(), "main"
    )

    restarted = Scheduler(Store(garden))
    competitor_task = restarted.store.task("DM-002")
    competitor = restarted.runs.new_run(
        competitor_task.id, "local", initial_status="preparing"
    )
    with pytest.raises(CanonicalCheckoutError, match=f"active run {first.run_id}"):
        restarted.prepare_canonical_run(
            competitor_task,
            competitor,
            restarted.runner_for(competitor_task, "local"),
            competitor_task.default_branch(),
            "main",
        )

    persisted = restarted.runs.runs_for(task.id)[-1]
    assert persisted.worktree == str(repo)
    assert json.loads(lease_path(repo).read_text())["run_id"] == first.run_id


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
    assert (lease / "run-id").read_text() == f"{run.run_id}\n"


def test_ssh_canonical_lease_survives_process_exit_until_run_is_reaped(garden, tmp_path):
    remote = _remote_clone(garden)
    scheduler = Scheduler(_enable(garden, (garden / "../repo").resolve()))
    task = scheduler.store.task("DM-001")
    runner = scheduler.runner_for(task, "ssh")
    runner.config["timeout_minutes"] = 0

    first = scheduler.runs.new_run(task.id, "ssh")
    first.host, first.branch, first.base = "boxA", task.default_branch(), "main"
    scheduler.prepare_canonical_run(task, first, runner, first.branch, first.base)
    runner.start(first, tmp_path, "brief")
    assert _wait_run(first) == 0
    lease = remote / ".git" / "garden-canonical-lease"
    assert (lease / "run-id").read_text() == f"{first.run_id}\n"

    # Process exit is not collection: the durable run record still names A as active, so a
    # fresh scheduler/controller cannot let B enter the canonical checkout yet.
    second = scheduler.runs.new_run("DM-002", "ssh")
    second.host, second.branch, second.base = "boxA", "garden/dm-002-second-task", "main"
    scheduler.prepare_canonical_run(scheduler.store.task("DM-002"), second, runner,
                                    second.branch, second.base)
    runner.start(second, tmp_path, "brief")
    assert _wait_run(second) == 4
    assert f"leased by active run {first.run_id}" in (second.path / "stderr.log").read_text()
    assert (lease / "run-id").read_text() == f"{first.run_id}\n"

    # Once A is terminal and no longer awaits collection/fencing, the next claim deliberately
    # recovers its clean lease and warm-reuses the checkout.
    first.status = "done"
    first.finished_at = "2026-09-08T00:00:00+00:00"
    first.save()
    second.status = "failed"
    second.finished_at = "2026-09-08T00:00:01+00:00"
    second.save()
    third = scheduler.runs.new_run(task.id, "ssh")
    third.host, third.branch, third.base = "boxA", task.default_branch(), "main"
    scheduler.prepare_canonical_run(task, third, runner,
                                    third.branch, third.base)
    runner.start(third, tmp_path, "brief")
    assert _wait_run(third) == 0
    assert (lease / "run-id").read_text() == f"{third.run_id}\n"


def test_ssh_reap_refuses_dirty_local_canonical_checkout(garden, fake_github, tmp_path):
    _remote_clone(garden)
    repo = (garden / "../repo").resolve()
    scheduler = Scheduler(_enable(garden, repo), github=fake_github)
    task = scheduler.store.task("DM-001")
    runner = scheduler.runner_for(task, "ssh")
    runner.config["timeout_minutes"] = 0

    run = scheduler.dispatch(task, runner=runner)
    (repo / "README.md").write_text("operator edit that must survive\n")
    assert _wait_run(run) == 0

    scheduler.tick()

    assert (repo / "README.md").read_text() == "operator edit that must survive\n"
    saved = next(item for item in scheduler.runs.runs_for(task.id) if item.run_id == run.run_id)
    assert saved.status == "failed"
    assert "uncommitted work" in saved.error


def test_remote_canonical_owner_remains_protected_during_interrupted_reap(garden):
    scheduler = Scheduler(_enable(garden, (garden / "../repo").resolve()))
    task = scheduler.store.task("DM-001")
    runner = scheduler.runner_for(task, "ssh")
    owner = scheduler.runs.new_run(task.id, "ssh")
    owner.host, owner.branch, owner.base = "boxA", task.default_branch(), "main"
    scheduler.prepare_canonical_run(task, owner, runner, owner.branch, owner.base)
    owner.finished_at = "2026-09-08T00:00:00+00:00"
    owner.status = "done"
    owner.save()
    task.status = Status.RUNNING
    scheduler.store.save(task)

    competitor = scheduler.runs.new_run("DM-002", "ssh")
    scheduler.prepare_canonical_run(scheduler.store.task("DM-002"), competitor, runner,
                                    "garden/dm-002-second-task", "main")
    assert competitor.env_snapshot["canonical_active_run_ids"] == [owner.run_id]


def test_ssh_canonical_claims_are_scoped_to_host_and_checkout(garden, tmp_path):
    first_repo = (garden / "../remote-clone").resolve()
    second_repo = (tmp_path / "other-remote-clone").resolve()
    data = yaml.safe_load((garden / "garden.yaml").read_text())
    data["products"]["demo"]["checkout"] = {
        "strategy": "in_place",
        "root": str((garden / "../repo").resolve()),
    }
    data["ssh"]["hosts"].append({
        "name": "boxB",
        "host": "boxB",
        "repos": {"demo": str(second_repo)},
        "max_parallel": 1,
    })
    (garden / "garden.yaml").write_text(yaml.safe_dump(data))
    scheduler = Scheduler(Store(garden))
    task = scheduler.store.task("DM-001")

    owner_runner = scheduler.runner_for(task, "ssh")
    owner = scheduler.runs.new_run(task.id, "ssh", initial_status="preparing")
    owner.host, owner.branch, owner.base = "boxA", task.default_branch(), "main"
    scheduler.prepare_canonical_run(task, owner, owner_runner, owner.branch, owner.base)
    assert json.loads(owner.env_snapshot["canonical_checkout_identity"]) == [
        "boxA", str(first_repo)
    ]

    other_host = scheduler.runs.new_run("DM-002", "ssh", initial_status="preparing")
    other_host.host = "boxB"
    scheduler.prepare_canonical_run(
        scheduler.store.task("DM-002"), other_host,
        scheduler.runner_for(scheduler.store.task("DM-002"), "ssh"),
        "garden/dm-002-second-task", "main",
    )
    assert other_host.env_snapshot["canonical_active_run_ids"] == []

    same_checkout = scheduler.runs.new_run("DM-002", "ssh", initial_status="preparing")
    same_checkout.host = "boxA"
    scheduler.prepare_canonical_run(
        scheduler.store.task("DM-002"), same_checkout,
        scheduler.runner_for(scheduler.store.task("DM-002"), "ssh"),
        "garden/dm-002-second-task", "main",
    )
    assert same_checkout.env_snapshot["canonical_active_run_ids"] == [owner.run_id]


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


def test_ssh_in_place_refuses_competing_active_claim_without_fetch_or_mutation(
    garden, tmp_path, monkeypatch
):
    remote = _remote_clone(garden)
    scheduler = Scheduler(_enable(garden, (garden / "../repo").resolve()))
    task = scheduler.store.task("DM-001")
    runner = scheduler.runner_for(task, "ssh")
    runner.config["timeout_minutes"] = 0
    lease = remote / ".git" / "garden-canonical-lease"
    lease.mkdir()
    (lease / "run-id").write_text("active-run\n")
    real_git = shutil.which("git")
    assert real_git is not None
    fetch_marker = tmp_path / "fetch-called"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_wrapper = fake_bin / "git"
    git_wrapper.write_text(
        "#!/bin/sh\n"
        f"[ \"$1\" != fetch ] || printf fetch > {shlex.quote(str(fetch_marker))}\n"
        f"exec {shlex.quote(real_git)} \"$@\"\n"
    )
    git_wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    before = subprocess.run(
        [real_git, "status", "--porcelain=v2", "--branch"],
        cwd=remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    refs_before = subprocess.run(
        [real_git, "for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    run = scheduler.runs.new_run(task.id, "ssh")
    run.host, run.branch, run.base = "boxA", task.default_branch(), "main"
    run.env_snapshot["canonical_active_run_ids"] = ["active-run"]
    runner.start(run, tmp_path, "brief")
    assert _wait_run(run) == 4
    assert "leased by active run active-run" in (run.path / "stderr.log").read_text()
    assert not fetch_marker.exists()
    assert (lease / "run-id").read_text() == "active-run\n"
    after = subprocess.run(
        [real_git, "status", "--porcelain=v2", "--branch"],
        cwd=remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    refs_after = subprocess.run(
        [real_git, "for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after == before
    assert refs_after == refs_before


@pytest.mark.parametrize("consumer", ["review", "persona", "rebase", "retro"])
@pytest.mark.parametrize("unsafe", ["dirty", "drift", "competing"])
def test_every_canonical_consumer_claims_before_git_operations(
    garden, fake_github, consumer, unsafe, monkeypatch
):
    repo = (garden / "../repo").resolve()
    data = yaml.safe_load((garden / "garden.yaml").read_text())
    data["products"]["demo"]["self"] = True
    (garden / "garden.yaml").write_text(yaml.safe_dump(data))
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
    mutations: list[str] = []
    monkeypatch.setattr(gitops, "fetch", lambda *_: mutations.append("fetch"))
    monkeypatch.setattr(gitops, "prepare_worktree", lambda *_: mutations.append("prepare"))
    with pytest.raises(CanonicalCheckoutError, match=expected):
        if consumer == "review":
            scheduler.dispatch_review(task)
        elif consumer == "persona":
            scheduler.dispatch_persona_pr(task, "security")
        elif consumer == "rebase":
            scheduler._rebase_and_record(task, "main")
        else:
            scheduler._dispatch_reconcile({
                "product": "demo", "phase_name": "p1", "self_product": "demo",
                "personas": [], "next_phase": "p2",
            })
    assert mutations == []
    if unsafe == "dirty":
        assert (repo / "operator.txt").read_text() == "preserve\n"


def test_retro_uses_canonical_root_without_preparing_worktree(garden, fake_github, monkeypatch):
    repo = (garden / "../repo").resolve()
    data = yaml.safe_load((garden / "garden.yaml").read_text())
    data["products"]["demo"]["self"] = True
    (garden / "garden.yaml").write_text(yaml.safe_dump(data))
    scheduler = Scheduler(_enable(garden, repo), github=fake_github)
    monkeypatch.setattr(gitops, "fetch", lambda *_: pytest.fail("retro fetched before canonical dispatch"))
    monkeypatch.setattr(
        gitops, "prepare_worktree", lambda *_: pytest.fail("retro prepared a disposable worktree")
    )

    entry = {
        "product": "demo", "phase_name": "p1", "self_product": "demo",
        "personas": [], "next_phase": "p2",
    }
    scheduler._dispatch_reconcile(entry)

    run = scheduler.runs.latest(entry["recon_task"])
    assert run is not None
    assert run.worktree == str(repo)
    assert entry["worktree"] == str(repo)
    assert gitops.git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip() == "garden/retro-demo-p1"


def test_default_checkout_still_uses_linked_worktree(garden, fake_github):
    scheduler = Scheduler(Store(garden), github=fake_github)
    task = scheduler.store.task("DM-001")
    run = scheduler.dispatch(task)
    assert Path(run.worktree) == scheduler.cfg.worktree_path(task.id)
    assert Path(run.worktree) != scheduler.repo_for(task)
