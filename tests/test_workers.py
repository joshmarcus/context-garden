from __future__ import annotations

import datetime as dt
import json
import time

import yaml
from fastapi.testclient import TestClient

from garden.runs import RunStore
from garden.store import Store
from garden.web.app import create_app
from garden.workers import WorkerContactStore, snapshot


def configure(garden, *, remote=True, ssh=True):
    path = garden / "garden.yaml"
    data = yaml.safe_load(path.read_text())
    data["ssh"]["hosts"] = data["ssh"]["hosts"] if ssh else []
    data["workers"] = {"poll_seconds": 5, "lease_seconds": 60, "recovery_seconds": 120,
                       "hosts": ([{"name": "pull-a", "token_env": "PULL_TOKEN",
                                   "max_parallel": 2}] if remote else [])}
    path.write_text(yaml.safe_dump(data))
    return Store(garden)


def test_idle_contact_survives_zero_jobs_and_api_matches_ui(garden, monkeypatch):
    store = configure(garden, ssh=False)
    monkeypatch.setenv("PULL_TOKEN", "secret")
    client = TestClient(create_app(store, watch=False, host="testserver"))

    poll = client.post("/api/runs/claim", json={"host": "pull-a", "capacity": 2,
                       "harnesses": ["claude"], "tiers": ["easy"],
                       "host_facts": {"provider_id": "provider-current", "observed_at": time.time()}},
                       headers={"Authorization": "Bearer secret"})
    assert poll.status_code == 204
    payload = client.get("/api/workers").json()
    assert "secret" not in json.dumps(payload).lower()
    assert "token_env" not in json.dumps(payload)
    assert payload["totals"]["workers"] == 1
    assert payload["totals"]["jobs"] == 0
    assert payload["workers"][0]["status"] == "available"
    assert payload["workers"][0]["evidence_stale"] is False
    page = client.get("/now/workers")
    assert page.status_code == 200
    assert "pull-a" in page.text and "0" in page.text and "polling for work" in page.text
    assert "Available pull workers poll for work" in page.text


def test_worker_snapshot_separates_contact_from_lease_and_excludes_reservations(garden):
    store = configure(garden)
    runs = RunStore(store.config.garden_dir)
    now = dt.datetime(2026, 9, 10, 3, tzinfo=dt.UTC)
    contact = WorkerContactStore(store.config.garden_dir)
    contact.record("pull-a", capacity=2, harnesses=["claude"], tiers=["easy"],
                   facts={"provider_id": "old-provider", "observed_at": now.timestamp()})
    contact.record("pull-a", capacity=2, harnesses=["claude"], tiers=["easy"],
                   facts={"provider_id": "new-provider", "observed_at": now.timestamp()})
    remote = runs.new_run("DM-001", "remote")
    remote.host = "pull-a"
    remote.claimed_at = (now - dt.timedelta(minutes=3)).isoformat()
    remote.lease_expires_at = (now - dt.timedelta(seconds=10)).isoformat()
    remote.recovery_expires_at = (now + dt.timedelta(seconds=90)).isoformat()
    remote.save()
    manual = runs.new_run("DM-002", "manual")
    queued = runs.new_run("QUEUED", "remote")

    fleet = snapshot(store.config, runs, now=now)
    pull = next(worker for worker in fleet["workers"] if worker["id"] == "pull-a")
    assert pull["status"] == "reconnecting"
    assert pull["current_jobs"][0]["lease_state"] == "recovering"
    assert pull["prior_provider_ids"] == ["old-provider"]
    assert fleet["totals"]["jobs"] == 1
    assert all(job["run_id"] not in {manual.run_id, queued.run_id}
               for worker in fleet["workers"] for job in worker["current_jobs"])


def test_worker_states_cover_stale_explicit_and_no_workers(garden):
    store = configure(garden, ssh=False)
    now = dt.datetime(2026, 9, 10, 3, tzinfo=dt.UTC)
    contacts = WorkerContactStore(store.config.garden_dir)
    contacts.record("pull-a", capacity=1, harnesses=[], tiers=[], facts={})
    saved = contacts.read()
    saved["pull-a"]["last_contact"] = (now - dt.timedelta(minutes=2)).isoformat()
    contacts.path.write_text(json.dumps({"workers": saved}))
    fleet = snapshot(store.config, RunStore(store.config.garden_dir), now=now)
    assert fleet["workers"][0]["status"] == "unreachable"
    assert fleet["workers"][0]["evidence_stale"] is True

    path = garden / "garden.yaml"
    data = yaml.safe_load(path.read_text())
    data["workers"]["hosts"] = [{"name": "pull-a", "token_env": "PULL_TOKEN", "state": "terminated"}]
    path.write_text(yaml.safe_dump(data))
    assert snapshot(Store(garden).config, RunStore(store.config.garden_dir), now=now)["workers"][0]["status"] == "terminated"

    contacts.path.unlink()
    data["workers"]["hosts"] = []
    path.write_text(yaml.safe_dump(data))
    empty = snapshot(Store(garden).config, RunStore(store.config.garden_dir), now=now)
    assert empty["totals"]["workers"] == 0


def test_reenrolled_provider_is_not_duplicated_under_stale_alias(garden):
    store = configure(garden, ssh=False)
    contacts = WorkerContactStore(store.config.garden_dir)
    contacts.record("old-name", capacity=1, harnesses=[], tiers=[],
                    facts={"provider_id": "same-host"})
    contacts.record("pull-a", capacity=1, harnesses=[], tiers=[],
                    facts={"provider_id": "same-host"})
    fleet = snapshot(store.config, RunStore(store.config.garden_dir))
    assert [worker["id"] for worker in fleet["workers"]] == ["pull-a"]


def test_mixed_local_remote_jobs_and_explicit_worker_states(garden):
    store = configure(garden, ssh=False)
    path = garden / "garden.yaml"
    data = yaml.safe_load(path.read_text())
    data["workers"]["hosts"].extend([
        {"name": "drain", "token_env": "DRAIN_TOKEN", "state": "draining"},
        {"name": "restart", "token_env": "RESTART_TOKEN", "state": "restarting"},
        {"name": "off", "token_env": "OFF_TOKEN", "state": "disabled"},
    ])
    path.write_text(yaml.safe_dump(data))
    store = Store(garden)
    contacts = WorkerContactStore(store.config.garden_dir)
    contacts.record("pull-a", capacity=2, harnesses=["claude"], tiers=[], facts={})
    runs = RunStore(store.config.garden_dir)
    remote = runs.new_run("DM-001", "remote")
    remote.host = "pull-a"
    remote.save()
    runs.new_run("DM-002", "local")

    fleet = snapshot(store.config, runs)
    states = {worker["id"]: worker for worker in fleet["workers"]}
    assert states["pull-a"]["status"] == "executing"
    assert states["pull-a"]["available_capacity"] == 1
    assert states["local"]["status"] == "executing"
    assert states["drain"]["status"] == "draining"
    assert states["restart"]["status"] == "restarting"
    assert states["off"]["status"] == "disabled"
    assert fleet["totals"]["jobs"] == 2


def test_workers_page_has_responsive_layout_and_drill_down_links(garden):
    store = configure(garden, remote=False)
    run = RunStore(store.config.garden_dir).new_run("DM-001", "ssh")
    run.host = "boxA"
    run.save()
    page = TestClient(create_app(store, watch=False, host="testserver")).get("/now/workers").text
    assert '@media (max-width:600px)' in page
    assert 'href="/tasks/DM-001"' in page
    assert f'href="/runs/DM-001/{run.run_id}"' in page
    assert "job lease: not applicable" in page
