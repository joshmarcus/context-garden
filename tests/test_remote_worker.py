from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from garden import gitops, managed_worker
from garden.harness import Harness
from garden.remote_worker import (
    WorkerRequestError,
    _claim_suffix,
    _host_check_data,
    _LeaseHeartbeat,
    _wait_for_process,
    doctor_worker,
    execute_claim,
)
from garden.runner.remote import RemoteRunner
from garden.runs import RunStore
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app
from tests.conftest import git, write


def remote_client(garden, monkeypatch, *, validation_timeout=900, capacity=1, max_bypasses=3,
                  in_place=False):
    path = garden / "garden.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["workers"] = {"lease_seconds": 60, "hosts": [{"name": "build-1", "token_env": "BUILD_TOKEN",
                                                        "max_parallel": capacity,
                                                        "in_place": in_place}]}
    cfg["max_parallel"] = 1
    cfg.setdefault("resources", {})["max_bypasses"] = max_bypasses
    cfg["products"]["demo"]["runner"] = "remote"
    cfg["checks"] = {"pre_pr": [
        {"name": "remote-context", "command": "test \"$GARDEN_BRANCH\" = garden/dm-001-first-task"}
    ], "ci": [], "timeout_seconds": validation_timeout}
    cfg["review"] = {"enabled": True, "max_rounds": 1}
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("BUILD_TOKEN", "secret-token")
    return TestClient(create_app(Store(garden), watch=False, host="testserver")), Store(garden)


def isolated_execution_runtime(tmp_path, monkeypatch):
    """Keep synthetic worker supervisors out of an enclosing validation's host slot."""
    runtime = tmp_path / "worker-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


def queued_run(store, task_id="DM-001"):
    run = RunStore(store.config.garden_dir).new_run(task_id, "remote", mode="work")
    run.branch, run.base, run.harness, run.model, run.difficulty = "garden/dm-001", "main", "claude", "small", "easy"
    RemoteRunner({"worker_env": store.config.get("worker_env")}, store.config.harness("claude")).start(run, store.root, "safe brief")
    return run


def test_lost_successful_claim_response_replays_one_generation(garden, monkeypatch):
    """A committed claim whose response is lost is allocated and executed exactly once."""
    http, store = remote_client(garden, monkeypatch)
    queued = queued_run(store)
    calls = 0

    class LostResponseClient:
        def post(self, path, payload):
            nonlocal calls
            response = http.post(path, json=payload,
                                 headers={"Authorization": "Bearer secret-token"})
            calls += 1
            if calls == 1:
                assert response.status_code == 200
                raise urllib.error.URLError("response lost after controller commit")
            if response.status_code >= 400:
                raise WorkerRequestError(response.status_code, response.text)
            return response.status_code, response.json() if response.content else {}

    executed = []
    monkeypatch.setattr(managed_worker, "AttributedClient",
                        lambda _config, _root: LostResponseClient())
    monkeypatch.setattr(managed_worker, "resources", lambda _root: {
        "memory_available_bytes": 2 * 1024**3,
        "disk_free_bytes": 2 * 1024**3,
    })
    monkeypatch.setattr(managed_worker, "execute_claim",
                        lambda claim, *_args, **_kwargs: executed.append(claim))
    managed_worker.run({
        "work_dir": str(garden.parent / "managed-host"),
        "endpoint": "https://garden.example",
        "worker_token": "secret-token",
        "host": "build-1",
        "harnesses": ["claude"],
        "memory_reserve_mib": 1,
        "disk_reserve_mib": 1,
    }, once=True)
    saved = RunStore(store.config.garden_dir).latest("DM-001")

    assert [claim["id"] for claim in executed] == [queued.run_id]
    assert calls == 2
    assert saved.lease_token == executed[0]["lease_token"]
    assert len(saved.claim_history) == 1


def test_claim_request_replay_fences_host_generation_and_expiry(garden, monkeypatch):
    http, store = remote_client(garden, monkeypatch)
    queued_run(store)
    payload = {"host": "build-1", "harnesses": ["claude"],
               "claim_request_id": "stable-request-identity"}
    first = http.post("/api/runs/claim", json=payload,
                      headers={"Authorization": "Bearer secret-token"})
    assert first.status_code == 200

    saved = RunStore(store.config.garden_dir).latest("DM-001")
    saved.host = "build-2"
    saved.save()
    wrong_host = http.post("/api/runs/claim", json=payload,
                           headers={"Authorization": "Bearer secret-token"})
    assert wrong_host.status_code == 409

    saved = RunStore(store.config.garden_dir).latest("DM-001")
    saved.host = "build-1"
    saved.lease_token = "replacement-generation"
    saved.save()
    replaced = http.post("/api/runs/claim", json=payload,
                         headers={"Authorization": "Bearer secret-token"})
    assert replaced.status_code == 409

    saved = RunStore(store.config.garden_dir).latest("DM-001")
    saved.lease_token = first.json()["lease_token"]
    expired = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    saved.lease_expires_at = expired
    saved.recovery_expires_at = expired
    saved.save()
    stale = http.post("/api/runs/claim", json=payload,
                      headers={"Authorization": "Bearer secret-token"})
    assert stale.status_code == 409


