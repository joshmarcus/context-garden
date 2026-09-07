
import pytest

from garden.hosts import EnvironmentProfile, HostDeclaration, PoolDeclaration
from garden.hosts.ec2 import EC2Provider
from garden.managed_worker import host_slot, run


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


def test_verified_artifact_bootstrap():
    data = EC2Provider._user_data(declaration(bootstrap_path="/opt/bootstrap", bootstrap_url="https://example.test/pinned", bootstrap_sha256="a" * 64))
    assert data.index("sha256sum --check") < data.index("mv ") < data.index(" --config ")
    assert "arn:secret" in data and "worker_token" not in data


@pytest.mark.parametrize("url,digest", [("http://example.test/a", "a" * 64), ("https://user:secret@example.test/a", "a" * 64), ("https://example.test/a?token=x", "a" * 64), ("https://example.test/a", "x")])
def test_unpinned_or_credentialed_artifact_rejected(url, digest):
    with pytest.raises(ValueError):
        EC2Provider._user_data(declaration(bootstrap_path="/opt/bootstrap", bootstrap_url=url, bootstrap_sha256=digest))
