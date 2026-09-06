from __future__ import annotations

import datetime as dt

import yaml
from fastapi.testclient import TestClient

from garden.remote_worker import execute_claim
from garden.runner.remote import RemoteRunner
from garden.runs import RunStore
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app


def remote_client(garden, monkeypatch):
    path = garden / "garden.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["workers"] = {"lease_seconds": 60, "hosts": [{"name": "build-1", "token_env": "BUILD_TOKEN", "max_parallel": 1}]}
    cfg["max_parallel"] = 1
    cfg["products"]["demo"]["runner"] = "remote"
    cfg["checks"] = {"pre_pr": [
        {"name": "remote-context", "command": "test \"$GARDEN_BRANCH\" = garden/dm-001-first-task"}
    ], "ci": []}
    cfg["review"] = {"enabled": True, "max_rounds": 1}
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("BUILD_TOKEN", "secret-token")
    return TestClient(create_app(Store(garden), watch=False, host="testserver")), Store(garden)


def queued_run(store):
    run = RunStore(store.config.garden_dir).new_run("DM-001", "remote", mode="work")
    run.branch, run.base, run.harness, run.model, run.difficulty = "garden/dm-001", "main", "claude", "small", "easy"
    RemoteRunner({"worker_env": store.config.get("worker_env")}, store.config.harness("claude")).start(run, store.root, "safe brief")
    return run


def test_remote_api_auth_claim_heartbeat_finish_and_origin(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}

    assert client.post("/api/runs/claim", json={"host": "build-1"}).status_code == 401
    assert client.post("/api/runs/claim", json={"host": "build-1"}, headers={"Origin": "https://evil.test"}).status_code == 403
    response = client.post("/api/runs/claim", json={"host": "build-1", "harnesses": ["claude"], "tiers": ["easy"], "capacity": 1}, headers=auth)
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == run.run_id and payload["brief"] == "safe brief"
    assert payload["lease_token"] and "secret-token" not in str(payload)
    assert set(payload) >= {"repo", "branch", "base", "setup", "turn_cap", "env_allowlist"}

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


def test_reclaimed_lease_fences_stale_worker_on_same_host(garden, monkeypatch):
    client, store = remote_client(garden, monkeypatch)
    run = queued_run(store)
    auth = {"Authorization": "Bearer secret-token"}
    claim1 = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).json()
    run = RunStore(store.config.garden_dir).latest("DM-001")
    run.lease_expires_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    run.save()
    claim2 = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).json()

    assert claim2["lease_token"] != claim1["lease_token"]
    stale = client.post(f"/api/runs/{run.run_id}/finish",
                        json={"lease_token": claim1["lease_token"], "exit_code": 0}, headers=auth)
    assert stale.status_code == 409
    fresh = client.post(f"/api/runs/{run.run_id}/heartbeat",
                        json={"lease_token": claim2["lease_token"]}, headers=auth)
    assert fresh.status_code == 200


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
    client, store = remote_client(garden, monkeypatch)
    scheduler = Scheduler(store, github=fake_github)
    scheduler.tick()  # dispatch work
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
    scheduler.tick()  # reap work and dispatch the remote pre-PR check
    check_claim = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).json()
    assert check_claim["mode"] == "check"
    assert check_claim["checks"]["ctx"]["branch"] == "garden/dm-001-first-task"
    assert "exec_root" not in check_claim["checks"]["ctx"]
    assert set(check_claim["checks"]["config"]) == {"worker_env"}
    execute_claim(check_claim, tmp_path / "independent-host", PostingClient())
    scheduler.tick()  # reap check, open PR, and dispatch review
    store.invalidate_tasks()
    task = store.task("DM-001")
    assert task.pr and task.status.value == "in_review"
    review_claim = client.post("/api/runs/claim", json={"host": "build-1"}, headers=auth).json()
    assert review_claim["mode"] == "review"
    execute_claim(review_claim, tmp_path / "independent-host", PostingClient())
    scheduler.tick()  # reap and apply the approving review
    assert scheduler.state.get("DM-001")["last_review"]["verdict"] == "approve"
    modes = {run.mode: run for run in RunStore(store.config.garden_dir).runs_for("DM-001")}
    assert modes["check"].result["checks"][0]["status"] == "pass"
    assert modes["review"].status == "done"
    assert "@build-1" in client.get(f"/runs/DM-001/{saved.run_id}").text
