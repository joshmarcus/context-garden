from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from garden.plugins import (
    API_VERSION,
    ActionProvenance,
    CommandCancelled,
    CommandTimedOut,
    JsonLinesCommand,
    LoadedPlugin,
    LoadedPlugins,
    MalformedOutput,
    PluginError,
    PluginRedactor,
    PluginRunner,
    UnsupportedCapabilities,
    manifest_from_dict,
    resolve_runner_transport,
)
from garden.scheduler import Scheduler
from garden.scheduler.report import TickReport
from garden.store import Store


def _loaded(kind: str = "runner_transport") -> LoadedPlugins:
    manifest = manifest_from_dict({
        "name": "example-plugin",
        "distribution": "example-plugin-dist",
        "distribution_version": "1.2.0",
        "api_version": API_VERSION,
        "core_range": {"minimum": "0"},
        "capabilities": [{
            "name": "example-plugin/transport",
            "kind": kind,
            "entry_point": "example_plugin:transport",
        }],
    })
    return LoadedPlugins((LoadedPlugin(
        manifest, "{}", PluginRedactor(()), "sha256:" + "0" * 64,
        "sha256:" + "1" * 64,
    ),))


def test_runner_transport_resolves_and_records_provenance(monkeypatch, tmp_path: Path) -> None:
    class Transport:
        name = "example"
        detached = True
        remote = False

        def start(self, run, worktree, brief_text):
            run.started = (worktree, brief_text)

        def collect(self, run):
            return {"result": {"ok": True}}

    monkeypatch.setattr("garden.plugins.loading._load_object", lambda _reference: lambda **_kw: Transport())
    transport, provenance = resolve_runner_transport(_loaded(), "example-plugin/transport")
    runner = PluginRunner(transport, provenance, {}, None)
    run = SimpleNamespace(worktree=str(tmp_path), env_snapshot={}, save=lambda: None)

    runner.start(run, tmp_path, "brief")

    assert run.started == (tmp_path, "brief")
    assert run.env_snapshot["plugin_invocation"]["capability_name"] == "example-plugin/transport"


def test_scheduler_durably_collects_plugin_result_for_restart_recovery(
    sched, fake_github, monkeypatch,
) -> None:
    collected = []

    class Transport:
        name = "example"
        detached = True
        remote = False

        def start(self, run, worktree, brief_text):
            raise AssertionError("this test starts from a completed transport operation")

        def collect(self, run):
            collected.append(run.run_id)
            return {"result": {"status": "done", "summary": "plugin result"}}

    monkeypatch.setattr(
        "garden.plugins.loading._load_object", lambda _reference: lambda **_kw: Transport(),
    )
    sched.plugins = _loaded()
    task = sched.store.task("DM-001")
    task.runner = "example-plugin/transport"
    task.status = task.status.RUNNING
    sched.store.save(task)
    run = sched.runs.new_run(task.id, task.runner, mode="work")
    run.worktree = str(sched.worktree_for(task))
    run.env_snapshot = {}
    run.save()
    monkeypatch.setattr(sched, "_git_guard_check", lambda *_args: ["stop after collection"])
    monkeypatch.setattr(sched, "_git_guard_fail", lambda *_args: None)

    sched.finalize(task, run, sched.runner_for(task, run.runner), TickReport())

    durable = sched.runs.latest(task.id)
    assert collected == [run.run_id]
    assert durable.result == {"status": "done", "summary": "plugin result"}
    assert durable.env_snapshot["plugin_invocation"]["capability_name"] == task.runner
    restarted = Scheduler(Store(sched.store.root), github=fake_github)
    recovered = restarted.runs.latest(task.id)
    assert recovered.finished_at
    assert restarted._is_unreaped(restarted.store.task(task.id), recovered)


def test_plugin_runner_rejects_checkout_widening(monkeypatch, tmp_path: Path) -> None:
    class Transport:
        name = "example"
        detached = True
        remote = False

        def start(self, run, _worktree, _brief):
            run.worktree = str(tmp_path.parent)

        def collect(self, run):
            return {}

    runner = PluginRunner(Transport(), ActionProvenance(
        plugin_name="example-plugin", distribution_version="1.2.0",
        api_version=API_VERSION, capability_name="example-plugin/transport",
        configuration_digest="sha256:" + "0" * 64,
    ), {}, None)
    run = SimpleNamespace(worktree=str(tmp_path), env_snapshot={}, save=lambda: None)

    with pytest.raises(PluginError, match="attempted to widen"):
        runner.start(run, tmp_path, "brief")
    assert run.worktree == str(tmp_path.resolve())


def test_json_lines_command_handshake_and_bounded_stderr() -> None:
    script = """
import json, sys
request = [json.loads(sys.stdin.readline()) for _ in range(2)]
key = request[1]["idempotency_key"]
print(json.dumps({"type":"handshake","protocol_version":"garden.plugin-command/v1","capabilities":["runner_transport"]}))
print(json.dumps({"type":"response","idempotency_key":key,"result":{"ok":True}}))
sys.stderr.write("x" * 100000 + "tail")
"""
    reply = JsonLinesCommand(
        [sys.executable, "-c", script], capability="runner_transport", max_stderr_bytes=4,
    ).invoke(
        "start", {"run": "one"}, idempotency_key="stable-key",
    )

    assert reply.result == {"ok": True}
    assert reply.stderr == "tail"
    assert reply.idempotency_key == "stable-key"


def test_json_lines_command_has_distinct_failure_types_and_redacted_audit(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        JsonLinesCommand,
        "_run_process",
        lambda *_a: (
            0,
            '{"type":"handshake","protocol_version":"garden.plugin-command/v1",'
            '"capabilities":[]}\n{}',
            "",
        ),
    )
    audit = tmp_path / "plugin-actions.jsonl"
    with pytest.raises(UnsupportedCapabilities):
        JsonLinesCommand(
            ["adapter"], capability="runner_transport", audit_path=audit,
            redact=lambda value: value.replace("runner_transport", "<redacted>"),
        ).invoke("start", {}, idempotency_key="request-1")
    recorded = json.loads(audit.read_text())
    assert recorded["idempotency_key"] == "request-1"
    assert "runner_transport" not in recorded["error"]

    monkeypatch.setattr(JsonLinesCommand, "_run_process", lambda *_a: (0, "not json", ""))
    with pytest.raises(MalformedOutput):
        JsonLinesCommand(["adapter"], capability="runner_transport").invoke("start", {})

    def timeout(*_args):
        raise CommandTimedOut("timed out")

    monkeypatch.setattr(JsonLinesCommand, "_run_process", timeout)
    with pytest.raises(CommandTimedOut):
        JsonLinesCommand(["adapter"], capability="runner_transport").invoke("start", {})


def test_json_lines_command_observes_cancellation_during_execution() -> None:
    started = time.monotonic()

    with pytest.raises(CommandCancelled):
        JsonLinesCommand(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            capability="runner_transport", timeout_seconds=10,
            cancelled=lambda: time.monotonic() - started > 0.05,
        ).invoke("start", {})

    assert time.monotonic() - started < 2


def test_json_lines_command_cancellation_terminates_descendants_with_inherited_pipes() -> None:
    script = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(30)
"""
    started = time.monotonic()

    with pytest.raises(CommandCancelled):
        JsonLinesCommand(
            [sys.executable, "-c", script],
            capability="runner_transport", timeout_seconds=10,
            cancelled=lambda: time.monotonic() - started > 0.05,
        ).invoke("start", {})

    assert time.monotonic() - started < 2
