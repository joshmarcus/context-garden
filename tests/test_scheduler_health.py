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


def test_scheduler_health_preserves_starting_until_running_or_tick_evidence(tmp_path):
    watchers = tmp_path / "watchers"
    _lease(watchers, 1, state="starting")

    starting = scheduler_health(tmp_path, now=NOW, process_matches=lambda pid, identity: True)
    assert starting["kind"] == "starting"
    assert starting["label"] == "standalone watcher starting"

    record = json.loads((watchers / "1-watch.json").read_text())
    record["last_tick"] = NOW.isoformat()
    (watchers / "1-watch.json").write_text(json.dumps(record))
    assert scheduler_health(
        tmp_path, now=NOW, process_matches=lambda pid, identity: True
    )["kind"] == "healthy"


def test_scheduler_health_bounds_retained_evidence(tmp_path):
    for pid in range(MAX_WATCHERS + 5):
        _lease(tmp_path / "watchers", pid)
    result = scheduler_health(tmp_path, now=NOW, process_matches=lambda pid, identity: True)
    assert len(result["records"]) == MAX_WATCHERS


def test_scheduler_health_prioritizes_fresh_lease_over_expired_history(tmp_path):
    watchers = tmp_path / "watchers"
    for pid in range(MAX_WATCHERS + 5):
        _lease(watchers, pid, age=1000)
    _lease(watchers, 999, age=0)

    result = scheduler_health(
        tmp_path, now=NOW, process_matches=lambda pid, identity: pid == 999
    )

    assert result["kind"] == "healthy"
    assert [record["pid"] for record in result["records"]] == [999]


def test_scheduler_health_does_not_hide_mixed_failed_or_stale_evidence(tmp_path):
    watchers = tmp_path / "watchers"
    _lease(watchers, 1)
    _lease(watchers, 2, state="failed")

    failed = scheduler_health(
        tmp_path, now=NOW, process_matches=lambda pid, identity: pid == 1
    )

    assert failed["kind"] == "failed"
    assert {record["pid"] for record in failed["records"]} == {1, 2}

    _lease(watchers, 2, age=136)
    duplicated = scheduler_health(
        tmp_path, now=NOW, process_matches=lambda pid, identity: pid in {1, 2}
    )

    assert duplicated["kind"] == "duplicated"
    assert duplicated["label"] == "duplicate standalone watchers (1 healthy, 1 stale)"
    assert {record["pid"] for record in duplicated["records"]} == {1, 2}


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


def test_web_reports_standalone_starting_separately_from_embedded_watch(garden):
    heartbeat = WatchHeartbeat(Store(garden).config.garden_dir, 60)
    heartbeat.write("starting")

    page = TestClient(create_app(Store(garden), watch=False, host="testserver")).get("/").text

    assert "scheduler: standalone watcher starting · embedded watch off" in page
    assert "standalone watcher healthy" not in page
    heartbeat.remove()


def test_embedded_watch_health_requires_pass_evidence_and_preserves_standalone_failure(
    garden, monkeypatch
):
    class LiveThread:
        def is_alive(self):
            return True

    hub = Hub(Store(garden), watch=False)
    hub.watch = True
    hub._embedded_state = "starting"
    hub._watch_thread = LiveThread()

    assert hub.scheduler_health()["effective"]["kind"] == "starting"
    hub._record_embedded("failed", "tick crashed")
    assert hub.scheduler_health()["effective"]["label"] == "embedded watcher failed"
    hub._record_embedded("healthy")
    assert hub.scheduler_health()["effective"]["label"] == "embedded watcher healthy"
    stale_at = dt.datetime.fromisoformat(hub._embedded_heartbeat) + dt.timedelta(seconds=136)
    assert hub._embedded_health(now=stale_at)["label"] == "embedded watcher stale"

    heartbeat = WatchHeartbeat(Store(garden).config.garden_dir, 60)
    heartbeat.write("failed", error="standalone crashed")
    status = hub.scheduler_health()
    assert status["effective"]["label"] == "standalone watcher failed"
    assert status["embedded_health"]["label"] == "embedded watcher healthy"
    heartbeat.remove()

    hub._watch_thread = type("StoppedThread", (), {"is_alive": lambda self: False})()
    assert hub.scheduler_health()["effective"]["label"] == "embedded watcher stopped"


def test_embedded_loop_failure_and_recovery_update_displayed_health(garden, monkeypatch):
    class LiveThread:
        def is_alive(self):
            return True

    class TwoPassStop:
        def __init__(self):
            self.passes = 0

        def is_set(self):
            return self.passes >= 2

        def wait(self, interval):
            self.passes += 1
            if self.passes == 1:
                assert hub.scheduler_health()["effective"]["label"] == "embedded watcher failed"

    class StartupScheduler:
        def reap_on_start(self):
            return None

    hub = Hub(Store(garden), watch=False)
    hub.watch = True
    hub._embedded_state = "starting"
    hub._watch_thread = LiveThread()
    hub._stop = TwoPassStop()
    outcomes = iter([RuntimeError("tick crashed"), "ok"])

    monkeypatch.setattr(hub, "scheduler", lambda: StartupScheduler())

    def tick_once():
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(hub, "tick", tick_once)
    hub._loop()

    assert hub.scheduler_health()["effective"]["label"] == "embedded watcher healthy"
    assert any(event["msg"] == "tick error: tick crashed" for event in hub.events)


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


def test_standalone_startup_reap_and_manual_tick_share_tick_lock(garden, monkeypatch):
    """Startup recovery completes before a manual tick can enter its pass body."""
    reap_entered = threading.Event()
    release_reap = threading.Event()
    tick_entered = threading.Event()

    def controlled_reap(self, report):
        reap_entered.set()
        assert release_reap.wait(timeout=5)

    def controlled_tick(self, dispatch=None):
        tick_entered.set()
        return TickReport()

    monkeypatch.setattr(Scheduler, "_reap_all", controlled_reap)
    monkeypatch.setattr(Scheduler, "_tick_locked", controlled_tick)
    standalone = Scheduler(Store(garden))
    hub = Hub(Store(garden), watch=False)
    startup = threading.Thread(target=standalone.reap_on_start)
    manual = threading.Thread(target=hub.tick)

    startup.start()
    assert reap_entered.wait(timeout=5)
    manual.start()
    assert not tick_entered.wait(timeout=0.1)
    release_reap.set()
    startup.join(timeout=5)
    manual.join(timeout=5)

    assert tick_entered.is_set()