@pytest.mark.parametrize("task_override,reference", [
    (False, "https://example.test/team/project.git"),
    (True, "https://example.test/team/project.git"),
    (False, "git@example.test:team/project.git"),
    (True, "../repo"),
])
def test_claim_resolves_controller_repository_before_reading_branch_head(
    garden, monkeypatch, task_override, reference,
):
    repo = garden.parent / "repo"
    gitops.git("push", "origin", "main:refs/heads/garden/dm-001", cwd=repo)
    expected_head = gitops.git("rev-parse", "HEAD", cwd=repo).strip()
    # Exercise real clone/fetch/ref lookup while keeping all transport inside the fixture.
    if reference != "../repo":
        git_config = garden.parent / "gitconfig"
        gitops.git("config", "--file", str(git_config),
                   f"url.{garden.parent / 'remote.git'}.insteadOf", reference, cwd=repo)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    if task_override:
        store = Store(garden)
        task = store.tasks()["DM-001"]
        task.repo = reference
        store.save(task)
    else:
        path = garden / "garden.yaml"
        cfg = yaml.safe_load(path.read_text())
        cfg["products"]["demo"]["repo"] = reference
        path.write_text(yaml.safe_dump(cfg))
    client, store = remote_client(garden, monkeypatch)
    queued_run(store)

    response = client.post("/api/runs/claim",
                           json={"host": "build-1", "harnesses": ["claude"]},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    saved = RunStore(store.config.garden_dir).latest("DM-001")
    assert saved.host == "build-1" and saved.start_head == expected_head
    if reference != "../repo":
        assert response.json()["repo"] == reference
        assert (store.config.repos_dir / "project/.git").is_dir()


@pytest.mark.parametrize("task_override,reference", [
    (False, "acct-1234@forge-one.test:team/repo.git"),
    (False, "forge-one.test:team/repo.git"),
    (True, "acct-1234@forge-one.test:team/repo.git"),
    (True, "forge-one.test:team/repo.git"),
])
def test_claim_preserves_configured_scp_repository_reference(
    garden, monkeypatch, task_override, reference,
):
    if task_override:
        store = Store(garden)
        task = store.tasks()["DM-001"]
        task.repo = reference
        store.save(task)
    else:
        path = garden / "garden.yaml"
        cfg = yaml.safe_load(path.read_text())
        cfg["products"]["demo"]["repo"] = reference
        path.write_text(yaml.safe_dump(cfg))
    client, store = remote_client(garden, monkeypatch)
    queued_run(store)
    repo = garden.parent / "repo"
    monkeypatch.setattr("garden.web.pages.api.gitops.ensure_repo", lambda *_args, **_kwargs: repo)

    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    assert response.json()["repo"] == reference


@pytest.mark.parametrize("reference", [
    "acct-1234@forge-one.test:team/repo.git",
    "forge-one.test:team/repo.git",
])
def test_claim_preserves_scp_repository_over_served_http(garden, monkeypatch, reference):
    """A real HTTP claim keeps each supported SCP spelling unchanged.

    An unknown bearer must not consume the queued run; the valid bearer then recovers
    the same claim.  Set ``GARDEN_SCP_CLAIM_INTERACTION_ARTIFACT`` to retain this
    disposable interaction's structured transcript outside the pytest temporary tree.
    """
    import httpx
    import uvicorn

    path = garden / "garden.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["products"]["demo"]["repo"] = reference
    path.write_text(yaml.safe_dump(cfg))
    client, store = remote_client(garden, monkeypatch)
    client.close()
    queued_run(store)
    repo = garden.parent / "repo"
    monkeypatch.setattr("garden.web.pages.api.gitops.ensure_repo", lambda *_args, **_kwargs: repo)
    application = create_app(store, watch=False, host="127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(application, log_level="error"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    events = []
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=url, timeout=15) as served:
            offer = {"host": "build-1", "harnesses": ["claude"]}
            failed = served.post("/api/runs/claim", json=offer,
                                 headers={"Authorization": "Bearer wrong-token"})
            assert failed.status_code == 403
            events.append({"kind": "http_request", "state": "failure", "outcome": "failure",
                           "method": "POST", "url": f"{url}/api/runs/claim", "status_code": 403,
                           "observed": "unknown bearer did not claim the queued run"})
            claimed = served.post("/api/runs/claim", json=offer,
                                  headers={"Authorization": "Bearer secret-token"})
            assert claimed.status_code == 200
            assert claimed.json()["repo"] == reference
            events.append({"kind": "http_request", "state": "affected", "outcome": "success",
                           "method": "POST", "url": f"{url}/api/runs/claim", "status_code": 200,
                           "observed": f"claim returned the exact configured remote {reference}"})
            events.append({"kind": "http_request", "state": "recovery", "outcome": "success",
                           "method": "POST", "url": f"{url}/api/runs/claim", "status_code": 200,
                           "observed": "valid bearer claimed the run rejected for the unknown bearer"})
            empty = served.post("/api/runs/claim", json=offer,
                                headers={"Authorization": "Bearer secret-token"})
            assert empty.status_code == 204
            events.append({"kind": "http_request", "state": "empty", "outcome": "empty",
                           "method": "POST", "url": f"{url}/api/runs/claim", "status_code": 204,
                           "observed": "no additional compatible queued run was available"})
        if destination := os.environ.get("GARDEN_SCP_CLAIM_INTERACTION_ARTIFACT"):
            artifact = Path(destination)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            head = gitops.git("rev-parse", "HEAD", cwd=Path.cwd()).strip()
            artifact.write_text(json.dumps({
                "head": head,
                "source_head": head,
                "test": "tests/test_remote_worker.py::test_claim_preserves_scp_repository_over_served_http",
                "transport": "real TCP HTTP", "environment": "disposable",
                "command": "pytest tests/test_remote_worker.py::test_claim_preserves_scp_repository_over_served_http",
                "reference": reference, "events": events,
                "states": {
                    "affected": "200 claim returns the configured remote unchanged",
                    "empty": "204 after the queued run is claimed",
                    "failure_recovery": "403 unknown bearer followed by a successful valid-bearer claim",
                },
            }, indent=2) + "\n")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_worker_host_doctor_checks_token_git_access_and_harness(monkeypatch):
    monkeypatch.setattr("garden.remote_worker.shutil.which", lambda name: f"/bin/{name}")

    class Probe:
        returncode = 1
        stdout = ""
        stderr = "SECRET_SENTINEL_AUTH_REVOKED"

    monkeypatch.setattr("garden.remote_worker.subprocess.run", lambda *args, **kwargs: Probe())

    assert doctor_worker("", "https://example.test/team/repo.git", ["claude"]) == [
        "worker bearer token is missing",
        "git cannot read 'https://example.test/team/repo.git'",
        "harness 'claude' authentication failed in scrubbed environment",
    ]

    monkeypatch.setattr(
        "garden.remote_worker.shutil.which",
        lambda name: None if name == "claude" else f"/bin/{name}",
    )
    assert doctor_worker("token", "", ["claude"]) == [
        "harness 'claude' is not on PATH",
    ]


def test_portable_worker_installs_claimed_config_mapping(tmp_path, monkeypatch):
    from garden.remote_worker import _env

    source = tmp_path / "host-tool.json"
    source.write_text("portable-tool-config")
    run = {"task_id": "T-1", "id": "run-1", "config_files": {
        "synthetic-tool": {"source": str(source), "destination": ".config/synthetic/tool.json",
                           "required": True},
    }}
    env = _env(["PATH"], tmp_path / "repo", run)
    copied = Path(env["HOME"]) / ".config/synthetic/tool.json"
    assert copied.read_text() == "portable-tool-config"
    assert copied.stat().st_mode & 0o777 == 0o600
    assert env["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
    assert env["GIT_CONFIG_VALUE_0"] == "false"
    assert env["GIT_CONFIG_KEY_1"] == "maintenance.auto"


def test_remote_claim_carries_mapping_but_not_config_contents(garden, tmp_path, monkeypatch):
    source = tmp_path / "host-tool.json"
    source.write_text("SECRET_SENTINEL_TOOL_CREDENTIAL")
    path = garden / "garden.yaml"
    config = yaml.safe_load(path.read_text())
    config.setdefault("worker_env", {})["config_files"] = {
        "synthetic-tool": {"source": str(source), "destination": ".config/synthetic/tool.json",
                           "required": True},
    }
    path.write_text(yaml.safe_dump(config))
    client, store = remote_client(garden, monkeypatch)
    queued_run(store)

    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    assert response.json()["config_files"]["synthetic-tool"]["destination"] == ".config/synthetic/tool.json"
    assert "SECRET_SENTINEL_TOOL_CREDENTIAL" not in response.text


@pytest.mark.parametrize("mode", ["work", "review", "persona"])
def test_worker_with_no_harnesses_cannot_claim_harness_backed_run(garden, monkeypatch, mode):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    run.mode = mode
    run.save()

    response = client.post(
        "/api/runs/claim",
        json={"host": "build-1", "harnesses": []},
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 204
    assert not RunStore(store.config.garden_dir).latest("DM-001").host


def test_worker_with_no_harnesses_can_claim_check_run(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    run.mode = "check"
    run.harness = ""
    (run.path / "checks_input.json").write_text('{"specs": [], "ctx": {}}')
    run.save()

    response = client.post(
        "/api/runs/claim",
        json={"host": "build-1", "harnesses": []},
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "check"
    assert response.json()["harness"] == ""


def test_remote_check_replaces_controller_only_spec_paths(tmp_path):
    repo = tmp_path / "repos" / "DM-001"
    run = {
        "id": "check-1",
        "checks": {
            "ctx": {"worktree": "/controller/worktree", "branch": "garden/dm-001"},
            "cwd": "/controller/worktree",
            "specs": [{
                "name": "ui",
                "python": "garden.walkthrough:ui_check",
                "worktree": "/controller/worktree",
                "out_dir": "/controller/run/ui",
            }],
        },
    }

    check_data = _host_check_data(run, repo)

    assert check_data["cwd"] == str(repo)
    assert check_data["ctx"] == {
        "worktree": str(repo), "exec_root": str(repo), "branch": "garden/dm-001",
    }
    assert check_data["specs"][0]["worktree"] == str(repo)
    assert check_data["specs"][0]["out_dir"] == str(
        repo.parent / "check-1-check-artifacts/0-ui"
    )
def test_remote_weighted_admission_first_fits_cheap_work_without_bypassing_cap(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch, capacity=4, max_bypasses=1)
    runs = RunStore(store.config.garden_dir)

    occupied = queued_run(store)
    occupied.host = "build-1"
    occupied.lease_token = "live"
    occupied.lease_expires_at = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat()
    occupied.env_snapshot = {"product": "demo", "resource_weight": 3}
    occupied.save()
    heavy = runs.new_run("HEAVY-001", "remote", mode="work")
    heavy.harness, heavy.difficulty = "claude", "easy"
    heavy.env_snapshot = {"product": "demo", "resource_weight": 2,
                          "execution_timeout_minutes": 120}
    RemoteRunner({}, store.config.harness("claude")).start(heavy, store.root, "heavy")
    cheap = runs.new_run("CHEAP-001", "remote", mode="work")
    cheap.harness, cheap.difficulty = "claude", "easy"
    cheap.env_snapshot = {"product": "demo", "resource_weight": 1,
                          "execution_timeout_minutes": 15}
    RemoteRunner({}, store.config.harness("claude")).start(cheap, store.root, "cheap")

    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"],
                                                    "capacity": 4},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    assert response.json()["id"] == cheap.run_id
    assert response.json()["resource_weight"] == 1
    assert response.json()["execution_timeout_minutes"] == 15
    claimed_at = dt.datetime.fromisoformat(
        RunStore(store.config.garden_dir).latest("CHEAP-001").execution_started_at
    )
    deadline = dt.datetime.fromisoformat(response.json()["execution_deadline_at"])
    assert deadline == claimed_at + dt.timedelta(minutes=20)
    assert not RunStore(store.config.garden_dir).latest("HEAVY-001").host

    claimed_cheap = RunStore(store.config.garden_dir).latest("CHEAP-001")
    claimed_cheap.status = "done"
    claimed_cheap.finished_at = dt.datetime.now(dt.UTC).isoformat()
    claimed_cheap.save()
    another = runs.new_run("CHEAP-002", "remote", mode="work")
    another.harness, another.difficulty = "claude", "easy"
    another.env_snapshot = {"product": "demo", "resource_weight": 1}
    RemoteRunner({}, store.config.harness("claude")).start(another, store.root, "another cheap")

    protected = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"],
                                                     "capacity": 4},
                            headers={"Authorization": "Bearer secret-token"})
    assert protected.status_code == 204
    assert not RunStore(store.config.garden_dir).latest("CHEAP-002").host


def test_remote_base_probe_materialises_its_advertised_source(garden, monkeypatch, tmp_path):
    """A remote base check reads its detached base commit, never the task branch."""
    isolated_execution_runtime(tmp_path, monkeypatch)
    client, store = remote_client(garden, monkeypatch)
    repo = garden.parent / "repo"
    write(repo / "source-marker.txt", "base\n")
    git("add", "source-marker.txt", cwd=repo)
    git("commit", "-m", "base marker", cwd=repo)
    git("push", "origin", "main", cwd=repo)
    base_head = gitops.git("rev-parse", "HEAD", cwd=repo).strip()
    git("checkout", "-b", "garden/dm-001", cwd=repo)
    write(repo / "source-marker.txt", "branch\n")
    git("commit", "-am", "branch marker", cwd=repo)
    git("push", "origin", "garden/dm-001", cwd=repo)
    branch_head = gitops.git("rev-parse", "HEAD", cwd=repo).strip()
    git("checkout", "main", cwd=repo)

    def base_check(source_head: str):
        run = queued_run(store)
        run.mode, run.harness, run.source_head = "check", "", source_head
        (run.path / "checks_input.json").write_text(json.dumps({
            "specs": [{"name": "base-marker", "command": "test \"$(cat source-marker.txt)\" = base"}],
            "ctx": {}, "timeout": 30, "config": {},
        }))
        run.save()
        return run

    base_check(base_head)
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth)
    assert claim.status_code == 200
    payload = claim.json()
    assert payload["source_head"] == base_head
    assert RunStore(store.config.garden_dir).latest("DM-001").start_head == base_head

    class PostingClient:
        def post(self, path, body):
            response = client.post(path, json=body, headers=auth)
            return response.status_code, response.json()

    execute_claim(payload, tmp_path / "independent-host", PostingClient())
    completed = RunStore(store.config.garden_dir).runs_for("DM-001")[-1]
    assert completed.start_head == completed.pushed_head == base_head
    assert json.loads((completed.path / "checks.json").read_text())[0]["status"] == "pass"
    assert gitops.git("rev-parse", "origin/garden/dm-001", cwd=repo).strip() == branch_head

    unavailable = base_check("0" * 40)
    bad_claim = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth)
    assert bad_claim.status_code == 200
    execute_claim(bad_claim.json(), tmp_path / "unavailable-source-host", PostingClient())
    unavailable = RunStore(store.config.garden_dir).runs_for("DM-001")[-1]
    posted = json.loads((unavailable.path / "remote_result.json").read_text())
    assert posted["env_error"] is True and posted["env_kind"] == "materialization"
    assert "checkout" in posted["error"]
    checks = json.loads((unavailable.path / "checks.json").read_text())
    assert checks[0]["summary"] == "check execution did not complete"
    assert unavailable.source_head == "0" * 40
    assert gitops.git("rev-parse", "origin/garden/dm-001", cwd=repo).strip() == branch_head

