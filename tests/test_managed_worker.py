import json
from dataclasses import replace

import pytest

from garden.hosts import EnvironmentProfile, HostDeclaration, PoolDeclaration
from garden.hosts.deadline import ExternalDeadlineCommand
from garden.hosts.ec2 import EC2Provider
from garden.managed_worker import AttributedClient, claim_with_retry, host_slot, resources, run
from garden.remote_worker import WorkerRequestError


def declaration(**options):
    profile = EnvironmentProfile("worker", "1", "ami-pinned", "1", 4, 16384, 40,
        endpoint="https://garden.example", enrollment_secret_ref="arn:secret", provider_options=options)
    return HostDeclaration("host", "operation", PoolDeclaration("pool", "owner", "test", "ec2", profile))


def test_host_slot_prevents_second_process_entry(tmp_path):
    with host_slot(tmp_path):
        with pytest.raises(BlockingIOError):
            with host_slot(tmp_path):
                pytest.fail("overlapping work admitted")
    with host_slot(tmp_path):
        pass


def test_resource_gate_precedes_claim(tmp_path, monkeypatch):
    import tempfile

    import garden.managed_worker as worker
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(worker, "resources", lambda root: {"memory_available_bytes": 0, "disk_free_bytes": 0})
    monkeypatch.setattr(worker.AttributedClient, "post", lambda *args: pytest.fail("resource gate bypassed"))
    run({"work_dir": str(tmp_path), "endpoint": "https://garden.example", "worker_token": "synthetic"}, once=True)


def test_idle_claim_retries_transient_failure_with_stable_identity():
    requests = []

    class Client:
        def post(self, path, payload):
            requests.append((path, payload))
            if len(requests) == 1:
                raise WorkerRequestError(502, "temporary gateway failure")
            return 204, {}

    assert claim_with_retry(Client(), {"host": "build-1"}, sleep=lambda _delay: None) == (204, {})
    assert requests[0] == requests[1]
    assert len(requests[0][1]["claim_request_id"]) >= 16


def test_idle_claim_does_not_retry_permanent_response():
    class Client:
        def post(self, _path, _payload):
            raise WorkerRequestError(403, "unknown worker token")

    with pytest.raises(WorkerRequestError, match="HTTP 403"):
        claim_with_retry(Client(), {"host": "build-1"}, sleep=lambda _delay: pytest.fail("slept"))


def test_worker_resources_use_shared_memory_probe(tmp_path, monkeypatch):
    import garden.managed_worker as worker

    monkeypatch.setattr(worker, "memory_bytes", lambda: (143360, 409600))
    observed = resources(tmp_path)
    assert observed["memory_available_bytes"] == 143360
    assert observed["memory_total_bytes"] == 409600
    monkeypatch.setattr(worker, "memory_bytes", lambda: (None, 409600))
    with pytest.raises(RuntimeError, match="memory statistics are unavailable"):
        resources(tmp_path)


