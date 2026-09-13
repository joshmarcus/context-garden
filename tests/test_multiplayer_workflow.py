"""Integrated two-person journeys across multiplayer authority boundaries."""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from garden.coordination import Conflict, Coordinator
from garden.members import MemberRegistry
from garden.publication import write_public_projection
from garden.store import Store
from garden.web.app import create_app
from garden.web.public import create_public_app


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 9, 13, tzinfo=dt.UTC)

    def __call__(self):
        return self.now


def enroll_two_people(garden):
    registry = MemberRegistry(garden / ".garden")
    admin_token = registry.enroll_administrator("shared", "admin", "coordinator")
    admin = registry.authenticate(admin_token)
    assert admin is not None
    registry.add_member(admin, "alex", "member", "assigned", ("demo",))
    registry.add_member(admin, "blair", "member", "assigned", ("demo",))
    tokens = {
        "alex": registry.issue_installation(admin, "alex", "alex-local"),
        "alex_second": registry.issue_installation(admin, "alex", "alex-second"),
        "blair": registry.issue_installation(admin, "blair", "blair-local"),
    }
    registry.set_assignment(admin, "alex", "demo", "p1", advance=True)
    registry.set_assignment(admin, "blair", "demo", "p2")
    people = {name: registry.authenticate(token) for name, token in tokens.items()}
    assert all(people.values())
    return registry, admin, people, tokens


def set_authority(coordinator, admin, kind, scope, owner, generation, version=0):
    return coordinator.set_authority(
        admin, garden_id="shared", kind=kind, scope=scope, owner_id=owner,
        authority_generation=generation, expected_version=version,
        operation_id=f"authority:{kind}:{scope}:{generation}",
    )


def claim(coordinator, actor, kind, scope, generation, version, operation):
    return coordinator.claim(
        actor, garden_id="shared", kind=kind, scope=scope, expected_version=version,
        accepted_owner=actor.member_id, authority_generation=generation,
        operation_id=operation,
    )


def test_two_local_users_reassign_and_recover_without_duplicate_ownership(garden, tmp_path):
    registry, admin, people, _tokens = enroll_two_people(garden)
    clock = Clock()
    database = tmp_path / "coordinator" / "coordination.db"
    coordinator = Coordinator(database, clock=clock)
    set_authority(coordinator, admin, "task", "DM-001", "alex", 1)
    set_authority(coordinator, admin, "task", "DM-002", "blair", 1)

    alex_work = claim(coordinator, people["alex"], "task", "DM-001", 1, 1, "alex-work")
    blair_work = claim(coordinator, people["blair"], "task", "DM-002", 1, 1, "blair-work")
    assert alex_work.installation_id == "alex-local"
    assert blair_work.installation_id == "blair-local"
    with pytest.raises(Conflict, match="waiting"):
        claim(coordinator, people["alex_second"], "task", "DM-001", 1, 1, "duplicate")

    coordinator.begin_effect(
        people["alex"], alex_work, provider="github", effect_key="publish:DM-001",
        operation_id="alex-publish", credential_scope="pull_requests:write",
        precondition="head=alex", request={"head": "alex"},
    )
    changed = set_authority(coordinator, admin, "task", "DM-001", "blair", 2, version=1)
    coordinator.retain_stale_evidence(
        people["alex"], garden_id="shared", kind="task", scope="DM-001",
        evidence_id="alex-late-result", operation_id="retain-alex-result",
        payload={"head": "alex", "result": "done"},
    )
    with pytest.raises(Conflict, match="stale or expired"):
        coordinator.transition(
            people["alex"], alex_work, expected_version=changed["version"],
            new_state="review", markdown="stale source", operation_id="stale-transition",
        )
    with pytest.raises(Conflict, match="old workers.*provider outcomes"):
        claim(coordinator, people["blair"], "task", "DM-001", 2, 2, "too-soon")

    # Restart preserves the handoff and its blocking unknown operation.
    restarted = Coordinator(database, clock=clock)
    restarted.acknowledge_cancellation(
        people["alex"], garden_id="shared", kind="task", scope="DM-001",
        fence=alex_work.fence,
    )
    restarted.finish_effect(admin, "shared", "alex-publish", outcome="succeeded",
                             result={"pr": 41})
    recovered = claim(restarted, people["blair"], "task", "DM-001", 2, 2, "blair-resume")
    assert recovered.fence > alex_work.fence
    snapshot = restarted.snapshot(admin, "shared")
    assert snapshot["handoffs"][0]["status"] == "ready"
    assert json.loads(snapshot["evidence"][0]["payload_json"])["stale"] is True
    assert registry.assignment("alex").advance is True
    assert registry.assignment("blair").phase == "p2"