def test_remote_check_has_no_worker_execution_deadline(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = RunStore(store.config.garden_dir).new_run("DM-001", "remote", mode="check")
    run.env_snapshot = {"product": "demo", "execution_timeout_minutes": 0}
    RemoteRunner({}, None).start_checks(run, store.root, {"specs": [], "ctx": {}})

    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": []},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    assert response.json()["execution_deadline_at"] == ""
    assert response.json()["execution_timeout_minutes"] == 0


def test_legacy_remote_check_has_no_product_execution_timeout(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = RunStore(store.config.garden_dir).new_run("DM-001", "remote", mode="check")
    run.env_snapshot = {"product": "demo"}
    RemoteRunner({}, None).start_checks(run, store.root, {"specs": [], "ctx": {}})

    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": []},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    assert response.json()["execution_deadline_at"] == ""
    assert response.json()["execution_timeout_minutes"] == 0

def test_in_place_host_claims_one_run_regardless_of_its_resource_weight(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch, capacity=4, in_place=True)
    first = queued_run(store)
    first.env_snapshot = {"product": "demo", "resource_weight": 2}
    first.save()
    second = queued_run(store, "DM-002")
    second.env_snapshot = {"product": "demo", "resource_weight": 1}
    second.save()
    auth = {"Authorization": "Bearer secret-token"}

    claimed = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"],
                                                   "capacity": 4}, headers=auth)

    assert claimed.status_code == 200
    assert claimed.json()["id"] == first.run_id
    assert claimed.json()["resource_weight"] == 2
    assert client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"],
                                                "capacity": 4}, headers=auth).status_code == 204
    saved = RunStore(store.config.garden_dir).latest("DM-001")
    saved.status = "done"
    saved.finished_at = dt.datetime.now(dt.UTC).isoformat()
    saved.save()
    replacement = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"],
                                                       "capacity": 4}, headers=auth)
    assert replacement.status_code == 200
    assert replacement.json()["id"] == second.run_id


def test_remote_api_auth_claim_heartbeat_finish_and_origin(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch, validation_timeout=731)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}

    assert client.post("/api/runs/claim", json={"host": "build-1"}).status_code == 401
    assert client.post("/api/runs/claim", json={"host": "build-1"}, headers={"Origin": "https://evil.test"}).status_code == 403
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"], "tiers": ["easy"], "capacity": 1}, headers=auth)
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == run.run_id and payload["brief"] == "safe brief"
    assert payload["lease_token"] and "secret-token" not in str(payload)
    assert set(payload) >= {"repo", "branch", "base", "push_ref", "setup", "turn_cap", "env_allowlist"}
    assert payload["validation_timeout_seconds"] == 731
    assert payload["push_ref"].startswith(f"refs/heads/garden-worker/{run.run_id}/")
    legacy = RunStore(store.config.garden_dir).latest("DM-001")
    assert dt.datetime.fromisoformat(payload["execution_deadline_at"]) == (
        dt.datetime.fromisoformat(legacy.execution_started_at) + dt.timedelta(minutes=6)
    )

    beat = client.post(f"/api/runs/{run.run_id}/heartbeat",
                       json={"lease_token": payload["lease_token"], "transcript": "hello\n"}, headers=auth)
    assert beat.status_code == 200
    done = client.post(f"/api/runs/{run.run_id}/finish", json={"lease_token": payload["lease_token"],
                       "exit_code": 0, "final_text": "done", "result": {"status": "done"},
                       "usage": {"input_tokens": 2}, "cost_usd": 0.1, "pushed_head": "abc",
                       "validation_receipts": [{"source_sha": "abc", "command": "pytest -q",
                                                "selection": ["pytest", "-q"], "exit_code": 0,
                                                "log_location": "/remote/path"}]}, headers=auth)
    assert done.status_code == 200
    saved = RunStore(store.config.garden_dir).latest("DM-001")
    assert saved.host == "build-1" and saved.pushed_head == "abc"
    assert saved.process_finished() and saved.stdout_text() == "hello\n"
    receipt = json.loads((saved.path / "validations" / "remote-0" / "result.json").read_text())
    assert receipt["source_sha"] == "abc" and receipt["exit_code"] == 0
    assert receipt["log_location"].endswith("validations/remote-0")
    saved.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    saved.save()
    assert client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).status_code == 204


def test_six_idle_claim_polls_with_concurrent_ui_only_materialize_active_runs(
    garden, monkeypatch,
):
    """Claim admission excludes terminal history while preserving generation fencing."""
    from concurrent.futures import ThreadPoolExecutor

    client, store = remote_client(garden, monkeypatch)
    runs = RunStore(store.config.garden_dir)
    for number in range(200):
        terminal = runs.new_run("DM-002", "remote", run_id=f"terminal-{number:04d}")
        terminal.status = "done"
        terminal.finished_at = "2026-01-01T00:00:00+00:00"
        terminal.save()
    run = queued_run(store)
    original_active = RunStore.active
    active_sizes = []

    def bounded_active(self):
        active = original_active(self)
        active_sizes.append(len(active))
        assert all(item.status in ("requested", "preparing", "running") for item in active)
        return active

    monkeypatch.setattr(RunStore, "active", bounded_active)
    auth = {"Authorization": "Bearer secret-token"}
    offer = {"host": "build-1", "harnesses": ["claude"]}

    first = client.post("/api/runs/claim", json=offer, headers=auth)
    assert first.status_code == 200
    first_token = first.json()["lease_token"]

    claimed = RunStore(store.config.garden_dir).latest("DM-001")
    claimed.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    claimed.recovery_expires_at = claimed.lease_expires_at
    claimed.save()
    replacement = client.post("/api/runs/claim", json=offer, headers=auth)
    assert replacement.status_code == 200
    replacement_token = replacement.json()["lease_token"]
    assert replacement_token != first_token
    assert client.post(
        f"/api/runs/{run.run_id}/heartbeat",
        json={"lease_token": first_token},
        headers=auth,
    ).status_code == 409
    assert client.post(
        f"/api/runs/{run.run_id}/heartbeat",
        json={"lease_token": replacement_token},
        headers=auth,
    ).status_code == 200

    with ThreadPoolExecutor(max_workers=7) as pool:
        idle = [
            pool.submit(client.post, "/api/runs/claim", json=offer, headers=auth)
            for _ in range(6)
        ]
        page = pool.submit(client.get, "/now")
        assert [future.result().status_code for future in idle] == [204] * 6
        assert page.result().status_code == 200

    assert active_sizes and max(active_sizes) == 1


def test_claim_and_heartbeat_persist_only_bounded_host_facts(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    expected = {
        "profile_version": "worker-v1", "bootstrap_version": "bootstrap-v2",
        "source_head": "a" * 40, "provider_id": "i-123", "memory_available_bytes": 1024,
        "memory_total_bytes": 2048, "disk_free_bytes": 4096, "cpu_count": 4,
        "observed_at": 1.5,
    }
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"],
                           "host_facts": {**expected, "controller_token": "must-not-persist"}},
                           headers=auth)
    assert response.status_code == 200
    assert json.loads((run.path / "host_facts.json").read_text()) == expected

    payload = response.json()
    too_long = client.post(f"/api/runs/{run.run_id}/heartbeat",
                           json={"lease_token": payload["lease_token"],
                                 "host_facts": {"provider_id": "x" * 129}}, headers=auth)
    assert too_long.status_code == 422
    assert json.loads((run.path / "host_facts.json").read_text()) == expected
    for invalid in (-1, True, 10**400):
        response = client.post(f"/api/runs/{run.run_id}/heartbeat",
                               json={"lease_token": payload["lease_token"],
                                     "host_facts": {"disk_free_bytes": invalid}}, headers=auth)
        assert response.status_code == 422
        assert json.loads((run.path / "host_facts.json").read_text()) == expected
    response = client.post(f"/api/runs/{run.run_id}/heartbeat",
                           json={"lease_token": payload["lease_token"],
                                 "host_facts": {**expected, "disk_free_bytes": 8192}}, headers=auth)
    assert response.status_code == 200
    assert json.loads((run.path / "host_facts.json").read_text()) == {**expected, "disk_free_bytes": 8192}


def test_malformed_claim_payloads_are_rejected_before_leasing(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    payloads = (
        [],
        {"host": "build-1", "harnesses": "claude"},
        {"host": "build-1", "tiers": {"hard": True}},
        {"host": "build-1", "capacity": "1"},
        {"host": "build-1", "capacity": True},
        {"host": "build-1", "capacity": 0},
    )
    for payload in payloads:
        assert client.post("/api/runs/claim", json=payload, headers=auth).status_code == 422
        saved = RunStore(store.config.garden_dir).latest("DM-001")
        assert not saved.host and not saved.lease_token
    assert client.post("/api/runs/claim", content="{invalid-json", headers=auth).status_code == 422
    assert not RunStore(store.config.garden_dir).latest("DM-001").lease_token


def test_invalid_finish_payload_does_not_partially_mutate_run(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                        headers=auth).json()
    run = RunStore(store.config.garden_dir).latest("DM-001")
    before = (run.path / "run.json").read_bytes()
    invalid = (
        [],
        {"result": []},
        {"usage": []},
        {"result": {"checks": ["not-an-object"]}},
        {"exit_code": "0"},
        {"exit_code": True},
    )
    for changes in invalid:
        payload = ({"lease_token": claim["lease_token"], "exit_code": 0,
                    "result": {"status": "done"}, **changes}
                   if isinstance(changes, dict) else changes)
        response = client.post(f"/api/runs/{run.run_id}/finish", json=payload, headers=auth)
        assert response.status_code == 422
        assert (run.path / "run.json").read_bytes() == before
        assert not any((run.path / name).exists()
                       for name in ("final.md", "remote_result.json", "checks.json", "exit_code"))


def test_versioned_host_facts_are_bound_to_authenticated_registry_identity(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    path = garden / "garden.yaml"
    config = yaml.safe_load(path.read_text())
    config["workers"]["hosts"][0].update({
        "provider_id": "i-trusted", "operation_id": "operation-trusted",
        "source_head": "a" * 40, "profile_version": "profile-v1",
        "bootstrap_version": "bootstrap-v1",
    })
    path.write_text(yaml.safe_dump(config))
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    run = queued_run(Store(garden))
    facts = {
        "schema_version": 1, "provider_id": "i-forged", "operation_id": "operation-trusted",
        "source_head": "a" * 40, "profile_version": "profile-v1",
        "bootstrap_version": "bootstrap-v1",
    }
    secret_bearing = {**facts, "provider_id": "i-trusted",
                      "source_bootstrap": {"source_head": "a" * 40,
                                           "bootstrap_sha256": "b" * 64,
                                           "operation_id": "operation-trusted"},
                      "readiness_attestations": {
                          "bootstrap_manifest": {"ok": True, "secret": "must-not-persist"}}}
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"],
                           "host_facts": secret_bearing},
                           headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 422
    assert not (run.path / "host_facts.json").exists()
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"],
                           "host_facts": facts},
                           headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 409
    assert not (run.path / "host_facts.json").exists()


