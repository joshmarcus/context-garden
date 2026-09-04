"""Tests for inbox draft/retrying cards and digest failure counting."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

from garden.events import digest
from garden.inbox import GROUP_KIND, build_inbox, decisions, notices
from garden.scheduler import State
from garden.store import Store


class _FakeSched:
    """Minimal scheduler stand-in for build_inbox."""

    def __init__(self, garden_dir: Path):
        self.state = State(garden_dir / "state.json")

    def budget_for(self, key: str):
        return None

    def spent_for(self, key: str):
        return 0.0


def _sched(garden: Path) -> _FakeSched:
    return _FakeSched(garden / ".garden")


def _store(garden: Path) -> Store:
    return Store(garden)


# --------------------------------------------------------------------------- inbox


def test_draft_card_shows_attempts_and_last_log(garden: Path, tmp_path: Path):
    """approve-group item carries attempt count and last log line in why."""
    task_path = garden / "demo" / "p1" / "tasks" / "DM-001-first.md"
    task_path.write_text(textwrap.dedent("""\
        ---
        id: DM-001
        title: First task
        status: draft
        depends_on: []
        priority: 1
        reading: []
        attempts: 2
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the first thing.

        ## Log

        - 2026-01-01T00:00:00+00:00 attempt 1 failed: max turns; will retry
        - 2026-01-02T00:00:00+00:00 attempt 2 failed: error; will retry
        """))
    store = _store(garden)
    items = build_inbox(store, _sched(garden))
    approve = [i for i in items if i["group"] == "approve" and i["task"] == "DM-001"]
    assert approve, "DM-001 should appear in approve group"
    item = approve[0]
    assert "2 attempts" in item["why"]
    assert "attempt 2 failed: error; will retry" in item["why"]
    assert item["attempts"] == 2
    assert "attempt 2 failed" in item["last_log"]


def test_retrying_group_for_ready_task_with_prior_failure(garden: Path):
    """ready task with attempts>0 appears in retrying group, not approve group."""
    task_path = garden / "demo" / "p1" / "tasks" / "DM-001-first.md"
    task_path.write_text(textwrap.dedent("""\
        ---
        id: DM-001
        title: First task
        status: ready
        depends_on: []
        priority: 1
        reading: []
        attempts: 1
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the first thing.

        ## Log

        - 2026-01-01T00:00:00+00:00 attempt 1 failed: max turns reached; will retry
        """))
    store = _store(garden)
    items = build_inbox(store, _sched(garden))
    retrying = [i for i in items if i["group"] == "retrying" and i["task"] == "DM-001"]
    assert retrying, "DM-001 should appear in retrying group"
    item = retrying[0]
    assert "attempt 1 failed: max turns reached; will retry" in item["why"]
    assert item["attempts"] == 1
    approve = [i for i in items if i["group"] == "approve" and i["task"] == "DM-001"]
    assert not approve, "ready task should not appear in approve group"


# --------------------------------------------------------------------------- digest


def test_digest_counts_failed_runs_as_notable():
    """run_finished with non-success status for work/revise mode is collected in failures."""
    events = [
        {"at": "2026-01-01T00:01:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "work", "status": "error", "cost_usd": 0.10},
        {"at": "2026-01-01T00:02:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "work", "status": "done", "cost_usd": 0.05},
        {"at": "2026-01-01T00:03:00+00:00", "kind": "run_finished", "task": "DM-002",
         "mode": "revise", "status": "timeout", "cost_usd": 0.08},
        {"at": "2026-01-01T00:04:00+00:00", "kind": "run_finished", "task": "DM-003",
         "mode": "review", "status": "no_result", "cost_usd": 0.01},
    ]
    d = digest(events)
    assert len(d["failures"]) == 2, "error and timeout work/revise runs are failures"
    task_ids = {ev["task"] for ev in d["failures"]}
    assert task_ids == {"DM-001", "DM-002"}
    assert round(d["cost_usd"], 2) == 0.24, "all run_finished contribute to cost"


def test_digest_nothing_notable_without_failures():
    """digest with only successful runs stays clean."""
    events = [
        {"at": "2026-01-01T00:01:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "work", "status": "done", "cost_usd": 0.05},
        {"at": "2026-01-01T00:02:00+00:00", "kind": "dispatch", "task": "DM-001"},
    ]
    d = digest(events)
    assert d["failures"] == []
    assert not any([d["needs_human"], d["prs_opened"], d["reviews"], d["merged"], d["discovered"], d["failures"]])


# --------------------------------------------------------------------------- decisions vs notices


def _garden_with_a_decision_and_a_notice(garden: Path) -> None:
    """DM-001 is a ready task retrying after a failed attempt (notice); DM-002 is an
    unapproved draft (decision)."""
    write = textwrap.dedent
    (garden / "demo" / "p1" / "tasks" / "DM-001-first.md").write_text(write("""\
        ---
        id: DM-001
        title: First task
        status: ready
        depends_on: []
        priority: 1
        reading: []
        attempts: 1
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the first thing.

        ## Log

        - 2026-01-01T00:00:00+00:00 attempt 1 failed: max turns reached; will retry
        """))
    (garden / "demo" / "p1" / "tasks" / "DM-002-second.md").write_text(write("""\
        ---
        id: DM-002
        title: Second task
        status: draft
        depends_on: []
        priority: 2
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the second thing.
        """))


def test_group_kind_classifies_retrying_as_notice_and_approve_as_decision():
    assert GROUP_KIND["retrying"] == "notice"
    assert GROUP_KIND["tool"] == "notice"
    for g in ("question", "decision", "triage", "review", "attention", "approve", "budget"):
        assert GROUP_KIND[g] == "decision", g


def test_decisions_and_notices_split_the_inbox(garden: Path):
    _garden_with_a_decision_and_a_notice(garden)
    store = _store(garden)
    items = build_inbox(store, _sched(garden))
    dec = decisions(items)
    notes = notices(items)
    assert [i["group"] for i in dec] == ["approve"]
    assert [i["group"] for i in notes] == ["retrying"]
    assert {i["task"] for i in dec} == {"DM-002"}
    assert {i["task"] for i in notes} == {"DM-001"}


def _cli(garden: Path, *args: str):
    from typer.testing import CliRunner

    from garden.cli import app

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(cwd)


def test_cli_inbox_prints_decisions_and_notices_separately(garden: Path):
    _garden_with_a_decision_and_a_notice(garden)
    r = _cli(garden, "inbox")
    assert r.exit_code == 0
    assert "1 need you" in r.output
    assert "DM-002" in r.output
    assert "Notices" in r.output and "no action needed" in r.output
    assert "DM-001" in r.output
    # the retrying notice must not be counted in the "need you" figure
    assert r.output.index("DM-002") < r.output.index("Notices")


def test_cli_digest_prints_decisions_and_notices_line(garden: Path):
    from garden.events import EventLog

    log = EventLog(garden / ".garden" / "events.jsonl")
    log.emit("needs_human", "DM-001", stop_kind="stall", reason="revise round changed nothing")
    log.emit("run_finished", "DM-002", mode="work", status="error", cost_usd=0.1)
    r = _cli(garden, "digest", "--since", "90d")
    assert r.exit_code == 0
    assert "1 decision" in r.output and "need you" in r.output
    assert "1 notice" in r.output and "no action needed" in r.output


def test_web_inbox_count_excludes_retrying(garden: Path):
    from fastapi.testclient import TestClient

    from garden.web.app import create_app

    _garden_with_a_decision_and_a_notice(garden)
    c = TestClient(create_app(Store(garden), watch=False))
    page = c.get("/").text
    assert '<div class="v">1</div>' in page  # "need you" KPI counts the one decision only
    assert "Auto-retrying" in page and "no action needed" in page
    assert "DM-002" in page and "DM-001" in page
