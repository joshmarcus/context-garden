from __future__ import annotations

import json
import multiprocessing
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import shutil
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


def _add_product(sched, garden: Path, name: str, task_id: str, weight: int, timeout: int) -> None:
    import yaml

    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["products"][name] = {"repo": "../repo", "base_branch": "main",
                                 "timeout_minutes": timeout, "resources": {"weight": weight}}
    config_path.write_text(yaml.safe_dump(config))
    source = garden / "demo/p1/tasks/DM-001-first.md"
    target = garden / name / "p1/tasks" / f"{task_id}-first.md"
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text().replace("DM-001", task_id))
    shutil.copytree(garden / "demo/p1/specs", garden / name / "p1/specs")
    (garden / name / "product.md").write_text(f"# {name}\n")
    (garden / name / "p1/goals.md").write_text("# p1\n")
    from garden.config import Config

    sched.store.config = Config.load(garden)
    sched.cfg = sched.store.config
    sched.store.invalidate_tasks()


def test_host_limit_counts_workers_reviews_and_checks_across_direct_launches(sched):
    _set_resource_limit(sched, "max_parallel", 2)
    worker = sched.runs.new_run("DM-001", "local", mode="work")
    worker.save()
    review = sched.runs.new_run("DM-002", "local", mode="review")
    review.save()

    assert sched.local_slots_free() == 0
    task = sched.store.task("DM-002")
    before = len(sched.runs.runs_for(task.id))
    with pytest.raises(ResourcePressureError, match="waits for a local execution slot"):
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
    with pytest.raises(ResourcePressureError, match="waits for a local execution slot"):
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


def test_weighted_capacity_is_atomic_and_reconciles_from_runs_after_restart(sched, garden):
    from garden.scheduler import Scheduler
    from garden.store import Store

    _set_resource_limit(sched, "max_parallel", 4)
    _add_product(sched, garden, "heavy", "HV-001", 3, 180)
    heavy = sched._new_local_run("HV-001", "work", "work")
    assert heavy.env_snapshot["resource_weight"] == 3
    # A cheap product still fits beside it.
    cheap = sched._new_local_run("DM-001", "work", "work")
    assert sched.resource_status().active == 4
    with pytest.raises(ResourcePressureError, match="needs 1 capacity unit"):
        sched._new_local_run("DM-002", "work", "work")

    restarted = Scheduler(Store(garden), read_only=True)
    restarted.set_override("resources.max_parallel", 4, by="test")
    assert restarted.resource_status().active == 4
    heavy.status = "done"
    heavy.save()
    assert restarted.resource_status().active == 1
    cheap.status = "done"
    cheap.save()


def test_product_execution_timeout_is_snapshotted_and_checks_stay_distinct(sched, garden, monkeypatch):
    _add_product(sched, garden, "heavy", "HV-001", 2, 180)
    task = sched.store.task("HV-001")
    assert sched.runner_for(task).config["timeout_minutes"] == 180
    run = sched._new_local_run(task.id, "work", "work")
    run.env_snapshot.update(product="heavy", execution_timeout_minutes=180)
    monkeypatch.setattr(run, "elapsed_minutes", lambda: 100)
    assert sched._finished_or_timed_out(run, sched.runner_for(task)) is False

    check = sched._new_local_run(task.id, "check", "check")
    check.env_snapshot.update(product="heavy", execution_timeout_minutes=0)
    monkeypatch.setattr(check, "elapsed_minutes", lambda: 1000)
    assert sched._finished_or_timed_out(check, sched.runner_for(task)) is False


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_product_resource_weight_requires_positive_integer(sched, value):
    sched.cfg.data["products"]["demo"]["resources"] = {"weight": value}
    with pytest.raises(ValueError, match="positive integer"):
        sched.cfg.product_resource_weight("demo")


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


