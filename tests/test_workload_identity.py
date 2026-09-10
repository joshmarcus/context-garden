from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from garden.config import executable_diff
from garden.workload_identity import (
    AuthorityRedactor,
    AuthorityRequest,
    ProviderAuthority,
    WorkloadIdentityError,
    WorkloadIdentityResolver,
)


class SyntheticProvider:
    adapter_version = 1
    capabilities = frozenset({"membership", "renewal", "revocation"})

    def __init__(self, config):
        self.config = config
        self.revoked = False
        self.resolutions = 0
        self.renewals = 0
        self.lock = threading.Lock()

    def _authority(self, request: AuthorityRequest, suffix: str = "") -> ProviderAuthority:
        with self.lock:
            self.resolutions += 1
            serial = self.resolutions
        return ProviderAuthority(
            {"token": f"synthetic-secret-{serial}{suffix}"},
            "synthetic://issuer",
            time.time() + request.lifetime_seconds,
            request.scopes,
            request.audience,
            request.run_identity,
            renewal_token=object(),
        )

    def resolve(self, request: AuthorityRequest) -> ProviderAuthority:
        authority = self._authority(request)
        changed = dict(self.config)
        return ProviderAuthority(
            authority.values,
            authority.issuer,
            time.time() + changed.get("lifetime_offset", request.lifetime_seconds),
            frozenset(changed.get("scopes", authority.scopes)),
            changed.get("audience", authority.audience),
            changed.get("membership", authority.membership),
            authority.renewal_token,
        )

    def renew(self, request: AuthorityRequest, authority: ProviderAuthority) -> ProviderAuthority:
        self.renewals += 1
        return self._authority(request, "-renewed")

    def validate(self, request: AuthorityRequest, authority: ProviderAuthority) -> None:
        if self.revoked:
            raise RuntimeError("revoked synthetic grant")


@pytest.fixture
def provider_module(monkeypatch):
    module = types.ModuleType("garden_test_identity_provider")
    module.SyntheticProvider = SyntheticProvider
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.__name__


def identity_config(provider_module: str, *, delivery: str = "environment", provider=None):
    binding = {"SERVICE_TOKEN": "token"} if delivery == "environment" else {"Authorization": "token"}
    return {"workload_identity": {
        "providers": {"synthetic": {
            "module": provider_module,
            "class": "SyntheticProvider",
            "version": 1,
            "capabilities": ["membership", "renewal", "revocation"],
            "config": provider or {},
        }},
        "references": {"packages/read": {
            "provider": "synthetic",
            "operation": "package.download",
            "audience": "packages.example",
            "scopes": ["packages:read"],
            "max_lifetime_seconds": 60,
            "delivery": delivery,
            "target": "worker",
            "bindings": binding,
        }},
        "boundaries": {"worker": {
            "reference": "packages/read", "operation": "package.download",
            "audience": "packages.example", "scopes": ["packages:read"],
            "lifetime_seconds": 30,
        }},
    }}


def resolve(resolver: WorkloadIdentityResolver, **kwargs):
    return resolver.resolve("packages/read", "package.download", "packages.example",
                            "automation:run-123", 30, {"packages:read"}, target="worker", **kwargs)


def test_resolves_bounded_authority_and_redacts_values(provider_module):
    resolver = WorkloadIdentityResolver(identity_config(provider_module))
    with resolver.operation("packages/read", "package.download", "packages.example",
                            "automation:run-123", 30, {"packages:read"},
                            target="worker") as authority:
        environment = authority.subprocess_env({"PATH": "/bin"}, "worker")
        assert environment["SERVICE_TOKEN"].startswith("synthetic-secret-")
        assert authority.metadata.automation_identity == "automation:run-123"
        assert authority.metadata.principal_kind == "automation"
        assert authority.metadata.issuer == "synthetic://issuer"
        assert "synthetic-secret" not in repr(authority)
        assert "synthetic-secret" not in repr(authority._authority)
        redactor = authority.redactor()
        assert redactor.redact(f"leaked {environment['SERVICE_TOKEN']}") == "leaked <redacted>"
        assert "synthetic-secret" not in repr(redactor)
    with pytest.raises(WorkloadIdentityError, match="closed"):
        authority.subprocess_env({}, "worker")


def test_stream_redaction_covers_split_values_and_nested_payloads():
    redactor = AuthorityRedactor(("synthetic-secret",))
    stream = redactor.stream()
    output = stream.feed("before synthetic-") + stream.feed("secret after") + stream.finish()
    assert output == "before <redacted> after"
    assert redactor.redact_data({"final": "synthetic-secret", "rows": ["safe"]}) == {
        "final": "<redacted>", "rows": ["safe"],
    }


def test_supervisor_redacts_local_run_records_before_completion(tmp_path, monkeypatch):
    from garden.run_supervisor import redact_authority_outputs

    monkeypatch.setenv("GARDEN_WORKLOAD_IDENTITY_BINDINGS", "SERVICE_TOKEN")
    monkeypatch.setenv("SERVICE_TOKEN", "synthetic-secret")
    for name in ("stdout.json", "stderr.log", "final.md"):
        (tmp_path / name).write_text(f"prefix synthetic-secret in {name}")
    redact_authority_outputs(tmp_path)
    for name in ("stdout.json", "stderr.log", "final.md"):
        assert (tmp_path / name).read_text() == f"prefix <redacted> in {name}"


