from __future__ import annotations

import multiprocessing
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
