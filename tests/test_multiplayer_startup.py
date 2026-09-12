"""Local multiplayer startup remains explicit, idle, and side-effect free."""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from garden.members import MemberRegistry
from garden.multiplayer_client import AuthoritativeView
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app


class _Coordinator:
    def __init__(self, role: str, assignment: dict | None = None):
        self.member_id = "alex"
        self.installation_id = "alex-laptop"
        self.snapshot = {
            "garden_id": "garden-1", "protocol_version": 1, "member_id": self.member_id,
            "installation_id": self.installation_id, "role": role, "assignment": assignment,
            "authority": [], "projections": [], "cancellation_requests": [],
        }

    def refresh(self, **_kwargs):
        return AuthoritativeView(self.snapshot, False)

    def projection_lag(self, _snapshot):
        return []


def _multiplayer_store(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    return Store(garden)


def test_unassigned_member_tick_does_not_create_scheduler_state(garden, monkeypatch):
    store = _multiplayer_store(garden)
    client = _Coordinator("member")
    monkeypatch.setattr("garden.scheduler.MultiplayerClient.from_config", lambda _config: client)
    before = {path.relative_to(garden): path.read_bytes() for path in garden.rglob("*") if path.is_file()}

    scheduler = Scheduler(store)
    assert scheduler.execution_status() == {"state": "unassigned", "label": "No work assignment"}
    assert scheduler.tick().changed is False

    after = {path.relative_to(garden): path.read_bytes() for path in garden.rglob("*") if path.is_file()}
    assert after == before


def test_unassigned_member_ui_names_idle_execution_scope(garden, monkeypatch):
    store = _multiplayer_store(garden)
    coordinator = _Coordinator("member")
    monkeypatch.setattr("garden.scheduler.MultiplayerClient.from_config", lambda _config: coordinator)
    monkeypatch.setattr("garden.web.common.MultiplayerClient.from_config", lambda _config: coordinator)
    registry = MemberRegistry(garden / ".garden")
    admin_token = registry.enroll_administrator("garden-1", "admin", "admin-laptop")
    admin = registry.authenticate(admin_token)
    assert admin is not None
    registry.add_member(admin, "alex", "member")
    token = registry.issue_installation(admin, "alex", "alex-laptop")

    response = TestClient(create_app(store, watch=False)).get(
        "/", headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert "No work assignment" in response.text
    assert "identity: alex (member)" in response.text
    assert "viewing: All authorized projects" in response.text
    assert "execution: No work assignment" in response.text


def test_viewer_serve_never_starts_embedded_scheduler(garden, monkeypatch):
    store = _multiplayer_store(garden)
    coordinator = _Coordinator("viewer")
    monkeypatch.setattr("garden.scheduler.MultiplayerClient.from_config", lambda _config: coordinator)
    monkeypatch.setattr("garden.web.common.MultiplayerClient.from_config", lambda _config: coordinator)

    app = create_app(store, watch=True)

    assert app.state.hub._watch_thread is None
    assert app.state.hub.scheduler_health()["embedded"] == "waiting"
    assert "Viewer session" in app.state.hub.scheduler_health()["effective"]["label"]
