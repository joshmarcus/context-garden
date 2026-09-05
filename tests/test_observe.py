"""Tests for `garden observe`: CG-219."""

from __future__ import annotations

import json
import os
import textwrap
import time
from pathlib import Path

import yaml

from garden import observe as observe_mod
from garden.events import EventLog
from garden.observe import BUILTIN_PROFILES, event_matches, resolve
from garden.runs import RunStore
from garden.scheduler import Scheduler
from garden.store import Store


def _cli(garden: Path, *args: str):
    from typer.testing import CliRunner

    from garden.cli import app

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(cwd)


def _add_draft_task(garden: Path) -> None:
    (garden / "demo" / "p1" / "tasks" / "DM-003-third.md").write_text(textwrap.dedent("""\
        ---
        id: DM-003
        title: Third task
        status: draft
        depends_on: []
        priority: 3
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the third thing.

        ## Acceptance criteria

        - [ ] It does the third thing, provably.
        """))


def _add_stuck_run(garden: Path) -> None:
    """A run record whose process finished (an exit_code landed) but was never reaped, so
    it still shows up as `running` — the "process is gone" stuck case."""
    rs = RunStore(Store(garden).config.garden_dir)
    run = rs.new_run("DM-001", "local", "work")
    (run.path / "exit_code").write_text("0")


# --------------------------------------------------------------------------- one pass


def test_observe_renders_status_line_cards_and_stuck_runs(garden: Path):
    _add_draft_task(garden)
    _add_stuck_run(garden)
    r = _cli(garden, "observe")
    assert r.exit_code == 0, r.output
    assert "garden: test" in r.output
    assert "workers" in r.output and "spend $" in r.output
    assert "needs you" in r.output and "DM-003" in r.output and "approve" in r.output
    assert "stuck runs" in r.output and "DM-001" in r.output and "process is gone" in r.output


def test_observe_json_carries_the_same_fields(garden: Path):
    _add_draft_task(garden)
    _add_stuck_run(garden)
    r = _cli(garden, "observe", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.output.strip().splitlines()[-1])
    assert data["status_line"].startswith("garden: test")
    assert any(c["task"] == "DM-003" for c in data["cards"])
    assert any(s["task"] == "DM-001" and s["gone"] for s in data["stuck"])
    assert "digest_lines" in data and "digest" in data


def test_observe_omits_empty_sections(garden: Path):
    """A clean garden (nothing to approve, no runs) prints only the status line."""
    r = _cli(garden, "observe")
    assert r.exit_code == 0, r.output
    assert "needs you" not in r.output
    assert "stuck runs" not in r.output
    assert "tracebacks" not in r.output
    assert "digest" not in r.output


def test_line_width_clips_every_rendered_line(garden: Path):
    """CG-219: observe.line_width bounds every printed line, so a card or the status line
    can't soft-wrap into two lines regardless of the terminal it happens to print to."""
    cfg_path = garden / "garden.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data.setdefault("observe", {})["line_width"] = 40
    cfg_path.write_text(yaml.safe_dump(data))
    _add_draft_task(garden)
    r = _cli(garden, "observe")
    assert r.exit_code == 0, r.output
    lines = [ln for ln in r.output.splitlines() if ln]
    assert lines  # something printed
    assert all(len(ln) <= 40 for ln in lines), lines


# --------------------------------------------------------------------------- --follow event streaming


def test_follow_pass_streams_only_configured_events(garden: Path):
    store = Store(garden)
    log = EventLog(store.config.garden_dir / "events.jsonl")
    since = "2026-01-01T00:00:00+00:00"
    log.emit("dispatch", "DM-001")  # not in the default (quiet) events
    log.emit("needs_human", "DM-001", stop_kind="stall", reason="stuck")  # matches
    log.emit("run_finished", "DM-002", mode="work", status="done", cost_usd=0.1)  # not matched
    settings = resolve(store.config)
    assert settings.events == BUILTIN_PROFILES["quiet"]["events"]
    seen: list[str] = []
    observe_mod.follow_pass(store, settings, since, log=seen.append)
    assert len(seen) == 1
    assert "needs_human" in seen[0]


def test_cli_observe_follow_sleeps_for_the_configured_interval(garden: Path, monkeypatch):
    calls: list[float] = []

    def fake_sleep(seconds):
        calls.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", fake_sleep)
    r = _cli(garden, "observe", "--follow")
    assert r.exit_code == 0, r.output
    assert calls == [30 * 60]  # observe.interval default: 30m
    assert "stopped" in r.output


# --------------------------------------------------------------------------- profiles


def test_builtin_profiles_quiet_and_debug_differ_on_the_same_event_log(garden: Path):
    events = [
        {"kind": "dispatch", "task": "DM-001"},
        {"kind": "transition", "task": "DM-001", "from": "running", "to": "in_review", "note": "opened"},
        {"kind": "review", "task": "DM-001", "verdict": "approve"},
        {"kind": "needs_human", "task": "DM-001", "stop_kind": "stall", "reason": "stuck"},
        {"kind": "transition", "task": "DM-002", "from": "running", "to": "failed", "note": "crashed"},
    ]
    store = Store(garden)
    quiet = resolve(store.config, profile_override="quiet")
    debug = resolve(store.config, profile_override="debug")
    assert quiet.interval_s == 30 * 60
    assert debug.interval_s == 5 * 60

    quiet_matches = [e for e in events if event_matches(e, quiet.events)]
    debug_matches = [e for e in events if event_matches(e, debug.events)]
    # quiet only ever sees the needs_human stall and the failed transition
    assert {e["kind"] for e in quiet_matches} == {"needs_human", "transition"}
    assert len(quiet_matches) == 2
    # debug additionally sees the plain dispatch, the in_review transition and the review
    assert len(debug_matches) > len(quiet_matches)
    assert any(e["kind"] == "dispatch" for e in debug_matches)
    assert any(e["kind"] == "review" for e in debug_matches)


def test_custom_profile_overrides_any_field(garden: Path):
    store = Store(garden)
    store.config.data.setdefault("observe", {})["profiles"] = {
        "myprofile": {"interval": "1m", "events": ["dispatch"]},
    }
    settings = resolve(store.config, profile_override="myprofile")
    assert settings.interval_s == 60
    assert settings.events == ["dispatch"]
    # fields the custom profile does not name keep the base value
    assert settings.digest_window_s == 30 * 60


def test_cli_profile_flag_selects_a_profile(garden: Path):
    r = _cli(garden, "observe", "--profile", "debug", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.output.strip().splitlines()[-1])
    assert data["profile"] == "debug"


def test_live_override_selects_the_profile_for_a_running_follow(garden: Path):
    """`garden set observe.profile ...` (what the Config page's live override goes through)
    changes what `resolve()` picks the next time it is called with that scheduler — the
    mechanism `--follow` uses to switch profile without a restart."""
    store = Store(garden)
    sched = Scheduler(store, log=print)
    assert resolve(store.config, sched).profile == ""
    sched.set_override("observe.profile", "watch")
    settings = resolve(store.config, sched)
    assert settings.profile == "watch"
    assert settings.interval_s == 10 * 60


def test_cli_set_and_clear_observe_profile(garden: Path):
    r = _cli(garden, "set", "observe.profile", "watch")
    assert r.exit_code == 0 and "observe.profile = watch" in r.output
    r = _cli(garden, "observe", "--json")
    data = json.loads(r.output.strip().splitlines()[-1])
    assert data["profile"] == "watch"
    r = _cli(garden, "clear", "observe.profile")
    assert r.exit_code == 0
