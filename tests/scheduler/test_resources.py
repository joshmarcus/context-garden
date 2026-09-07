from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from garden.observe import resolve, status_line
from garden.scheduler import State
from garden.scheduler.resources import ResourcePressureError
from garden.web.app import create_app


def _set_resource_limit(sched, key: str, value: int) -> None:
    sched.set_override(f"resources.{key}", value, by="test")


def _claim_slot(root: str, start, outcomes) -> None:
    from garden.scheduler import Scheduler
    from garden.store import Store

    scheduler = Scheduler(Store(Path(root)), read_only=True)
    start.wait()
    try:
        scheduler._new_local_run(f"race-{multiprocessing.current_process().pid}", "work", "work")
        outcomes.put("admitted")
    except ResourcePressureError:
        outcomes.put("deferred")


def test_host_limit_counts_workers_reviews_and_checks_across_direct_launches(sched):
    _set_resource_limit(sched, "max_parallel", 2)
    worker = sched.runs.new_run("DM-001", "local", mode="work")
    worker.save()
    review = sched.runs.new_run("DM-002", "local", mode="review")
    review.save()

    assert sched.local_slots_free() == 0
    task = sched.store.task("DM-002")
    before = len(sched.runs.runs_for(task.id))
    with pytest.raises(ResourcePressureError, match="local execution limit reached"):
        sched.dispatch(task)  # the same method used by `garden dispatch`
    assert len(sched.runs.runs_for(task.id)) == before
    assert task.status.value == "ready"

    worker.status = "done"
    worker.save()
    assert sched.local_slots_free() == 1


def test_worker_admission_keeps_worker_count_separate_from_shared_host_limit(sched):
    """Checks and edits are absent from max_parallel occupancy, but still reserve host capacity."""
    _set_resource_limit(sched, "max_parallel", 2)
    check = sched.runs.new_run("DM-001", "local", mode="check")
    check.save()
    edit = sched.runs.new_run("DM-002", "local", mode="edit")
    edit.save()

    assert len(sched.worker_runs_active()) == 0
    assert sched.slots_free() == 2
    assert sched.local_slots_free() == 0

    task = sched.store.task("DM-001")
    with pytest.raises(ResourcePressureError, match="local execution limit reached"):
        sched.dispatch(task)
    assert len(sched.worker_runs_active()) == 0
    assert len(sched.runs.runs_for(task.id)) == 1


def test_concurrent_launchers_atomically_claim_the_last_host_slot(sched):
    _set_resource_limit(sched, "max_parallel", 1)
    context = multiprocessing.get_context("fork")
    start, outcomes = context.Event(), context.Queue()
    processes = [context.Process(target=_claim_slot, args=(str(sched.store.root), start, outcomes))
                 for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    result = sorted(outcomes.get(timeout=5) for _ in processes)
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert result == ["admitted", "deferred"]


def test_memory_or_temp_pressure_records_environment_stop_and_recovers(sched, monkeypatch):
    import garden.scheduler.resources as resources

    _set_resource_limit(sched, "min_memory_available_mb", 1500)
    _set_resource_limit(sched, "min_temp_free_mb", 1000)
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 900)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: None)
    monkeypatch.setattr(resources, "_free_mb", lambda path: 700)

    with pytest.raises(ResourcePressureError, match="available memory.*temporary storage"):
        sched._admit_local_launch("base_probe check")
    pressure = State(sched.state.path).get("_control")["resource_pressure"]
    assert "not" not in pressure["reason"]
    assert any(e["kind"] == "resource_pressure" for e in sched.events.read())

    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 2000)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: None)
    monkeypatch.setattr(resources, "_free_mb", lambda path: 2000)
    sched.refresh_resource_pressure()
    assert "resource_pressure" not in State(sched.state.path).get("_control")
    assert any(e["kind"] == "resource_recovered" for e in sched.events.read())


def test_effective_memory_uses_tighter_cgroup_headroom(sched, monkeypatch):
    import garden.scheduler.resources as resources

    _set_resource_limit(sched, "min_memory_available_mb", 1500)
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 8000)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: 900)
    status = sched.resource_status()
    assert status.memory_available_mb == 900
    assert status.cgroup_available_mb == 900
    assert "available memory 900 MiB is below 1500 MiB" in status.reasons


def test_configured_execution_cgroup_is_the_admission_boundary(sched, monkeypatch, tmp_path):
    """A roomy controller cannot admit work beyond the configured execution budget."""
    import garden.scheduler.resources as resources

    execution = tmp_path / "execution"
    execution.mkdir()
    monkeypatch.setattr(sched, "effective", lambda key, default=None: {
        "resources.min_memory_available_mb": 1500,
        "resources.execution_cgroup": str(execution),
    }.get(key, default))
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 8000)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: 7000)

    def cgroup_status(path):
        return (900, {"high": 3, "max": 0, "oom": 0, "oom_kill": 0}) if path == execution else (7000, {})

    monkeypatch.setattr(resources, "_cgroup_memory_status", cgroup_status)
    status = sched.resource_status()

    assert status.memory_available_mb == 900
    assert status.cgroup_boundary == "execution cgroup"
    assert status.cgroup_events == (("high", 3), ("max", 0), ("oom", 0), ("oom_kill", 0))
    assert "execution cgroup available memory 900 MiB is below 1500 MiB" in status.reasons
    with pytest.raises(ResourcePressureError, match="execution cgroup available memory"):
        sched._admit_local_launch("work")


