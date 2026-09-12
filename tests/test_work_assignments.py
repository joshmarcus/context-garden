from __future__ import annotations

from pathlib import Path

import pytest

from garden.members import MemberRegistry, Principal
from garden.model import Phase, Status, Task
from garden.runner.manual import ManualRunner
from garden.scheduler import Scheduler


def _registry(tmp_path: Path):
    registry = MemberRegistry(tmp_path / ".garden")
    token = registry.enroll_administrator("garden", "admin", "admin-machine")
    admin = registry.authenticate(token)
    assert admin is not None
    registry.add_member(admin, "alice", "member")
    registry.add_member(admin, "bob", "member")
    alice_token = registry.issue_installation(admin, "alice", "alice-machine")
    alice = registry.authenticate(alice_token)
    assert alice is not None
    return registry, admin, alice


def _phase(tmp_path: Path, *, default_owner: str = "") -> Phase:
    meta = {"default_owner": default_owner} if default_owner else {}
    return Phase("demo", "p1", tmp_path / "demo/p1", None, [], [], [], meta=meta)


def _task(tmp_path: Path, task_id: str, *, owner: str = "", phase: str = "p1",
          status: Status = Status.READY, dependencies: list[str] | None = None) -> Task:
    return Task(tmp_path / f"{task_id}.md", task_id, task_id, status=status,
                product="demo", phase=phase, owner=owner, depends_on=dependencies or [])


def test_effective_owner_requires_active_membership_and_preserves_precedence(tmp_path):
    registry, admin, _alice = _registry(tmp_path)
    phase = _phase(tmp_path, default_owner="alice")
    inherited = _task(tmp_path, "DM-1")
    override = _task(tmp_path, "DM-2", owner="bob")
    explicit_none = _task(tmp_path, "DM-3")
    explicit_none.owner_unassigned = True

    assert registry.effective_task_owner(inherited, phase) == ("alice", "phase")
    assert registry.effective_task_owner(override, phase) == ("bob", "task")
    assert registry.effective_task_owner(explicit_none, phase) == ("", "unassigned")
    registry.set_member_active(admin, "bob", False)
    assert registry.effective_task_owner(override, phase) == ("", "invalid")
    phase.meta["default_owner"] = "legacy-team"
    assert registry.effective_task_owner(inherited, phase) == ("", "invalid")


def test_owner_change_previews_separate_default_bulk_effects_from_override(tmp_path):
    registry, _admin, _alice = _registry(tmp_path)
    phase = _phase(tmp_path, default_owner="alice")
    inherited = _task(tmp_path, "DM-1")
    override = _task(tmp_path, "DM-2", owner="bob")
    unassigned = _task(tmp_path, "DM-3")
    unassigned.owner_unassigned = True

    default = registry.preview_default_owner_change(
        phase, [inherited, override, unassigned], "bob",
    )
    explicit = registry.preview_task_owner_change(override, phase, "alice")
    assert (default.change, default.before, default.after, default.affected_issues) == (
        "default_owner", "alice", "bob", ("DM-1",),
    )
    assert (explicit.change, explicit.before, explicit.after, explicit.affected_issues) == (
        "task_owner", "bob", "alice", ("DM-2",),
    )


def test_assignment_is_single_versioned_cursor_and_new_members_have_none(tmp_path):
    registry, admin, alice = _registry(tmp_path)
    assert registry.assignment("alice") is None
    assigned = registry.set_assignment(admin, "alice", "demo", "p1")
    assert (assigned.generation, assigned.enabled, assigned.advance) == (1, True, False)
    paused = registry.set_assignment(admin, "alice", "demo", "p2", enabled=False,
                                     advance=True, expected_generation=1)
    assert registry.assignment("alice") == paused
    assert (paused.project, paused.phase, paused.generation) == ("demo", "p2", 2)
    with pytest.raises(RuntimeError, match="stale assignment"):
        registry.set_assignment(admin, "alice", "demo", "p3", expected_generation=1)
    with pytest.raises(PermissionError, match="administrator"):
        registry.set_assignment(alice, "alice", "demo", "p3", expected_generation=2)
    registry.clear_assignment(admin, "alice", expected_generation=2)
    assert registry.assignment("alice") is None
    with pytest.raises(RuntimeError, match="stale assignment"):
        registry.set_assignment(admin, "alice", "demo", "p3", expected_generation=0)
    recreated = registry.set_assignment(admin, "alice", "demo", "p3",
                                         expected_generation=3)
    assert recreated.generation == 4


