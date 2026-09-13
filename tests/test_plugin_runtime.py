from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from garden.plugins import (
    API_VERSION,
    ActionProvenance,
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


def test_json_lines_command_handshake_and_bounded_stderr(monkeypatch) -> None:
    def run(_argv, *, input, **_kwargs):
        request = [json.loads(line) for line in input.splitlines()]
        key = request[1]["idempotency_key"]
        stdout = "\n".join((
            json.dumps({"type": "handshake", "protocol_version": "garden.plugin-command/v1",
                        "capabilities": ["runner_transport"]}),
            json.dumps({"type": "response", "idempotency_key": key, "result": {"ok": True}}),
        ))
        return SimpleNamespace(stdout=stdout, stderr="0123456789", returncode=0)

    monkeypatch.setattr("garden.plugins.command.subprocess.run", run)
    reply = JsonLinesCommand(["adapter"], capability="runner_transport", max_stderr_bytes=4).invoke(
        "start", {"run": "one"}, idempotency_key="stable-key",
    )

    assert reply.result == {"ok": True}
    assert reply.stderr == "6789"
    assert reply.idempotency_key == "stable-key"


def test_json_lines_command_has_distinct_failure_types_and_redacted_audit(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr("garden.plugins.command.subprocess.run", lambda *_a, **_kw: SimpleNamespace(
        stdout='{"type":"handshake","protocol_version":"garden.plugin-command/v1","capabilities":[]}\n{}',
        stderr="", returncode=0,
    ))
    audit = tmp_path / "plugin-actions.jsonl"
    with pytest.raises(UnsupportedCapabilities):
        JsonLinesCommand(
            ["adapter"], capability="runner_transport", audit_path=audit,
            redact=lambda value: value.replace("runner_transport", "<redacted>"),
        ).invoke("start", {}, idempotency_key="request-1")
    recorded = json.loads(audit.read_text())
    assert recorded["idempotency_key"] == "request-1"
    assert "runner_transport" not in recorded["error"]

    monkeypatch.setattr("garden.plugins.command.subprocess.run", lambda *_a, **_kw: SimpleNamespace(
        stdout="not json", stderr="", returncode=0,
    ))
    with pytest.raises(MalformedOutput):
        JsonLinesCommand(["adapter"], capability="runner_transport").invoke("start", {})

    def timeout(*_args, **_kwargs):
        from subprocess import TimeoutExpired
        raise TimeoutExpired("adapter", 1)

    monkeypatch.setattr("garden.plugins.command.subprocess.run", timeout)
    with pytest.raises(CommandTimedOut):
        JsonLinesCommand(["adapter"], capability="runner_transport").invoke("start", {})