def test_atomic_final_replacement_is_redacted_before_publication(tmp_path):
    from garden.run_supervisor import _FinalOutput

    raw = tmp_path / ".final.raw"
    final = tmp_path / "final.md"
    os.mkfifo(raw, 0o600)
    collector = _FinalOutput(raw, final, AuthorityRedactor(("synthetic-secret",)))
    replacement = tmp_path / "replacement"
    replacement.write_text("result synthetic-secret")
    replacement.replace(raw)

    collector.finish()

    assert final.read_text() == "result <redacted>"
    assert not raw.exists()


def test_running_supervisor_streams_redacted_output_and_fails_on_rotated_renewal(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = identity_config("tests.test_workload_identity")
    config["workload_identity"]["boundaries"]["worker"]["lifetime_seconds"] = 1
    env = dict(os.environ)
    for name in ("GARDEN_HEAVY_EXECUTION", "GARDEN_EXECUTION_OWNER",
                 "GARDEN_EXECUTION_RUN_DIR", "GARDEN_OWNER_SCOPED"):
        env.pop(name, None)
    env.update({
        "GARDEN_WORKLOAD_IDENTITY_CONFIG": json.dumps(config),
        "GARDEN_WORKLOAD_IDENTITY_TARGET": "worker",
        "GARDEN_WORKLOAD_IDENTITY_RUN": "automation:live-run",
        "GARDEN_RAW_FINAL_PATH": str(run_dir / ".final.raw"),
        "GARDEN_FINAL_PATH": str(run_dir / "final.md"),
    })
    script = (
        f"{sys.executable} -c \"import os,time; token=os.environ['SERVICE_TOKEN']; "
        "print('prefix '+token[:10], end='', flush=True); time.sleep(.1); "
        "print(token[10:]+' suffix'+('x'*40), flush=True); "
        "f=open(os.environ['GARDEN_RAW_FINAL_PATH'],'w'); f.write(token[:10]); f.flush(); "
        "time.sleep(.1); f.write(token[10:]); f.close(); time.sleep(5)\""
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "garden.run_supervisor", str(run_dir), script], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 2
    persisted = ""
    while time.monotonic() < deadline and proc.poll() is None:
        if (run_dir / "stdout.json").exists():
            persisted = (run_dir / "stdout.json").read_text()
            assert "synthetic-secret" not in persisted
            if (run_dir / "final.md").exists():
                assert "synthetic-secret" not in (run_dir / "final.md").read_text()
            if "<redacted>" in persisted:
                break
        time.sleep(.02)
    assert "<redacted>" in persisted
    proc.wait(timeout=4)
    assert json.loads((run_dir / "identity_error.json").read_text())["error"].endswith(
        "renewed with rotated authority"
    )
    assert "synthetic-secret" not in (run_dir / "stdout.json").read_text()
    assert (run_dir / "final.md").read_text() == "<redacted>"


def test_remote_process_is_stopped_when_running_authority_is_revoked(provider_module):
    from garden.remote_worker import _wait_for_process

    authority = resolve(WorkloadIdentityResolver(identity_config(provider_module)))

    class Heartbeat:
        def ensure_not_failed(self):
            return None

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    timer = threading.Timer(.1, setattr, args=(authority._provider, "revoked", True))
    timer.start()
    try:
        with pytest.raises(WorkloadIdentityError, match="unavailable"):
            _wait_for_process(proc, Heartbeat(), interval=.02,
                              enforce_authority=authority.enforce_current)
        assert proc.poll() is not None
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()


@pytest.mark.parametrize("change, message", [
    ({"audience": "wrong.example"}, "different audience"),
    ({"scopes": ["packages:write"]}, "mismatched scopes"),
    ({"membership": "human:operator"}, "different run membership"),
    ({"lifetime_offset": 300}, "exceeding the requested lifetime"),
])
def test_provider_output_fails_closed(provider_module, change, message):
    resolver = WorkloadIdentityResolver(identity_config(provider_module, provider=change))
    with pytest.raises(WorkloadIdentityError, match=message):
        resolve(resolver)


def test_request_cannot_broaden_reference_or_register_provider(provider_module):
    resolver = WorkloadIdentityResolver(identity_config(provider_module))
    with pytest.raises(WorkloadIdentityError, match="scope is not allowed"):
        resolver.resolve("packages/read", "package.download", "packages.example",
                         "automation:run-123", 30, {"packages:write"}, target="worker")
    with pytest.raises(WorkloadIdentityError, match="operation or audience"):
        resolver.resolve("packages/read", "package.publish", "packages.example",
                         "automation:run-123", 30, set(), target="worker")
    with pytest.raises(WorkloadIdentityError, match="unknown workload identity"):
        resolver.resolve("task-output-provider", "package.download", "packages.example",
                         "automation:run-123", 30, set(), target="worker")
    assert executable_diff({}, identity_config(provider_module)) == ["workload_identity"]


def test_renews_then_detects_revocation(provider_module):
    resolver = WorkloadIdentityResolver(identity_config(provider_module))
    authority = resolve(resolver)
    authority._authority = ProviderAuthority(
        authority._authority.values, "synthetic://issuer", time.time() - 1,
        frozenset({"packages:read"}), "packages.example", "automation:run-123",
    )
    assert authority.subprocess_env({}, "worker")["SERVICE_TOKEN"].endswith("-renewed")
    authority._provider.revoked = True
    with pytest.raises(WorkloadIdentityError, match="unavailable: RuntimeError"):
        authority.subprocess_env({}, "worker")


def test_concurrent_runs_get_distinct_authority(provider_module):
    resolver = WorkloadIdentityResolver(identity_config(provider_module))
    barrier = threading.Barrier(4)
    values = []

    def worker(number: int) -> None:
        barrier.wait()
        authority = resolver.resolve("packages/read", "package.download", "packages.example",
                                     f"automation:run-{number}", 30, {"packages:read"},
                                     target="worker")
        values.append(authority.subprocess_env({}, "worker")["SERVICE_TOKEN"])

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(values)) == 4


