
import pytest

from garden.hosts import EnvironmentProfile, HostDeclaration, PoolDeclaration
from garden.hosts.ec2 import EC2Provider
from garden.managed_worker import claim_with_retry, host_slot, run
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
