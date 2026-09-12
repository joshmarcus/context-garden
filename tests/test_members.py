from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from garden.members import MemberRegistry, Principal, authorize
from garden.store import Store
from garden.web.app import create_app, multiplayer_tls_files


def _registry(tmp_path):
    registry = MemberRegistry(tmp_path / ".garden")
    token = registry.enroll_administrator("garden-1", "alice", "alice-laptop")
    principal = registry.authenticate(token)
    assert principal is not None
    return registry, token, principal


def test_enrollment_credentials_are_private_stable_and_garden_bound(tmp_path):
    registry, token, alice = _registry(tmp_path)
    state = json.loads(registry.path.read_text())

    assert alice == Principal("garden-1", "alice", "alice-laptop", "administrator", "all")
    assert token not in registry.path.read_text()
    assert "verifier" in state["installations"]["alice-laptop"]
    assert registry.path.stat().st_mode & 0o777 == 0o600
    parts = token.split(".")
    wrong_garden = ".".join([parts[0], "b3RoZXItZ2FyZGVu", *parts[2:]])
    assert registry.authenticate(wrong_garden) is None
    with pytest.raises(PermissionError, match="empty registry"):
        registry.enroll_administrator("garden-1", "mallory", "other")


def test_disabled_members_and_revoked_or_rotated_installations_are_rejected(tmp_path):
    registry, _token, alice = _registry(tmp_path)
    registry.add_member(alice, "bob", "member", "assigned")
    old = registry.issue_installation(alice, "bob", "bob-desktop")
    bob = registry.authenticate(old)
    assert bob and bob.project_visibility == "assigned"

    new = registry.rotate_installation(bob, "bob-desktop")
    assert registry.authenticate(old) is None
    bob = registry.authenticate(new)
    assert bob is not None
    registry.revoke_installation(bob, "bob-desktop")
    assert registry.authenticate(new) is None

    second = registry.issue_installation(alice, "bob", "bob-phone")
    registry.set_member_active(alice, "bob", False)
    assert registry.authenticate(second) is None


def test_assigned_visibility_records_concrete_projects(tmp_path):
    registry, _token, alice = _registry(tmp_path)
    registry.add_member(alice, "bob", "member", "assigned", ("demo", "docs"))
    token = registry.issue_installation(alice, "bob", "bob-desktop")
    bob = registry.authenticate(token)

    assert bob and bob.projects == frozenset({"demo", "docs"})
    assert authorize(bob, "read", project="demo")
    assert not authorize(bob, "read", project="private")


def test_roles_separate_administration_owned_work_and_viewing():
    admin = Principal("g", "alice", "a", "administrator", "all")
    member = Principal("g", "bob", "b", "member", "assigned")
    viewer = Principal("g", "eve", "e", "viewer", "all")

    assert authorize(admin, "administer")
    assert not authorize(admin, "mutate_work", owner_id="bob")
    assert authorize(member, "mutate_work", owner_id="bob")
    assert not authorize(member, "mutate_work", owner_id="alice")
    assert not authorize(member, "mutate_work")
    assert not authorize(admin, "mutate_work")
    assert authorize(viewer, "read")
    assert not authorize(viewer, "mutate_work")
    assert not authorize(member, "read", owner_id="alice")


def test_multiplayer_web_boundary_rejects_spoofing_and_enforces_roles(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    store = Store(garden)
    registry, admin_token, admin = _registry(garden)
    registry.add_member(admin, "viewer", "viewer")
    viewer_token = registry.issue_installation(admin, "viewer", "viewer-browser")
    client = TestClient(create_app(store, watch=False, host="testserver"))

    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/tasks", headers={"Authorization": f"Bearer {viewer_token}"}).status_code == 200
    direct = client.post("/tick", headers={"Authorization": f"Bearer {viewer_token}"},
                         follow_redirects=False)
    assert direct.status_code == 403
    assert client.post("/tick", headers={"Authorization": f"Bearer {admin_token}"},
                       follow_redirects=False).status_code == 303
    parts = admin_token.split(".")
    spoofed = ".".join([parts[0], "Z2FyZGVuLTI", *parts[2:]])
    assert client.post("/tick", headers={"Authorization": f"Bearer {spoofed}"}).status_code == 403


def test_multiplayer_nonlocal_listener_requires_https_transport(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    with pytest.raises(RuntimeError, match="HTTPS"):
        create_app(Store(garden), watch=False, host="0.0.0.0")

    config["multiplayer"]["transport"] = "https"
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    with pytest.raises(RuntimeError, match="tls_certfile"):
        create_app(Store(garden), watch=False, host="0.0.0.0")


def test_multiplayer_https_validates_configured_certificate_pair(garden, monkeypatch):
    cert = garden / "private/server.crt"
    key = garden / "private/server.key"
    cert.parent.mkdir()
    cert.write_text("certificate")
    key.write_text("private key")
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {
        "enabled": True,
        "transport": "https",
        "tls_certfile": "private/server.crt",
        "tls_keyfile": "private/server.key",
    }
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    loaded = []
    monkeypatch.setattr("ssl.SSLContext.load_cert_chain",
                        lambda _self, certfile, keyfile: loaded.append((certfile, keyfile)))

    assert multiplayer_tls_files(Store(garden), "0.0.0.0") == (str(cert), str(key))
    assert loaded == [(str(cert), str(key))]


def test_multiplayer_filters_project_reads_and_allows_owned_api_actions(garden):
    task_path = next((garden / "demo" / "p1" / "tasks").glob("DM-001-*.md"))
    task_path.write_text(task_path.read_text().replace("status: ready", "status: ready\nowner: bob"))
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    registry, _admin_token, admin = _registry(garden)
    registry.add_member(admin, "bob", "member", "assigned", ("demo",))
    bob_token = registry.issue_installation(admin, "bob", "bob-browser")
    registry.add_member(admin, "eve", "viewer", "assigned", ())
    eve_token = registry.issue_installation(admin, "eve", "eve-browser")
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))

    bob = {"Authorization": f"Bearer {bob_token}"}
    eve = {"Authorization": f"Bearer {eve_token}"}
    rows = client.get("/api/tasks", headers=bob).json()
    assert rows and {row["product"] for row in rows} == {"demo"}
    assert client.get("/api/tasks", headers=eve).json() == []
    assert client.get("/config", headers=bob).status_code == 403
    assert client.get("/tasks/DM-001", headers=bob).status_code == 200
    assert client.post("/api/tasks/DM-001/manual-mode", headers=bob).status_code != 403
    assert client.post("/api/tasks/DM-001/manual-mode", headers=eve).status_code == 403


def test_multiplayer_worker_protocol_uses_member_bound_installation(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    registry, admin_token, _admin = _registry(garden)
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    auth = {"Authorization": f"Bearer {admin_token}"}

    assert client.post("/api/runs/claim", headers=auth,
                       json={"host": "alice-laptop"}).status_code == 204
    assert client.post("/api/runs/claim", headers=auth,
                       json={"host": "spoofed"}).status_code == 403


def test_legacy_loopback_behavior_is_unchanged(garden):
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    assert client.get("/api/tasks").status_code == 200
    assert client.post("/tick", follow_redirects=False).status_code == 303