def test_local_environment_and_remote_protocol_delivery_share_policy(provider_module):
    local = resolve(WorkloadIdentityResolver(identity_config(provider_module)))
    remote = resolve(WorkloadIdentityResolver(identity_config(provider_module, delivery="headers")))
    assert set(local.subprocess_env({}, "worker")) == {"SERVICE_TOKEN"}
    assert set(remote.request_headers({}, "worker")) == {"Authorization"}
    with pytest.raises(WorkloadIdentityError, match="not configured for protocol"):
        local.request_headers({}, "worker")
    with pytest.raises(WorkloadIdentityError, match="not configured for subprocess"):
        remote.subprocess_env({}, "worker")


def test_authority_cannot_be_delivered_to_another_named_target(provider_module):
    authority = resolve(WorkloadIdentityResolver(identity_config(provider_module)))
    with pytest.raises(WorkloadIdentityError, match="not configured for this subprocess"):
        authority.subprocess_env({}, "setup")


def test_local_runner_delivers_only_to_worker_process(sched, provider_module, monkeypatch):
    from tests.inprocess import InProcessRunner

    config = identity_config(provider_module)
    config["worker_env"] = {"pass": ["FAKE_CLAUDE_ECHO_ENV"]}
    sched.cfg.data.update(config)
    monkeypatch.setenv("FAKE_CLAUDE_ECHO_ENV", "SERVICE_TOKEN")
    captured = {}
    original = InProcessRunner.launch

    def launch(self, run, worktree, brief_path, env):
        captured.update(env)
        return original(self, run, worktree, brief_path, env)

    monkeypatch.setattr(InProcessRunner, "launch", launch)
    assert sched.tick().dispatched == ["DM-001(work)"]
    assert captured["SERVICE_TOKEN"].startswith("synthetic-secret-")
    run = sched.runs.latest("DM-001")
    audit = (run.path / "workload_identity.json").read_text()
    assert "synthetic://issuer" in audit
    assert captured["SERVICE_TOKEN"] not in audit
    assert captured["SERVICE_TOKEN"] not in (run.path / "command.txt").read_text()
    persisted = (run.path / "stdout.json").read_text() + (run.path / "stderr.log").read_text()
    assert captured["SERVICE_TOKEN"] not in persisted
    assert "<redacted>" in persisted


def test_local_identity_failure_uses_environment_error_recovery(sched, provider_module):
    config = identity_config(provider_module, provider={"membership": "human:operator"})
    sched.cfg.data.update(config)
    assert sched.tick().dispatched == ["DM-001(work)"]
    run = sched.runs.latest("DM-001")
    assert not (run.path / "stdout.json").exists()
    report = sched.tick()
    assert "DM-001 -> ready (env_error: workload_identity)" in report.transitions
    assert sched.store.tasks()["DM-001"].attempts == 0


def test_setup_environment_excludes_worker_authority(monkeypatch):
    from garden.run_supervisor import setup_environment

    monkeypatch.setenv("SERVICE_TOKEN", "synthetic-secret")
    monkeypatch.setenv("GARDEN_WORKLOAD_IDENTITY_BINDINGS", "SERVICE_TOKEN")
    monkeypatch.setenv("ORDINARY_VALUE", "kept")
    env = setup_environment()
    assert "SERVICE_TOKEN" not in env
    assert "GARDEN_WORKLOAD_IDENTITY_BINDINGS" not in env
    assert env["ORDINARY_VALUE"] == "kept"


def test_unavailable_or_misdeclared_provider_is_actionable(provider_module):
    config = identity_config(provider_module)
    config["workload_identity"]["providers"]["synthetic"]["capabilities"] = ["renewal"]
    with pytest.raises(WorkloadIdentityError, match="capabilities do not match"):
        WorkloadIdentityResolver(config)
    config = identity_config("missing_provider_module")
    with pytest.raises(WorkloadIdentityError, match="is unavailable"):
        WorkloadIdentityResolver(config)
