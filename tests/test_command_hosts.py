from __future__ import annotations

import json
import sys
import time
from dataclasses import replace

import pytest

from garden.hosts import (
    CommandProvider,
    CommandResult,
    CommandTransport,
    EnvironmentProfile,
    EnvironmentStop,
    HostLifecycle,
    HostRequirements,
    JsonStateStore,
    PoolDeclaration,
)

DEFAULT_ACQUIRE = object()


class Wrapper:
    def __init__(self):
        self.hosts = []
        self.calls = []
        self.ready = True
        self.ready_error = ""
        self.acquire_response = DEFAULT_ACQUIRE
        self.admissions = {}
        self.admission = {
            "eligible": True,
            "host_class": "large",
            "environment": "linux",
            "capabilities": ["python", "browser"],
            "memory_available_mib": 8192,
            "disk_free_gib": 50,
        }

    def run(self, argv, stdin, *, timeout_seconds):
        action = argv[-1]
        request = json.loads(stdin)
        self.calls.append((tuple(argv), request, timeout_seconds))
        if action == "inspect":
            value = self.hosts
        elif action == "acquire":
            value = (
                self.acquire_response
                if self.acquire_response is not DEFAULT_ACQUIRE
                else {
                    **request,
                    "provider_id": "provider-1",
                    "state": "ready",
                }
            )
            if isinstance(value, dict):
                self.hosts[:] = [value]
        elif action == "ready":
            if self.ready_error == "provider":
                return CommandResult(tuple(argv), stdin, b"", b"probe failed", 9)
            if self.ready_error == "json":
                return CommandResult(tuple(argv), stdin, b"not-json", b"", 0)
            value = {
                "workspace": self.ready,
                "revision": self.ready,
                "provisioned": self.ready,
                "harness_login": self.ready,
                "smoke_probe": self.ready,
                "detail": "verified" if self.ready else "checkout reconciliation failed",
            }
        elif action == "admit":
            lease_id = f"lease-{request['acquisition_id']}"
            if request["heavy"] and self.admissions:
                value = {
                    **self.admission,
                    "eligible": False,
                    "measured_at": time.time(),
                    "detail": "heavy-check capacity is full",
                }
            else:
                value = {
                    "measured_at": time.time(),
                    "lease_id": lease_id,
                    "lease_expires_at": time.time() + request["lease_seconds"],
                    **self.admission,
                }
                if value["lease_id"]:
                    self.admissions[value["lease_id"]] = value
        elif action == "renew-admission":
            value = self.admissions.get(request["lease_id"])
            if value is None:
                return CommandResult(tuple(argv), stdin, b"", b"lease lost", 4)
            value = {**value, "lease_expires_at": time.time() + 120}
            self.admissions[request["lease_id"]] = value
        elif action == "release-admission":
            self.admissions.pop(request["lease_id"], None)
            value = {"released": True}
        elif action == "retire":
            value = {**self.hosts[0], "state": "terminated"}
            self.hosts[:] = [value]
        elif action == "release":
            value = {**self.hosts[0], "state": "stopped"}
            self.hosts[:] = [value]
        elif action == "start":
            value = {**self.hosts[0], "state": "ready"}
            self.hosts[:] = [value]
        else:
            value = self.hosts[0]
        output = json.dumps(value).encode()
        return CommandResult(tuple(argv), stdin, output, b"", 0)


def command_pool(**changes):
    profile = EnvironmentProfile(
        name="worker",
        version="v1",
        image="image-v1",
        bootstrap_version="tools-v1",
        cpu=2,
        memory_mib=4096,
        disk_gib=20,
    )
    value = PoolDeclaration(
        name="workers",
        owner="team",
        purpose="worker",
        provider="command",
        profile=profile,
        enabled=True,
        desired=1,
        maximum=1,
        provider_options={"command": ["host-wrapper", "--profile", "worker"], "timeout_seconds": 7},
    )
    return replace(value, **changes)