def test_rendered_status_distinguishes_capacity_from_resource_pressure(sched, monkeypatch):
    """Inbox and Config show ordinary full slots separately from true host gates."""
    import garden.scheduler.resources as resources

    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 4096)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: None)
    monkeypatch.setattr(resources, "_free_mb", lambda path: 4096)

    app = TestClient(create_app(sched.store, watch=False))

    # Available capacity has no warning.
    assert "At local execution capacity" not in app.get("/").text

    _set_resource_limit(sched, "max_parallel", 2)
    for task_id in ("DM-001", "DM-002"):
        sched.runs.new_run(task_id, "local", mode="check").save()
    inbox = app.get("/").text
    config = app.get("/config").text
    line = status_line(sched.store, sched, resolve(sched.cfg, sched))
    assert "At local execution capacity" in inbox
    assert "Eligible work waits for a slot and dispatches automatically when one opens" in inbox
    assert "Resource pressure" not in inbox
    assert "At local execution capacity" in config
    assert "at capacity 2/2" in line
    assert "pressure " not in line

    # Headroom pressure is visible even with a local slot available.
    _set_resource_limit(sched, "max_parallel", 3)
    _set_resource_limit(sched, "min_memory_available_mb", 1500)
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 900)
    inbox = app.get("/").text
    config = app.get("/config").text
    assert "Resource pressure" in inbox and "available memory 900 MiB is below 1500 MiB" in inbox
    assert "At local execution capacity" not in inbox
    assert "Resource pressure" in config

    # Both conditions remain visible together; occupancy does not hide the memory gate.
    _set_resource_limit(sched, "max_parallel", 2)
    inbox = app.get("/").text
    config = app.get("/config").text
    assert "At local execution capacity" in inbox
    assert "Resource pressure" in inbox
    assert "Also at local execution capacity (2/2 busy)" in inbox
    assert "Also at local execution capacity (2/2 busy)" in config


def test_operator_feed_names_capacity_without_calling_it_pressure(sched, monkeypatch):
    import garden.scheduler.resources as resources

    _set_resource_limit(sched, "max_parallel", 1)
    sched.runs.new_run("DM-001", "local", mode="check").save()
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 4096)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: None)
    monkeypatch.setattr(resources, "_free_mb", lambda path: 4096)

    line = status_line(sched.store, sched, resolve(sched.cfg, sched))
    assert "local 1/1" in line
    assert "at capacity 1/1" in line
    assert "pressure " not in line


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
    assert sched.reap_check(task, type("Report", (), {})()) is False
    assert handled == [True]


def _cache_limited(sched, monkeypatch, tmp_path, *, inactive_file=700 * 1024 * 1024):
    import garden.scheduler.resources as resources

    group = tmp_path / "execution"
    group.mkdir()
    for name, value in (("memory.current", str(900 * 1024 * 1024)),
                        ("memory.high", str(1800 * 1024 * 1024)),
                        ("memory.max", str(2048 * 1024 * 1024)),
                        ("memory.events", "high 0\nmax 0\noom 0\noom_kill 0\n"),
                        ("memory.stat", f"file {inactive_file}\nshmem {300 * 1024 * 1024}\ninactive_file {inactive_file}\n"),
                        ("memory.reclaim", ""), ("cgroup.procs", ""), ("cpu.max", "100000 100000")):
        (group / name).write_text(value)
    values = {
        "resources.min_memory_available_mb": 1500,
        "resources.execution_cgroup": str(group),
        "resources.reclaim_max_mb": 256,
        "resources.reclaim_cooldown_seconds": 300,
        "resources.reclaim_timeout_seconds": 1,
    }
    monkeypatch.setattr(sched, "effective", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 8000)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: 7000)
    return group, values


def test_cache_limited_admission_starts_one_bounded_helper_and_still_stops(sched, monkeypatch, tmp_path):
    import garden.scheduler.resources as resources

    group, _values = _cache_limited(sched, monkeypatch, tmp_path)
    launches = []

    class Process:
        pid = 4242

    monkeypatch.setattr(resources.subprocess, "Popen", lambda command, **kwargs: launches.append(command) or Process())
    monkeypatch.setattr(resources, "_reclaim_pid_alive", lambda pid, token: True)

    with pytest.raises(ResourcePressureError, match="execution cgroup available memory"):
        sched._new_local_run("DM-001", "work", "work")
    with pytest.raises(ResourcePressureError):
        sched._new_local_run("DM-002", "review", "review")

    assert len(launches) == 1
    assert launches[0][launches[0].index("--bytes") + 1] == str(256 * 1024 * 1024)
    state = json.loads((sched.cfg.garden_dir / "resource-reclaim.json").read_text())
    assert state["memory_stat"]["shmem"] == 300 * 1024 * 1024
    assert not sched.runs.runs_for("DM-001") and not sched.runs.runs_for("DM-002")


