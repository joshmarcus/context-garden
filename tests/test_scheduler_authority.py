from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml

from garden.scheduler import Scheduler
from garden.store import Store


class CoordinatorStub:
    def __init__(self, snapshot, member_id="alice"):
        self.member_id = member_id
        self.installation_id = f"{member_id}-laptop"
        self.snapshot = snapshot
        self.effects = []

    def refresh(self, *, allow_stale):
        assert not allow_stale
        return SimpleNamespace(snapshot=self.snapshot)

    def prepare(self, *, mutation):
        assert mutation
        return SimpleNamespace(snapshot=self.snapshot)

    @contextmanager
    def effect(self, **request):
        self.effects.append(request)
        yield {"fence": 1}


def scheduler(snapshot, member_id="alice"):
    value = Scheduler.__new__(Scheduler)
    value.cfg = {"multiplayer.enabled": True}
    value.coordinator = CoordinatorStub(snapshot, member_id)
    value._authority_snapshot = snapshot
    value.store = SimpleNamespace(
        phase=lambda _product, _phase: SimpleNamespace(default_owner="alice")
    )
    return value


def task(task_id, product="demo", phase="p1"):
    return SimpleNamespace(id=task_id, product=product, phase=phase, owner="",
                           owner_unassigned=False)


def snapshot(*, assignment=True):
    return {
        "assignment": ({"member_id": "alice", "project": "demo", "phase": "p1",
                        "generation": 3, "enabled": True} if assignment else None),
        "authority": [
            {"kind": "task", "scope": "A-1", "owner": "alice", "version": 7,
             "authority_generation": 4},
            {"kind": "task", "scope": "B-1", "owner": "bob", "version": 2,
             "authority_generation": 1},
            {"kind": "task", "scope": "A-2", "owner": "alice", "version": 1,
             "authority_generation": 1},
            {"kind": "phase", "scope": "demo/p1", "owner": "bob", "version": 5,
             "authority_generation": 2},
        ],
    }


def test_task_lifecycle_requires_assignment_owner_and_phase():
    sched = scheduler(snapshot())

    assert sched.task_is_authorized(task("A-1"))
    assert not sched.task_is_authorized(task("B-1"))
    assert not sched.task_is_authorized(task("A-2", phase="p2"))
    assert not sched.phase_is_authorized("demo", "p1")
    with pytest.raises(PermissionError, match="outside"):
        sched._task_authority(task("A-1", product="other"))


def test_task_effect_carries_current_generation_and_revision():
    sched = scheduler(snapshot())

    with sched.task_effect(task("A-1"), "merge:A-1"):
        pass

    assert sched.coordinator.effects == [{
        "kind": "task", "scope": "A-1", "owner_id": "alice",
        "authority_generation": 4, "expected_version": 7,
        "effect_key": "merge:A-1",
    }]


def test_unassigned_member_has_no_executable_tick_scope(garden, monkeypatch):
    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["multiplayer"] = {"enabled": True}
    config_path.write_text(yaml.safe_dump(config))
    coordinator = CoordinatorStub(snapshot(assignment=False))
    monkeypatch.setattr(
        "garden.scheduler.MultiplayerClient.from_config",
        lambda _config: coordinator,
    )
    before = {
        path.relative_to(garden): path.read_bytes()
        for path in garden.rglob("*") if path.is_file()
    }

    sched = Scheduler(Store(garden), github=object())

    assert not sched._refresh_execution_authority()
    assert not sched.task_is_authorized(task("A-1"))
    assert sched.tick().summary() == "nothing to do"
    after = {
        path.relative_to(garden): path.read_bytes()
        for path in garden.rglob("*") if path.is_file()
    }
    assert after == before


def test_phase_authority_is_distinct_from_task_and_admin_visibility():
    value = snapshot()
    value["authority"].append({
        "kind": "phase", "scope": "demo/p2", "owner": "alice", "version": 1,
        "authority_generation": 1,
    })
    sched = scheduler(value)

    assert sched.phase_is_authorized("demo", "p2")
    with pytest.raises(PermissionError, match="not owned"):
        sched.require_phase_authority("demo", "p1")


def test_two_installations_and_mixed_owners_cannot_exchange_lifecycle_effects():
    value = snapshot()
    value["authority"].append({
        "kind": "phase", "scope": "demo/p2", "owner": "alice", "version": 3,
        "authority_generation": 6,
    })
    alice_laptop = scheduler(value)
    alice_desktop = scheduler(value)
    alice_desktop.coordinator.installation_id = "alice-desktop"
    bob = scheduler(value, "bob")
    bob.store.phase = lambda _product, _phase: SimpleNamespace(default_owner="bob")

    lifecycle = ("dispatch", "result", "revision", "ci", "merge")
    for stage in lifecycle:
        with alice_laptop.task_effect(task("A-1"), f"{stage}:A-1"):
            pass
        with alice_desktop.task_effect(task("A-1"), f"{stage}:A-1:retry"):
            pass
        with pytest.raises(PermissionError):
            bob.task_effect(task("A-1"), f"{stage}:A-1").__enter__()

    with alice_laptop.phase_effect("demo", "p2", "retro-queue:demo/p2:source"):
        pass
    with pytest.raises(PermissionError):
        bob.phase_effect("demo", "p2", "retro-queue:demo/p2:source").__enter__()

    assert [effect["effect_key"] for effect in alice_laptop.coordinator.effects] == [
        *(f"{stage}:A-1" for stage in lifecycle),
        "retro-queue:demo/p2:source",
    ]

def test_handoff_cancellation_fences_matching_local_run_without_losing_record():
    value = scheduler(snapshot())
    saved = []
    run = SimpleNamespace(
        task_id="A-1", status="running", finished_at="", error="",
        kill=lambda: None, process_finished=lambda: True, save=lambda: saved.append(True),
    )
    value.runs = SimpleNamespace(active=lambda: [run])

    assert value._cancel_fenced_scope("task", "A-1")
    assert run.status == "cancelled"
    assert run.finished_at and run.error == "fenced by multiplayer ownership handoff"
    assert saved == [True]