def test_reclaimed_lease_fences_stale_worker_on_same_host(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    offer = {"host": "build-1", "harnesses": ["claude"]}
    claim1 = client.post("/api/runs/claim", json=offer, headers=auth).json()
    run = RunStore(store.config.garden_dir).latest("DM-001")
    run.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    run.recovery_expires_at = run.lease_expires_at
    run.save()
    claim2 = client.post("/api/runs/claim", json=offer, headers=auth).json()

    assert claim2["lease_token"] != claim1["lease_token"]
    assert claim2["push_ref"] != claim1["push_ref"]
    stale = client.post(f"/api/runs/{run.run_id}/finish",
                        json={"lease_token": claim1["lease_token"], "exit_code": 0}, headers=auth)
    assert stale.status_code == 409
    fresh = client.post(f"/api/runs/{run.run_id}/heartbeat",
                        json={"lease_token": claim2["lease_token"]}, headers=auth)
    assert fresh.status_code == 200


def test_remote_queue_age_is_not_execution_age_and_timestamps_survive_reclaim(
    garden, monkeypatch, fake_github,
):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    queued = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=4)).isoformat()
    run.started_at = queued
    run.queued_at = queued
    run.save()
    auth = {"Authorization": "Bearer secret-token"}
    offer = {"host": "build-1", "harnesses": ["claude"]}

    first = client.post("/api/runs/claim", json=offer, headers=auth).json()
    claimed = RunStore(store.config.garden_dir).latest("DM-001")
    assert claimed.started_at == queued and claimed.queued_at == queued
    assert claimed.execution_started_at == claimed.claimed_at
    assert claimed.execution_minutes() < 1
    first_claimed_at = claimed.claimed_at
    first_execution_at = claimed.execution_started_at

    claimed.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    claimed.recovery_expires_at = claimed.lease_expires_at
    claimed.save()
    second = client.post("/api/runs/claim", json=offer, headers=auth).json()
    reclaimed = RunStore(store.config.garden_dir).latest("DM-001")
    assert reclaimed.claimed_at == first_claimed_at
    assert reclaimed.execution_started_at == first_execution_at
    assert len(reclaimed.claim_history) == 2
    assert second["lease_token"] != first["lease_token"]
    assert client.post(f"/api/runs/{run.run_id}/heartbeat",
                       json={"lease_token": first["lease_token"]}, headers=auth).status_code == 409

    scheduler = Scheduler(store, github=fake_github)
    assert not scheduler._finished_or_timed_out(reclaimed, scheduler.runner_for(
        store.task("DM-001"), "remote", reclaimed.harness
    ))

    legacy = RunStore(store.config.garden_dir).new_run("DM-002", "remote", run_id="legacy-queued")
    legacy.started_at = queued
    legacy.queued_at = ""
    legacy.save()
    assert legacy.execution_minutes() == 0


def test_remote_idle_uses_first_claim_and_ignores_controller_checkout(
    garden, monkeypatch, fake_github, tmp_path,
):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    now = dt.datetime.now(dt.UTC)
    queued = (now - dt.timedelta(minutes=20, seconds=4)).isoformat()
    run.started_at = queued
    run.queued_at = queued
    controller_checkout = tmp_path / "controller-checkout"
    controller_checkout.mkdir()
    controller_file = controller_checkout / "unrelated.py"
    controller_file.write_text("# not the remote checkout\n")
    old = (now - dt.timedelta(hours=2)).timestamp()
    os.utime(controller_file, (old, old))
    run.worktree = str(controller_checkout)
    run.save()

    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                        headers=auth).json()
    claimed = RunStore(store.config.garden_dir).latest("DM-001")
    first_claim = (now - dt.timedelta(minutes=12, seconds=37)).isoformat()
    claimed.claimed_at = first_claim
    claimed.execution_started_at = first_claim
    claimed.lease_updated_at = now.isoformat()
    claimed.lease_expires_at = (now + dt.timedelta(minutes=8)).isoformat()
    claimed.claim_history[0]["claimed_at"] = first_claim
    claimed.save()

    scheduler = Scheduler(store, github=fake_github)
    scheduler.cfg.data["idle_kill_minutes"] = 20
    scheduler.cfg.data["timeout_minutes"] = 90
    runner = scheduler.runner_for(store.task("DM-001"), "remote", claimed.harness)
    assert 12.5 < claimed.idle_minutes() < 13
    assert not scheduler._finished_or_timed_out(claimed, runner)

    # A current lease alone is not productive activity. Once actual execution has been
    # silent for the configured interval, the ordinary bounded idle policy still applies.
    claimed.execution_started_at = (now - dt.timedelta(minutes=21)).isoformat()
    claimed.save()
    assert scheduler._finished_or_timed_out(claimed, runner)
    expired = RunStore(store.config.garden_dir).latest("DM-001")
    assert expired.status == "timeout" and "idle 21 min" in expired.error
    assert claim["lease_token"] and not expired.lease_token and not expired.lease_expires_at


def test_remote_idle_legacy_claim_fallback_survives_reclaim(garden, monkeypatch, fake_github):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    now = dt.datetime.now(dt.UTC)
    original_started = (now - dt.timedelta(hours=3)).isoformat()
    first_claim = (now - dt.timedelta(minutes=2)).isoformat()
    auth = {"Authorization": "Bearer secret-token"}
    first = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                        headers=auth)
    assert first.status_code == 200
    run = RunStore(store.config.garden_dir).latest("DM-001")
    run.started_at = original_started
    run.queued_at = ""
    run.claimed_at = first_claim
    run.execution_started_at = ""
    run.lease_expires_at = (now - dt.timedelta(seconds=1)).isoformat()
    run.recovery_expires_at = run.lease_expires_at
    run.save()

    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                           headers=auth)
    assert response.status_code == 200
    reclaimed = RunStore(store.config.garden_dir).latest("DM-001")
    assert reclaimed.started_at == original_started
    assert reclaimed.claimed_at == first_claim
    assert reclaimed.execution_started_at == first_claim
    assert len(reclaimed.claim_history) == 2
    assert 1.9 < reclaimed.idle_minutes() < 2.1

    scheduler = Scheduler(store, github=fake_github)
    scheduler.cfg.data["idle_kill_minutes"] = 20
    scheduler.cfg.data["timeout_minutes"] = 1
    reclaimed.claimed_at = (now - dt.timedelta(minutes=8)).isoformat()
    reclaimed.execution_started_at = reclaimed.claimed_at
    reclaimed.save()
    runner = scheduler.runner_for(store.task("DM-001"), "remote", reclaimed.harness)
    assert scheduler._finished_or_timed_out(reclaimed, runner)
    assert RunStore(store.config.garden_dir).latest("DM-001").error == "timed out"


def test_remote_timeout_revokes_generation_and_rejects_late_evidence(
    garden, monkeypatch, fake_github,
):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                        headers=auth).json()
    run = RunStore(store.config.garden_dir).latest("DM-001")
    run.execution_started_at = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
    run.save()
    scheduler = Scheduler(store, github=fake_github)

    assert scheduler._finished_or_timed_out(
        run, scheduler.runner_for(store.task("DM-001"), "remote", run.harness)
    )
    timed_out = RunStore(store.config.garden_dir).latest("DM-001")
    assert timed_out.status == "timeout"
    assert not timed_out.lease_token and not timed_out.lease_expires_at
    late_beat = client.post(f"/api/runs/{run.run_id}/heartbeat",
                            json={"lease_token": claim["lease_token"], "transcript": "late"}, headers=auth)
    late_finish = client.post(f"/api/runs/{run.run_id}/finish",
                              json={"lease_token": claim["lease_token"], "exit_code": 0,
                                    "result": {"status": "done"}, "usage": {"input_tokens": 99}},
                              headers=auth)
    assert late_beat.status_code == late_finish.status_code == 409
    assert not (run.path / "remote_result.json").exists()
    assert RunStore(store.config.garden_dir).usage_for("DM-001")["input_tokens"] == 0
    from garden.model import Status
    task = store.task("DM-001")
    task.status = Status.RUNNING
    store.save(task)
    report = scheduler.tick()
    assert any("DM-001" in transition for transition in report.transitions), report
    old = next(item for item in RunStore(store.config.garden_dir).runs_for("DM-001")
               if item.run_id == run.run_id)
    assert old.started_at == run.started_at and old.execution_started_at == run.execution_started_at
    active = [item for item in scheduler.active_runs() if item.task_id == "DM-001"]
    assert len(active) <= 1
    assert all(item.run_id != old.run_id for item in active)


def test_each_run_keeps_its_own_trusted_fence_manifest(garden, fake_github):
    store = Store(garden)
    scheduler = Scheduler(store, github=fake_github)
    task = store.task("DM-001")
    first = RunStore(store.config.garden_dir).new_run(task.id, "remote", run_id="generation-one")
    scheduler._fence_snapshot(task, first)
    first = RunStore(store.config.garden_dir).runs_for(task.id)[0]
    second = RunStore(store.config.garden_dir).new_run(task.id, "remote", run_id="generation-two")
    (garden / "garden.yaml").write_text((garden / "garden.yaml").read_text() + "\n# second generation\n")
    scheduler._fence_snapshot(task, second)
    second = RunStore(store.config.garden_dir).latest(task.id)

    assert first.fence_manifest_sha256
    assert second.fence_manifest_sha256
    assert first.fence_manifest_sha256 != second.fence_manifest_sha256
    assert scheduler._fence_guard_check(task, first) == []
    assert scheduler._fence_guard_check(task, second) == []


