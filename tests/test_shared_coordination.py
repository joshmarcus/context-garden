from __future__ import annotations

import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient

from garden.coordination import Conflict, Coordinator, ProtocolMismatch
from garden.coordination_api import create_coordination_app
from garden.members import MemberRegistry, Principal


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)

    def __call__(self):
        return self.now


def principals():
    admin = Principal("garden", "admin", "admin-box", "administrator", "all")
    alice_a = Principal("garden", "alice", "alice-a", "member", "all")
    alice_b = Principal("garden", "alice", "alice-b", "member", "all")
    bob = Principal("garden", "bob", "bob-a", "member", "all")
    return admin, alice_a, alice_b, bob


def authority_and_claim(coordinator, admin, actor, *, kind="task", scope="CG-1",
                        authority_generation=4, prefix="one"):
    authority = coordinator.set_authority(
        admin, garden_id="garden", kind=kind, scope=scope, owner_id=actor.member_id,
        authority_generation=authority_generation, expected_version=0,
        operation_id=f"{prefix}-authority",
    )
    claim = coordinator.claim(
        actor, garden_id="garden", kind=kind, scope=scope,
        expected_version=authority["version"], accepted_owner=actor.member_id,
        authority_generation=authority_generation, operation_id=f"{prefix}-claim",
    )
    return authority, claim


def test_claim_is_authenticated_versioned_fenced_and_idempotent(tmp_path):
    admin, alice_a, alice_b, bob = principals()
    clock = Clock()
    coordinator = Coordinator(tmp_path / "coordination.db", clock=clock)
    authority, claim = authority_and_claim(coordinator, admin, alice_a)

    duplicate = coordinator.claim(
        alice_a, garden_id="garden", kind="task", scope="CG-1",
        expected_version=authority["version"], accepted_owner="alice",
        authority_generation=4, operation_id="one-claim",
    )
    assert duplicate == claim
    assert claim.fence == 1 and claim.installation_id == "alice-a"
    with pytest.raises(Conflict, match="waiting"):
        coordinator.claim(
            alice_b, garden_id="garden", kind="task", scope="CG-1",
            expected_version=1, accepted_owner="alice", authority_generation=4,
            operation_id="other-installation",
        )
    with pytest.raises(PermissionError):
        coordinator.claim(
            bob, garden_id="garden", kind="task", scope="CG-1", expected_version=1,
            accepted_owner="alice", authority_generation=4, operation_id="spoof",
        )
    with pytest.raises(ProtocolMismatch, match="requires 1"):
        coordinator.snapshot(alice_a, "garden", protocol_version=2)