def test_command_transport_preserves_argv_stdin_exit_and_output(tmp_path):
    script = tmp_path / "wrapper.py"
    script.write_text(
        "import sys\n"
        "data=sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(data+b'\\x00out')\n"
        "sys.stderr.buffer.write(b'err\\xff')\n"
        "raise SystemExit(23)\n"
    )
    argv = [sys.executable, str(script), "argument with spaces"]
    result = CommandTransport().run(argv, b"input\n", timeout_seconds=2)

    assert result.argv == tuple(argv)
    assert result.stdin == b"input\n"
    assert result.stdout == b"input\n\x00out"
    assert result.stderr == b"err\xff"
    assert result.exit_code == 23


def test_acquisition_is_durable_idempotent_and_warm_reuse_waits_for_terminal_run(tmp_path):
    wrapper = Wrapper()
    path = tmp_path / "hosts.json"
    lifecycle = HostLifecycle(
        {"command": CommandProvider(wrapper)}, JsonStateStore(path), retry_delay=lambda _: None
    )
    spec = command_pool()

    first = lifecycle.acquire_ready(
        spec,
        workspace="/work/product",
        revision="abc123",
        harness="codex",
        process_terminal=lambda _: True,
    )
    lifecycle.attach_run(first.provider_id, "run-1")
    with pytest.raises(EnvironmentStop, match="no ready host"):
        lifecycle.acquire_ready(
            spec,
            workspace="/work/product",
            revision="abc123",
            harness="codex",
            process_terminal=lambda _: False,
        )

    restarted = HostLifecycle({"command": CommandProvider(wrapper)}, JsonStateStore(path))
    reused = restarted.acquire_ready(
        spec,
        workspace="/work/product",
        revision="def456",
        harness="codex",
        process_terminal=lambda run_id: run_id == "run-1",
    )

    assert reused.provider_id == first.provider_id
    assert [call[0][-1] for call in wrapper.calls].count("acquire") == 1
    ready_request = [call[1] for call in wrapper.calls if call[0][-1] == "ready"][-1]
    assert ready_request == {
        "provider_id": "provider-1",
        "workspace": "/work/product",
        "revision": "def456",
        "harness": "codex",
        "read_only": True,
    }
    assert "provider-1" not in restarted.orphaned(process_terminal=lambda _: False)


def test_unbound_acquisition_is_exclusive_across_lifecycle_instances(tmp_path):
    wrapper = Wrapper()
    path = tmp_path / "hosts.json"
    first = HostLifecycle({"command": CommandProvider(wrapper)}, JsonStateStore(path))
    second = HostLifecycle({"command": CommandProvider(wrapper)}, JsonStateStore(path))
    kwargs = {
        "workspace": "/work/product",
        "revision": "abc123",
        "harness": "codex",
        "process_terminal": lambda _: True,
    }

    host = first.acquire_ready(command_pool(), **kwargs)
    with pytest.raises(EnvironmentStop, match="no ready host"):
        second.acquire_ready(command_pool(), **kwargs)
    lease = json.loads(path.read_text())["leases"][host.provider_id]
    assert lease["run_id"] == "" and lease["released"] is False

    first.cancel_acquisition(host.provider_id)
    assert second.acquire_ready(command_pool(), **kwargs).provider_id == host.provider_id


def test_stale_unbound_acquisition_recovers_after_bound(tmp_path):
    wrapper = Wrapper()
    path = tmp_path / "hosts.json"
    lifecycle = HostLifecycle(
        {"command": CommandProvider(wrapper)}, JsonStateStore(path), reservation_seconds=10
    )
    kwargs = {
        "workspace": "/work/product",
        "revision": "abc123",
        "harness": "codex",
        "process_terminal": lambda _: True,
    }

    lifecycle.acquire_ready(command_pool(), now=lambda: 0, **kwargs)
    with pytest.raises(EnvironmentStop, match="no ready host"):
        lifecycle.acquire_ready(command_pool(), now=lambda: 9, **kwargs)
    assert (
        lifecycle.acquire_ready(command_pool(), now=lambda: 10, **kwargs).provider_id
        == "provider-1"
    )


