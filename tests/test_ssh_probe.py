"""A static SSH host earns the presence a pull worker reports for itself, by being probed.

The probe is deliberately narrow: bounded per host, read-only on the host, cached on a
configured cadence, and run in a process of its own.  These tests hold it to all four.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import os
import subprocess
import sys
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.runs import RunStore
from garden.scheduler import fleet as scheduler_fleet
from garden.ssh_probe import (
    COMPLETE,
    SSHProbeStore,
    main,
    probe_lines,
    probe_readings,
    probe_settings,
    probes_due,
    run_probe_pass,
    unreachable_hosts,
)
from garden.store import Store
from garden.web.app import create_app
from garden.workers import snapshot

START = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)


def with_hosts(garden, hosts):
    """Rewrite the configured static hosts and return the reloaded configuration."""
    path = garden / "garden.yaml"
    data = yaml.safe_load(path.read_text())
    data["ssh"]["hosts"] = hosts
    path.write_text(yaml.safe_dump(data))
    return Store(garden).config


def checkout(tmp_path):
    """Create the checkout the configured host declares, so a probe can find it."""
    directory = tmp_path / "remote-clone"
    (directory / ".git").mkdir(parents=True)
    return directory


def ssh_row(fleet):
    return next(row for row in fleet["workers"] if row["id"] == "boxA")


def doctor(garden):
    """Run `garden doctor` in the garden, the way the CLI is always entered."""
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(app, ["doctor"])
    finally:
        os.chdir(cwd)


def answers(stdout="", stderr="", code=0):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    return run


def test_a_probe_records_latency_and_a_read_only_failure_reason(garden):
    config = Store(garden).config

    results = run_probe_pass(config)

    assert list(results) == ["boxA"]
    assert results["boxA"]["ok"] is False
    assert "missing checkout" in results["boxA"]["reason"]
    reading = probe_readings(config)["boxA"]
    assert (reading["ok"], reading["stale"]) == (False, False)
    assert reading["latency_ms"] >= 0
    assert reading["checked_at"] and reading["last_success_at"] is None


def test_a_reachable_host_with_its_checkout_is_available_in_the_projection(garden, tmp_path):
    checkout(tmp_path)
    config = Store(garden).config

    run_probe_pass(config)

    row = ssh_row(snapshot(config, RunStore(config.garden_dir)))
    assert row["status"] == "available"
    assert (row["probe"]["ok"], row["probe"]["stale"]) == (True, False)
    assert row["unavailable_reason"] == "no current job"


def test_a_failed_probe_makes_a_static_host_unreachable_on_the_api_and_the_page(garden):
    store = Store(garden)
    run_probe_pass(store.config)
    client = TestClient(create_app(store, watch=False, host="testserver"))

    row = ssh_row(client.get("/api/workers").json())
    assert row["status"] == "unreachable"
    assert "missing checkout" in row["unavailable_reason"]
    assert row["probe"]["last_success_at"] is None
    page = client.get("/now/workers").text
    assert "probe: failed" in page and "last success never" in page


def test_a_later_failure_keeps_the_last_successful_contact(garden, tmp_path):
    config = Store(garden).config
    directory = checkout(tmp_path)
    run_probe_pass(config, now=START)
    (directory / ".git").rmdir()
    directory.rmdir()
    later = START + dt.timedelta(seconds=300)

    run_probe_pass(config, now=later)

    reading = probe_readings(config, now=later)["boxA"]
    assert reading["ok"] is False
    assert reading["last_success_at"] == START.isoformat()
    assert reading["checked_at"] == later.isoformat()


def test_a_reading_two_cadences_old_is_stale_and_says_when_it_last_ran(garden, tmp_path):
    checkout(tmp_path)
    config = Store(garden).config
    run_probe_pass(config, now=START)
    later = START + dt.timedelta(seconds=probe_settings(config).stale_after_seconds + 1)

    reading = probe_readings(config, now=later)["boxA"]
    assert (reading["ok"], reading["stale"], reading["age_seconds"]) == (True, True, 601)

    row = ssh_row(snapshot(config, RunStore(config.garden_dir), now=later))
    assert row["status"] == "unknown"
    assert row["unavailable_reason"] == (
        f"no fresh reachability probe (probe last ran {START.isoformat()})")


def test_a_pass_is_owed_on_the_cadence_and_never_while_one_is_in_flight(garden):
    config = Store(garden).config
    settings = probe_settings(config)
    assert probes_due(config, now=START) is True

    SSHProbeStore(config.garden_dir).request(now=START)
    assert probes_due(config, now=START) is False
    in_flight = dt.timedelta(seconds=settings.timeout_seconds * 2)
    assert probes_due(config, now=START + in_flight) is True

    run_probe_pass(config, now=START + in_flight)
    cadence = dt.timedelta(seconds=settings.interval_seconds)
    assert probes_due(config, now=START + in_flight + cadence - dt.timedelta(seconds=1)) is False
    assert probes_due(config, now=START + in_flight + cadence) is True


def test_a_garden_with_no_static_host_probes_nothing(garden):
    config = with_hosts(garden, [])

    assert probes_due(config, now=START) is False
    assert run_probe_pass(config) == {}
    assert not (config.garden_dir / "ssh-probes.json").exists()


def test_the_probe_is_bounded_and_asks_the_host_only_to_read(garden):
    config = Store(garden).config
    settings = probe_settings(config)
    calls: list[tuple[list[str], dict]] = []

    def record(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, f"{COMPLETE}\n", "")

    assert run_probe_pass(config, run=record)["boxA"]["ok"] is True

    command, kwargs = calls[0]
    assert command[0] == settings.ssh_bin and command[-3:] == ["boxA", "sh", "-s"]
    assert kwargs["timeout"] == settings.timeout_seconds
    script = kwargs["input"]
    assert "command -v git" in script and "command -v tmux" in script
    assert script.strip().endswith(f"echo {COMPLETE}")
    for written in ("git status", "git fetch", "git checkout", "git clone", "git add",
                    "git stash", "rm ", "mkdir", "install", "fmt", "lint"):
        assert written not in script


def test_a_real_pass_leaves_the_checkout_exactly_as_it_found_it(garden, tmp_path):
    directory = checkout(tmp_path)
    (directory / "messy.py").write_text("x = { 'a' :1 }\n")
    before = sorted((path.name, path.stat().st_mtime_ns) for path in directory.iterdir())

    assert run_probe_pass(Store(garden).config)["boxA"]["ok"] is True

    assert (directory / "messy.py").read_text() == "x = { 'a' :1 }\n"
    assert sorted((path.name, path.stat().st_mtime_ns) for path in directory.iterdir()) == before


def test_hosts_are_probed_at_once_so_one_slow_host_does_not_delay_the_rest(garden):
    config = with_hosts(garden, [{"name": f"box{index}", "host": f"box{index}",
                                  "repos": {}, "max_parallel": 1} for index in range(4)])

    def slow(command, **kwargs):
        time.sleep(0.3)
        return subprocess.CompletedProcess(command, 0, f"{COMPLETE}\n", "")

    started = time.monotonic()
    results = run_probe_pass(config, run=slow)

    assert len(results) == 4 and all(row["ok"] for row in results.values())
    assert time.monotonic() - started < 0.9


def test_a_host_that_does_not_answer_within_the_bound_reports_the_timeout(garden):
    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    results = run_probe_pass(Store(garden).config, run=hang)

    assert results["boxA"]["ok"] is False
    assert results["boxA"]["reason"] == "probe timed out after 15s"


def test_a_transport_failure_or_truncated_session_is_never_read_as_a_clean_host(garden):
    config = Store(garden).config

    refused = run_probe_pass(config, run=answers(
        stderr="debug: reading configuration\nssh: connect to host port 22: refused\n", code=255))
    assert refused["boxA"] == {**refused["boxA"], "ok": False,
                               "reason": "ssh exited 255: ssh: connect to host port 22: refused"}

    truncated = run_probe_pass(config, run=answers(stdout="", code=0))
    assert truncated["boxA"]["ok"] is False
    assert truncated["boxA"]["reason"] == "probe did not complete on the host"


@pytest.mark.starts_host_probe
def test_a_tick_starts_the_pass_in_a_process_it_never_waits_for(garden, sched, monkeypatch):
    launched: list[tuple[list[str], dict]] = []

    class Started:
        pid = 4321

    def fake_popen(command, **kwargs):
        launched.append((command, kwargs))
        return Started()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    sched.probe_ssh_workers()

    command, kwargs = launched[0]
    assert command == [sys.executable, "-m", "garden.ssh_probe", str(sched.cfg.root)]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
    # The cadence is claimed before the child exists, so the next tick starts nothing while
    # this pass is still running.
    sched.probe_ssh_workers()
    assert len(launched) == 1


def test_doctor_accounts_for_every_static_host_from_the_cache(garden):
    config = Store(garden).config
    SSHProbeStore(config.garden_dir).record(
        {"boxA": {"ok": False, "latency_ms": 42, "reason": "missing checkout: /srv/demo"}})

    assert probe_lines(config)[0].startswith("static host boxA: unreachable · 42 ms · checked ")
    assert probe_lines(config)[0].endswith("· last success never · missing checkout: /srv/demo")
    assert unreachable_hosts(config) == ["boxA"]

    result = doctor(garden)
    assert "static host boxA: unreachable" in result.output
    assert "missing checkout: /srv/demo" in result.output
    assert "failed:" in result.output and "static host boxA" in result.output.split("failed:")[1]


def test_doctor_says_so_when_a_static_host_has_never_been_probed(garden):
    config = Store(garden).config

    assert probe_lines(config) == ["static host boxA: no probe reading yet"]
    assert unreachable_hosts(config) == []


@pytest.mark.starts_host_probe
def test_the_startup_pass_and_every_tick_ask_for_a_probe(sched, monkeypatch):
    asked: list[object] = []
    monkeypatch.setattr(scheduler_fleet.FleetMixin, "probe_ssh_workers",
                        lambda self: asked.append(self.cfg.root))

    sched.reap_on_start()
    sched.tick(dispatch=False)

    assert asked == [sched.cfg.root, sched.cfg.root]


def test_the_module_entry_point_runs_one_pass_and_yields_to_the_holder(garden, tmp_path):
    checkout(tmp_path)
    config = Store(garden).config

    assert main([str(garden)]) == 0
    assert probe_readings(config)["boxA"]["ok"] is True
    assert main([]) == 2

    cache = config.garden_dir / "ssh-probes.json"
    cache.unlink()
    with (config.garden_dir / "ssh-probe.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert main([str(garden)]) == 0
    assert not cache.exists()
