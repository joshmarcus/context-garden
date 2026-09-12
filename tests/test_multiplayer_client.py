from __future__ import annotations

import hashlib

import httpx
import pytest

from garden.config import Config
from garden.multiplayer_client import MultiplayerClient, MultiplayerUnavailable, ProjectionConflict
from garden.runner.base import scrubbed_env


def response(status: int, payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "https://coordinator.test")
    return httpx.Response(status, json=payload, request=request)


class Service:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot
        self.online = True
        self.posts = []

    def __call__(self, method, _url, **kwargs):
        if not self.online:
            raise httpx.ConnectError("offline")
        if method == "GET":
            return response(200, self.snapshot)
        self.posts.append(kwargs["json"])
        return response(200, {"version": kwargs["json"]["expected_version"] + 1})


def client(root, service, installation="alice-a"):
    return MultiplayerClient(
        root=root, garden_id="garden", endpoint="https://coordinator.test",
        credential="private", member_id="alice", installation_id=installation,
        request=service,
    )


def snapshot_identity(value: dict, installation="alice-a") -> dict:
    return {**value, "member_id": "alice", "installation_id": installation, "role": "member"}


def test_enrollment_uses_local_overlay_and_environment_credential(tmp_path, monkeypatch):
    (tmp_path / "garden.yaml").write_text("name: shared\n")
    (tmp_path / "garden.local.yaml").write_text("""
multiplayer:
  enabled: true
  garden_id: garden
  coordinator_url: https://coordinator.test
  member_id: alice
  installation_id: alice-a
  credential_env: ALICE_GARDEN_TOKEN
""")
    monkeypatch.setenv("ALICE_GARDEN_TOKEN", "not-in-config")

    enrolled = MultiplayerClient.from_config(Config.load(tmp_path))

    assert enrolled is not None
    assert enrolled.installation_id == "alice-a"
    assert "not-in-config" not in repr(Config.load(tmp_path).data)
    monkeypatch.setenv("SAFE_VALUE", "yes")
    worker = scrubbed_env({
        "worker_env": {"pass": ["*_TOKEN", "SAFE_VALUE"]},
        "multiplayer": {"credential_env": "ALICE_GARDEN_TOKEN"},
    })
    assert "ALICE_GARDEN_TOKEN" not in worker and worker["SAFE_VALUE"] == "yes"


def test_two_roots_keep_stale_reads_but_refuse_offline_commands(tmp_path):
    snapshot = snapshot_identity({
        "protocol_version": 1, "garden_id": "garden", "projections": [],
        "authority": [{"kind": "task", "scope": "CG-1", "version": 3}],
    })
    service = Service(snapshot)
    first = client(tmp_path / "alice", service, "alice-a")
    service.snapshot = snapshot_identity(snapshot, "alice-b")
    second = client(tmp_path / "alice-laptop", service, "alice-b")
    service.snapshot = snapshot
    assert not first.refresh().stale
    service.snapshot = snapshot_identity(snapshot, "alice-b")
    assert not second.refresh().stale

    service.online = False
    assert first.refresh().stale and second.refresh().stale
    with pytest.raises(MultiplayerUnavailable, match="unavailable"):
        first.command("/transitions", {}, kind="task", scope="CG-1", expected_version=3)
    assert service.posts == []


def test_commands_require_fresh_matching_revision_and_projection_sync_is_non_destructive(tmp_path):
    original = "local canonical\n"
    projected = "authoritative\n"
    path = "demo/p1/tasks/CG-1-task.md"
    snapshot = snapshot_identity({
        "protocol_version": 1, "garden_id": "garden",
        "authority": [{"kind": "task", "scope": "CG-1", "version": 2}],
        "projections": [{"kind": "task", "scope": "CG-1", "version": 2,
                         "path": path, "markdown": projected,
                         "base_revision": hashlib.sha256(original.encode()).hexdigest()}],
    })
    service = Service(snapshot)
    local = client(tmp_path, service)
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(original)

    assert local.projection_lag(snapshot) == ["task:CG-1@2"]
    assert local.synchronize(snapshot) == [path]
    assert local.projection_lag(snapshot) == []
    assert target.read_text() == projected
    target.write_text("authored locally\n")
    snapshot["projections"][0].update(version=3, markdown="new authority\n")
    snapshot["authority"][0]["version"] = 3
    with pytest.raises(ProjectionConflict, match="conflicts"):
        local.synchronize(snapshot)
    assert target.read_text() == "authored locally\n"

    with pytest.raises(MultiplayerUnavailable, match="stale task revision"):
        local.command("/transitions", {}, kind="task", scope="CG-1", expected_version=2)
    assert local.command(
        "/transitions", {}, kind="task", scope="CG-1", expected_version=3,
    ) == {"version": 4}


def test_snapshot_rejects_garden_and_protocol_mismatch_and_reports_projection_lag(tmp_path):
    service = Service(snapshot_identity({"protocol_version": 1, "garden_id": "other"}))
    with pytest.raises(MultiplayerUnavailable, match="different garden"):
        client(tmp_path, service).refresh()
    service.snapshot = snapshot_identity({
        "protocol_version": 1, "garden_id": "garden",
        "authority": [{"kind": "task", "scope": "CG-1", "version": 4}],
        "projections": [{"kind": "task", "scope": "CG-1", "version": 3}],
    })
    assert client(tmp_path, service).refresh().projection_lag() == ["task:CG-1@4"]


def test_stale_cache_is_bound_to_the_authenticated_installation(tmp_path):
    service = Service(snapshot_identity({
        "protocol_version": 1, "garden_id": "garden", "authority": [], "projections": [],
    }))
    client(tmp_path, service).refresh()
    service.online = False

    with pytest.raises(MultiplayerUnavailable, match="unavailable"):
        client(tmp_path, service, "alice-b").refresh()


def test_cancellation_is_acknowledged_only_after_local_worker_stops(tmp_path):
    state = snapshot_identity({
        "protocol_version": 1, "garden_id": "garden", "authority": [], "projections": [],
        "cancellation_requests": [{"kind": "task", "scope": "CG-1",
                                   "installation": "alice-a", "fence": 7}],
    })

    class CancellationService(Service):
        def __call__(self, method, _url, **kwargs):
            if method == "GET":
                return response(200, self.snapshot)
            self.posts.append(kwargs["json"])
            return response(200, {"status": "acknowledged"})

    service = CancellationService(state)
    local = client(tmp_path, service)
    assert local.acknowledge_cancellations(state, lambda _kind, _scope: False) == []
    assert service.posts == []
    assert local.acknowledge_cancellations(state, lambda kind, scope: (kind, scope)
                                           == ("task", "CG-1")) == ["task:CG-1"]
    assert service.posts == [{"kind": "task", "scope": "CG-1", "fence": 7}]