def test_phase_owner_is_explicit_distinct_versioned_and_attributed(tmp_path):
    registry, admin, alice = _registry(tmp_path)
    assert registry.phase_owner("demo", "p1") is None
    owner = registry.set_phase_owner(admin, "demo", "p1", "alice")
    assert (owner.owner_id, owner.generation, owner.changed_by) == ("alice", 1, "admin")
    assert registry.authorize_phase_operation(alice, "demo", "p1")
    spoofed = Principal("garden", "alice", "made-up", "member", "all")
    assert not registry.authorize_phase_operation(spoofed, "demo", "p1")
    with pytest.raises(RuntimeError, match="stale phase owner"):
        registry.set_phase_owner(admin, "demo", "p1", "bob", expected_generation=0)
    vacant = registry.set_phase_owner(admin, "demo", "p1", None, expected_generation=1)
    assert vacant.owner_id == "" and vacant.generation == 2
    assert not registry.authorize_phase_operation(alice, "demo", "p1")
    registry.set_phase_owner(admin, "demo", "p1", "bob", expected_generation=2)
    registry.set_member_active(admin, "bob", False)
    assert not registry.authorize_phase_operation(alice, "demo", "p1")


def test_execution_scope_keeps_dependencies_holds_and_other_phases_out(tmp_path):
    registry, admin, _alice = _registry(tmp_path)
    registry.set_assignment(admin, "alice", "demo", "p1", advance=True)
    phase = _phase(tmp_path, default_owner="alice")
    other_phase = Phase("demo", "p2", tmp_path / "demo/p2", None, [], [], [],
                        meta={"default_owner": "alice"})
    done = _task(tmp_path, "DM-1", status=Status.DONE)
    ready = _task(tmp_path, "DM-2", dependencies=["DM-1"])
    blocked = _task(tmp_path, "DM-3", dependencies=["DM-4"])
    bob_work = _task(tmp_path, "DM-4", owner="bob")
    outside = _task(tmp_path, "DM-5", phase="p2")
    tasks = {task.id: task for task in (done, ready, blocked, bob_work, outside)}
    phases = {phase.key: phase, other_phase.key: other_phase}

    assert [task.id for task in registry.executable_tasks("alice", tasks, phases)] == ["DM-2"]
    assert not registry.can_advance_assignment("alice", tasks, phases)
    ready.status = Status.DONE
    blocked.status = Status.DONE
    bob_work.status = Status.DONE
    assert registry.can_advance_assignment("alice", tasks, phases)
    phase.meta["frozen"] = "2026-09-12"
    ready.status = Status.READY
    assert registry.executable_tasks("alice", tasks, phases) == []
    ready.freeze_exception = True
    ready.freeze_exception_reason = "owner-approved repair"
    assert [task.id for task in registry.executable_tasks("alice", tasks, phases)] == ["DM-2"]
    ready.status = Status.DONE
    advanced = registry.advance_assignment(admin, "alice", "p2", tasks,
                                            expected_generation=1)
    assert (advanced.phase, advanced.generation) == ("p2", 2)


def test_cross_project_dependency_remains_a_blocker_without_disclosing_details(tmp_path):
    registry, admin, _alice = _registry(tmp_path)
    registry.set_assignment(admin, "alice", "demo", "p1")
    phase = _phase(tmp_path, default_owner="alice")
    dependency = Task(tmp_path / "secret.md", "PV-1", "secret title", status=Status.READY,
                      product="private", phase="hidden")
    task = _task(tmp_path, "DM-1", dependencies=["PV-1"])
    assert registry.executable_tasks("alice", {"DM-1": task, "PV-1": dependency},
                                     {phase.key: phase}) == []
    information = registry.dependency_information(
        task, {"DM-1": task, "PV-1": dependency}, frozenset({"demo"}),
    )
    assert information == {"blockers": (), "inaccessible_blocker_count": 1}
    assert "PV-1" not in repr(information)


