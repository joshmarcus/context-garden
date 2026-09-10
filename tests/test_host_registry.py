from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from garden.hosts.registry import authenticate_worker, enrolled_hosts
from garden.runner.remote import RemoteRunner
from garden.store import Store
from garden.web.app import create_app


def registry(path, hosts):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({"hosts": hosts}))
    path.chmod(0o600)


def worker(**changes):
    return {"name": "scale-0", "token_sha256": hashlib.sha256(b"enrolled-token").hexdigest(),
            "max_parallel": 1, **changes}


def test_enrollment_and_revocation_reach_live_api_without_restart(garden):
    store = Store(garden)
    path = store.config.garden_dir / "hosts/enrollment/controller-hosts.json"
    http = TestClient(create_app(store, host="testserver", watch=False))
    headers = {"Authorization": "Bearer enrolled-token", "Origin": "https://worker.test"}
    body = {"host": "scale-0", "harnesses": ["claude"], "capacity": 1}
    assert http.post("/api/runs/claim", headers=headers, json=body).status_code == 403
    registry(path, [worker()])
    assert http.post("/api/runs/claim", headers=headers, json=body).status_code == 204
    assert http.post("/api/runs/claim", headers=headers,
                     json={**body, "host": "another-host"}).status_code == 403
    # A worker token does not authorize control actions or relax their origin policy.
    assert http.post("/pause", headers=headers, data={"reason": "forbidden"}).status_code == 403
    registry(path, [])
    assert http.post("/api/runs/claim", headers=headers, json=body).status_code == 403


def test_static_tokens_and_private_registry_coexist(tmp_path, monkeypatch):
    path = tmp_path / "private/hosts.json"
    registry(path, [worker()])
    monkeypatch.setenv("STATIC_WORKER_TOKEN", "static-token")
    config = {"hosts": [{"name": "old", "token_env": "STATIC_WORKER_TOKEN"}],
              "enrollment_registry": str(path)}
    assert authenticate_worker(config, "static-token")["name"] == "old"
    assert authenticate_worker(config, "enrolled-token")["name"] == "scale-0"
    assert authenticate_worker(config, "wrong") is None
    assert RemoteRunner(config).doctor() == []
    assert RemoteRunner({"enrollment_registry": str(path)}).doctor() == []


@pytest.mark.parametrize("expiry", ["expired", "2000-01-01T00:00:00Z", "2099-01-01"])
def test_expired_or_invalid_enrollment_never_authenticates(tmp_path, expiry):
    path = tmp_path / "private/hosts.json"
    registry(path, [worker(deadline_utc=expiry)])
    assert authenticate_worker({"enrollment_registry": str(path)}, "enrolled-token") is None


def test_duplicate_registry_names_fail_closed(tmp_path):
    path = tmp_path / "private/hosts.json"
    registry(path, [worker(), worker()])
    with pytest.raises(ValueError, match="duplicate"):
        enrolled_hosts({"enrollment_registry": str(path)})
