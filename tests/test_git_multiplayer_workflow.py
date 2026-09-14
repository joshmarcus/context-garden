"""Supported Git multiplayer journey through migration and scheduler entry points."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from garden.cli import app as cli_app
from garden.git_coordination import GitCoordinationError, GitMultiplayerClient
from garden.members import MemberRegistry
from garden.migration import GardenMigration
from garden.model import Status
from garden.scheduler import MultiplayerExecutionUnavailable, Scheduler
from garden.store import Store


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, text=True, capture_output=True,
    )
    return result.stdout.strip()


def configure_installation(root: Path, member: str, installation: str) -> None:
    """Use the public connect command to create an installation-local overlay."""
    previous = Path.cwd()
    try:
        import os

        os.chdir(root)
        result = CliRunner().invoke(
            cli_app, ["members", "connect", "shared", member, installation],
        )
    finally:
        os.chdir(previous)
    assert result.exit_code == 0, result.output


def test_migrated_installations_coordinate_supported_scheduler_journey(garden, tmp_path):
    """One bounded journey joins migration, startup, recovery, handoff and closure.

    Detailed mutation-guard and late-result invariants remain in
    ``test_git_coordination.py``; this test deliberately exercises their supported
    configuration and Scheduler integration rather than repeating every boundary case.
    """
    goals = garden / "demo" / "p1" / "goals.md"
    goals.write_text("---\ndefault_owner: alice\n---\n\n# p1\n")
    registry = MemberRegistry(garden / ".garden")
    token = registry.enroll_administrator("shared", "alice", "alice-laptop")
    admin = registry.authenticate(token)
    assert admin is not None
    registry.add_member(admin, "bob", "member", "all", ())
    registry.issue_installation(admin, "bob", "bob-laptop")
    registry.set_assignment(admin, "alice", "demo", "p1")
    registry.set_assignment(admin, "bob", "demo", "p1")

    remote = tmp_path / "shared.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(garden)], check=True)
    git(garden, "config", "user.name", "Alice")
    git(garden, "config", "user.email", "alice@example.test")
    git(garden, "remote", "add", "origin", str(remote))
    (garden / ".gitignore").write_text(".garden/\ngarden.local.yaml\n")
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {
        "enabled": True,
        "garden_id": "shared",
        "git": {"remote": "origin", "state_ref": "refs/heads/garden-state"},
        "member_id": "alice",
        "installation_id": "alice-laptop",
    }
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    git(garden, "add", ".")
    git(garden, "commit", "-m", "seed standalone garden")
    git(garden, "push", "-u", "origin", "HEAD:refs/heads/main")

    migration = GardenMigration(Store(garden))
    choices = {
        "owner_map": {"alice": "alice"},
        "phase_owners": {"demo/p1": "alice"},
        "installations": {"alice-laptop": "alice", "bob-laptop": "bob"},
    }
    plan = migration.preview(choices)
    assert plan["ready"] is True
    assert migration.commit(plan["preview_id"], admin)["status"] == "committed"

    bob_root = tmp_path / "bob-root"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", str(remote), str(bob_root)], check=True,
    )
    git(bob_root, "config", "user.name", "Bob")
    git(bob_root, "config", "user.email", "bob@example.test")
    configure_installation(bob_root, "bob", "bob-laptop")

    # Both independently constructed schedulers select the Git client from normal
    # configuration. Alice starts with task and phase authority; Bob starts safely idle.
    alice = Scheduler(Store(garden))
    bob = Scheduler(Store(bob_root))
    assert isinstance(alice.coordinator, GitMultiplayerClient)
    assert isinstance(bob.coordinator, GitMultiplayerClient)
    assert alice._refresh_execution_authority()
    assert bob._refresh_execution_authority()
    task = alice.store.task("DM-001")
    assert alice.task_is_authorized(task)
    assert alice.phase_is_authorized("demo", "p1")
    assert not bob.task_is_authorized(bob.store.task("DM-001"))
    with pytest.raises(PermissionError, match="not owned"):
        bob.require_task_authority(bob.store.task("DM-001"))

    # An accepted effect makes the authority unresolved. The production handoff API
    # refuses to transfer it until the same durable Git record is reconciled.
    claim = alice.coordinator.claim(
        kind="task", scope="DM-001", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    alice.coordinator.store.apply(
        "admit-run", actor="alice", installation="alice-laptop",
        expected_versions={"task:DM-001": 0},
        changes={
            "permits": {"run:DM-001": {"claim": claim["operation_id"]}},
            "effects": {"run:DM-001": {
                "claim": claim["operation_id"], "provider": "scheduler",
                "scope": "task:DM-001", "effect_key": "run:DM-001", "outcome": "unknown",
            }},
        },
    )
    handoff = alice.coordinator.begin_handoff(
        kind="task", scope="DM-001", pending_owner="bob", expected_version=0,
    )
    assert handoff["status"] == "blocked"
    with pytest.raises(GitCoordinationError, match="blocked"):
        alice._refresh_execution_authority()
    alice.coordinator.store.reconcile_effect(
        "recover-run", actor="alice", installation="alice-laptop",
        effect_operation_id="run:DM-001", outcome="fenced",
        evidence={"execution": "stopped", "publication": "not published"},
    )
    assert alice._refresh_execution_authority()  # acknowledges the stopped old generation

    bob_task = bob.store.task("DM-001")
    bob_task.owner = "bob"
    bob.store.save(bob_task)
    assert bob._refresh_execution_authority()
    with bob.task_effect(bob_task, "review:DM-001"):
        pass

    # A lost authority ref fails closed even with a cached snapshot. Restoring the exact
    # ref models partition recovery and allows fresh authority to resume progress.
    state_commit = git(remote, "rev-parse", "refs/heads/garden-state")
    git(remote, "update-ref", "-d", "refs/heads/garden-state", state_commit)
    with pytest.raises(MultiplayerExecutionUnavailable, match="unavailable|missing"):
        bob._refresh_execution_authority()
    git(remote, "update-ref", "refs/heads/garden-state", state_commit)
    assert bob._refresh_execution_authority()

    # The independent phase owner uses the actual scheduler close operation. Force only
    # bypasses stabilization evidence; multiplayer phase ownership is still enforced.
    for local_task in alice.store.tasks().values():
        local_task.status = Status.DONE
        alice.store.save(local_task)
    alice.close_phase(alice.store.phase("demo", "p1"), force=True)
    assert alice.store.phase("demo", "p1").closed
    with pytest.raises(PermissionError, match="not owned"):
        bob.close_phase(bob.store.phase("demo", "p1"), force=True)
