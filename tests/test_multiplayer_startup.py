"""Local multiplayer startup remains explicit, idle, and side-effect free."""

from __future__ import annotations

import os

import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app as cli_app
from garden.multiplayer_client import AuthoritativeView, MultiplayerClient
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app


class _Coordinator:
    authenticate_local_session = MultiplayerClient.authenticate_local_session

    def __init__(self, role: str, assignment: dict | None = None):
        self.garden_id = "garden-1"
        self.member_id = "alex"
        self.installation_id = "alex-laptop"
        self.snapshot = {
            "garden_id": "garden-1", "protocol_version": 1, "member_id": self.member_id,
            "installation_id": self.installation_id, "role": role, "assignment": assignment,
            "project_visibility": "all", "projects": [],
            "authority": [], "projections": [], "cancellation_requests": [],
        }
        self.preparations = []

    def refresh(self, **_kwargs):
        return AuthoritativeView(self.snapshot, False)

    def prepare(self, *, mutation=False):
        self.preparations.append(mutation)
        return AuthoritativeView(self.snapshot, False)

    def projection_lag(self, _snapshot):
        return []


def _multiplayer_store(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    return Store(garden)


def _run(garden, *args):
    previous = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(cli_app, list(args))
    finally:
        os.chdir(previous)


def test_unenrolled_multiplayer_web_request_fails_closed(garden):
    client = TestClient(create_app(_multiplayer_store(garden), watch=False))

    response = client.get("/api/tasks")

    assert response.status_code == 503
    assert response.json() == {"detail": "multiplayer Git enrollment is incomplete"}


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


def test_viewer_serve_never_starts_embedded_scheduler(garden, monkeypatch):
    store = _multiplayer_store(garden)
    coordinator = _Coordinator("viewer")
    monkeypatch.setattr("garden.scheduler.MultiplayerClient.from_config", lambda _config: coordinator)
    monkeypatch.setattr("garden.web.common.MultiplayerClient.from_config", lambda _config: coordinator)

    app = create_app(store, watch=True)

    assert app.state.hub._watch_thread is None
    assert app.state.hub.scheduler_health()["embedded"] == "waiting"
    assert "Viewer session" in app.state.hub.scheduler_health()["effective"]["label"]


def test_viewer_serve_rejects_worker_ingress_and_controller_helpers(garden, monkeypatch):
    store = _multiplayer_store(garden)
    coordinator = _Coordinator("viewer")
    monkeypatch.setattr("garden.scheduler.MultiplayerClient.from_config", lambda _config: coordinator)
    monkeypatch.setattr("garden.web.common.MultiplayerClient.from_config", lambda _config: coordinator)
    monkeypatch.setattr(
        "garden.hosts.registry.authenticate_worker",
        lambda *_args: (_ for _ in ()).throw(AssertionError("viewer authenticated a worker")),
    )
    client = TestClient(create_app(store, watch=True))

    for method, path in (
        ("post", "/api/runs/claim"),
        ("post", "/maintenance/pause"),
        ("post", "/tick"),
        ("get", "/api/maintenance"),
        ("get", "/api/control/status"),
        ("get", "/api/workers"),
    ):
        assert getattr(client, method)(
            path, headers={"Authorization": "Bearer worker-credential"},
        ).status_code == 404
    assert True in coordinator.preparations
    assert False in coordinator.preparations

    assert client.get("/healthz").status_code == 200
