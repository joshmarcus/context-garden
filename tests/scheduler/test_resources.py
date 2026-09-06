from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from garden.observe import resolve, status_line
from garden.scheduler import State
from garden.scheduler.resources import ResourcePressureError


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
    monkeypatch.setattr(resources, "_free_mb", lambda path: 700)

    with pytest.raises(ResourcePressureError, match="available memory.*temporary storage"):
        sched._admit_local_launch("base_probe check")
    pressure = State(sched.state.path).get("_control")["resource_pressure"]
    assert "not" not in pressure["reason"]
    assert any(e["kind"] == "resource_pressure" for e in sched.events.read())

    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 2000)
    monkeypatch.setattr(resources, "_free_mb", lambda path: 2000)
    sched.refresh_resource_pressure()
    assert "resource_pressure" not in State(sched.state.path).get("_control")
    assert any(e["kind"] == "resource_recovered" for e in sched.events.read())


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
