from __future__ import annotations

import datetime as dt
import json
import threading

from fastapi.testclient import TestClient

from garden.scheduler import Scheduler, TickReport
from garden.scheduler_health import MAX_WATCHERS, WatchHeartbeat, scheduler_health
from garden.store import Store
from garden.web.app import create_app
from garden.web.common import Hub

NOW = dt.datetime(2026, 9, 9, 20, 0, tzinfo=dt.UTC)


def _lease(directory, pid: int, *, age: int = 0, state: str = "running", interval: int = 60):
    directory.mkdir(parents=True, exist_ok=True)
    heartbeat = NOW - dt.timedelta(seconds=age)
    (directory / f"{pid}-watch.json").write_text(json.dumps({
        "pid": pid,
        "process_identity": str(pid),
        "heartbeat_at": heartbeat.isoformat(),
        "interval_seconds": interval,
        "state": state,
    }))


def test_scheduler_health_reports_singleton_duplicate_stale_failed_and_missing(tmp_path):
    watchers = tmp_path / "watchers"

    def alive(pid, identity):
        return pid in {1, 2} and identity == str(pid)

    assert scheduler_health(tmp_path, now=NOW, process_matches=alive)["kind"] == "missing"
    _lease(watchers, 1)
    assert scheduler_health(tmp_path, now=NOW, process_matches=alive)["kind"] == "healthy"
    _lease(watchers, 2)
    assert scheduler_health(tmp_path, now=NOW, process_matches=alive)["kind"] == "duplicated"
    (watchers / "2-watch.json").unlink()
    _lease(watchers, 1, age=136)
    assert scheduler_health(tmp_path, now=NOW, process_matches=alive)["kind"] == "stale"
    _lease(watchers, 1, state="failed")
    assert scheduler_health(tmp_path, now=NOW, process_matches=alive)["kind"] == "failed"
    _lease(watchers, 1)
    assert scheduler_health(tmp_path, now=NOW, process_matches=lambda pid, identity: False)["kind"] == "failed"


def test_scheduler_health_bounds_lease_reads(tmp_path):
    for pid in range(MAX_WATCHERS + 5):
        _lease(tmp_path / "watchers", pid)
    result = scheduler_health(tmp_path, now=NOW, process_matches=lambda pid, identity: True)
    assert len(result["records"]) == MAX_WATCHERS


def test_web_reports_standalone_health_separately_from_embedded_watch(garden):
    heartbeat = WatchHeartbeat(Store(garden).config.garden_dir, 60)
    heartbeat.write("running")
    hub = Hub(Store(garden), watch=False)

    status = hub.scheduler_health()

    assert status["embedded"] == "off"
    assert status["effective"]["kind"] == "healthy"
    assert status["effective"]["label"] == "standalone watcher healthy"
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    assert "scheduler: standalone watcher healthy · embedded watch off" in client.get("/").text

    heartbeat.write("failed", error="watch pass crashed")
    assert "scheduler: standalone watcher failed · embedded watch off" in client.get("/").text

    heartbeat.write("running")
    assert "scheduler: standalone watcher healthy · embedded watch off" in client.get("/").text
    heartbeat.remove()


def test_manual_and_standalone_ticks_share_the_process_wide_tick_lock(garden, monkeypatch):
    """A web tick and standalone scheduler pass cannot enter their pass bodies together."""
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def controlled_tick(self, dispatch=None):
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            entered.set()
            assert release.wait(timeout=5)
        else:
            second_entered.set()
        return TickReport()

    monkeypatch.setattr(Scheduler, "_tick_locked", controlled_tick)
    hub = Hub(Store(garden), watch=False)
    standalone = Scheduler(Store(garden))
    first = threading.Thread(target=standalone.tick)
    second = threading.Thread(target=hub.tick)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    assert not second_entered.wait(timeout=0.1)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert second_entered.is_set()