def test_phase_owner_is_exclusive_fenced_and_keeps_separate_child_reviews(garden, tmp_path):
    _registry, admin, people, _tokens = enroll_two_people(garden)
    coordinator = Coordinator(tmp_path / "coordinator.db")
    phase = set_authority(coordinator, admin, "phase", "demo/p1", "alex", 1)
    phase_work = claim(coordinator, people["alex"], "phase", "demo/p1", 1,
                       phase["version"], "phase-review")
    repeated = claim(coordinator, people["alex"], "phase", "demo/p1", 1,
                     phase["version"], "phase-review")
    assert repeated == phase_work
    for actor in (people["alex_second"], people["blair"]):
        with pytest.raises((Conflict, PermissionError)):
            claim(coordinator, actor, "phase", "demo/p1", 1, phase["version"],
                  f"competing-{actor.installation_id}")

    child_effects = []
    for task_id in ("DM-001", "DM-002"):
        operation = f"child-review:{task_id}"
        child_effects.append(coordinator.begin_effect(
            people["alex"], phase_work, provider="scheduler", effect_key=operation,
            operation_id=operation, credential_scope="scheduler:write",
            precondition=f"task={task_id}", request={"task": task_id},
        ))
        coordinator.finish_effect(people["alex"], "shared", operation, outcome="succeeded")
    assert [effect["operation_id"] for effect in child_effects] == [
        "child-review:DM-001", "child-review:DM-002",
    ]

    reassigned = set_authority(coordinator, admin, "phase", "demo/p1", "blair", 2, version=1)
    with pytest.raises(Conflict, match="stale or expired"):
        coordinator.begin_effect(
            people["alex"], phase_work, provider="scheduler", effect_key="retro:demo/p1",
            operation_id="stale-retro", credential_scope="scheduler:write",
            precondition="", request={},
        )
    coordinator.acknowledge_cancellation(
        people["alex"], garden_id="shared", kind="phase", scope="demo/p1",
        fence=phase_work.fence,
    )
    current = claim(coordinator, people["blair"], "phase", "demo/p1", 2,
                    reassigned["version"], "blair-phase")
    assert current.owner_id == "blair"


def test_view_focus_and_public_revocation_do_not_change_execution_scope(garden, tmp_path):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    config["publication"] = {"projects": {"demo": {"fields": ["task.summary"]}}}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    registry, _admin, _people, tokens = enroll_two_people(garden)
    private = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    headers = {"Authorization": f"Bearer {tokens['alex']}"}
    before = registry.assignment("alex")
    assert private.get("/board?project=demo", headers=headers).status_code == 200
    assert private.get("/inbox?project=demo&view=team", headers=headers).status_code == 200
    assert registry.assignment("alex") == before

    output = tmp_path / "approved-projection"
    write_public_projection(Store(garden), output)
    viewer = TestClient(create_public_app(output))
    assert viewer.get("/api/projects").json()["projects"][0]["id"] == "demo"
    assert viewer.get("/runs").status_code == 404
    assert viewer.post("/tick").status_code == 404

    config["publication"]["projects"] = {}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    write_public_projection(Store(garden), output)
    assert viewer.get("/api/projects").json()["projects"] == []
    assert registry.assignment("alex") == before
