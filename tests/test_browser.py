from __future__ import annotations

import json
import subprocess

from garden.browser import classify_browser_failure, probe_browser_runtime
from garden.model import Status
from garden.scheduler.report import TickReport


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
    monkeypatch.setattr("garden.scheduler.browser.probe_browser_runtime", lambda *_args, **_kwargs: {
        "ready": True, "kind": "ready", "diagnostic": "launched chromium"})
    dispatched.clear()
    sched.dispatch_ready(TickReport())
    assert dispatched.count("DM-001") == 1