@pytest.mark.parametrize("other_gate", ["slot", "temp", "oom", "host"])
def test_reclaim_is_not_considered_while_an_ordinary_gate_also_blocks(
        sched, monkeypatch, tmp_path, other_gate):
    import garden.scheduler.resources as resources

    group, values = _cache_limited(sched, monkeypatch, tmp_path)
    if other_gate == "slot":
        values["resources.max_parallel"] = 1
        sched.runs.new_run("busy", "local", mode="check").save()
    elif other_gate == "temp":
        values["resources.min_temp_free_mb"] = 1000
        monkeypatch.setattr(resources, "_free_mb", lambda path: 10)
    elif other_gate == "oom":
        (group / "memory.events").write_text("high 0\nmax 0\noom 1\noom_kill 0\n")
    else:
        monkeypatch.setattr(resources, "_memory_available_mb", lambda: 500)
    monkeypatch.setattr(resources.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("reclaim started"))

    with pytest.raises(ResourcePressureError):
        sched._new_local_run("DM-001", "check", "check")


def test_partial_reclaim_requires_fresh_normal_gate_and_cooldown(sched, monkeypatch, tmp_path):
    import garden.scheduler.resources as resources

    _group, _values = _cache_limited(sched, monkeypatch, tmp_path)
    state_path, report_path = sched._reclaim_paths()
    started = resources.time.time() - 2
    state_path.write_text(json.dumps({"running": True, "pid": 123, "started_at": started, "token": "x"}) + "\n")
    report_path.write_text(json.dumps({"token": "x", "started_at": started, "finished_at": resources.time.time(),
                                      "status": "complete", "headroom_before_bytes": 900 << 20,
                                      "headroom_after_bytes": 1200 << 20}) + "\n")
    monkeypatch.setattr(resources.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("cooldown ignored"))

    with pytest.raises(ResourcePressureError):
        sched._new_local_run("DM-001", "work", "work")
    assert "900→1200 MiB actual headroom" in sched.resource_status().reclaim
    observe = status_line(sched.store, sched, resolve(sched.cfg, sched))
    config = TestClient(create_app(sched.store, watch=False)).get("/config").text
    assert "last bounded cache reclaim complete (900→1200 MiB actual headroom)" in observe
    assert "Admission still requires a fresh ordinary headroom check" in config


def test_fresh_headroom_under_lock_admits_after_completed_reclaim(sched, monkeypatch, tmp_path):
    import garden.scheduler.resources as resources

    _group, _values = _cache_limited(sched, monkeypatch, tmp_path)
    state_path, report_path = sched._reclaim_paths()
    started = resources.time.time() - 2
    state_path.write_text(json.dumps({"running": True, "pid": 123, "started_at": started, "token": "x"}) + "\n")
    report_path.write_text(json.dumps({"token": "x", "started_at": started, "finished_at": resources.time.time(),
                                      "status": "complete", "headroom_before_bytes": 900 << 20,
                                      "headroom_after_bytes": 1600 << 20}) + "\n")
    monkeypatch.setattr(resources, "_cgroup_memory_status", lambda path: (1600, {"high": 0, "max": 0, "oom": 0, "oom_kill": 0}))

    run = sched._new_local_run("DM-001", "check", "check")
    assert run.status == "running"


def test_missing_cache_reading_and_unavailable_delegation_preserve_stop(sched, monkeypatch, tmp_path):
    import garden.scheduler.resources as resources

    group, _values = _cache_limited(sched, monkeypatch, tmp_path)
    (group / "memory.stat").unlink()
    monkeypatch.setattr(resources.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("reclaim started"))

    with pytest.raises(ResourcePressureError):
        sched._new_local_run("DM-001", "work", "work")
    assert not (sched.cfg.garden_dir / "resource-reclaim.json").exists()