def test_attributed_client_posts_versioned_identity_bound_facts(tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr("garden.managed_worker.resources", lambda _root: {"cpu_count": 4})
    monkeypatch.setattr("garden.remote_worker.WorkerClient.post",
                        lambda _self, path, payload: posted.append((path, payload)) or (204, {}))
    config = {
        "endpoint": "https://garden.example", "worker_token": "secret", "profile_version": "v1",
        "bootstrap_version": "b1", "source_head": "a" * 40, "provider_id": "i-1",
        "operation_id": "operation-1", "source_bootstrap": {
            "source_head": "a" * 40, "bootstrap_sha256": "b" * 64,
            "operation_id": "operation-1"},
        "readiness_attestations": {"bootstrap_manifest": {"ok": True}},
    }
    client = AttributedClient(config, tmp_path)
    client.post("/api/runs/claim", {"host": "worker-1"})
    facts = posted[0][1]["host_facts"]
    assert facts["schema_version"] == 1
    assert facts["operation_id"] == "operation-1"
    assert facts["source_bootstrap"] == config["source_bootstrap"]
    assert facts["readiness_attestations"]["authenticated_registration"] == {
        "ok": False, "method": "scoped-worker-token"}
    assert client.authenticated_registration is True


def test_attributed_client_preserves_legacy_config_fact_shape(tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr("garden.managed_worker.resources", lambda _root: {"cpu_count": 4})
    monkeypatch.setattr("garden.remote_worker.WorkerClient.post",
                        lambda _self, path, payload: posted.append((path, payload)) or (200, {}))
    client = AttributedClient({
        "endpoint": "https://garden.example", "worker_token": "legacy-secret",
        "profile_version": "rc16", "bootstrap_version": "bootstrap-rc16",
        "source_head": "a" * 40, "provider_id": "i-legacy",
        "readiness_attestations": {"bootstrap_manifest": True, "repository_ci": True},
    }, tmp_path)

    client.post("/api/runs/claim", {"host": "legacy-worker"})
    first = posted[-1][1]["host_facts"]
    assert "schema_version" not in first
    assert "operation_id" not in first and "source_bootstrap" not in first
    assert first["readiness_attestations"] == {
        "bootstrap_manifest": True,
        "repository_ci": True,
        "authenticated_registration": False,
    }
    client.post("/api/runs/legacy/heartbeat", {"lease_token": "lease"})
    assert posted[-1][1]["host_facts"]["readiness_attestations"][
        "authenticated_registration"] is True


def test_setup_runs_inside_host_slot_before_managed_work(tmp_path, monkeypatch):
    import tempfile

    import garden.managed_worker as worker

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    events = []
    claim = {"setup": {"command": "prepare product", "timeout_seconds": 37}, "env_allowlist": ["PATH"]}
    monkeypatch.setattr(worker, "resources", lambda root: {
        "memory_available_bytes": 2 * 1024**3, "disk_free_bytes": 2 * 1024**3,
    })
    monkeypatch.setattr(worker.AttributedClient, "post", lambda *args: (200, claim))

    def execute(payload, root, client, *, setup_command=""):
        with pytest.raises(BlockingIOError):
            with host_slot(root):
                pass
        events.append((payload, setup_command))

    monkeypatch.setattr(worker, "execute_claim", execute)
    worker.run({"work_dir": str(tmp_path), "endpoint": "https://garden.example",
                "worker_token": "synthetic", "host": "build-1", "harnesses": ["claude"],
                "memory_reserve_mib": 1, "env_pass": ["GIT_SSH_COMMAND"],
                "disk_reserve_mib": 1}, once=True)

    assert events == [(claim, "prepare product")]
    assert claim["setup"]["timeout_seconds"] == 37
    assert claim["env_allowlist"] == ["PATH", "GIT_SSH_COMMAND"]


def test_verified_artifact_bootstrap():
    data = EC2Provider._user_data(declaration(bootstrap_path="/opt/bootstrap", bootstrap_url="https://example.test/pinned", bootstrap_sha256="a" * 64))
    assert data.index("sha256sum --check") < data.index("mv ") < data.index(" --config ")
    assert "arn:secret" in data and "worker_token" not in data


@pytest.mark.parametrize("url,digest", [("http://example.test/a", "a" * 64), ("https://user:secret@example.test/a", "a" * 64), ("https://example.test/a?token=x", "a" * 64), ("https://example.test/a", "x")])
def test_unpinned_or_credentialed_artifact_rejected(url, digest):
    with pytest.raises(ValueError):
        EC2Provider._user_data(declaration(bootstrap_path="/opt/bootstrap", bootstrap_url=url, bootstrap_sha256=digest))


@pytest.mark.parametrize("seconds", [0, True, 21601])
def test_bootstrap_refuses_unbounded_runtime(seconds):
    with pytest.raises(ValueError, match="bootstrap_runtime_seconds"):
        EC2Provider._user_data(declaration(bootstrap_path="/opt/bootstrap", bootstrap_runtime_seconds=seconds))


def test_bootstrap_runtime_is_explicit_and_secret_free():
    data = EC2Provider._user_data(declaration(bootstrap_path="/opt/bootstrap", bootstrap_runtime_seconds=21600))
    assert '"runtime_seconds": 21600' in data
    assert "codex_auth" not in data and "PRIVATE KEY" not in data


def test_deadline_timer_precedes_download_and_transports_immutable_identity():
    spec = declaration(bootstrap_path="/opt/bootstrap", bootstrap_url="https://example.test/pinned",
                       bootstrap_sha256="a" * 64)
    spec = replace(spec, pool=replace(spec.pool, profile=replace(
        spec.pool.profile, source_head="b" * 40)), deadline_utc="2030-01-01T00:00:00Z")
    data = EC2Provider._user_data(spec)
    assert data.index("systemctl enable --now") < data.index("curl --max-time")
    assert "Persistent=true" in data
    assert '"operation_id": "operation"' in data
    assert '"source_head": "' + "b" * 40 + '"' in data
    assert '"bootstrap_sha256": "' + "a" * 64 + '"' in data
    assert '"deadline_utc": "2030-01-01T00:00:00Z"' in data
    assert '"cpu": 4' in data
    assert '"memory_mib": 16384' in data
    assert '"disk_gib": 40' in data


def test_external_deadline_command_requires_exact_verified_receipt(monkeypatch):
    spec = declaration(bootstrap_path="/opt/bootstrap")
    spec = HostDeclaration(spec.host_id, spec.operation_id, spec.pool, "2030-01-01T00:00:00Z")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        receipt = {"armed": True, "host_id": spec.host_id,
                   "operation_id": spec.operation_id, "deadline_utc": spec.deadline_utc}
        return type("Completed", (), {"returncode": 0, "stdout": json.dumps(receipt)})()

    monkeypatch.setattr("garden.hosts.deadline.subprocess.run", run)
    ExternalDeadlineCommand(["deadline-helper", "arm"]).arm_and_verify(spec)
    assert calls[0][0] == ("deadline-helper", "arm")
    assert calls[0][1]["shell"] is False
    request = json.loads(calls[0][1]["input"])
    assert request == {"contract_version": "garden.hosts/v1", "host_id": "host",
                       "operation_id": "operation", "deadline_utc": "2030-01-01T00:00:00Z",
                       "provider": "ec2", "owner": "owner", "pool": "pool"}

    monkeypatch.setattr("garden.hosts.deadline.subprocess.run", lambda *a, **k:
                        type("Completed", (), {"returncode": 0, "stdout": "{}"})())
    with pytest.raises(RuntimeError, match="did not verify"):
        ExternalDeadlineCommand(["deadline-helper"]).arm_and_verify(spec)