def test_claim_strips_repo_credentials_and_harness_arguments(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    queued_run(store)
    original_git = __import__("garden.gitops", fromlist=["git"]).git

    def credentialed_remote(*args, **kwargs):
        if args == ("remote", "get-url", "origin"):
            return "https://scheduler-token@example.test/team/repo.git?access_token=also-secret"
        return original_git(*args, **kwargs)

    monkeypatch.setattr("garden.web.pages.api.gitops.git", credentialed_remote)
    store.config.data["harnesses"]["claude"]["args"] = ["--api-key", "harness-secret"]
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["repo"] == "https://example.test/team/repo.git"
    assert "args" not in payload["harness_config"]
    assert "scheduler-token" not in str(payload)
    assert "also-secret" not in str(payload)
    assert "harness-secret" not in str(payload)


@pytest.mark.parametrize("remote", [
    "oauth2:secret@example.test:team/repo.git",
    "ssh://deploy:secret@example.test/team/repo.git",
    "ssh://deploy%40other@example.test/team/repo.git",
    "https://user:secret@example.test:bad/repo.git",
])
def test_claim_rejects_credentialed_or_malformed_git_remotes(garden, monkeypatch, remote):
    client, store = remote_client(garden, monkeypatch)
    queued_run(store)
    original_git = __import__("garden.gitops", fromlist=["git"]).git

    def unsafe_remote(*args, **kwargs):
        if args == ("remote", "get-url", "origin"):
            return remote
        return original_git(*args, **kwargs)

    monkeypatch.setattr("garden.web.pages.api.gitops.git", unsafe_remote)
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 409
    assert "secret" not in response.text


@pytest.mark.parametrize("remote", [
    "git@example.test:team/repo.git",
    "deploy@example.test:team/repo.git",
    "acct-1234@example.test:team/repo.git",
    "ssh://git@example.test/team/repo.git",
    "ssh://acct-1234@example.test:443/team/repo.git",
    "ssh://acct-1234@example.test:2222/team/repo.git",
])
def test_claim_preserves_safe_ssh_transport_usernames(garden, monkeypatch, remote):
    client, store = remote_client(garden, monkeypatch)
    queued_run(store)
    original_git = __import__("garden.gitops", fromlist=["git"]).git

    def safe_remote(*args, **kwargs):
        if args == ("remote", "get-url", "origin"):
            return remote
        return original_git(*args, **kwargs)

    monkeypatch.setattr("garden.web.pages.api.gitops.git", safe_remote)
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                           headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    assert response.json()["repo"] == remote


def test_expired_lease_is_claimable_without_failing_task(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    run.host = "build-1"
    run.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    run.recovery_expires_at = run.lease_expires_at
    run.save()
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"], "tiers": ["easy"]},
                           headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200 and response.json()["id"] == run.run_id
    assert store.task("DM-001").status.value == "ready"


def test_expired_lease_waits_for_durable_recovery_window(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                        headers=auth).json()
    run = RunStore(store.config.garden_dir).latest("DM-001")
    run.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    run.save()

    assert client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                       headers=auth).status_code == 204
    page = client.get(f"/runs/DM-001/{run.run_id}")
    assert "Reconnecting after a controller interruption" in page.text
    assert "seconds to reconnect before this lease can be reassigned" in page.text
    if capture_dir := os.environ.get("GARDEN_REMOTE_RECOVERY_CAPTURES"):
        import socket

        import uvicorn
        from playwright.sync_api import sync_playwright

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        application = create_app(Store(garden), watch=False, host="127.0.0.1")
        server = uvicorn.Server(uvicorn.Config(application, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        output = Path(capture_dir)
        output.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                for width in (1280, 390):
                    for scheme in ("light", "dark"):
                        context = browser.new_context(
                            viewport={"width": width, "height": 900}, color_scheme=scheme,
                        )
                        browser_page = context.new_page()
                        browser_page.goto(f"http://127.0.0.1:{port}/runs/DM-001/{run.run_id}")
                        assert browser_page.locator("body").evaluate("el => el.scrollWidth") == width
                        browser_page.screenshot(
                            path=str(output / f"run-reconnecting-{width}-{scheme}.png"), full_page=True,
                        )
                        context.close()
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
    beat = client.post(f"/api/runs/{run.run_id}/heartbeat",
                       json={"lease_token": claim["lease_token"]}, headers=auth)
    assert beat.status_code == 200
    assert RunStore(store.config.garden_dir).latest("DM-001").lease_token == claim["lease_token"]


def test_cancelled_remote_lease_is_explicitly_rejected(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                        headers=auth).json()
    run = RunStore(store.config.garden_dir).latest("DM-001")
    run.status = "cancelled"
    run.save()

    rejected = client.post(f"/api/runs/{run.run_id}/heartbeat",
                           json={"lease_token": claim["lease_token"]}, headers=auth)
    assert rejected.status_code == 409
    assert "generation is no longer active" in rejected.text


def test_transcript_replay_is_ordered_and_idempotent(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                        headers=auth).json()
    path = f"/api/runs/{run.run_id}/heartbeat"
    payload = {"lease_token": claim["lease_token"], "transcript_offset": 0,
               "transcript": "héllo\n"}

    first = client.post(path, json=payload, headers=auth)
    replay = client.post(path, json=payload, headers=auth)
    ahead = client.post(path, json={**payload, "transcript_offset": 99}, headers=auth)

    assert first.json()["transcript_offset"] == len("héllo\n".encode())
    assert replay.status_code == 200
    assert replay.json()["transcript_offset"] == first.json()["transcript_offset"]
    assert ahead.status_code == 409
    assert RunStore(store.config.garden_dir).latest("DM-001").stdout_text() == "héllo\n"


def test_finish_acknowledgement_replay_collects_one_result(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                        headers=auth).json()
    payload = {"lease_token": claim["lease_token"], "exit_code": 0, "result": {"status": "done"},
               "pushed_head": "abc", "final_text": "done"}
    path = f"/api/runs/{run.run_id}/finish"

    assert client.post(path, json=payload, headers=auth).json() == {"ok": True}
    assert client.post(path, json=payload, headers=auth).json() == {
        "ok": True, "already_finished": True,
    }
    assert client.post(path, json={**payload, "pushed_head": "other"}, headers=auth).status_code == 409


def test_heartbeat_retries_transient_failure_but_rejection_is_terminal(monkeypatch):
    class RecoveringClient:
        def __init__(self):
            self.calls = 0

        def post(self, path, payload):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionRefusedError("controller restarting")
            return 200, {"transcript_offset": len(str(payload.get("transcript") or "").encode())}

    run = {"id": "run-1", "lease_token": "lease", "heartbeat_seconds": 0.05,
           "recovery_seconds": 1}
    recovering = RecoveringClient()
    heartbeat = _LeaseHeartbeat(run, recovering)
    assert heartbeat.upload(0, "kept") == 4
    assert recovering.calls == 2

    class RejectingClient:
        def post(self, path, payload):
            raise WorkerRequestError(403, "revoked")

    started = time.monotonic()
    with pytest.raises(WorkerRequestError, match="403"):
        _LeaseHeartbeat(run, RejectingClient()).ensure_current()
    assert time.monotonic() - started < 0.5


def test_heartbeat_uses_full_controller_recovery_window(monkeypatch):
    """The worker and controller fence the same generation at the 120 + 300 boundary."""
    now = 0.0
    controller_online = False

    def monotonic():
        return now

    class RecoveringClient:
        def post(self, path, payload):
            if not controller_online:
                raise ConnectionRefusedError("controller restarting")
            return 200, {}

    run = {
        "id": "run-1", "lease_token": "lease", "heartbeat_seconds": 120,
        "recovery_seconds": 300, "recovery_window_seconds": 420,
    }
    heartbeat = _LeaseHeartbeat(run, RecoveringClient())

    def advance(delay):
        nonlocal now, controller_online
        now += delay
        if now >= 301:
            controller_online = True
        return False

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(heartbeat.stop_event, "wait", advance)
    heartbeat.ensure_current()
    assert now >= 301
    assert heartbeat.recovery_deadline == pytest.approx(now + 420)

    controller_online = False
    now = heartbeat.recovery_deadline + 0.01
    with pytest.raises(ConnectionRefusedError, match="controller restarting"):
        heartbeat.ensure_current()


def test_terminal_lease_loss_stops_supervised_process_tree(tmp_path):
    execution_dir = tmp_path / "execution"
    execution_dir.mkdir()
    stopped = tmp_path / "stopped"
    child = tmp_path / "child.py"
    child.write_text(
        "import signal, time\n"
        "from pathlib import Path\n"
        f"stopped = Path({str(stopped)!r})\n"
        "def stop(*_args):\n"
        "    stopped.write_text('stopped')\n"
        "    raise SystemExit(143)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "stopped.with_suffix('.ready').write_text('ready')\n"
        "while True: time.sleep(0.1)\n"
    )
    execution_env = dict(os.environ)
    for key in ("GARDEN_EXECUTION_OWNER", "GARDEN_EXECUTION_RUN_DIR",
                "GARDEN_HEAVY_EXECUTION", "GARDEN_OWNER_SCOPED"):
        execution_env.pop(key, None)
    proc = subprocess.Popen([
        sys.executable, "-m", "garden.run_supervisor", str(execution_dir),
        f"{sys.executable} {child}",
    ], env=execution_env)
    ready = stopped.with_suffix(".ready")
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    heartbeat = _LeaseHeartbeat({
        "id": "obsolete", "lease_token": "replaced", "recovery_seconds": 1,
    }, object())
    heartbeat.failure = WorkerRequestError(409, "lease replaced")

    with pytest.raises(RuntimeError, match="lease renewal failed"):
        _wait_for_process(proc, heartbeat)

    assert proc.poll() is not None
    assert stopped.read_text() == "stopped"


def test_worker_executes_pushes_and_scheduler_opens_pr(garden, monkeypatch, tmp_path, fake_github):
    isolated_execution_runtime(tmp_path, monkeypatch)
    client, store = remote_client(garden, monkeypatch, validation_timeout=731)
    scheduler = Scheduler(store, github=fake_github)
    report = scheduler.tick()  # dispatch work
    assert report.dispatched, report
    auth = {"Authorization": "Bearer secret-token"}
    payload = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"], "tiers": ["easy", "medium", "hard"]}, headers=auth).json()
    assert payload["repo"].endswith("remote.git")

    class PostingClient:
        def post(self, path, body):
            response = client.post(path, json=body, headers=auth)
            return response.status_code, response.json()

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")
    execute_claim(payload, tmp_path / "independent-host", PostingClient())
    saved = RunStore(store.config.garden_dir).latest("DM-001")
    assert saved.process_finished() and saved.pushed_head
    assert (tmp_path / "independent-host" / "repos" / "DM-001" / ".git").exists()
    assert (saved.path / "remote_result.json").exists()
    assert saved.stdout_text(), "the completed harness transcript is uploaded"
    report = scheduler.tick()  # reap work and dispatch the remote pre-PR check
    assert not report.errors, report
    assert any("check" in x for x in report.dispatched), (report, scheduler.state.get("DM-001"))
    check_claim = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).json()
    assert check_claim.get("mode") == "check", check_claim
    assert check_claim["checks"]["ctx"]["branch"] == "garden/dm-001-first-task"
    assert "exec_root" not in check_claim["checks"]["ctx"]
    assert set(check_claim["checks"]["config"]) == {"worker_env"}
    execute_claim(check_claim, tmp_path / "independent-host", PostingClient())
    check_execution = next(
        path for path in (tmp_path / "independent-host/runs").iterdir()
        if (path / "checks_input.json").exists()
    )
    execution = json.loads((check_execution / "execution.json").read_text())
    assert execution["state"] == "finished" and execution["timeout_seconds"] == 731
    scheduler.tick()  # reap check, open PR, and dispatch review
    store.invalidate_tasks()
    task = store.task("DM-001")
    assert task.pr and task.status.value == "in_review"
    review_claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]}, headers=auth).json()
    assert review_claim["mode"] == "review"
    execute_claim(review_claim, tmp_path / "independent-host", PostingClient())
    scheduler.tick()  # reap and apply the approving review
    assert scheduler.state.get("DM-001")["last_review"]["verdict"] == "approve"
    modes = {run.mode: run for run in RunStore(store.config.garden_dir).runs_for("DM-001")}
    assert modes["check"].result["checks"][0]["status"] == "pass"
    assert modes["review"].status == "done"
    assert "@build-1" in client.get(f"/runs/DM-001/{saved.run_id}").text


