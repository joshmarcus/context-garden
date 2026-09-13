"""Integrated two-person journeys across multiplayer authority boundaries."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient

from garden.coordination import Claim, Conflict, Coordinator
from garden.coordination_api import create_coordination_app
from garden.members import MemberRegistry
from garden.model import Status
from garden.multiplayer_client import MultiplayerClient, MultiplayerUnavailable
from garden.publication import write_public_projection
from garden.scheduler import Scheduler
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


def local_scheduler(source, root, principal, token, http):
    """Build a scheduler in its own root using the production HTTP coordination client."""
    shutil.copytree(source, root)
    config = yaml.safe_load((root / "garden.yaml").read_text())
    config["multiplayer"] = {"enabled": True}
    (root / "garden.yaml").write_text(yaml.safe_dump(config))
    scheduler = Scheduler(Store(root))
    scheduler.coordinator = MultiplayerClient(
        root=root, garden_id="shared", endpoint="http://coordinator", credential=token,
        member_id=principal.member_id, installation_id=principal.installation_id,
        request=http.request,
    )
    return scheduler


def test_two_local_users_reassign_and_recover_without_duplicate_ownership(garden, tmp_path):
    shared = tmp_path / "shared"
    registry, admin, people, tokens = enroll_two_people(shared)
    registry.set_assignment(admin, "blair", "demo", "p1", expected_generation=1)
    clock = Clock()
    state_dir = shared / ".garden"
    database = state_dir / "coordination.db"
    coordinator = Coordinator(database, clock=clock)
    set_authority(coordinator, admin, "task", "DM-001", "alex", 1)
    set_authority(coordinator, admin, "task", "DM-002", "blair", 1)
    http = TestClient(create_coordination_app(state_dir))
    alex = local_scheduler(garden, tmp_path / "alex-root", people["alex"], tokens["alex"], http)
    blair = local_scheduler(garden, tmp_path / "blair-root", people["blair"], tokens["blair"], http)
    for scheduler, owners in ((alex, {"DM-001": "alex", "DM-002": "blair"}),
                              (blair, {"DM-001": "alex", "DM-002": "blair"})):
        for task_id, owner in owners.items():
            task = scheduler.store.task(task_id)
            task.owner = owner
            scheduler.store.save(task)

    # Both scheduler clients refresh independently. Alex sees executable DM-001, while
    # Blair cannot bypass DM-002's dependency gate (nor Alex's ownership boundary).
    assert alex._refresh_execution_authority()
    assert blair._refresh_execution_authority()
    assert [task.id for task, _mode, _why in alex.dispatch_queue()] == ["DM-001"]
    assert blair.dispatch_queue() == []
    assert not alex.task_is_authorized(alex.store.task("DM-002"))
    assert not blair.task_is_authorized(blair.store.task("DM-001"))
    assert blair.store.task("DM-002").depends_on == ["DM-001"]

    # Once the dependency lands, the same root admits Blair's task. Operational holds
    # and the review/CI state remain scheduler gates rather than coordinator shortcuts.
    dependency = blair.store.task("DM-001")
    dependency.status = Status.DONE
    blair.store.save(dependency)
    assert [task.id for task, _mode, _why in blair.dispatch_queue()] == ["DM-002"]
    blair.state.get("DM-002")["runner_hold"] = {"reason": "operator hold"}
    assert blair.dispatch_queue() == []
    blair.state.get("DM-002").pop("runner_hold")
    under_review = blair.store.task("DM-002")
    under_review.status = Status.IN_REVIEW
    blair.store.save(under_review)
    assert blair.dispatch_queue() == []  # review and exact-head CI must resolve first

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
    assert registry.assignment("blair").phase == "p1"


def test_phase_owner_is_exclusive_fenced_and_keeps_separate_child_reviews(garden, tmp_path):
    shared = tmp_path / "shared"
    registry, admin, people, tokens = enroll_two_people(shared)
    state_dir = shared / ".garden"
    coordinator = Coordinator(state_dir / "coordination.db")
    http = TestClient(create_coordination_app(state_dir))
    phase = set_authority(coordinator, admin, "phase", "demo/p1", "alex", 1)
    set_authority(coordinator, admin, "task", "DM-001", "alex", 1)
    alex = local_scheduler(garden, tmp_path / "alex-root", people["alex"], tokens["alex"], http)
    alex.store.task("DM-001").owner = "alex"
    alex.store.save(alex.store.task("DM-001"))
    alex._refresh_execution_authority()
    target = alex.store.phase("demo", "p1")

    # These are the scheduler's phase-effect-wrapped entry points. Stub only their heavy
    # local bodies: authorization, claims, retry behavior, and effect records remain real.
    alex._start_kickoff = lambda value: f"kickoff:{value.key}"
    alex._retro_decide = lambda value, choice, note, by: {"phase": value.key, "choice": choice}
    alex._close_phase = lambda value, force, date: date or "2026-09-13"
    assert alex.start_kickoff(target) == "kickoff:demo/p1"
    assert alex.start_kickoff(target) == "kickoff:demo/p1"
    assert alex.retro_decide(target, "close")["choice"] == "close"
    assert alex.close_phase(target) == "2026-09-13"
    with alex.task_effect(alex.store.task("DM-001"), "child-review:DM-001"):
        pass
    phase_work = Claim(**alex.coordinator._claims[("phase", "demo/p1")])

    second = MultiplayerClient(
        root=tmp_path / "alex-second", garden_id="shared", endpoint="http://coordinator",
        credential=tokens["alex_second"], member_id="alex", installation_id="alex-second",
        request=http.request,
    )
    second.refresh(allow_stale=False)
    with pytest.raises(MultiplayerUnavailable, match="409 Conflict"):
        second.claim(kind="phase", scope="demo/p1", owner_id="alex",
                     authority_generation=1, expected_version=phase["version"])

    set_authority(coordinator, admin, "phase", "demo/p1", "blair", 2, version=1)
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
    registry.set_assignment(admin, "blair", "demo", "p1", expected_generation=1)
    blair = local_scheduler(garden, tmp_path / "blair-root", people["blair"], tokens["blair"], http)
    blair._refresh_execution_authority()
    assert blair.phase_is_authorized("demo", "p1")
    with blair.phase_effect("demo", "p1", "review:after-handoff"):
        pass

    with sqlite3.connect(state_dir / "coordination.db") as connection:
        effects = connection.execute(
            "SELECT effect_key, status FROM effects WHERE garden=?", ("shared",),
        ).fetchall()
    assert {effect_key for effect_key, status in effects if status == "succeeded"} >= {
        "kickoff:demo/p1", "retro-decision:demo/p1", "close-phase:demo/p1",
        "child-review:DM-001", "review:after-handoff",
    }
    assert sum(effect_key == "kickoff:demo/p1" for effect_key, _status in effects) == 1


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
