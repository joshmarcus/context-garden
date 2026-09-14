from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile

import pytest
import yaml
from typer.testing import CliRunner

import garden.migration as migration_module
from garden.cli import app as cli_app
from garden.coordination import Coordinator
from garden.git_coordination import GitMultiplayerClient, GitStateStore
from garden.members import MemberRegistry
from garden.migration import GardenMigration, MigrationRefused, standalone_fence
from garden.scheduler import MultiplayerExecutionUnavailable, Scheduler
from garden.store import Store


def _prepared(garden):
    goals = garden / "demo" / "p1" / "goals.md"
    goals.write_text("---\ndefault_owner: legacy-alice\n---\n\n# p1\n")
    task = garden / "demo" / "p1" / "tasks" / "DM-002-second.md"
    task.write_text(task.read_text().replace("status: ready", "status: ready\nowner: unassigned"))
    registry = MemberRegistry(garden / ".garden")
    token = registry.enroll_administrator("garden-1", "alice", "alice-laptop")
    admin = registry.authenticate(token)
    assert admin is not None
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    remote = garden.parent / "garden-state.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(garden)], check=True)
    subprocess.run(["git", "config", "user.name", "Migration Test"], cwd=garden, check=True)
    subprocess.run(["git", "config", "user.email", "migration@example.test"], cwd=garden,
                   check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=garden, check=True)
    (garden / ".gitignore").write_text(".garden/\ngarden.local.yaml\n")
    config["multiplayer"] = {"enabled": True, "garden_id": "garden-1",
        "git": {"remote": "origin", "state_ref": "refs/heads/garden-state"},
        "coordinator_url": "", "member_id": "alice",
        "installation_id": "alice-laptop", "credential_env": ""}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    subprocess.run(["git", "add", "."], cwd=garden, check=True)
    subprocess.run(["git", "commit", "-qm", "seed garden"], cwd=garden, check=True)
    choices = {"owner_map": {"legacy-alice": "alice"},
               "phase_owners": {"demo/p1": "alice"},
               "installations": {"alice-laptop": "alice"}}
    return admin, choices


def _enroll_legacy_worker(garden, name="legacy-worker"):
    path = garden / ".garden/hosts/enrollment/controller-hosts.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({"hosts": [{
        "name": name, "token_sha256": hashlib.sha256(b"legacy-secret").hexdigest()
    }]}))
    path.chmod(0o600)
    return path


def test_preview_preserves_unassignment_and_separates_phase_owner(garden):
    _admin, choices = _prepared(garden)
    plan = GardenMigration(Store(garden)).preview(choices)

    assert plan["ready"] is True
    assert next(row for row in plan["tasks"] if row["id"] == "DM-001")["member_id"] == "alice"
    unassigned = next(row for row in plan["tasks"] if row["id"] == "DM-002")
    assert (unassigned["owner_source"], unassigned["member_id"]) == ("unassigned", "")
    assert plan["phases"] == [{"scope": "demo/p1", "legacy_default_owner": "legacy-alice",
                               "member_id": "alice"}]
    assert not (garden / ".garden" / "authority-mode.json").exists()
    assert not (garden / ".garden" / "coordination.db").exists()


def test_preview_reports_unknown_dirty_active_and_required_setup(garden):
    _admin, choices = _prepared(garden)
    choices["owner_map"] = {}
    repo = garden.parent / "repo"
    (repo / "dirty.txt").write_text("local")
    plan = GardenMigration(Store(garden)).preview(choices)

    assert plan["unknown_owners"] == ["legacy-alice"]
    assert "unknown legacy owners must be mapped or explicitly made unassigned" in plan["required_setup"]
    # Product repositories are also part of the local-edit preflight.
    assert plan["ready"] is False


