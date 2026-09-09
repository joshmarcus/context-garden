from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from garden import gitops
from garden.remote_worker import doctor_worker, execute_claim
from garden.runner.remote import RemoteRunner
from garden.runs import RunStore
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app


def remote_client(garden, monkeypatch, *, validation_timeout=900):
    path = garden / "garden.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["workers"] = {"lease_seconds": 60, "hosts": [{"name": "build-1", "token_env": "BUILD_TOKEN", "max_parallel": 1}]}
    cfg["max_parallel"] = 1
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


def queued_run(store):
    run = RunStore(store.config.garden_dir).new_run("DM-001", "remote", mode="work")
    run.branch, run.base, run.harness, run.model, run.difficulty = "garden/dm-001", "main", "claude", "small", "easy"
    RemoteRunner({"worker_env": store.config.get("worker_env")}, store.config.harness("claude")).start(run, store.root, "safe brief")
    return run


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

    beat = client.post(f"/api/runs/{run.run_id}/heartbeat",
                       json={"lease_token": payload["lease_token"], "transcript": "hello\n"}, headers=auth)
    assert beat.status_code == 200
    done = client.post(f"/api/runs/{run.run_id}/finish", json={"lease_token": payload["lease_token"],
                       "exit_code": 0, "final_text": "done", "result": {"status": "done"},
                       "usage": {"input_tokens": 2}, "cost_usd": 0.1, "pushed_head": "abc"}, headers=auth)
    assert done.status_code == 200
    saved = RunStore(store.config.garden_dir).latest("DM-001")
    assert saved.host == "build-1" and saved.pushed_head == "abc"
    assert saved.process_finished() and saved.stdout_text() == "hello\n"
    saved.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    saved.save()
    assert client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).status_code == 204


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


def test_reclaimed_lease_fences_stale_worker_on_same_host(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    offer = {"host": "build-1", "harnesses": ["claude"]}
    claim1 = client.post("/api/runs/claim", json=offer, headers=auth).json()
    run = RunStore(store.config.garden_dir).latest("DM-001")
    run.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
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
    run.save()
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"], "tiers": ["easy"]},
                           headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200 and response.json()["id"] == run.run_id
    assert store.task("DM-001").status.value == "ready"


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
            run.save()
            assert client.post(f"/api/runs/{run.run_id}/heartbeat",
                               json={"lease_token": claim["lease_token"]}, headers=auth).status_code == 409
            # Reclaim through the actual CLI, without a controller object in that process.
            env = {k: v for k, v in os.environ.items() if k in
                   {"PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT"} or k.startswith("FAKE_")}
            env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                       GARDEN_WORKER_TOKEN="secret-token", FAKE_CLAUDE_MODE="done")
            setup_counts = {}
            worker_config = {
                "endpoint": url, "worker_token": "secret-token", "host": "build-1",
                "work_dir": str(tmp_path / "http-host"), "harnesses": ["claude"],
                "profile_version": "fixture-v1", "bootstrap_version": "fixture-v1",
                "source_head": "a" * 40, "provider_id": "disposable-http-host",
                "memory_reserve_mib": 0, "disk_reserve_mib": 0,
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