def test_readiness_stop_recovers_without_creating_another_host_and_can_cancel_or_retire(tmp_path):
    wrapper = Wrapper()
    wrapper.ready = False
    path = tmp_path / "hosts.json"
    lifecycle = HostLifecycle({"command": CommandProvider(wrapper)}, JsonStateStore(path))
    spec = command_pool()
    kwargs = {
        "workspace": "/work/product",
        "revision": "abc123",
        "harness": "codex",
        "process_terminal": lambda _: True,
    }

    with pytest.raises(EnvironmentStop, match="checkout reconciliation failed"):
        lifecycle.acquire_ready(spec, **kwargs)
    assert json.loads(path.read_text())["environment_stops"]["workers"]["detail"] == (
        "workers-0: checkout reconciliation failed"
    )
    wrapper.ready = True
    host = lifecycle.acquire_ready(spec, **kwargs)
    assert "workers" not in json.loads(path.read_text())["environment_stops"]
    lifecycle.attach_run(host.provider_id, "terminal-run")
    assert lifecycle.orphaned(process_terminal=lambda _: True) == ["provider-1"]
    lifecycle.cancel_acquisition(host.provider_id)
    assert lifecycle.orphaned(process_terminal=lambda _: True) == []
    host = lifecycle.acquire_ready(spec, **kwargs)
    lifecycle.attach_run(host.provider_id, "finished-run")
    released = lifecycle.release(spec, host.provider_id)
    assert released.state.value == "stopped"
    assert lifecycle.orphaned(process_terminal=lambda _: True) == []
    with pytest.raises(EnvironmentStop, match="no ready host"):
        lifecycle.acquire_ready(
            spec,
            workspace="/work/product",
            revision="abc123",
            harness="codex",
            process_terminal=lambda _: False,
        )
    assert not any(call[0][-1] == "start" for call in wrapper.calls)
    reused = lifecycle.acquire_ready(spec, **kwargs)
    assert reused.provider_id == host.provider_id
    assert [call[0][-1] for call in wrapper.calls].count("start") == 1
    retired = lifecycle.destroy(spec, host.provider_id, delete_storage=True)

    assert retired.state.value == "terminated"
    assert [call[0][-1] for call in wrapper.calls].count("acquire") == 1


def test_incompatible_warm_host_is_not_admitted(tmp_path):
    wrapper = Wrapper()
    lifecycle = HostLifecycle(
        {"command": CommandProvider(wrapper)}, JsonStateStore(tmp_path / "hosts.json")
    )
    lifecycle.reconcile(command_pool())
    wrapper.hosts[0]["bootstrap_version"] = "older-tools"

    with pytest.raises(EnvironmentStop, match="no ready host"):
        lifecycle.acquire_ready(
            command_pool(),
            workspace="/work",
            revision="abc",
            harness="codex",
            process_terminal=lambda _: True,
        )
    assert not any(call[0][-1] == "ready" for call in wrapper.calls)


def test_pool_bounds_and_ttl_retire_expired_host(tmp_path):
    wrapper = Wrapper()
    lifecycle = HostLifecycle(
        {"command": CommandProvider(wrapper)}, JsonStateStore(tmp_path / "hosts.json")
    )
    with pytest.raises(ValueError, match="capacity"):
        lifecycle.plan(command_pool(desired=2))
    spec = command_pool(maximum_age_minutes=1)
    host = lifecycle.acquire_ready(
        spec,
        workspace="/work",
        revision="abc",
        harness="codex",
        process_terminal=lambda _: True,
        now=lambda: 0,
    )
    lifecycle.attach_run(host.provider_id, "old-run")
    with pytest.raises(EnvironmentStop):
        lifecycle.acquire_ready(
            spec,
            workspace="/work",
            revision="def",
            harness="codex",
            process_terminal=lambda _: True,
            now=lambda: 61,
        )
    assert "retire" in [call[0][-1] for call in wrapper.calls]
    replacement = lifecycle.acquire_ready(
        spec,
        workspace="/work",
        revision="def",
        harness="codex",
        process_terminal=lambda _: True,
        now=lambda: 62,
    )
    assert replacement.state.value == "ready"
    assert [call[0][-1] for call in wrapper.calls].count("acquire") == 2