def test_stuck_reclaim_helper_is_killed_and_cooldown_preserves_stop(sched, monkeypatch, tmp_path):
    import garden.scheduler.resources as resources

    _group, _values = _cache_limited(sched, monkeypatch, tmp_path)
    state_path, _report_path = sched._reclaim_paths()
    state_path.write_text(json.dumps({"running": True, "pid": 456, "started_at": resources.time.time() - 10,
                                      "token": "stuck"}) + "\n")
    killed = []
    monkeypatch.setattr(resources, "_reclaim_pid_alive", lambda pid, token: True)
    monkeypatch.setattr(resources.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(resources.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("cooldown ignored"))

    with pytest.raises(ResourcePressureError):
        sched._new_local_run("DM-001", "review", "review")
    assert killed == [(456, resources.signal.SIGKILL)]
    result = json.loads(state_path.read_text())["result"]
    assert result == {"error": "reclaim helper timed out", "status": "error"}


def test_resource_status_reads_completed_reclaim_without_publishing(sched, monkeypatch):
    state_path, report_path = sched._reclaim_paths()
    state_path.write_text(json.dumps({"running": True, "pid": 123, "started_at": 1, "token": "x"}))
    report_path.write_text(json.dumps({"token": "x", "status": "complete", "finished_at": 2}))
    monkeypatch.setattr(sched, "_write_reclaim_state", lambda *args: pytest.fail("read path wrote state"))

    errors = []
    def read_status():
        try:
            sched.resource_status()
        except Exception as exc:  # noqa: BLE001 - retain failures raised inside test threads
            errors.append(exc)

    threads = [threading.Thread(target=read_status, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert json.loads(state_path.read_text())["running"] is True


def test_dispatch_queue_attempts_reclaim_before_worker_slot_reader_stops_it(sched, monkeypatch):
    attempts = []
    monkeypatch.setattr(sched, "_try_reclaim_for_pending_local_launch", lambda: attempts.append("worker"))
    monkeypatch.setattr(sched, "_drain_pending_reviews", lambda tasks, rep: None)
    monkeypatch.setattr(sched, "local_slots_free", lambda: 0)

    sched.dispatch_ready(type("Report", (), {"dispatched": [], "errors": []})())

    assert attempts == ["worker"]


def test_pending_review_attempts_reclaim_before_review_slot_reader_stops_it(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.status = task.status.IN_REVIEW
    sched.state.get(task.id)["pending_reviews"] = [{"kind": "review"}]
    attempts = []
    monkeypatch.setattr(sched, "dispatch_queue", lambda: [])
    monkeypatch.setattr(sched, "_try_reclaim_for_pending_local_launch", lambda: attempts.append("review"))
    monkeypatch.setattr(sched, "_drain_pending_reviews", lambda tasks, rep: None)

    sched.dispatch_ready(type("Report", (), {"dispatched": [], "errors": []})())

    assert attempts == ["review"]


@pytest.mark.skipif(not __import__("os").environ.get("CG385_REAL_REPORT"),
                    reason="CG-385 disposable cgroup evidence only")
def test_real_disk_cache_reclaim_recovers_normal_tick_admission(sched, monkeypatch, request):
    """Measure partial then successful real reclaim while the disposable web app responds."""
    import os

    import garden.scheduler.resources as resources

    relative = next(line.split("::", 1)[1] for line in Path("/proc/self/cgroup").read_text().splitlines()
                    if line.startswith("0::"))
    group = Path("/sys/fs/cgroup") / relative.lstrip("/")
    cache_file = Path.cwd() / ".cg385-real-disk-cache.bin"
    cache_file.unlink(missing_ok=True)
    request.addfinalizer(lambda: cache_file.unlink(missing_ok=True))
    block = b"x" * (1024 * 1024)
    with cache_file.open("wb") as stream:
        for _ in range(600):
            stream.write(block)
        os.fsync(stream.fileno())
    with cache_file.open("rb") as stream:
        while stream.readinto(bytearray(1024 * 1024)):
            pass
    # Brief anonymous pressure ages the disk pages onto inactive_file without deleting
    # them; the subsequent bounded memory.reclaim is the operation under test.
    pressure_pages = bytearray(192 * 1024 * 1024)
    for offset in range(0, len(pressure_pages), 4096):
        pressure_pages[offset] = 1
    del pressure_pages

    def snapshot() -> dict[str, object]:
        status, events = resources._cgroup_memory_status(group)
        stat = resources._memory_stat(group) or {}
        return {"headroom_mb": status, "memory.current": int((group / "memory.current").read_text()),
                "memory.peak": int((group / "memory.peak").read_text()), "memory.stat": stat,
                "events": events, "memory.pressure": (group / "memory.pressure").read_text().splitlines()}

    before = snapshot()
    print("cg385 cache snapshot", before)
    assert int(before["memory.stat"]["inactive_file"]) >= 256 << 20
    minimum = int(before["headroom_mb"]) + 160
    original_effective = sched.effective
    values = {"resources.execution_cgroup": str(group), "resources.min_memory_available_mb": minimum,
              "resources.reclaim_max_mb": 32, "resources.reclaim_cooldown_seconds": 0,
              "resources.reclaim_timeout_seconds": 5}
    monkeypatch.setattr(sched, "effective", lambda key, default=None: values.get(key, original_effective(key, default)))
    monkeypatch.setattr(resources, "_memory_available_mb", lambda: 8192)
    monkeypatch.setattr(resources, "_cgroup_memory_available_mb", lambda: 8192)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    counter = sched.store.root / "served-counts.json"
    counter.write_text('{"reads": 0, "scans": 0}')
    server = subprocess.Popen([
        sys.executable, str(Path(__file__).parents[2] / "docs/validation/cg385/served_app.py"),
        str(sched.store.root), str(counter), str(port),
    ])
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while True:
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1).read()
            break
        except Exception:
            if time.monotonic() >= deadline:
                server.terminate()
                raise
            time.sleep(0.05)

    latencies: list[dict[str, object]] = []

    def http_probe(stage: str) -> None:
        started = time.monotonic()
        with urllib.request.urlopen(url + "/config", timeout=3) as response:
            response.read()
            latencies.append({"stage": stage, "status_code": response.status,
                              "seconds": time.monotonic() - started})

    def await_report(token: str | None = None) -> dict[str, object]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            http_probe("helper_running")
            try:
                result = json.loads(report_path.read_text())
            except (OSError, ValueError):
                result = {}
            if result and (token is None or result.get("token") != token):
                return result
            time.sleep(0.02)
        raise AssertionError("bounded reclaim helper did not publish its report")

    report_path = sched._reclaim_paths()[1]
    try:
        first = sched.tick()
        assert not first.dispatched
        partial_reclaim = await_report()
        assert partial_reclaim["status"] == "complete"
        after_partial = snapshot()
        assert int(after_partial["headroom_mb"]) < minimum

        values["resources.reclaim_max_mb"] = 256
        second = sched.tick()
        assert not second.dispatched
        recovered_reclaim = await_report(str(partial_reclaim["token"]))
        assert recovered_reclaim["status"] == "complete"
        after_reclaim = snapshot()
        assert int(after_reclaim["headroom_mb"]) >= minimum

        third = sched.tick()
        assert any(item.startswith("DM-001(work)") for item in third.dispatched)
        http_probe("after_admission")
    finally:
        server.terminate()
        server.wait(timeout=5)

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    source_tree = subprocess.check_output(["git", "rev-parse", "HEAD:src"], text=True).strip()
    test_tree = subprocess.check_output(["git", "rev-parse", "HEAD:tests"], text=True).strip()
    evidence = {
        "invocation": os.environ.get("CG385_INVOCATION", ""), "source_sha": head,
        "source_tree": source_tree, "test_tree": test_tree,
        "workload": "real disk-backed page cache, real bounded kernel reclaim, supervised test worker, and served HTTP requests",
        "synthetic": False, "cgroup": str(group), "limits": {
            "memory.high": (group / "memory.high").read_text().strip(),
            "memory.max": (group / "memory.max").read_text().strip(),
            "memory.swap.max": (group / "memory.swap.max").read_text().strip(),
        }, "minimum_headroom_mb": minimum, "before": before,
        "partial_reclaim": partial_reclaim, "after_partial": after_partial,
        "recovered_reclaim": recovered_reclaim, "after_reclaim": after_reclaim,
        "ticks": [first.dispatched, second.dispatched, third.dispatched],
        "http": {"requests": latencies, "max_seconds": max(item["seconds"] for item in latencies)},
        "cache_file_bytes": cache_file.stat().st_size,
    }
    Path(os.environ["CG385_REAL_REPORT"]).write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
