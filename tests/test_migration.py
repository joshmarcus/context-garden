from __future__ import annotations

import json
import tarfile

import pytest
import yaml

from garden.coordination import Coordinator
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
    config["multiplayer"] = {"enabled": True, "garden_id": "garden-1",
        "coordinator_url": "https://coordinator.test", "member_id": "alice",
        "installation_id": "alice-laptop", "credential_env": "GARDEN_ALICE"}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    choices = {"owner_map": {"legacy-alice": "alice"},
               "phase_owners": {"demo/p1": "alice"},
               "installations": {"alice-laptop": "alice"}}
    return admin, choices


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
    assert Coordinator(garden / ".garden" / "coordination.db").pending_outbox("garden-1") == []


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
    original = Coordinator.set_authority
    calls = 0

    def interrupted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Coordinator, "set_authority", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        migration.commit(plan["preview_id"], admin)
    journal = json.loads((garden / ".garden" / "migration" / "cutover.json").read_text())
    assert journal["status"] == "prepared"

    monkeypatch.setattr(Coordinator, "set_authority", original)
    result = migration.commit(plan["preview_id"], admin)
    assert result["status"] == "committed"
    fence = standalone_fence(garden)
    assert fence and fence["protocol_version"] == 1
    with tarfile.open(result["snapshot"]) as archive:
        names = archive.getnames()
        assert ".garden/members.json" not in names

    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": False}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    scheduler = Scheduler(Store(garden))
    with pytest.raises(MultiplayerExecutionUnavailable):
        scheduler.require_execution_authority()


def test_standalone_export_requires_quiescence_and_is_outside_live_garden(garden, tmp_path):
    _admin, _choices = _prepared(garden)
    migration = GardenMigration(Store(garden))
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