def test_repeated_warm_reuse_preserves_original_ttl(tmp_path):
    wrapper = Wrapper()
    lifecycle = HostLifecycle(
        {"command": CommandProvider(wrapper)}, JsonStateStore(tmp_path / "hosts.json")
    )
    spec = command_pool(maximum_age_minutes=1)
    kwargs = {
        "workspace": "/work",
        "harness": "codex",
        "process_terminal": lambda _: True,
    }
    host = lifecycle.acquire_ready(spec, revision="one", now=lambda: 0, **kwargs)
    lifecycle.attach_run(host.provider_id, "run-one")
    lifecycle.release(spec, host.provider_id)
    lifecycle.acquire_ready(spec, revision="two", now=lambda: 30, **kwargs)
    lifecycle.attach_run(host.provider_id, "run-two")
    lifecycle.release(spec, host.provider_id)

    with pytest.raises(EnvironmentStop, match="no ready host"):
        lifecycle.acquire_ready(spec, revision="three", now=lambda: 60, **kwargs)

    assert [call[0][-1] for call in wrapper.calls].count("retire") == 1


@pytest.mark.parametrize("failure", ["provider", "json"])
def test_readiness_wrapper_error_records_environment_stop_and_recovers(tmp_path, failure):
    wrapper = Wrapper()
    wrapper.ready_error = failure
    path = tmp_path / "hosts.json"
    lifecycle = HostLifecycle({"command": CommandProvider(wrapper)}, JsonStateStore(path))
    spec = command_pool()
    kwargs = {
        "workspace": "/work",
        "revision": "abc",
        "harness": "codex",
        "process_terminal": lambda _: True,
    }

    with pytest.raises(EnvironmentStop, match="workers-0: command ready"):
        lifecycle.acquire_ready(spec, **kwargs)
    detail = json.loads(path.read_text())["environment_stops"]["workers"]["detail"]
    assert "command ready" in detail

    wrapper.ready_error = ""
    assert lifecycle.acquire_ready(spec, **kwargs).provider_id == "provider-1"
    assert "workers" not in json.loads(path.read_text())["environment_stops"]


@pytest.mark.parametrize("payload", [[], None, "host", 7])
def test_malformed_acquire_response_records_environment_stop_and_recovers(tmp_path, payload):
    wrapper = Wrapper()
    wrapper.acquire_response = payload
    path = tmp_path / "hosts.json"
    lifecycle = HostLifecycle({"command": CommandProvider(wrapper)}, JsonStateStore(path))
    kwargs = {
        "workspace": "/work",
        "revision": "abc",
        "harness": "codex",
        "process_terminal": lambda _: True,
    }

    with pytest.raises(EnvironmentStop, match="command acquire returned invalid host facts"):
        lifecycle.acquire_ready(command_pool(), **kwargs)
    assert (
        "invalid host facts"
        in json.loads(path.read_text())["environment_stops"]["workers"]["detail"]
    )

    wrapper.acquire_response = DEFAULT_ACQUIRE
    assert lifecycle.acquire_ready(command_pool(), **kwargs).provider_id == "provider-1"

def _requirements(**changes):
    value = HostRequirements(
        activity="check", host_class="large", environment="linux",
        capabilities=("python",), memory_mib=4096, disk_gib=20, heavy=True,
    )
    return replace(value, **changes)