def test_concurrent_installations_admit_only_one_claim(tmp_path):
    admin, alice_a, alice_b, _bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    coordinator.set_authority(
        admin, garden_id="garden", kind="task", scope="CG-1", owner_id="alice",
        authority_generation=1, expected_version=0, operation_id="authority",
    )
    barrier = threading.Barrier(2)
    results = []

    def compete(actor, operation):
        barrier.wait()
        try:
            results.append(coordinator.claim(
                actor, garden_id="garden", kind="task", scope="CG-1", expected_version=1,
                accepted_owner="alice", authority_generation=1, operation_id=operation,
            ))
        except Conflict as exc:
            results.append(exc)

    threads = [threading.Thread(target=compete, args=(alice_a, "a")),
               threading.Thread(target=compete, args=(alice_b, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, Conflict) for result in results) == 1


def test_server_clock_expiry_restart_and_stale_fences(tmp_path):
    admin, alice, _alice_b, _bob = principals()
    clock = Clock()
    path = tmp_path / "coordination.db"
    coordinator = Coordinator(path, clock=clock)
    _authority, old = authority_and_claim(coordinator, admin, alice)
    clock.now += dt.timedelta(seconds=121)
    restarted = Coordinator(path, clock=clock)
    new = restarted.claim(
        alice, garden_id="garden", kind="task", scope="CG-1", expected_version=1,
        accepted_owner="alice", authority_generation=4, operation_id="replacement",
    )
    assert new.fence == 2
    with pytest.raises(Conflict, match="stale or expired"):
        restarted.transition(
            alice, old, expected_version=1, new_state="doing", markdown="old",
            operation_id="stale-transition",
        )


def test_transition_outbox_is_recoverable_and_stale_projection_is_blocked(tmp_path):
    admin, alice, _alice_b, _bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    _authority, claim = authority_and_claim(coordinator, admin, alice)
    result = coordinator.transition(
        alice, claim, expected_version=1, new_state="doing", markdown="version two",
        operation_id="transition-1", evidence={"id": "run-1", "result": "immutable"},
    )
    duplicate = coordinator.transition(
        alice, claim, expected_version=1, new_state="doing", markdown="version two",
        operation_id="transition-1", evidence={"id": "run-1", "result": "immutable"},
    )
    assert result == duplicate == {"version": 2, "outbox_status": "pending"}
    pending = coordinator.pending_outbox("garden")
    assert {row["effect_kind"] for row in pending} == {"task_transition", "git_projection"}
    projection = next(row for row in pending if row["effect_kind"] == "git_projection")
    coordinator.finish_outbox(
        admin, garden_id="garden", outbox_id=projection["id"], authority_version=2,
        success=False, error="git unavailable",
    )
    failed = next(row for row in coordinator.pending_outbox("garden")
                  if row["effect_kind"] == "git_projection")
    assert failed["operation_id"] == "transition-1"
    assert failed["attempts"] == 1 and failed["last_error"] == "git unavailable"

    # A later authoritative transition makes the older Markdown ineligible to publish.
    coordinator.transition(
        alice, claim, expected_version=2, new_state="review", markdown="version three",
        operation_id="transition-2",
    )
    with pytest.raises(Conflict, match="stale Markdown"):
        coordinator.finish_outbox(
            admin, garden_id="garden", outbox_id=projection["id"], authority_version=2,
            success=True,
        )


def test_unknown_provider_effect_blocks_retry_until_reconciliation(tmp_path):
    admin, alice, _alice_b, _bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    _authority, claim = authority_and_claim(coordinator, admin, alice)
    started = coordinator.begin_effect(
        alice, claim, provider="github", effect_key="publish:CG-1", operation_id="publish-1",
        credential_scope="pull_requests:write", precondition="head=abc", request={"head": "abc"},
    )
    assert started["status"] == "pending"
    coordinator.finish_effect(alice, "garden", "publish-1", outcome="unknown")
    with pytest.raises(Conflict, match="reconciliation required"):
        coordinator.begin_effect(
            alice, claim, provider="github", effect_key="publish:CG-1",
            operation_id="publish-2", credential_scope="pull_requests:write",
            precondition="head=abc", request={"head": "abc"},
        )
    snapshot = coordinator.snapshot(admin, "garden")
    assert snapshot["member_id"] == "admin" and snapshot["installation_id"] == "admin-box"
    assert snapshot["active_claims"][0]["installation"] == "alice-a"
    assert snapshot["blocking_effects"] == [
        {"provider": "github", "effect_key": "publish:CG-1", "status": "unknown"}
    ]
    # The ledger records the requested narrow scope, never credential material.
    assert "secret-delegated-token" not in (
        tmp_path / "coordination.db"
    ).read_bytes().decode(errors="ignore")


def test_capacity_and_spend_reservations_are_atomic(tmp_path):
    _admin, alice_a, alice_b, _bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    barrier = threading.Barrier(2)
    results = []

    def reserve(actor, operation):
        barrier.wait()
        try:
            results.append(coordinator.reserve(
                actor, garden_id="garden", pool="phase-10", operation_id=operation,
                units=1, spend_micros=600, unit_limit=1, spend_limit_micros=1000,
            ))
        except Conflict as exc:
            results.append(exc)

    threads = [threading.Thread(target=reserve, args=(alice_a, "a")),
               threading.Thread(target=reserve, args=(alice_b, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, Conflict) for result in results) == 1


def test_phase_claim_is_exclusive_and_reassignment_invalidates_children(tmp_path):
    admin, alice, _alice_b, bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    _authority, phase_claim = authority_and_claim(
        coordinator, admin, alice, kind="phase", scope="demo/phase-10", prefix="phase",
    )
    with pytest.raises(Conflict, match="waiting"):
        coordinator.claim(
            alice, garden_id="garden", kind="phase", scope="demo/phase-10",
            expected_version=1, accepted_owner="alice", authority_generation=4,
            operation_id="second-workflow",
        )
    changed = coordinator.set_authority(
        admin, garden_id="garden", kind="phase", scope="demo/phase-10", owner_id="bob",
        authority_generation=5, expected_version=1, operation_id="phase-reassign",
    )
    with pytest.raises(Conflict, match="stale or expired"):
        coordinator.begin_effect(
            alice, phase_claim, provider="github", effect_key="phase-review",
            operation_id="child-review", credential_scope="pull_requests:read",
            precondition="", request={},
        )
    request = coordinator.snapshot(alice, "garden")["cancellation_requests"][0]
    coordinator.acknowledge_cancellation(
        alice, garden_id="garden", kind="phase", scope="demo/phase-10",
        fence=request["fence"],
    )
    bob_claim = coordinator.claim(
        bob, garden_id="garden", kind="phase", scope="demo/phase-10",
        expected_version=changed["version"], accepted_owner="bob", authority_generation=5,
        operation_id="bob-phase",
    )
    assert bob_claim.fence == 2
    reservation = coordinator.reserve_phase(
        bob, bob_claim, pool="reviews", operation_id="review-capacity", units=1,
        spend_micros=100, unit_limit=1, spend_limit_micros=100,
    )
    assert reservation["status"] == "active"
    with pytest.raises(PermissionError, match="global"):
        coordinator.reserve(
            bob, garden_id="garden", pool="global:publishing", operation_id="global",
            units=1, spend_micros=0, unit_limit=1, spend_limit_micros=0,
        )


def test_handoff_waits_for_worker_and_original_provider_operation(tmp_path):
    admin, alice, _alice_b, bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    _authority, old = authority_and_claim(coordinator, admin, alice)
    coordinator.begin_effect(
        alice, old, provider="github", effect_key="publish:CG-1", operation_id="publish-old",
        credential_scope="pull_requests:write", precondition="head=old", request={"head": "old"},
    )
    changed = coordinator.set_authority(
        admin, garden_id="garden", kind="task", scope="CG-1", owner_id="bob",
        authority_generation=5, expected_version=1, operation_id="handoff",
    )
    view = coordinator.snapshot(bob, "garden")
    assert view["handoffs"][0]["status"] == "reconciling"
    assert view["cancellation_requests"][0]["installation"] == "alice-a"
    with pytest.raises(Conflict, match="old workers.*provider outcomes"):
        coordinator.claim(
            bob, garden_id="garden", kind="task", scope="CG-1",
            expected_version=changed["version"], accepted_owner="bob",
            authority_generation=5, operation_id="bob-too-soon",
        )

    coordinator.acknowledge_cancellation(
        alice, garden_id="garden", kind="task", scope="CG-1", fence=old.fence,
    )
    with pytest.raises(Conflict, match="provider outcomes"):
        coordinator.claim(
            bob, garden_id="garden", kind="task", scope="CG-1",
            expected_version=changed["version"], accepted_owner="bob",
            authority_generation=5, operation_id="bob-still-too-soon",
        )
    coordinator.finish_effect(
        admin, "garden", "publish-old", outcome="succeeded", result={"pr": 17},
    )
    resumed = coordinator.claim(
        bob, garden_id="garden", kind="task", scope="CG-1",
        expected_version=changed["version"], accepted_owner="bob",
        authority_generation=5, operation_id="bob-resume",
    )
    assert resumed.owner_id == "bob" and resumed.fence == 2
    assert coordinator.snapshot(bob, "garden")["handoffs"][0]["status"] == "ready"


def test_unassignment_and_late_result_preserve_stale_evidence_without_transition(tmp_path):
    admin, alice, _alice_b, _bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    _authority, old = authority_and_claim(coordinator, admin, alice)
    changed = coordinator.set_authority(
        admin, garden_id="garden", kind="task", scope="CG-1", owner_id="",
        authority_generation=5, expected_version=1, operation_id="unassign",
    )
    with pytest.raises(Conflict, match="stale or expired"):
        coordinator.transition(
            alice, old, expected_version=changed["version"], new_state="review",
            markdown="late source", operation_id="late-transition",
        )
    coordinator.retain_stale_evidence(
        alice, garden_id="garden", kind="task", scope="CG-1", evidence_id="late-run",
        operation_id="late-result", payload={"result": "done", "head": "old"},
    )
    view = coordinator.snapshot(admin, "garden")
    assert view["authority"][0]["owner"] == ""
    assert view["projections"] == []
    assert '"stale": true' in view["evidence"][0]["payload_json"]


def test_handoff_waits_until_old_projection_is_explicitly_superseded(tmp_path):
    admin, alice, _alice_b, bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    _authority, old = authority_and_claim(coordinator, admin, alice)
    coordinator.transition(
        alice, old, expected_version=1, new_state="review", markdown="old source",
        operation_id="old-transition", path="tasks/CG-1.md", canonical_revision="old-base",
    )
    changed = coordinator.set_authority(
        admin, garden_id="garden", kind="task", scope="CG-1", owner_id="bob",
        authority_generation=5, expected_version=2, operation_id="handoff-after-transition",
    )
    coordinator.acknowledge_cancellation(
        admin, garden_id="garden", kind="task", scope="CG-1", fence=old.fence,
    )
    with pytest.raises(Conflict, match="projections are pending"):
        coordinator.claim(
            bob, garden_id="garden", kind="task", scope="CG-1",
            expected_version=changed["version"], accepted_owner="bob",
            authority_generation=5, operation_id="blocked-by-outbox",
        )
    for row in coordinator.pending_outbox("garden"):
        coordinator.supersede_outbox(
            admin, garden_id="garden", outbox_id=row["id"],
            authority_version=row["authority_version"],
        )
    claim = coordinator.claim(
        bob, garden_id="garden", kind="task", scope="CG-1",
        expected_version=changed["version"], accepted_owner="bob",
        authority_generation=5, operation_id="after-outbox-reconciled",
    )
    assert claim.owner_id == "bob"


def test_successive_reassignments_preserve_each_handoff_generation(tmp_path):
    admin, alice, _alice_b, bob = principals()
    coordinator = Coordinator(tmp_path / "coordination.db")
    _authority, old = authority_and_claim(coordinator, admin, alice)
    first = coordinator.set_authority(
        admin, garden_id="garden", kind="task", scope="CG-1", owner_id="bob",
        authority_generation=5, expected_version=1, operation_id="alice-to-bob",
    )
    coordinator.set_authority(
        admin, garden_id="garden", kind="task", scope="CG-1", owner_id="alice",
        authority_generation=6, expected_version=first["version"], operation_id="bob-to-alice",
    )
    handoffs = coordinator.snapshot(admin, "garden")["handoffs"]
    assert [(row["from_owner"], row["to_owner"], row["to_generation"]) for row in handoffs] == [
        ("alice", "bob", 5), ("bob", "alice", 6),
    ]
    coordinator.acknowledge_cancellation(
        admin, garden_id="garden", kind="task", scope="CG-1", fence=old.fence,
    )
    resumed = coordinator.claim(
        alice, garden_id="garden", kind="task", scope="CG-1", expected_version=3,
        accepted_owner="alice", authority_generation=6, operation_id="alice-resumes",
    )
    assert resumed.authority_generation == 6


def test_http_service_authenticates_and_reports_protocol_conflicts(tmp_path):
    garden_dir = tmp_path / ".garden"
    registry = MemberRegistry(garden_dir)
    token = registry.enroll_administrator("garden", "admin", "admin-box")
    client = TestClient(create_coordination_app(garden_dir))

    assert client.get("/v1/gardens/garden/snapshot").status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    snapshot = client.get("/v1/gardens/garden/snapshot", headers=headers)
    assert snapshot.status_code == 200
    assert snapshot.json()["protocol_version"] == 1
    incompatible = client.get(
        "/v1/gardens/garden/snapshot?protocol_version=99", headers=headers,
    )
    assert incompatible.status_code == 426

    authority = client.post("/v1/gardens/garden/authority", headers=headers, json={
        "kind": "phase", "scope": "demo/p1", "owner_id": "admin",
        "authority_generation": 1, "expected_version": 0, "operation_id": "authority",
    })
    assert authority.status_code == 200 and authority.json()["version"] == 1