def test_preview_rejects_owner_without_project_visibility(garden):
    admin, choices = _prepared(garden)
    registry = MemberRegistry(garden / ".garden")
    registry.add_member(admin, "bob", "member", "assigned", ())
    choices["owner_map"] = {"legacy-alice": "bob"}

    plan = GardenMigration(Store(garden)).preview(choices)

    assert plan["unknown_owners"] == ["legacy-alice"]
    assert all(row["member_id"] == "" for row in plan["tasks"])


def test_commit_is_explicit_resumable_and_fences_legacy_scheduler(garden, monkeypatch):
    admin, choices = _prepared(garden)
    migration = GardenMigration(Store(garden))
    plan = migration.preview(choices)
    original = GitStateStore.initialize_authority
    calls = 0

    def interrupted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("interrupted")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "initialize_authority", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        migration.commit(plan["preview_id"], admin)
    journal = json.loads((garden / ".garden" / "migration" / "cutover.json").read_text())
    assert journal["status"] == "prepared"

    monkeypatch.setattr(GitStateStore, "initialize_authority", original)
    result = migration.commit(plan["preview_id"], admin)
    assert result["status"] == "committed"
    fence = standalone_fence(garden)
    assert fence and fence["protocol_version"] == 1
    with tarfile.open(result["snapshot"]) as archive:
        names = archive.getnames()
        assert ".garden/members.json" not in names

    # Normal startup reads the authority just accepted through the configured Git ref.
    Scheduler(Store(garden)).require_execution_authority()

    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": False}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    scheduler = Scheduler(Store(garden))
    with pytest.raises(MultiplayerExecutionUnavailable):
        scheduler.require_execution_authority()


def test_standalone_export_requires_quiescence_and_is_outside_live_garden(garden, tmp_path):
    admin, choices = _prepared(garden)
    migration = GardenMigration(Store(garden))
    migration.commit(migration.preview(choices)["preview_id"], admin)
    with pytest.raises(MigrationRefused, match="outside"):
        migration.export_standalone(garden / "export.tar.gz")
    destination = tmp_path / "standalone.tar.gz"
    migration.export_standalone(destination)
    with tarfile.open(destination) as archive:
        marker = json.loads(archive.extractfile(".garden/authority-mode.json").read())
        config = yaml.safe_load(archive.extractfile("garden.yaml").read())
        assert marker["mode"] == "standalone"
        assert config["multiplayer"] == {"enabled": False}
        assert ".garden/members.json" not in archive.getnames()


def test_preview_accepts_two_unique_installations_for_one_member(garden):
    admin, choices = _prepared(garden)
    registry = MemberRegistry(garden / ".garden")
    registry.issue_installation(admin, "alice", "alice-desktop")
    choices["installations"]["alice-desktop"] = "alice"

    plan = GardenMigration(Store(garden)).preview(choices)

    assert plan["ready"] is True
    assert plan["installations"] == [
        {"installation_id": "alice-desktop", "member_id": "alice"},
        {"installation_id": "alice-laptop", "member_id": "alice"},
    ]


def test_preview_requires_distinct_member_binding_for_every_legacy_worker(garden):
    admin, choices = _prepared(garden)
    registry = MemberRegistry(garden / ".garden")
    registry.add_member(admin, "bob", "member", "all", ())
    _enroll_legacy_worker(garden)

    plan = GardenMigration(Store(garden)).preview(choices)
    assert plan["ready"] is False
    assert "all legacy worker enrollments must be assigned: legacy-worker" in plan["required_setup"]

    choices["worker_enrollments"] = {"legacy-worker": "bob"}
    plan = GardenMigration(Store(garden)).preview(choices)
    assert plan["ready"] is True
    assert plan["worker_enrollments"] == [{"worker_name": "legacy-worker", "member_id": "bob"}]


def test_commit_revokes_selected_legacy_worker_enrollments(garden):
    admin, choices = _prepared(garden)
    _enroll_legacy_worker(garden)
    choices["worker_enrollments"] = {"legacy-worker": "alice"}
    migration = GardenMigration(Store(garden))

    result = migration.commit(migration.preview(choices)["preview_id"], admin)

    assert result["revoked_worker_enrollments"] == ["legacy-worker"]
    registry = json.loads((garden / ".garden/hosts/enrollment/controller-hosts.json").read_text())
    assert registry == {"hosts": []}