@pytest.mark.parametrize("checkout_state", ["untracked", "tracked-dirty", "unmerged"])
def test_materialization_failure_is_preserved_and_clean_generation_retries(
    garden, monkeypatch, tmp_path, fake_github, checkout_state,
):
    """A poisoned warm checkout reports infrastructure failure without launching an author."""
    isolated_execution_runtime(tmp_path, monkeypatch)
    client, store = remote_client(garden, monkeypatch)
    scheduler = Scheduler(store, github=fake_github)
    scheduler.tick()
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post(
        "/api/runs/claim",
        json={"host": "build-1", "harnesses": ["claude"], "tiers": ["easy", "medium", "hard"]},
        headers=auth,
    ).json()
    claim["setup"] = {"command": "mkdir -p .venv && touch .venv/prepared"}
    host_root = tmp_path / f"{checkout_state}-host"
    repo = host_root / "repos" / "DM-001"
    repo.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", claim["repo"], str(repo)], check=True)
    subprocess.run(["git", "checkout", "-B", "main", "origin/main"], cwd=repo, check=True)
    poisoned = repo / "unpublished.txt"
    poisoned.write_text("preserve me\n")
    if checkout_state == "tracked-dirty":
        subprocess.run(["git", "add", "unpublished.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=garden", "-c", "user.email=garden@localhost",
             "commit", "-m", "unpublished local commit"],
            cwd=repo, check=True,
        )
        poisoned.write_text("preserve my tracked edit\n")
    if checkout_state == "unmerged":
        blobs = []
        for content in ("base\n", "ours\n", "theirs\n"):
            blobs.append(subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=content,
                capture_output=True, text=True, check=True,
            ).stdout.strip())
        entries = "".join(f"100644 {blob} {stage}\tconflict.txt\n"
                          for stage, blob in enumerate(blobs, 1))
        subprocess.run(["git", "update-index", "--index-info"], cwd=repo, input=entries,
                       text=True, check=True)
    index_before = (repo / ".git" / "index").read_bytes()
    from garden.runner.base import setup_marker
    marker = setup_marker(repo)
    marker.write_text("stale prepared checkout")

    finish_posts = []

    class PostingClient:
        def post(self, path, body):
            response = client.post(path, json=body, headers=auth)
            if path.endswith("/finish") and response.status_code == 200:
                finish_posts.append(body)
            return response.status_code, response.json()

    original_command = Harness.command
    monkeypatch.setattr(Harness, "command", lambda *args, **kwargs: pytest.fail("author launched"))
    execute_claim(claim, host_root, PostingClient(), setup_command=claim["setup"]["command"])
    monkeypatch.setattr(Harness, "command", original_command)

    assert len(finish_posts) == 1
    first = RunStore(store.config.garden_dir).runs_for("DM-001")[-1]
    posted = json.loads((first.path / "remote_result.json").read_text())
    assert posted["env_error"] is True and posted["env_kind"] == "materialization"
    assert first.read_exit_code() == 1 and not first.pushed_head
    preserved = list((host_root / "preserved-materializations" / "DM-001").iterdir())
    checkouts = [path for path in preserved if path.is_dir()]
    assert len(checkouts) == 1
    archived = checkouts[0]
    expected = "preserve my tracked edit\n" if checkout_state == "tracked-dirty" else "preserve me\n"
    assert (archived / "unpublished.txt").read_text() == expected
    assert (archived / ".git" / "index").read_bytes() == index_before
    assert list(archived.parent.glob(f"{archived.name}.setup-marker"))
    assert not repo.exists()

    report = scheduler.tick()
    refreshed_runs = RunStore(store.config.garden_dir).runs_for("DM-001")
    refreshed_first = next(run for run in refreshed_runs if run.run_id == first.run_id)
    assert refreshed_first.status == "env_error"
    assert scheduler.state.get("DM-001")["consecutive_env_errors"] == 1
    assert any("env_error: materialization" in item for item in report.transitions)
    scheduler.tick()
    replacement = RunStore(store.config.garden_dir).latest("DM-001")
    assert replacement.run_id != first.run_id and replacement.status == "running"

    clean_claim = client.post(
        "/api/runs/claim",
        json={"host": "build-1", "harnesses": ["claude"], "tiers": ["easy", "medium", "hard"]},
        headers=auth,
    ).json()
    clean_claim["setup"] = claim["setup"]
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")
    execute_claim(clean_claim, host_root, PostingClient(),
                  setup_command=clean_claim["setup"]["command"])
    clean = RunStore(store.config.garden_dir).latest("DM-001")
    assert clean.read_exit_code() == 0 and clean.pushed_head
    assert (host_root / "repos" / "DM-001" / ".git").exists()
    assert (host_root / "repos" / "DM-001" / ".venv" / "prepared").exists()


def test_required_config_materialization_failure_finishes_without_author(
    garden, monkeypatch, tmp_path, fake_github,
):
    """A supported host-config failure remains scoped to its claim generation."""
    isolated_execution_runtime(tmp_path, monkeypatch)
    client, store = remote_client(garden, monkeypatch)
    scheduler = Scheduler(store, github=fake_github)
    scheduler.tick()
    auth = {"Authorization": "Bearer secret-token"}
    claim = client.post(
        "/api/runs/claim",
        json={"host": "build-1", "harnesses": ["claude"]},
        headers=auth,
    ).json()
    claim["config_files"] = {
        "required-tool": {
            "source": str(tmp_path / "unavailable.json"),
            "destination": ".config/tool/config.json",
            "required": True,
        },
    }
    finish_posts = []

    class PostingClient:
        def post(self, path, body):
            response = client.post(path, json=body, headers=auth)
            if path.endswith("/finish") and response.status_code == 200:
                finish_posts.append(body)
            return response.status_code, response.json()

    monkeypatch.setattr(Harness, "command", lambda *args, **kwargs: pytest.fail("author launched"))
    execute_claim(claim, tmp_path / "config-host", PostingClient())

    assert len(finish_posts) == 1
    assert finish_posts[0]["env_kind"] == "materialization"
    assert "required config file 'required-tool' is unavailable" in finish_posts[0]["error"]
    saved = RunStore(store.config.garden_dir).latest("DM-001")
    assert saved.read_exit_code() == 1 and not saved.pushed_head


def test_live_checkout_owner_is_refused_without_quarantine_or_author_launch(tmp_path, monkeypatch):
    root = tmp_path / "host"
    lock_path = root / "repo-locks" / "T-1.lock"
    lock_path.parent.mkdir(parents=True)
    posts = []

    class Client:
        def post(self, path, body):
            posts.append((path, body))
            return 200, {}

    run = {"id": "run-1", "task_id": "T-1", "lease_token": "current",
           "heartbeat_seconds": 3600}
    monkeypatch.setattr(Harness, "command", lambda *args, **kwargs: pytest.fail("author launched"))
    with lock_path.open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        execute_claim(run, root, Client())

    finishes = [(path, body) for path, body in posts if path.endswith("/finish")]
    assert len(finishes) == 1
    assert finishes[0][1]["env_kind"] == "materialization"
    assert "still owned" in finishes[0][1]["error"]
    assert not (root / "preserved-materializations").exists()


@pytest.mark.parametrize("failure", ["directory", "open", "flock"])
def test_repo_lock_materialization_failure_finishes_without_author(
    tmp_path, monkeypatch, failure,
):
    root = tmp_path / "host"
    lock_dir = root / "repo-locks"
    lock_path = lock_dir / "T-1.lock"
    if failure == "directory":
        lock_dir.parent.mkdir(parents=True)
        lock_dir.write_text("not a directory")
    elif failure == "open":
        lock_path.mkdir(parents=True)
    else:
        lock_dir.mkdir(parents=True)

        def fail_flock(*_args):
            raise OSError("lock filesystem unavailable")

        monkeypatch.setattr("garden.remote_worker.fcntl.flock", fail_flock)
    posts = []

    class Client:
        def post(self, path, body):
            posts.append((path, body))
            return 200, {}

    run = {"id": "run-1", "task_id": "T-1", "lease_token": "current",
           "heartbeat_seconds": 3600}
    monkeypatch.setattr(Harness, "command", lambda *args, **kwargs: pytest.fail("author launched"))

    execute_claim(run, root, Client())

    finishes = [body for path, body in posts if path.endswith("/finish")]
    assert len(finishes) == 1
    assert finishes[0]["env_kind"] == "materialization"
    assert "checkout ownership" in finishes[0]["error"]
    assert "repository lock" in finishes[0]["error"]
    assert not (root / "preserved-materializations").exists()


@pytest.mark.parametrize("failure", ["preservation-directory", "setup-marker-rename"])
def test_quarantine_filesystem_failure_finishes_without_author(
    tmp_path, monkeypatch, failure,
):
    """Partial or failed preservation remains a claim-scoped infrastructure outcome."""
    root = tmp_path / "host"
    repo = root / "repos" / "T-1"
    repo.parent.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    unpublished = repo / "unpublished.txt"
    unpublished.write_text("keep exactly\n")
    from garden.runner.base import setup_marker

    marker = setup_marker(repo)
    marker.write_text("keep setup state\n")
    run = {"id": "run-1", "task_id": "T-1", "lease_token": "current",
           "heartbeat_seconds": 3600}
    destination = root / "preserved-materializations" / "T-1" / _claim_suffix(run)
    original_mkdir = Path.mkdir
    original_rename = Path.rename
    if failure == "preservation-directory":
        def fail_mkdir(path, *args, **kwargs):
            if path == destination.parent:
                raise OSError("preservation directory unavailable")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    else:
        def fail_rename(path, target):
            if path == marker:
                raise OSError("marker filesystem unavailable")
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", fail_rename)
    posts = []

    class Client:
        def post(self, path, body):
            posts.append((path, body))
            return 200, {}

    monkeypatch.setattr(Harness, "command", lambda *args, **kwargs: pytest.fail("author launched"))
    execute_claim(run, root, Client())

    finishes = [body for path, body in posts if path.endswith("/finish")]
    assert len(finishes) == 1
    assert finishes[0]["env_kind"] == "materialization"
    assert "checkout preflight" in finishes[0]["error"]
    assert "warm checkout has dirty worktree" in finishes[0]["error"]
    assert "preservation failed" in finishes[0]["error"]
    source = destination if failure == "setup-marker-rename" else repo
    assert (source / "unpublished.txt").read_bytes() == b"keep exactly\n"
    assert marker.read_bytes() == b"keep setup state\n"


