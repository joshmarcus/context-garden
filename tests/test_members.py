from __future__ import annotations

import hashlib
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from garden.members import MemberRegistry, Principal, authorize
from garden.runs import RunStore
from garden.scheduler import (
    MULTIPLAYER_EXECUTION_UNAVAILABLE,
    MultiplayerExecutionUnavailable,
    Scheduler,
)
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
    assert authorize(member, "mutate_work", owner_id="bob", project="demo") is False
    visible_member = Principal("g", "bob", "b", "member", "assigned", frozenset({"demo"}))
    assert authorize(visible_member, "mutate_work", owner_id="bob", project="demo")
    assert not authorize(visible_member, "mutate_work", owner_id="bob", project="private")
    assert not authorize(member, "mutate_work", owner_id="alice")
    assert not authorize(member, "mutate_work")
    assert not authorize(admin, "mutate_work")
    assert authorize(viewer, "read")
    assert not authorize(viewer, "mutate_work", owner_id="eve", project="demo")
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
    refused = client.post("/tick", headers={"Authorization": f"Bearer {admin_token}"},
                          follow_redirects=False)
    assert refused.status_code == 409
    assert refused.json()["detail"] == MULTIPLAYER_EXECUTION_UNAVAILABLE
    parts = admin_token.split(".")
    spoofed = ".".join([parts[0], "Z2FyZGVuLTI", *parts[2:]])
    assert client.post("/tick", headers={"Authorization": f"Bearer {spoofed}"}).status_code == 403