def test_commit_preserves_assignments_and_revoked_installation_history(garden):
    admin, choices = _prepared(garden)
    registry = MemberRegistry(garden / ".garden")
    registry.set_assignment(admin, "alice", "demo", "p1")
    registry.issue_installation(admin, "alice", "retired-laptop")
    registry.revoke_installation(admin, "retired-laptop")
    migration = GardenMigration(Store(garden))

    migration.commit(migration.preview(choices)["preview_id"], admin)

    state = GitStateStore(garden, garden_id="garden-1").read()[1]
    assert state["members"]["alice"]["assignment"] == {
        "project": "demo", "phase": "p1", "generation": 1,
        "enabled": True, "advance": False,
    }
    assert state["revoked_installations"] == {"retired-laptop": "alice"}
    assert (garden / "demo/p1/tasks/DM-001-first.md").exists()


def test_preview_rejects_shared_member_worker_bindings(garden):
    _admin, choices = _prepared(garden)
    path = _enroll_legacy_worker(garden)
    value = json.loads(path.read_text())
    value["hosts"].append({
        "name": "legacy-worker-2",
        "token_sha256": hashlib.sha256(b"legacy-secret-2").hexdigest(),
    })
    path.write_text(json.dumps(value))
    choices["worker_enrollments"] = {
        "legacy-worker": "alice",
        "legacy-worker-2": "alice",
    }

    with pytest.raises(MigrationRefused, match="distinct active member"):
        GardenMigration(Store(garden)).preview(choices)


def test_commit_resumes_after_worker_registry_was_revoked(garden, monkeypatch):
    admin, choices = _prepared(garden)
    _enroll_legacy_worker(garden)
    choices["worker_enrollments"] = {"legacy-worker": "alice"}
    migration = GardenMigration(Store(garden))
    plan = migration.preview(choices)
    original = migration_module.revoke_enrolled_hosts
    calls = 0

    def interrupted(workers, expected_names):
        nonlocal calls
        calls += 1
        original(workers, expected_names)
        if calls == 1:
            raise RuntimeError("interrupted after revocation")

    monkeypatch.setattr(migration_module, "revoke_enrolled_hosts", interrupted)
    with pytest.raises(RuntimeError, match="interrupted after revocation"):
        migration.commit(plan["preview_id"], admin)

    result = migration.commit(plan["preview_id"], admin)
    assert result["status"] == "committed"
    assert result["revoked_worker_enrollments"] == ["legacy-worker"]


def test_active_git_claim_blocks_resumed_cutover(garden, monkeypatch):
    admin, choices = _prepared(garden)
    migration = GardenMigration(Store(garden))
    plan = migration.preview(choices)
    original_snapshot = migration._snapshot

    def claim_during_cutover(preview_id):
        path = original_snapshot(preview_id)
        migration._initialize_git_authority(
            plan, admin, operation_id=f"migration:{plan['preview_id']}:authority"
        )
        client = GitMultiplayerClient(
            GitStateStore(garden, garden_id="garden-1"), "alice", "alice-laptop"
        )
        client.claim(kind="task", scope="DM-001", owner_id="alice",
                     authority_generation=1, expected_version=0)
        return path

    monkeypatch.setattr(migration, "_snapshot", claim_during_cutover)
    with pytest.raises(MigrationRefused, match="quiescent"):
        migration.commit(plan["preview_id"], admin)
    refreshed = migration.preview(choices)
    assert refreshed["active_claims"]
    assert "active coordinator claims must drain or be cancelled" in refreshed["required_setup"]