def test_host_local_admission_routes_on_fresh_resources_and_capabilities(tmp_path):
    wrapper = Wrapper()
    path = tmp_path / "hosts.json"
    lifecycle = HostLifecycle({"command": CommandProvider(wrapper)}, JsonStateStore(path))

    host = lifecycle.acquire_ready(
        command_pool(), workspace="/work", revision="abc", harness="codex",
        process_terminal=lambda _: True, requirements=_requirements(),
    )

    request = next(call[1] for call in wrapper.calls if call[0][-1] == "admit")
    assert request["activity"] == "check"
    assert request["capabilities"] == ["python"]
    assert request["memory_mib"] == 4096
    lease = json.loads(path.read_text())["leases"][host.provider_id]["admission"]
    assert lease["host_class"] == "large"
    assert lease["memory_available_mib"] == 8192
    lifecycle.cancel_acquisition(host.provider_id, pool=command_pool())
    assert not wrapper.admissions

    host = lifecycle.acquire_ready(
        command_pool(), workspace="/work", revision="abc", harness="codex",
        process_terminal=lambda _: True, requirements=_requirements(),
    )
    lifecycle.release(command_pool(), host.provider_id)
    assert not wrapper.admissions
    assert "release-admission" in [call[0][-1] for call in wrapper.calls]


@pytest.mark.parametrize(
    ("admission_change", "requirements_change", "reason"),
    [
        ({"measured_at": 1}, {}, "resource probe is stale"),
        ({"measured_at": float("nan")}, {}, "resource probe timestamp is invalid"),
        ({"host_class": "small"}, {}, "host class 'small'"),
        ({"environment": "other"}, {}, "environment 'other'"),
        ({"capabilities": []}, {}, "missing capabilities: python"),
        ({"memory_available_mib": 1000}, {}, "host memory 1000 MiB"),
        ({"disk_free_gib": 1}, {}, "host disk 1 GiB"),
        ({"lease_id": "", "lease_expires_at": 0}, {}, "lease is missing or expired"),
    ],
)
def test_ineligible_host_admission_fails_closed_with_alias(
    tmp_path, admission_change, requirements_change, reason
):
    wrapper = Wrapper()
    wrapper.admission.update(admission_change)
    if "measured_at" in admission_change:
        # Wrapper normally supplies its measured time; retain the deliberately stale value.
        original = wrapper.run

        def run(argv, stdin, *, timeout_seconds):
            result = original(argv, stdin, timeout_seconds=timeout_seconds)
            if argv[-1] == "admit" and result.exit_code == 0:
                value = json.loads(result.stdout)
                value["measured_at"] = admission_change["measured_at"]
                return replace(result, stdout=json.dumps(value).encode())
            return result

        wrapper.run = run
    lifecycle = HostLifecycle(
        {"command": CommandProvider(wrapper)}, JsonStateStore(tmp_path / "hosts.json")
    )

    with pytest.raises(EnvironmentStop, match=reason):
        lifecycle.acquire_ready(
            command_pool(), workspace="/work", revision="abc", harness="codex",
            process_terminal=lambda _: True, requirements=_requirements(**requirements_change),
        )
    detail = json.loads((tmp_path / "hosts.json").read_text())["environment_stops"]["workers"]["detail"]
    assert detail.startswith("workers-0:")
    assert "provider-1" not in detail
    assert not wrapper.admissions


@pytest.mark.parametrize("activity", ["work", "setup", "base_probe", "check", "review"])
def test_all_activities_share_host_owned_heavy_lease_across_controllers(tmp_path, activity):
    wrapper = Wrapper()
    first = HostLifecycle(
        {"command": CommandProvider(wrapper)}, JsonStateStore(tmp_path / "controller-one.json")
    )
    second = HostLifecycle(
        {"command": CommandProvider(wrapper)}, JsonStateStore(tmp_path / "controller-two.json")
    )
    kwargs = dict(
        pool=command_pool(), workspace="/work", revision="abc", harness="codex",
        process_terminal=lambda _: True, requirements=_requirements(activity=activity),
    )
    host = first.acquire_ready(**kwargs)

    with pytest.raises(EnvironmentStop, match="workers-0: heavy-check capacity is full"):
        second.acquire_ready(**kwargs)
    renewed = first.renew_admission(command_pool(), host.provider_id)
    assert renewed.lease_id

    wrapper.admissions.clear()
    with pytest.raises(EnvironmentStop, match="host admission lease lost"):
        first.renew_admission(command_pool(), host.provider_id)
