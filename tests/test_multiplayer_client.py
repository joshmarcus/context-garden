from __future__ import annotations

import datetime as dt
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

    def __call__(self, method, url, **kwargs):
        if not self.online:
            raise httpx.ConnectError("offline")
        if method == "GET":
            return response(200, self.snapshot)
        self.posts.append(kwargs["json"])
        if url.endswith("/claims"):
            body = kwargs["json"]
            return response(200, {
                "garden_id": "garden", "kind": body["kind"], "scope": body["scope"],
                "owner_id": "alice", "authority_generation": body["authority_generation"],
                "installation_id": "alice-a", "operation_id": body["operation_id"],
                "fence": 1, "lease_expires_at": "2026-09-12T00:02:00+00:00",
            })
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


def test_transition_refreshes_syncs_claims_and_commits_before_local_write(tmp_path):
    path = "demo/p1/tasks/CG-1-task.md"
    original = "old\n"
    snapshot = snapshot_identity({
        "protocol_version": 1, "garden_id": "garden",
        "authority": [{"kind": "task", "scope": "CG-1", "version": 3,
                       "owner": "alice", "authority_generation": 7}],
        "projections": [{"kind": "task", "scope": "CG-1", "version": 3,
                         "path": path, "markdown": original,
                         "base_revision": hashlib.sha256(b"").hexdigest()}],
    })
    service = Service(snapshot)
    local = client(tmp_path, service)

    result = local.transition(
        kind="task", scope="CG-1", new_state="running", markdown="new\n", path=path,
        canonical_revision=hashlib.sha256(original.encode()).hexdigest(),
    )

    assert result == {"version": 4}
    assert (tmp_path / path).read_text() == original
    assert service.posts[0]["accepted_owner"] == "alice"
    assert service.posts[1]["claim"]["installation_id"] == "alice-a"
    assert service.posts[1]["canonical_revision"] == hashlib.sha256(original.encode()).hexdigest()

    service.online = False
    with pytest.raises(MultiplayerUnavailable, match="unavailable"):
        local.transition(
            kind="task", scope="CG-1", new_state="done", markdown="offline\n", path=path,
            canonical_revision=hashlib.sha256(original.encode()).hexdigest(),
        )
    assert (tmp_path / path).read_text() == original


class LeaseService(Service):
    def __init__(self, snapshot: dict):
        super().__init__(snapshot)
        self.claims = 0
        self.reject_next_effect = False

    def __call__(self, method, url, **kwargs):
        if method == "GET":
            return super().__call__(method, url, **kwargs)
        body = kwargs["json"]
        self.posts.append(body)
        if url.endswith("/claims"):
            self.claims += 1
            return response(200, {
                "garden_id": "garden", "kind": body["kind"], "scope": body["scope"],
                "owner_id": body["accepted_owner"],
                "authority_generation": body["authority_generation"],
                "installation_id": "alice-a", "operation_id": body["operation_id"],
                "fence": self.claims,
                "lease_expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=2)).isoformat(),
            })
        if url.endswith("/effects") and self.reject_next_effect:
            self.reject_next_effect = False
            return response(409, {"detail": "stale or expired fencing lease"})
        return response(200, {"status": "pending"})


def test_expired_dispatch_claim_is_renewed_before_later_reap_effect(tmp_path):
    service = LeaseService(snapshot_identity({
        "protocol_version": 1, "garden_id": "garden", "projections": [],
        "authority": [{"kind": "task", "scope": "CG-1", "version": 3}],
    }))
    local = client(tmp_path, service)

    with local.effect(kind="task", scope="CG-1", owner_id="alice",
                      authority_generation=2, expected_version=3, effect_key="dispatch:CG-1"):
        pass
    local._claims[("task", "CG-1")]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    with local.effect(kind="task", scope="CG-1", owner_id="alice",
                      authority_generation=2, expected_version=3, effect_key="reap:CG-1"):
        pass

    assert service.claims == 2
    assert local._claims[("task", "CG-1")]["fence"] == 2


def test_stale_effect_rejection_invalidates_and_reacquires_cached_claim(tmp_path):
    service = LeaseService(snapshot_identity({
        "protocol_version": 1, "garden_id": "garden", "projections": [],
        "authority": [{"kind": "task", "scope": "CG-1", "version": 3}],
    }))
    local = client(tmp_path, service)
    local.claim(kind="task", scope="CG-1", owner_id="alice",
                authority_generation=2, expected_version=3)
    service.reject_next_effect = True

    with local.effect(kind="task", scope="CG-1", owner_id="alice",
                      authority_generation=2, expected_version=3, effect_key="reap:CG-1"):
        pass

    assert service.claims == 2
    assert local._claims[("task", "CG-1")]["fence"] == 2

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
