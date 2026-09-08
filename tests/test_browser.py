from __future__ import annotations

import json
import subprocess

from garden.browser import _probe_child, classify_browser_failure, probe_browser_runtime
from garden.model import Status
from garden.scheduler import Scheduler
from garden.scheduler.report import TickReport
from garden.store import Store


def test_probe_classifies_missing_library_in_scrubbed_child(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs["env"])
        result = {"ready": False, "kind": "missing_libraries",
                  "diagnostic": "libnss3.so: cannot open shared object file"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    monkeypatch.setattr("garden.browser.subprocess.run", run)
    result = probe_browser_runtime({"worker_env": {"pass": []}}, worktree=tmp_path)
    assert result["kind"] == "missing_libraries"
    assert len(calls) == 2
    assert calls[0]["HOME"] != calls[1].get("HOME")


def test_probe_reports_service_to_worker_environment_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/unprivileged/chromium-libs")
    outcomes = iter([
        {"ready": False, "kind": "missing_libraries", "diagnostic": "libnss3.so missing"},
        {"ready": True, "kind": "ready", "diagnostic": "launched chromium"},
    ])

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(next(outcomes)), "")

    monkeypatch.setattr("garden.browser.subprocess.run", run)
    result = probe_browser_runtime({"worker_env": {"pass": []}}, worktree=tmp_path)
    assert result["kind"] == "environment_mismatch"
    assert "worker_env.pass" in result["diagnostic"]


def test_failure_categories_are_actionable():
    assert classify_browser_failure("Executable doesn't exist at /cache/chrome")[0] == "missing_executable"
    assert classify_browser_failure("libnspr4.so: cannot open shared object file")[0] == "missing_libraries"
    kind, diagnostic = classify_browser_failure("No usable sandbox")
    assert kind == "launch_failure"
    assert "sandbox" in diagnostic


def test_probe_child_distinguishes_missing_playwright(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "playwright", None)
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", None)
    result = _probe_child()
    assert result["kind"] == "missing_playwright"
    assert "browser-check dependencies" in str(result["diagnostic"])


def test_probe_child_classifies_missing_configured_executable(monkeypatch):
    import sys
    import types

    class Chromium:
        executable_path = "/cache/chromium"

        def launch(self):
            raise RuntimeError("Executable doesn't exist at /cache/chromium")

    class Context:
        def __enter__(self):
            return types.SimpleNamespace(chromium=Chromium())

        def __exit__(self, *_args):
            return False

    package = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = Context
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    result = _probe_child()
    assert result["kind"] == "missing_executable"
    assert "/cache/chromium" in str(result["diagnostic"])


def test_capture_hold_is_cached_recovers_once_and_does_not_hold_unrelated_work(sched, monkeypatch):
    capture_task = sched.store.task("DM-001")
    capture_task.extra["requires"] = ["captures"]
    sched.store.save(capture_task)
    unrelated = sched.store.task("DM-002")
    unrelated.depends_on = []
    unrelated.status = Status.READY
    sched.store.save(unrelated)
    probes = []

    def probe(*_args, **_kwargs):
        probes.append(True)
        return {"ready": False, "kind": "missing_libraries", "diagnostic": "install libnss3"}

    monkeypatch.setattr("garden.scheduler.browser.probe_browser_runtime", probe)
    dispatched = []
    def dispatch(task, **_kwargs):
        dispatched.append(task.id)
        task.status = Status.RUNNING

    monkeypatch.setattr(sched, "dispatch", dispatch)
    sched.dispatch_ready(TickReport())
    sched.dispatch_ready(TickReport())
    assert probes == [True]
    assert dispatched == ["DM-002"]
    assert capture_task.status == Status.READY
    assert capture_task.attempts == 0
    assert sched.state.get("DM-001")["infrastructure_hold"]["kind"] == "missing_libraries"

    sched.control()["browser_readiness"]["browser:demo"]["checked_at"] = "2000-01-01T00:00:00+00:00"
    sched.state.save()
    monkeypatch.setattr("garden.scheduler.browser.probe_browser_runtime", lambda *_args, **_kwargs: {
        "ready": True, "kind": "ready", "diagnostic": "launched chromium"})
    dispatched.clear()
    sched.dispatch_ready(TickReport())
    assert dispatched.count("DM-001") == 1


def test_advisory_capture_policy_skips_browser_hold_without_claiming_readiness(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.extra["requires"] = ["captures"]
    sched.store.save(task)
    sched.cfg.data.setdefault("review", {})["capture_infrastructure_policy"] = "advisory"
    monkeypatch.setattr(
        "garden.scheduler.browser.probe_browser_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("advisory admission must not probe")),
    )
    dispatched = []

    def dispatch(candidate, **_kwargs):
        dispatched.append(candidate.id)
        candidate.status = Status.RUNNING

    monkeypatch.setattr(sched, "dispatch", dispatch)
    sched.dispatch_ready(TickReport())

    assert "DM-001" in dispatched
    assert not sched.capture_required(task)
    assert "infrastructure_hold" not in sched.state.get(task.id)


def test_timeout_failure_cache_survives_scheduler_restart(sched, fake_github, monkeypatch):
    task = sched.store.task("DM-001")
    task.extra["requires"] = ["captures"]
    sched.store.save(task)
    calls = []

    def timeout(*args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("garden.browser.subprocess.run", timeout)
    assert not sched.browser_ready_for(task)
    assert len(calls) == 2  # scrubbed child and service comparison are both bounded

    restarted = Scheduler(Store(sched.store.root), github=fake_github)
    assert not restarted.browser_ready_for(restarted.store.task("DM-001"))
    assert len(calls) == 2


def test_recovery_preserves_failed_run_branch_feedback_and_dispatches_revision_once(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.extra["requires"] = ["captures"]
    task.status = Status.CHANGES_REQUESTED
    task.branch = "garden/dm-001-preserved"
    sched.store.save(task)
    feedback = "- keep the original capture failure and repair the runtime"
    sched.state.get(task.id)["pending_feedback"] = feedback
    sched.state.save()
    failed = sched.runs.new_run(task.id, "local", mode="work", run_id="preserved-failure")
    failed.status = "failed"
    failed.branch = task.branch
    failed.error = "capture runtime lacked libnss3"
    failed.result = {"captures": [], "verified": [{"not_done": True, "reason": "no PNGs"}]}
    failed.save()

    readiness = {"ready": False, "kind": "missing_libraries", "diagnostic": "install libnss3"}
    monkeypatch.setattr("garden.scheduler.browser.probe_browser_runtime", lambda *_a, **_k: readiness)
    first = sched.tick()
    assert not any(item.startswith("DM-001(") for item in first.dispatched)
    assert sched.store.task(task.id).branch == task.branch
    assert sched.state.get(task.id)["pending_feedback"] == feedback

    sched.control()["browser_readiness"]["browser:demo"]["checked_at"] = "2000-01-01T00:00:00+00:00"
    sched.state.save()
    readiness.update({"ready": True, "kind": "ready", "diagnostic": "launched chromium"})
    recovered = sched.tick()
    assert "DM-001(revise)" in recovered.dispatched
    assert sched.store.task(task.id).branch == task.branch
    assert sched.runs.runs_for(task.id)[0].error == "capture runtime lacked libnss3"
    revise_runs = [run for run in sched.runs.runs_for(task.id) if run.mode == "revise"]
    assert len(revise_runs) == 1 and revise_runs[0].env_snapshot["pending_feedback"] == feedback

    sched.tick()
    assert len([run for run in sched.runs.runs_for(task.id) if run.mode == "revise"]) == 1