def test_orphaned_author_keeps_checkout_lock_and_new_claim_refuses_mutation(tmp_path, monkeypatch):
    """The supervisor passes repo ownership to the real author, not only its own process."""
    isolated_execution_runtime(tmp_path, monkeypatch)
    root = tmp_path / "host"
    repo = root / "repos" / "T-1"
    repo.mkdir(parents=True)
    unpublished = repo / "unpublished.txt"
    unpublished.write_text("still active\n")
    lock_path = root / "repo-locks" / "T-1.lock"
    lock_path.parent.mkdir(parents=True)
    owner = lock_path.open("a")
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run_dir = root / "runs" / "supervisor"
    run_dir.mkdir(parents=True)
    child_pid_path = root / "author.pid"
    child = root / "author.py"
    child.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(.1)\n"
    )
    env = dict(os.environ)
    env["GARDEN_PRESERVE_FDS"] = str(owner.fileno())
    supervisor = subprocess.Popen(
        [sys.executable, "-m", "garden.run_supervisor", str(run_dir),
         f"{sys.executable} {child}"], env=env, pass_fds=(owner.fileno(),),
    )
    owner.close()
    author_pid = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert child_pid_path.exists()
        author_pid = int(child_pid_path.read_text())
        supervisor.kill()
        supervisor.wait(timeout=5)

        posts = []

        class Client:
            def post(self, path, body):
                posts.append((path, body))
                return 200, {}

        claim = {"id": "replacement", "task_id": "T-1", "lease_token": "current",
                 "heartbeat_seconds": 3600}
        execute_claim(claim, root, Client())
        finishes = [body for path, body in posts if path.endswith("/finish")]
        assert len(finishes) == 1 and "still owned" in finishes[0]["error"]
        assert unpublished.read_text() == "still active\n"
        assert not (root / "preserved-materializations").exists()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
        if author_pid is not None:
            try:
                os.kill(author_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + 5
    while True:
        with lock_path.open("a") as released:
            try:
                fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pass
        assert time.monotonic() < deadline
        time.sleep(.02)


def test_stale_lease_cannot_quarantine_dirty_checkout(tmp_path):
    root = tmp_path / "host"
    repo = root / "repos" / "T-1"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    unpublished = repo / "unpublished.txt"
    unpublished.write_text("keep\n")

    class ReplacedClient:
        def post(self, path, body):
            raise WorkerRequestError(409, "run lease has been replaced")

    run = {"id": "run-1", "task_id": "T-1", "lease_token": "stale",
           "heartbeat_seconds": 3600}
    with pytest.raises(WorkerRequestError, match="409"):
        execute_claim(run, root, ReplacedClient())

    assert unpublished.read_text() == "keep\n"
    assert not (root / "preserved-materializations").exists()



@pytest.mark.parametrize("validation_exit", [0, 7])
def test_remote_harness_receives_working_owned_validation(
    garden, monkeypatch, tmp_path, fake_github, validation_exit,
):
    """The real harness child invokes the advertised wrapper, including its failure path."""
    client, store = remote_client(garden, monkeypatch)
    scheduler = Scheduler(store, github=fake_github)
    scheduler.tick()
    auth = {"Authorization": "Bearer secret-token"}
    payload = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]},
                          headers=auth).json()
    probe = tmp_path / "validation_harness.py"
    probe.write_text(
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "sys.stdin.read()\n"
        "outer = Path(os.environ['GARDEN_EXECUTION_RUN_DIR'])\n"
        "assert outer.is_dir() and os.environ['GARDEN_EXECUTION_OWNER'] != 'wrong-owner'\n"
        "assert os.environ['GARDEN_VALIDATION_RUNNER'] != '/missing/controller/python'\n"
        "assert os.environ['GARDEN_VALIDATION_TIMEOUT_SECONDS'] == '900'\n"
        "assert 'GARDEN_EXECUTION_TIMEOUT_SECONDS' not in os.environ\n"
        "command = [os.environ['GARDEN_VALIDATION_RUNNER'], '-m', 'garden.validation', '--', "
        "sys.executable, '-c', 'import sys; sys.exit(" + str(validation_exit) + ")']\n"
        "result = subprocess.run(command, capture_output=True, text=True, timeout=10)\n"
        "assert result.returncode == " + str(validation_exit) + ", result.stderr\n"
        "states = list((outer / 'validations').glob('*/execution.json'))\n"
        "assert states and json.loads(states[0].read_text())['state'] == 'finished'\n"
        "Path('.git/validation-probe.json').write_text(json.dumps({'outer': str(outer), "
        "'owner': os.environ['GARDEN_EXECUTION_OWNER'], 'exit': result.returncode}))\n"
        "print(json.dumps({'type': 'result', 'result': 'GARDEN_RESULT: {\"status\":\"done\"}'}))\n"
    )
    payload["harness_config"] = {"command": [sys.executable, str(probe)], "output": "claude-json"}
    # Even an overly broad allowlist cannot reuse another process's control identity.
    for key, value in {"GARDEN_EXECUTION_OWNER": "wrong-owner",
                       "GARDEN_EXECUTION_RUN_DIR": str(tmp_path / "wrong-run"),
                       "GARDEN_VALIDATION_RUNNER": "/missing/controller/python",
                       "GARDEN_EXECUTION_TIMEOUT_SECONDS": "0.01",
                       "GARDEN_HEAVY_EXECUTION": "1", "GARDEN_OWNER_SCOPED": "1"}.items():
        monkeypatch.setenv(key, value)
    isolated_execution_runtime(tmp_path, monkeypatch)
    payload["env_allowlist"] = [*payload["env_allowlist"], "GARDEN_*", "XDG_RUNTIME_DIR", "PYTHONPATH"]
    # The source path is needed only because this fixture exercises a worktree, not an install.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))

    class PostingClient:
        def post(self, path, body):
            response = client.post(path, json=body, headers=auth)
            return response.status_code, response.json()

    host_root = tmp_path / "validation-host"
    execute_claim(payload, host_root, PostingClient())
    saved = RunStore(store.config.garden_dir).latest("DM-001")
    assert saved.read_exit_code() == 0, saved.stderr_text()
    evidence = json.loads((host_root / "repos/DM-001/.git/validation-probe.json").read_text())
    assert evidence["exit"] == validation_exit
    assert str(host_root / "runs") in evidence["outer"]
    assert evidence["owner"] != "wrong-owner"
    assert not (tmp_path / "wrong-run").exists()

def test_worker_renews_short_lease_during_setup_and_check(garden, monkeypatch, tmp_path, fake_github):
    isolated_execution_runtime(tmp_path, monkeypatch)
    client, store = remote_client(garden, monkeypatch)
    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["workers"]["lease_seconds"] = 1
    config["checks"]["pre_pr"][0]["command"] = (
        "sleep 2 && test \"$GARDEN_BRANCH\" = garden/dm-001-first-task"
    )
    config_path.write_text(yaml.safe_dump(config))
    store = Store(garden)
    client = TestClient(create_app(store, watch=False, host="testserver"))
    scheduler = Scheduler(store, github=fake_github)
    scheduler.tick()
    auth = {"Authorization": "Bearer secret-token"}

    class PostingClient:
        def post(self, path, body):
            response = client.post(path, json=body, headers=auth)
            return response.status_code, response.json()

    def execute_while_asserting_not_reclaimed(payload, *, setup_command=""):
        errors = []

        def target():
            try:
                execute_claim(payload, tmp_path / "long-running-host", PostingClient(),
                              setup_command=setup_command)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=target)
        worker.start()
        time.sleep(1.25)
        competing = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth)
        assert competing.status_code == 204
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert not errors

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")
    work_claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]}, headers=auth).json()
    execute_while_asserting_not_reclaimed(work_claim, setup_command="sleep 2")
    scheduler.tick()
    check_claim = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).json()
    assert check_claim.get("mode") == "check", check_claim
    execute_while_asserting_not_reclaimed(check_claim)


def test_active_worker_completes_once_across_controller_stop_start(
    garden, monkeypatch, tmp_path, fake_github,
):
    """A disposable real HTTP controller disappears while a real harness child is active."""
    import socket
    import subprocess

    import httpx
    import uvicorn

    _, store = remote_client(garden, monkeypatch)
    config_path = garden / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["workers"].update(lease_seconds=2, recovery_seconds=5)
    config_path.write_text(yaml.safe_dump(config))
    store = Store(garden)
    scheduler = Scheduler(store, github=fake_github)
    assert scheduler.tick().dispatched
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    url = f"http://127.0.0.1:{port}"

    def start_controller():
        server = uvicorn.Server(uvicorn.Config(
            create_app(Store(garden), watch=False, host="127.0.0.1"),
            host="127.0.0.1", port=port, log_level="error",
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        return server, thread

    server, controller_thread = start_controller()
    auth = {"Authorization": "Bearer secret-token"}
    claim = httpx.post(f"{url}/api/runs/claim", json={
        "host": "build-1", "harnesses": ["claude"],
    }, headers=auth).json()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    monkeypatch.setenv("FAKE_CLAUDE_STALL_SECONDS", "2.5")
    worker_errors = []

    def work():
        try:
            from garden.remote_worker import WorkerClient

            execute_claim(claim, tmp_path / "restart-host", WorkerClient(url, "secret-token"))
        except BaseException as exc:
            worker_errors.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    time.sleep(0.8)
    server.should_exit = True
    controller_thread.join(timeout=5)
    assert not controller_thread.is_alive() and worker.is_alive()
    with pytest.raises(httpx.ConnectError):
        httpx.post(f"{url}/api/runs/claim", json={"host": "build-1"}, headers=auth, timeout=0.5)
    time.sleep(1.0)
    server, controller_thread = start_controller()
    worker.join(timeout=15)

    assert not worker.is_alive() and not worker_errors
    saved = RunStore(store.config.garden_dir).latest("DM-001")
    assert saved.process_finished() and saved.read_exit_code() == 0
    assert json.loads((saved.path / "remote_result.json").read_text())["result"]["status"] == "done"
    assert len(list(saved.path.glob("remote_result.json"))) == 1
    empty = httpx.post(f"{url}/api/runs/claim", json={
        "host": "build-1", "harnesses": ["claude"],
    }, headers=auth)
    assert empty.status_code == 204
    artifact = {
        "head": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
            cwd=Path(__file__).resolve().parents[1],
        ).stdout.strip(),
        "environment": "disposable FastAPI/uvicorn controller and real fake-harness child over TCP",
        "command": (
            ".venv/bin/python -m pytest "
            "tests/test_remote_worker.py::test_active_worker_completes_once_across_controller_stop_start -q"
        ),
        "states": ["active", "controller_down", "reconnecting", "completed", "empty"],
        "events": [
            {"state": "active", "method": "POST", "url": "/api/runs/claim", "status": 200,
             "consequence": "one worker generation started a live harness child"},
            {"state": "controller_down", "action": "stop disposable controller",
             "outcome": "connection refused", "consequence": "the harness child remained active"},
            {"state": "reconnecting", "action": "restart controller on the same port",
             "outcome": "heartbeat accepted", "consequence": "the original generation retained authority"},
            {"state": "completed", "method": "POST", "url": f"/api/runs/{saved.run_id}/finish",
             "status": 200, "consequence": "one durable result completed with exit code 0"},
            {"state": "empty", "method": "POST", "url": "/api/runs/claim", "status": 204,
             "consequence": "no duplicate work remained to claim"},
        ],
        "automated_checks": [
            "one remote_result.json exists", "saved exit code is 0", "worker thread exited without error",
        ],
        "unverified": [],
    }
    if destination := os.environ.get("GARDEN_REMOTE_RESTART_ARTIFACT"):
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, indent=2) + "\n")
    server.should_exit = True
    controller_thread.join(timeout=5)