def test_resource_status_reports_authoritative_capacity_conflict(sched, monkeypatch, tmp_path):
    import garden.scheduler.resources as resources

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 8000)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: None)
    _set_resource_limit(sched, "heavy_test_parallel", 1)
    assert sched.resource_status().heavy_limit == 1
    _set_resource_limit(sched, "heavy_test_parallel", 2)
    status = sched.resource_status()
    assert status.requested_heavy_limit == 2
    assert status.heavy_limit == 1
    assert status.heavy_conflict == "configured limit 2 conflicts with authoritative limit 1"


def test_authoritative_capacity_conflict_agrees_across_operator_surfaces(sched, monkeypatch, tmp_path):
    import garden.scheduler.resources as resources

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 8000)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: None)
    _set_resource_limit(sched, "heavy_test_parallel", 1)
    assert sched.resource_status().heavy_limit == 1
    _set_resource_limit(sched, "heavy_test_parallel", 2)

    def rendered() -> tuple[str, str, str]:
        app = TestClient(create_app(sched.store, watch=False))
        return status_line(sched.store, sched, resolve(sched.cfg, sched)), app.get("/").text, app.get("/config").text

    conflict = "configured limit 2 conflicts with authoritative limit 1"
    observe, rail, config = rendered()
    assert "heavy 0/1 authoritative (requested 2; 0 waiting)" in observe
    assert f"conflict {conflict}" in observe
    assert "heavy 0/1 authoritative (requested 2)" in rail
    assert f"capacity conflict: {conflict}" in rail
    assert "heavy execution: <strong>0/1</strong> authoritative" in config
    assert "(requested 2)" in config and f"Heavy capacity conflict: {conflict}" in config

    running = sched.runs.new_run("DM-001", "local", mode="check")
    (running.path / "execution.json").write_text('{"state": "running"}')
    running.save()
    waiting = sched.runs.new_run("DM-002", "local", mode="check")
    (waiting.path / "execution.json").write_text('{"state": "waiting"}')
    waiting.save()

    observe, rail, config = rendered()
    assert "heavy 1/1 authoritative (requested 2; 1 waiting)" in observe
    assert "heavy 1/1 authoritative (requested 2) (1 waiting)" in rail
    assert "heavy execution: <strong>1/1</strong> authoritative" in config
    assert "(1 waiting: heavy-test budget full)" in config


def test_writable_but_unbounded_execution_cgroup_is_not_enforced(sched, monkeypatch, tmp_path):
    group = tmp_path / "execution"
    group.mkdir()
    for name, value in (("cgroup.procs", ""), ("cpu.max", "max 100000"),
                        ("memory.high", "max"), ("memory.max", "max")):
        (group / name).write_text(value)
    monkeypatch.setattr(sched, "effective", lambda key, default=None:
                        str(group) if key == "resources.execution_cgroup" else default)

    status = sched.resource_status()

    assert status.isolation.startswith("execution cgroup is unbounded")


def test_disabled_heavy_budget_is_rendered_as_zero(sched):
    _set_resource_limit(sched, "heavy_test_parallel", 0)
    assert sched.resource_status().heavy_limit == 0


def test_operator_feed_names_effective_limit_pressure_and_recovery(sched, monkeypatch):
    import garden.scheduler.resources as resources

    _set_resource_limit(sched, "max_parallel", 1)
    run = sched.runs.new_run("DM-001", "local", mode="check")
    run.save()
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 4096)
    monkeypatch.setattr(resources, "_free_mb", lambda path: 4096)

    line = status_line(sched.store, sched, resolve(sched.cfg, sched))
    assert "local 1/1" in line
    assert "pressure local execution limit reached (1/1)" in line
    assert "wait for drain or pause dispatch" in line


def test_completed_check_continuation_survives_pressure_until_next_tick(sched, monkeypatch):
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.result = {"checks": [{"name": "tests", "status": "fail"}]}
    run.save()
    sched.state.get(task.id)["check_run"] = {
        "run_id": run.run_id, "stage": "base_probe", "cont": {}, "collected": True,
    }
    sched.state.save()

    def pressured(*args):
        raise ResourcePressureError("temp headroom low")

    monkeypatch.setattr(sched, "_after_base_probe_check", pressured)
    with pytest.raises(ResourcePressureError):
        sched.reap_check(task, type("Report", (), {})())
    assert sched.state.get(task.id)["check_run"]["run_id"] == run.run_id

    handled = []
    monkeypatch.setattr(sched, "_after_base_probe_check", lambda *args: handled.append(True))
    assert sched.reap_check(task, type("Report", (), {})()) is True
    assert handled == [True]
    assert sched.state.get(task.id)["check_run"] == {}