@pytest.mark.parametrize(
    ("workflow", "forbidden_calls"),
    [
        ("kickoff", ("require_maintenance_running", "_aux_list", "_new_local_run")),
        ("kickoff_now", ("file_kickoff",)),
        ("retro", ("require_maintenance_running", "_controller_lock", "_self_product")),
        ("trial", ("require_maintenance_running", "_manual_reserved", "dispatch")),
    ],
)
def test_direct_multiplayer_workflows_refuse_before_preparation(
    sched, monkeypatch, workflow, forbidden_calls
):
    sched.cfg.data["multiplayer"] = {"enabled": True}
    before = {
        path.relative_to(sched.store.root): path.read_bytes()
        for path in sched.store.root.rglob("*")
        if path.is_file()
    }

    def unexpected(*_args, **_kwargs):
        raise AssertionError("workflow performed preparation before checking execution authority")

    for name in forbidden_calls:
        monkeypatch.setattr(sched, name, unexpected)
    if workflow == "kickoff_now":
        monkeypatch.setattr("garden.planner.run_planner", unexpected)

    phase = sched.store.phase("demo", "p1")
    task = sched.store.task("DM-001")
    calls = {
        "kickoff": lambda: sched.start_kickoff(phase),
        "kickoff_now": lambda: sched.run_kickoff_now(phase),
        "retro": lambda: sched.start_retro(phase),
        "trial": lambda: sched.start_trial(task, ["claude:sonnet", "codex:gpt"]),
    }
    with pytest.raises(
        MultiplayerExecutionUnavailable,
        match="identity-less scheduling is disabled",
    ):
        calls[workflow]()

    after = {
        path.relative_to(sched.store.root): path.read_bytes()
        for path in sched.store.root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_multiplayer_administrator_reads_do_not_inherit_project_visibility(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    registry, admin_token, admin = _registry(garden)
    registry.add_member(admin, "bob", "member", "all")
    member_token = registry.issue_installation(admin, "bob", "bob-browser")
    registry.add_member(admin, "eve", "viewer", "all")
    viewer_token = registry.issue_installation(admin, "eve", "eve-browser")
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    administrator_reads = (
        "/api/workers", "/api/events", "/api/worker-diagnostics", "/api/control/status",
        "/now/workers", "/docs", "/design", "/design/report.pdf", "/trials",
    )
    for token in (member_token, viewer_token):
        headers = {"Authorization": f"Bearer {token}"}
        for method in ("GET", "HEAD", "OPTIONS"):
            response = client.request(method, "/config", headers=headers)
            assert response.status_code == 403, (method, response.status_code)
            assert "garden.yaml" not in response.text
        assert client.get("/config?product=demo", headers=headers).status_code == 403
        trailing = client.get("/config/", headers=headers, follow_redirects=False)
        assert trailing.status_code == 403
        assert trailing.headers.get("location") is None
        assert client.get("/board", headers=headers).status_code == 200
        for path in administrator_reads:
            assert client.get(path, headers=headers, follow_redirects=False).status_code == 403

    assert client.get("/config", headers=admin_headers).status_code == 200
    assert client.get("/config?product=demo", headers=admin_headers).status_code == 200
    for path in administrator_reads:
        assert client.get(path, headers=admin_headers, follow_redirects=False).status_code != 403


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

def test_multiplayer_https_accepts_only_its_same_origin_mutations(garden, monkeypatch):
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
    monkeypatch.setattr("ssl.SSLContext.load_cert_chain", lambda *_args: None)
    _registry_obj, admin_token, _admin = _registry(garden)
    client = TestClient(create_app(Store(garden), watch=False, host="garden.example", port=8765))
    auth = {"Authorization": f"Bearer {admin_token}"}

    response = client.post(
        "/tick", headers={**auth, "Origin": "https://garden.example:8765"},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == MULTIPLAYER_EXECUTION_UNAVAILABLE
    for origin in (
        "http://garden.example:8765",
        "https://garden.example:8766",
        "https://evil.example:8765",
    ):
        response = client.post(
            "/tick", headers={**auth, "Origin": origin}, follow_redirects=False,
        )
        assert response.status_code == 403


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
    for path in ("/", "/board", "/inbox", "/now"):
        assert client.get(path, headers=bob).status_code == 200
    assert client.get("/tasks/DM-001", headers=bob).status_code == 200
    assert client.post("/api/tasks/DM-001/manual-mode", headers=bob).status_code != 403
    assert client.post("/api/tasks/DM-001/manual-mode", headers=eve).status_code == 403


def test_project_neutral_pages_do_not_disclose_another_project(garden):
    private = garden / "private" / "secret-phase"
    (private / "tasks").mkdir(parents=True)
    (garden / "private" / "product.md").write_text("# PRIVATE_PRODUCT_MARKER\n")
    (private / "goals.md").write_text("# SECRET_PHASE_MARKER\n")
    (private / "tasks" / "PV-001-secret.md").write_text("""---
id: PV-001
title: PRIVATE_TASK_MARKER
status: ready
owner: bob
depends_on: []
priority: 1
reading: []
created: '2026-01-01T00:00:00+00:00'
updated: '2026-01-01T00:00:00+00:00'
---
PRIVATE_BODY_MARKER
""")
    events = garden / ".garden" / "events.jsonl"
    events.parent.mkdir()
    events.write_text(json.dumps({"at": "2026-01-02T00:00:00+00:00", "kind": "note",
                                  "task": "PV-001", "message": "PRIVATE_EVENT_MARKER"}) + "\n")
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["products"]["private"] = {
        "repo": "../repo", "base_branch": "main", "id_prefix": "PV",
        "github": "test/private",
    }
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    registry, _admin_token, admin = _registry(garden)
    registry.add_member(admin, "bob", "member", "assigned", ("demo",))
    token = registry.issue_installation(admin, "bob", "bob-browser")
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    headers = {"Authorization": f"Bearer {token}"}

    private_run = RunStore(garden / ".garden").new_run(
        "PV-001", "remote", mode="check", run_id="private-project-run",
    )
    private_run.env_snapshot = {"product": "private"}
    private_run.save()
    denied_claim = client.post(
        "/api/runs/claim", headers=headers,
        json={"host": "bob-browser", "claim_request_id": "private-project-claim"},
    )
    assert denied_claim.status_code == 204
    saved_private_run = RunStore(garden / ".garden").runs_for("PV-001")[0]
    assert saved_private_run.run_id == "private-project-run"
    assert saved_private_run.host == ""

    for path in (
        "/", "/inbox", "/board", "/board?product=demo", "/now", "/now1", "/now2",
        "/events", "/costs",
        "/runs", "/trellis", "/graph", "/herbarium", "/api/decisions",
        "/partials/now/head?burst=member-test",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, path
        if path not in {"/api/decisions", "/partials/now/head?burst=member-test"}:
            assert "demo" in response.text, path
        for marker in ("PRIVATE_PRODUCT_MARKER", "SECRET_PHASE_MARKER", "PRIVATE_TASK_MARKER",
                       "PRIVATE_BODY_MARKER", "PRIVATE_EVENT_MARKER", "test/private"):
            assert marker not in response.text, (path, marker)

    assert client.get("/board?product=private", headers=headers).status_code == 403
    assert client.get("/board?view=prs&product=private", headers=headers).status_code == 403
    assert client.get("/trellis?product=private", headers=headers).status_code == 403
    assert client.get("/costs?product=private", headers=headers).status_code == 403
    assert client.get("/tasks/PV-001", headers=headers).status_code == 403
    assert client.get("/api/operations/PV-001/private-run", headers=headers).status_code == 403
    assert client.get("/partials/tasks/DM-001/stdout", headers=headers).status_code == 200
    assert client.get("/partials/tasks/PV-001/stdout", headers=headers).status_code == 403
    assert client.get("/partials/runs/PV-001/private-run/stdout", headers=headers).status_code == 403
    assert client.post("/api/tasks/PV-001/manual-mode", headers=headers).status_code == 403
    assert client.post(
        "/api/control/tasks/PV-001/launch", headers=headers,
        json={"idempotency_key": "invisible-project", "expected_run_id": ""},
    ).status_code == 403
    assert client.post("/tasks/PV-001/cancel", headers=headers).status_code == 403

    with client.stream(
        "GET", "/now/stream?start=0&seconds=0.01", headers=headers,
    ) as response:
        stream_text = "".join(response.iter_text())
    assert response.status_code == 200
    assert "PRIVATE_EVENT_MARKER" not in stream_text


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


def test_member_worker_lifecycle_requires_current_project_visibility(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    task_path = next((garden / "demo" / "p1" / "tasks").glob("DM-001-*.md"))
    task_path.write_text(task_path.read_text().replace("status: ready", "status: ready\nowner: bob"))
    registry, _admin_token, admin = _registry(garden)
    registry.add_member(admin, "bob", "member", "assigned", ("demo",))
    token = registry.issue_installation(admin, "bob", "bob-worker")
    headers = {"Authorization": f"Bearer {token}"}
    runs = RunStore(garden / ".garden")
    run = runs.new_run("DM-001", "remote", mode="check", run_id="member-visible-run")
    run.env_snapshot = {"product": "demo"}
    run.save()
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))

    claim = client.post(
        "/api/runs/claim", headers=headers,
        json={"host": "bob-worker", "claim_request_id": "visible-project-claim"},
    )
    assert claim.status_code == 200
    lease_token = claim.json()["lease_token"]

    state = json.loads(registry.path.read_text())
    state["members"]["bob"]["projects"] = []
    registry.path.write_text(json.dumps(state))
    heartbeat = client.post(
        "/api/runs/member-visible-run/heartbeat", headers=headers,
        json={"lease_token": lease_token, "transcript": "must not persist"},
    )
    finish = client.post(
        "/api/runs/member-visible-run/finish", headers=headers,
        json={"lease_token": lease_token, "result": {}, "final_text": "must not persist"},
    )
    assert heartbeat.status_code == 403
    assert finish.status_code == 403
    assert not (run.path / "stdout.json").exists()
    assert not (run.path / "final.md").exists()
    assert not run.process_finished()


def test_multiplayer_worker_protocol_keeps_legacy_enrollment_credentials(garden):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    enrollment = garden / ".garden/hosts/enrollment/controller-hosts.json"
    enrollment.parent.mkdir(parents=True, mode=0o700)
    enrollment.write_text(json.dumps({"hosts": [{
        "name": "legacy", "token_sha256": hashlib.sha256(b"legacy-secret").hexdigest()
    }]}))
    enrollment.chmod(0o600)
    config.setdefault("workers", {})["enrollment_registry"] = str(enrollment)
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    _registry(garden)
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))

    response = client.post(
        "/api/runs/claim",
        headers={"Authorization": "Bearer legacy-secret"},
        json={"host": "legacy"},
    )
    assert response.status_code == 204


def test_multiplayer_watch_tick_and_direct_dispatch_fail_closed_for_all_owners(garden):
    first_path = next((garden / "demo" / "p1" / "tasks").glob("DM-001-*.md"))
    first_path.write_text(first_path.read_text().replace("status: ready", "status: ready\nowner: alice"))
    second_path = next((garden / "demo" / "p1" / "tasks").glob("DM-002-*.md"))
    second_path.write_text(second_path.read_text().replace("status: ready", "status: ready\nowner: bob"))
    legacy_scheduler = Scheduler(Store(garden))
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    registry, admin_token, _admin = _registry(garden)

    with TestClient(create_app(Store(garden), watch=True, host="testserver")) as client:
        hub = client.app.state.hub
        assert hub._watch_thread is None
        assert hub.scheduler_health()["embedded_health"] == {
            "kind": "waiting",
            "label": MULTIPLAYER_EXECUTION_UNAVAILABLE,
            "state": "waiting",
        }
        response = client.post(
            "/tick",
            headers={"Authorization": f"Bearer {admin_token}"},
            follow_redirects=False,
        )
        assert response.status_code == 409

    scheduler = Scheduler(Store(garden))
    with pytest.raises(RuntimeError, match="identity-less scheduling is disabled"):
        legacy_scheduler.tick()
    with pytest.raises(RuntimeError, match="identity-less scheduling is disabled"):
        scheduler.tick()
    with pytest.raises(RuntimeError, match="identity-less scheduling is disabled"):
        scheduler.dispatch(scheduler.store.task("DM-002"))
    assert {task.id: task.status.value for task in Store(garden).tasks().values()} == {
        "DM-001": "ready",
        "DM-002": "ready",
    }
    assert RunStore(garden / ".garden").active() == []


def test_legacy_loopback_behavior_is_unchanged(garden):
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    assert client.get("/api/tasks").status_code == 200
    assert client.post("/tick", follow_redirects=False).status_code == 303