def test_real_dispatch_enforces_authenticated_owner_cursor_and_generation(sched):
    sched.cfg.data["multiplayer"] = {"enabled": True}
    registry = MemberRegistry(sched.cfg.garden_dir)
    token = registry.enroll_administrator("garden", "alice", "alice-machine")
    alice = registry.authenticate(token)
    assert alice is not None
    task = sched.store.task("DM-001")
    task.owner = "alice"
    sched.store.save(task)
    assignment = registry.set_assignment(alice, "alice", task.product, task.phase)

    unbound = Scheduler(sched.store, github=sched.github)
    with pytest.raises(RuntimeError, match="identity-less scheduling"):
        unbound.dispatch(task, runner=ManualRunner({}), worktree=False)

    bound = Scheduler(sched.store, github=sched.github, principal=alice)
    with pytest.raises(RuntimeError, match="stale assignment"):
        bound.dispatch(task, runner=ManualRunner({}), worktree=False,
                       assignment_generation=assignment.generation - 1)
    run = bound.dispatch(task, runner=ManualRunner({}), worktree=False,
                         assignment_generation=assignment.generation)
    assert run.task_id == task.id


def test_retry_enforces_authenticated_owner_cursor_and_generation_before_mutation(sched):
    sched.cfg.data["multiplayer"] = {"enabled": True}
    registry = MemberRegistry(sched.cfg.garden_dir)
    token = registry.enroll_administrator("garden", "alice", "alice-machine")
    alice = registry.authenticate(token)
    assert alice is not None
    task = sched.store.task("DM-001")
    task.owner = "alice"
    task.status = Status.FAILED
    sched.store.save(task)
    assignment = registry.set_assignment(alice, "alice", task.product, task.phase)

    bound = Scheduler(sched.store, github=sched.github, principal=alice)
    with pytest.raises(RuntimeError, match="stale assignment"):
        bound.retry(task, assignment_generation=assignment.generation - 1)
    assert task.status == Status.FAILED

    paused = registry.set_assignment(
        alice, "alice", task.product, task.phase, enabled=False,
        expected_generation=assignment.generation,
    )
    with pytest.raises(PermissionError, match="paused"):
        bound.retry(task, assignment_generation=paused.generation)
    assert task.status == Status.FAILED

    wrong_phase = registry.set_assignment(
        alice, "alice", task.product, "p2", expected_generation=paused.generation,
    )
    with pytest.raises(PermissionError, match="outside"):
        bound.retry(task, assignment_generation=wrong_phase.generation)
    assert task.status == Status.FAILED

    registry.clear_assignment(alice, "alice", expected_generation=wrong_phase.generation)
    with pytest.raises(PermissionError, match="no execution assignment"):
        bound.retry(task)
    assert task.status == Status.FAILED

    current = registry.set_assignment(alice, "alice", task.product, task.phase,
                                      expected_generation=wrong_phase.generation + 1)
    bound.retry(task, assignment_generation=current.generation)
    assert task.status == Status.READY


def test_real_phase_operations_require_current_explicit_versioned_owner(sched):
    sched.cfg.data["multiplayer"] = {"enabled": True}
    registry = MemberRegistry(sched.cfg.garden_dir)
    admin_token = registry.enroll_administrator("garden", "admin", "admin-machine")
    admin = registry.authenticate(admin_token)
    assert admin is not None
    registry.add_member(admin, "alice", "member")
    alice_token = registry.issue_installation(admin, "alice", "alice-machine")
    alice = registry.authenticate(alice_token)
    assert alice is not None
    phase = sched.store.phase("demo", "p1")
    sched.store.set_phase_closed(phase, "2026-09-12")
    sched.store.invalidate()
    phase = sched.store.phase("demo", "p1")

    bound = Scheduler(sched.store, github=sched.github, principal=alice)
    with pytest.raises(PermissionError, match="explicit active phase owner"):
        bound.reopen_phase(phase)
    owner = registry.set_phase_owner(admin, phase.product, phase.name, "alice")
    with pytest.raises(RuntimeError, match="stale phase owner"):
        bound.reopen_phase(phase, owner_generation=owner.generation - 1)
    bound.reopen_phase(phase, owner_generation=owner.generation)
    assert not bound.store.phase(phase.product, phase.name).closed
