from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.defects import DefectConflict, DefectStore
from garden.members import MemberRegistry
from garden.model import Status
from garden.store import Store
from garden.web.app import create_app


def _closed(store: Store, task_id: str = "DM-001"):
    task = store.task(task_id)
    task.status = Status.DONE
    store.save(task)
    return store.task(task_id)


def test_closed_task_records_analysis_and_preserves_task_and_phase(garden):
    store = Store(garden)
    task = _closed(store)
    original_task = task.path.read_bytes()
    phase = store.phase(task.product, task.phase)
    original_goals = (phase.path / "goals.md").read_bytes()
    ledger = DefectStore(store.config.garden_dir)
    minor, _ = ledger.create(task, "minor", "A label wraps", "alice", idempotency_key="one")
    major, _ = ledger.create(
        task, "major", "Export loses records", "bob", idempotency_key="two",
        expected="Every row", observed="First page only", impact="Incomplete output",
        evidence_links=["https://example.invalid/repro"], affected_source="abc123",
        affected_release="1.2", affected_run="run-7", follow_up="DM-999",
    )
    assert minor["id"] != major["id"]
    updated = ledger.update(
        major["id"], "carol", 1, severity="minor", disposition="reviewed",
        known_facts="Pagination stops", hypotheses="Cursor handling", unknowns="Older releases",
        could_have_caught="Integration test", prevention="Paginated fixture",
        proposed_follow_up="DM-999",
    )
    assert updated["revision"] == 2
    assert updated["history"][0]["changed_by"] == "carol"
    assert updated["history"][0]["prior"]["severity"] == "major"
    assert ledger.summary(task_id=task.id) == {
        "total": 2, "minor": 2, "major": 0, "unreviewed": 1, "reviewed": 1,
    }
    assert task.path.read_bytes() == original_task
    assert (phase.path / "goals.md").read_bytes() == original_goals
    assert store.task(task.id).status is Status.DONE


def test_retry_and_concurrency_are_idempotent_without_lost_records(garden):
    store = Store(garden)
    task = _closed(store)
    ledger = DefectStore(store.config.garden_dir)

    def create(key: str):
        return ledger.create(task, "minor", f"problem {key}", "alice", idempotency_key=key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, ["same"] * 4 + [f"unique-{n}" for n in range(4)]))
    rows = ledger.list(task_id=task.id)
    assert len(rows) == 5
    assert len({row["id"] for row, _created in results[:4]}) == 1
    assert sum(created for _row, created in results[:4]) == 1
    with pytest.raises(DefectConflict, match="different defect data"):
        ledger.create(task, "major", "different", "alice", idempotency_key="same")
    with pytest.raises(DefectConflict, match="current revision"):
        ledger.update(rows[0]["id"], "bob", 0, impact="stale")


def test_open_task_and_invalid_severity_are_rejected(garden):
    store = Store(garden)
    ledger = DefectStore(store.config.garden_dir)
    with pytest.raises(ValueError, match="closed task"):
        ledger.create(store.task("DM-001"), "minor", "problem", "alice")
    task = _closed(store)
    with pytest.raises(ValueError, match="minor or major"):
        ledger.create(task, "critical", "problem", "alice")


def test_web_api_task_and_retro_surfaces_show_defects(garden):
    store = Store(garden)
    task = _closed(store)
    client = TestClient(create_app(store, watch=False))
    before = task.path.read_bytes()
    payload = {"severity": "major", "description": "The export is incomplete",
               "idempotency_key": "request-1", "impact": "Missing rows"}
    response = client.post(f"/api/tasks/{task.id}/defects", json=payload)
    assert response.status_code == 201
    defect = response.json()["defect"]
    assert client.post(f"/api/tasks/{task.id}/defects", json=payload).json()["created"] is False
    amended = client.patch(f"/api/tasks/{task.id}/defects/{defect['id']}", json={
        "expected_revision": 1, "disposition": "reviewed", "known_facts": "Rows are absent",
    })
    assert amended.status_code == 200
    assert amended.json()["defect"]["history"][0]["changed_by"] == "api"
    too_long = client.patch(f"/api/tasks/{task.id}/defects/{defect['id']}", json={
        "expected_revision": 2, "description": "x" * 1001,
    })
    assert too_long.status_code == 422
    assert too_long.json()["detail"] == "description must be at most 1000 characters"
    assert DefectStore(store.config.garden_dir).get(defect["id"])["revision"] == 2
    listing = client.get("/api/defects?severity=major&disposition=reviewed").json()
    assert listing["summary"]["total"] == 1
    assert defect["id"] in client.get(f"/tasks/{task.id}").text
    assert "The export is incomplete" in client.get(
        f"/phases/{task.product}/{task.phase}/retro"
    ).text
    assert task.path.read_bytes() == before


def test_multiplayer_permissions_and_project_projection_apply(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    store = Store(garden)
    task = _closed(store)
    task.owner = "alice"
    store.save(task)
    registry = MemberRegistry(store.config.garden_dir)
    admin_token = registry.enroll_administrator("garden-1", "alice", "alice-browser")
    admin = registry.authenticate(admin_token)
    assert admin
    registry.add_member(admin, "eve", "viewer", "all")
    viewer_token = registry.issue_installation(admin, "eve", "eve-browser")
    registry.add_member(admin, "bob", "member", "assigned", ("other",))
    bob_token = registry.issue_installation(admin, "bob", "bob-browser")
    client = TestClient(create_app(store, watch=False, host="testserver"))
    payload = {"severity": "minor", "description": "problem", "idempotency_key": "one"}
    assert client.post(f"/api/tasks/{task.id}/defects", json=payload,
                       headers={"Authorization": f"Bearer {viewer_token}"}).status_code == 403
    created = client.post(f"/api/tasks/{task.id}/defects", json=payload,
                          headers={"Authorization": f"Bearer {admin_token}"})
    assert created.status_code == 201
    assert created.json()["defect"]["reporter"] == "alice"
    hidden = client.get("/api/defects", headers={"Authorization": f"Bearer {bob_token}"})
    assert hidden.status_code == 200
    assert hidden.json()["summary"]["total"] == 0


def test_cli_record_list_and_analyze_workflow(garden, monkeypatch):
    monkeypatch.chdir(garden)
    _closed(Store(garden))
    runner = CliRunner()
    recorded = runner.invoke(app, ["defect-record", "DM-001", "--severity", "minor",
                                   "--description", "Small regression", "--reporter", "alice",
                                   "--idempotency-key", "cli-1"])
    assert recorded.exit_code == 0, recorded.output
    defect_id = json.loads(recorded.output)["defect"]["id"]
    updated = runner.invoke(app, ["defect-update", defect_id, "--expected-revision", "1",
                                  "--actor", "alice", "--disposition", "reviewed",
                                  "--could-have-caught", "unit test"])
    assert updated.exit_code == 0, updated.output
    listed = runner.invoke(app, ["defect-list", "--task", "DM-001"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["summary"]["reviewed"] == 1