def test_standalone_archive_excludes_nested_host_enrollment(garden, tmp_path):
    admin, choices = _prepared(garden)
    migration = GardenMigration(Store(garden))
    migration.commit(migration.preview(choices)["preview_id"], admin)
    private = garden / ".garden" / "hosts" / "enrollment"
    private.mkdir(parents=True)
    (private / "credential.json").write_text('{"token":"must-not-export"}')

    destination = tmp_path / "standalone.tar.gz"
    migration.export_standalone(destination)

    with tarfile.open(destination) as archive:
        assert not any(name.startswith(".garden/hosts/") for name in archive.getnames())


def test_standalone_export_blocks_another_installations_unresolved_permit(garden, tmp_path):
    admin, choices = _prepared(garden)
    registry = MemberRegistry(garden / ".garden")
    registry.issue_installation(admin, "alice", "alice-desktop")
    choices["installations"]["alice-desktop"] = "alice"
    migration = GardenMigration(Store(garden))
    migration.commit(migration.preview(choices)["preview_id"], admin)
    client = GitMultiplayerClient(
        GitStateStore(garden, garden_id="garden-1"), "alice", "alice-desktop"
    )
    claim = client.claim(
        kind="task", scope="DM-001", owner_id="alice", authority_generation=1,
        expected_version=0,
    )
    client.store.apply(
        "desktop-permit", actor="alice", installation="alice-desktop",
        expected_versions={}, changes={"permits": {"desktop-permit": {
            "claim": claim["operation_id"], "status": "pending",
        }}},
    )

    with pytest.raises(MigrationRefused, match="Git claims and effects must be quiescent"):
        migration.export_standalone(tmp_path / "blocked.tar.gz")


def test_standalone_export_refuses_unavailable_git_authority(garden, tmp_path):
    admin, choices = _prepared(garden)
    migration = GardenMigration(Store(garden))
    migration.commit(migration.preview(choices)["preview_id"], admin)
    subprocess.run(
        ["git", "--git-dir", str(garden.parent / "garden-state.git"), "update-ref", "-d",
         "refs/heads/garden-state"], check=True,
    )

    with pytest.raises(MigrationRefused, match="unavailable; standalone export refused"):
        migration.export_standalone(tmp_path / "uncertain.tar.gz")


def test_migration_cli_commits_git_authority_and_starts_normally(
    garden, tmp_path, monkeypatch,
):
    admin, choices = _prepared(garden)
    choice_path = tmp_path / "migration-choices.json"
    choice_path.write_text(json.dumps(choices))
    monkeypatch.chdir(garden)
    monkeypatch.setattr("garden.cli.migration._actor", lambda _credential_env: admin)

    preview = CliRunner().invoke(
        cli_app, ["migration", "preview", "--choices", str(choice_path)]
    )
    assert preview.exit_code == 0, preview.output
    preview_id = json.loads(preview.output)["preview_id"]
    committed = CliRunner().invoke(
        cli_app,
        ["migration", "commit", "--preview-id", preview_id,
         "--credential-env", "TEST_ADMIN"],
    )
    assert committed.exit_code == 0, committed.output
    status = CliRunner().invoke(cli_app, ["members", "status"])
    assert status.exit_code == 0, status.output
    assert "mode: multiplayer Git" in status.output
    assert "Git: synchronized at" in status.output


def test_legacy_sqlite_obligation_still_blocks_standalone_export(garden, tmp_path):
    admin, _choices = _prepared(garden)
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": False}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    coordinator = Coordinator(garden / ".garden" / "coordination.db")
    coordinator.set_authority(
        admin, garden_id="garden-1", kind="task", scope="legacy-active",
        owner_id="alice", authority_generation=1, expected_version=0,
        operation_id="seed-authority",
    )
    coordinator.claim(
        admin, garden_id="garden-1", kind="task", scope="legacy-active",
        expected_version=1, accepted_owner="alice", authority_generation=1,
        operation_id="active-claim",
    )

    with pytest.raises(MigrationRefused, match="claims and effects must be quiescent"):
        GardenMigration(Store(garden)).export_standalone(tmp_path / "legacy-blocked.tar.gz")