def test_worker_cli_setup_option(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from garden.cli import app
    calls = []
    monkeypatch.setenv("GARDEN_WORKER_TOKEN", "test-only")
    monkeypatch.setattr("garden.remote_worker.run_worker", lambda *a, **kw: calls.append(kw))
    result = CliRunner().invoke(app, ["worker", "--garden", "http://localhost:1234",
                                   "--host", "build-1", "--once", "--setup-command", "echo host-owned"])
    assert result.exit_code == 0, result.output
    assert calls == [{"setup_command": "echo host-owned"}]


@pytest.mark.parametrize("managed", [False, True], ids=["standalone", "managed"])
def test_remote_lifecycle_over_served_http(garden, monkeypatch, tmp_path, fake_github, managed):
    """Real TCP HTTP and a separate CLI process; GitHub is the only external fake.

    This proves process/transport separation, not VM or EC2 provisioning.
    """
    import json
    import os
    import shlex
    import socket
    import subprocess
    import sys
    from pathlib import Path

    import httpx
    import uvicorn

    _, store = remote_client(garden, monkeypatch)
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    # The simulated remote host runs on this test process's machine.  Give its detached
    # check supervisors their own lease namespace so they cannot wait on the enclosing
    # validation command's host-wide slot after this test has returned.
    isolated_execution_runtime(tmp_path, monkeypatch)
    config["worker_env"]["pass"].append("XDG_RUNTIME_DIR")
    config["products"]["demo"]["setup"] = {
        "command": "echo configured-product-setup", "timeout_seconds": 37,
        "env": {"PRIVATE_SETUP_VALUE": "must-not-travel"},
    }
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    if managed:
        # Real setup in each worker subprocess must observe the machine lock already held.
        setup_code = """import fcntl
from pathlib import Path
with (Path.cwd().parents[1] / 'host.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise RuntimeError('setup executed outside the managed host lock')
p = Path('.git/setup-count')
p.write_text(str((int(p.read_text()) if p.exists() else 0) + 1))
"""
        config["products"]["demo"]["setup"]["command"] = "python3 -c " + shlex.quote(setup_code)
        config["checks"]["pre_pr"][0]["command"] += " && test -f .git/setup-count"
        (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    store = Store(garden)
    from garden.model import Status
    for other in store.tasks().values():
        if other.id != "DM-001":
            other.status = Status.CANCELLED
            store.save(other)
    scheduler = Scheduler(store, github=fake_github)
    application = create_app(store, watch=False, host="127.0.0.1")
    http_events = []

    @application.middleware("http")
    async def trace_http(request, call_next):
        response = await call_next(request)
        http_events.append({"method": request.method, "path": request.url.path,
                            "status": response.status_code})
        return response

    server = uvicorn.Server(uvicorn.Config(application, log_level="error"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    events = []
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=url, timeout=15) as client:
            assert client.post("/api/runs/claim", json={"host": "build-1"}).status_code == 401
            assert client.post("/api/runs/claim", json={"host": "build-1"},
                               headers={"Origin": "https://evil.test"}).status_code == 403
            assert scheduler.tick().dispatched
            auth = {"Authorization": "Bearer secret-token"}
            claim = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"]}, headers=auth).json()
            assert claim["setup"] == {"command": config["products"]["demo"]["setup"]["command"], "timeout_seconds": 37}
            assert "PRIVATE_SETUP_VALUE" not in json.dumps(claim)
            assert "must-not-travel" not in json.dumps(claim)
            run = scheduler.runs.latest("DM-001")
            run.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
            run.recovery_expires_at = run.lease_expires_at
            run.save()
            assert client.post(f"/api/runs/{run.run_id}/heartbeat",
                               json={"lease_token": claim["lease_token"]}, headers=auth).status_code == 409
            # Reclaim through the actual CLI, without a controller object in that process.
            env = {k: v for k, v in os.environ.items() if k in
                   {"PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "XDG_RUNTIME_DIR"}
                   or k.startswith("FAKE_")}
            env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                       GARDEN_WORKER_TOKEN="secret-token", FAKE_CLAUDE_MODE="done")
            setup_counts = {}
            worker_config = {
                "endpoint": url, "worker_token": "secret-token", "host": "build-1",
                "work_dir": str(tmp_path / "http-host"), "harnesses": ["claude"],
                "profile_version": "fixture-v1", "bootstrap_version": "fixture-v1",
                "source_head": "a" * 40, "provider_id": "disposable-http-host",
                "operation_id": "fixture-operation",
                "source_bootstrap": {"source_head": "a" * 40,
                                     "bootstrap_sha256": "b" * 64,
                                     "operation_id": "fixture-operation"},
                "memory_reserve_mib": 0, "disk_reserve_mib": 0,
                "readiness_attestations": {
                    "bootstrap_manifest": {"ok": True, "source_head": "a" * 40,
                                           "profile_version": "fixture-v1",
                                           "bootstrap_version": "fixture-v1",
                                           "bootstrap_sha256": "b" * 64,
                                           "installed_distribution": "context-garden",
                                           "direct_url_commit": "a" * 40},
                    "repository_access": {"ok": True, "repository": "example/project"},
                    "ci_provider_read": {"ok": True, "source_head": "a" * 40},
                },
            }
            config_file = tmp_path / "managed-worker.json"
            config_file.write_text(json.dumps(worker_config))
            def worker(mode, task_id="DM-001"):
                command = ([sys.executable, "-m", "garden.managed_worker", "--config", str(config_file), "--once"]
                           if managed else [sys.executable, "-m", "garden", "worker", "--garden", url,
                           "--host", "build-1", "--work-dir", str(tmp_path / "http-host"),
                           "--harness", "claude", "--once"])
                result = subprocess.run(command, env=env, cwd=tmp_path,
                                        capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stderr
                latest = scheduler.runs.latest(task_id)
                assert latest.mode == mode and latest.process_finished()
                if managed:
                    setup_counts[task_id] = setup_counts.get(task_id, 0) + 1
                    marker = tmp_path / "http-host" / "repos" / task_id / ".git/setup-count"
                    assert int(marker.read_text()) == setup_counts[task_id], "setup must run exactly once per claim"
                    facts = json.loads((latest.path / "host_facts.json").read_text())
                    assert facts["provider_id"] == "disposable-http-host"
                    assert facts["profile_version"] == "fixture-v1"
                    assert facts["disk_free_bytes"] > 0
                    assert facts["readiness_attestations"] == {
                        **worker_config["readiness_attestations"],
                        "authenticated_registration": {
                            "ok": True, "method": "scoped-worker-token",
                        },
                    }
                events.append({"mode": mode, "run": latest.run_id, "host": latest.host,
                               "setup_count": setup_counts.get(task_id), "managed": managed})
            worker("work")
            scheduler.tick()
            worker("check")
            scheduler.tick()
            worker("review")
            scheduler.tick()
            assert scheduler.state.get("DM-001")["last_review"]["verdict"] == "approve"
            task = scheduler.store.task("DM-001")
            assert task.pr
            persona = scheduler.dispatch_persona_pr(task, "user")
            assert persona.runner == "remote"
            worker("persona")
            scheduler.tick()
            assert scheduler.state.get("DM-001").get("persona_reviews")
            phase_run = scheduler.dispatch_persona_phase(store.product("demo").phases[0], "user")
            assert phase_run.runner == "remote"
            worker("persona", phase_run.task_id)
            scheduler.tick()
            assert scheduler.runs.latest(phase_run.task_id).status == "done"
            assert client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).status_code == 204
            assert "@build-1" in client.get(f"/runs/DM-001/{run.run_id}").text
            artifact = {
                "source_head": subprocess.run(
                    ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
                    cwd=Path(__file__).resolve().parents[1],
                ).stdout.strip(),
                "test": "tests/test_remote_worker.py::test_remote_lifecycle_over_served_http",
                "transport": "real TCP HTTP",
                "worker_process": "separate python -m garden.managed_worker process" if managed else "separate python -m garden worker CLI process",
                "worker_command": "python -m garden.managed_worker --config isolated-config --once" if managed else "python -m garden worker --garden URL --host build-1 --work-dir isolated --harness claude --once",
                "actions": [
                    "reject unauthenticated and cross-origin claims",
                    "claim then expire a work lease and reject its stale heartbeat",
                    "reclaim and finish work, check, review, PR persona, and phase persona runs",
                    "open the run page and drain the queue",
                ],
                "observations": {
                    "setup_environment_absent_from_claim": True,
                    "managed_setup_once_inside_host_lock": managed,
                    "managed_host_attribution_persisted": managed,
                    "stale_heartbeat_status": 409,
                    "review_verdict": "approve",
                    "pr_opened": True,
                    "run_page_host": "build-1",
                    "final_claim_status": 204,
                },
                "runs": events,
                "http": http_events,
            }
            (tmp_path / "served-remote-events.json").write_text(json.dumps(artifact, indent=2))
            if destination := os.environ.get("GARDEN_REMOTE_INTERACTION_ARTIFACT"):
                output = Path(destination)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(artifact, indent=2) + "\n")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
