"""Now 1 (docs/design/now-1.md): the now module's computations, the page and its partials,
the live stream, the text view and the heat-map shading."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden import now1
from garden.cli import app
from garden.events import EventLog, difficulty_by_model, metrics
from garden.harness import Harness
from garden.runs import Run, RunStore
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app
from tests.conftest import FakeGitHub

NOW = dt.datetime(2026, 9, 6, 2, 0, tzinfo=dt.UTC)


def _client(garden) -> TestClient:
    return TestClient(create_app(Store(garden), watch=False, host="testserver"))


def _run(task: str, mode: str, status: str, minutes: float, harness: str = "claude", difficulty: str = "easy",
         days_ago: float = 0.0, run_id: str = "") -> Run:
    start = NOW - dt.timedelta(days=days_ago, minutes=minutes)
    return Run(task_id=task, run_id=run_id or f"{start:%Y%m%dT%H%M%SZ}-{mode}", dir="/nowhere", runner="local", mode=mode,
               harness=harness, difficulty=difficulty, status=status, started_at=start.isoformat(),
               finished_at=(NOW - dt.timedelta(days=days_ago)).isoformat())


def _record_running(garden, task="DM-001", mode="work", stdout="", harness="claude", model="sonnet",
                    started_minutes_ago: float = 7.0, pid: int | None = 4242) -> Run:
    rs = RunStore(Store(garden).config.garden_dir)
    run = rs.new_run(task, "local", mode)
    run.harness, run.model, run.difficulty, run.pid = harness, model, "easy", pid
    run.started_at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=started_minutes_ago)).isoformat()
    run.save()
    if stdout:
        (run.path / "stdout.json").write_text(stdout)
    return run


# ---- the clock and the typical duration ----------------------------------------------

def test_clock_format_is_the_owners_rule():
    assert now1.clock(42) == "42 s"
    assert now1.clock(59.9) == "59 s"
    assert now1.clock(60) == "1:00"
    assert now1.clock(420) == "7:00"
    assert now1.clock(3725) == "1:02:05"
    assert now1.clock(36000) == "10:00:00"
    assert now1.minutes(40) == "40 s" and now1.minutes(89) == "89 s" and now1.minutes(1080) == "18 min"


def test_typical_seconds_needs_three_outcomes_per_mode_harness_and_tier():
    runs = [_run("T-1", "work", "done", 10), _run("T-2", "work", "done", 20), _run("T-3", "work", "failed", 30),
            _run("T-4", "work", "cancelled", 400),          # says nothing about how long the work takes
            _run("T-5", "review", "done", 5), _run("T-6", "review", "done", 6),  # only two: no typical
            _run("T-7", "rebase", "done", 0.2, harness=""), _run("T-8", "rebase", "done", 0.3, harness=""),
            _run("T-9", "rebase", "done", 0.4, harness=""),  # a mechanical rebase's median is its own
            _run("T-10", "rebase", "done", 9, harness="claude"), _run("T-11", "rebase", "done", 10, harness="claude"),
            _run("T-12", "rebase", "done", 11, harness="claude")]
    typical = now1.typical_seconds(runs, NOW)
    assert typical[now1.typical_key("work", "claude", "easy")] == 20 * 60
    assert now1.typical_key("review", "claude", "easy") not in typical
    assert typical[now1.typical_key("rebase", "", "easy")] == 18
    assert typical[now1.typical_key("rebase", "claude", "easy")] == 600
    # a run from an older tier map falls back to the mode-and-harness figure over all time
    old = [_run("T-1", "work", "done", 10, difficulty="hard", days_ago=10), _run("T-2", "work", "done", 20, difficulty="hard", days_ago=10),
           _run("T-3", "work", "done", 30, difficulty="hard", days_ago=10)]
    typical = now1.typical_seconds(old, NOW)
    assert now1.typical_key("work", "claude", "hard") not in typical
    assert typical[now1.typical_key("work", "claude")] == 20 * 60
    assert now1.typical_for(_run("T-x", "work", "running", 1, difficulty="medium"), typical) == 20 * 60


def test_resolve_window():
    assert now1.resolve_window("hour", NOW) == ("2026-09-06T01:00:00+00:00", "hour", "hour")
    assert now1.resolve_window("today", NOW) == ("2026-09-06T00:00:00+00:00", "hour", "today")
    assert now1.resolve_window("24h", NOW) == ("2026-09-05T02:00:00+00:00", "hour", "24h")
    assert now1.resolve_window("phase", NOW, "2026-09-01T00:00:00+00:00") == ("2026-09-01T00:00:00+00:00", "day", "phase")
    # no open phase with a dispatch: the phase window falls back to the last 24 hours
    assert now1.resolve_window("phase", NOW) == ("2026-09-05T02:00:00+00:00", "hour", "24h")
    assert now1.resolve_window("bogus", NOW)[2] == "hour"


def test_goal_marks_read_the_numbered_goals_and_the_ids_they_name():
    text = ("# p1 goals\n\n## Goals\n\n1. **Onboarding.** `garden onboard` (CG-215) drafts a garden.\n"
            "2. Shared quotas across accounts, per CG-230 and CG-231.\n3. A goal that names no task.\n\n## Non-goals\n\n- CG-999\n")
    tasks = {"CG-215": SimpleNamespace(id="CG-215", status=now1.Status.DONE),
             "CG-230": SimpleNamespace(id="CG-230", status=now1.Status.IN_REVIEW),
             "CG-231": SimpleNamespace(id="CG-231", status=now1.Status.CANCELLED)}
    marks = now1.goal_marks(text, tasks)
    assert [g["label"] for g in marks] == ["Onboarding", "Shared quotas across accounts, per CG-230 and CG-231", "A goal that names no task"]
    assert marks[0]["mark"] == "done" and marks[0]["word"] == "merged" and marks[0]["done"] == 1
    assert marks[1]["mark"] == "running" and marks[1]["ids"] == ["CG-230"]  # the cancelled task drops out
    assert marks[2]["mark"] == "" and marks[2]["word"] == "unlinked"
    assert now1.goal_marks("no goals here", tasks) == []
    assert [now1.stage_word_for(f) for f in (0, 0.1, 0.3, 0.6, 0.9, 1.0)] == ["seed", "sprout", "in leaf", "in bud", "in flower", "in fruit"]


# ---- the difficulty-by-model tables (events.metrics) ---------------------------------

def _ev(kind, task, at, **kw):
    return {"kind": kind, "task": task, "at": at, **kw}


def test_difficulty_by_model_credits_the_model_that_got_the_task_accepted():
    tasks = {"T-1": SimpleNamespace(difficulty="easy"), "T-2": SimpleNamespace(difficulty="easy"),
             "T-3": SimpleNamespace(difficulty="medium"), "T-4": SimpleNamespace(difficulty="easy")}
    events = [
        _ev("dispatch", "T-1", "2026-09-05T10:00:00+00:00", mode="work"),
        _ev("run_finished", "T-1", "2026-09-05T10:20:00+00:00", mode="work", model="claude-sonnet-5", cost_usd=2.0),
        _ev("review", "T-1", "2026-09-05T10:30:00+00:00", verdict="approve"),
        _ev("run_finished", "T-1", "2026-09-05T10:30:00+00:00", mode="review", cost_usd=0.5),
        _ev("dispatch", "T-1", "2026-09-05T10:40:00+00:00", mode="revise"),
        _ev("run_finished", "T-1", "2026-09-05T10:50:00+00:00", mode="revise", model="claude-sonnet-5", cost_usd=1.0),
        _ev("transition", "T-1", "2026-09-05T12:00:00+00:00", to="done"),
        _ev("dispatch", "T-2", "2026-09-05T10:00:00+00:00", mode="work"),
        _ev("run_finished", "T-2", "2026-09-05T10:20:00+00:00", mode="work", model="claude-sonnet-5", cost_usd=1.0),
        _ev("review", "T-2", "2026-09-05T10:30:00+00:00", verdict="request_changes"),
        _ev("dispatch", "T-2", "2026-09-05T11:00:00+00:00", mode="revise"),
        _ev("run_finished", "T-2", "2026-09-05T11:30:00+00:00", mode="revise", model="claude-opus-4-8", cost_usd=5.0),
        _ev("transition", "T-2", "2026-09-05T14:00:00+00:00", to="done"),
        _ev("dispatch", "T-3", "2026-09-04T10:00:00+00:00", mode="work"),
        _ev("run_finished", "T-3", "2026-09-04T10:20:00+00:00", mode="work", model="claude-sonnet-5", cost_usd=9.0),
        _ev("transition", "T-3", "2026-09-04T12:00:00+00:00", to="done"),
        _ev("dispatch", "T-4", "2026-09-05T13:00:00+00:00", mode="work"),
        _ev("run_finished", "T-4", "2026-09-05T13:20:00+00:00", mode="work", model="claude-sonnet-5", cost_usd=3.0),
    ]
    out = difficulty_by_model(events, tasks, since="2026-09-05T00:00:00+00:00")
    assert out["models"] == ["claude-sonnet-5", "claude-opus-4-8"]
    assert [m["key"] for m in out["metrics"]] == ["cost_per_accepted", "first_pass", "work_run_cost", "revise_rounds", "lead_time"]
    by = {m["key"]: m["rows"] for m in out["metrics"]}
    cell = by["cost_per_accepted"]["easy"]
    assert (cell["claude-sonnet-5"]["value"], cell["claude-sonnet-5"]["n"]) == (3.5, 1)
    assert (cell["claude-opus-4-8"]["value"], cell["claude-opus-4-8"]["n"]) == (6.0, 1)
    assert by["cost_per_accepted"]["medium"] == {}
    assert by["first_pass"]["easy"]["claude-sonnet-5"]["value"] == 0.5 and by["first_pass"]["easy"]["claude-sonnet-5"]["n"] == 2
    assert by["work_run_cost"]["easy"]["claude-sonnet-5"]["value"] == 1.75 and by["work_run_cost"]["easy"]["claude-sonnet-5"]["n"] == 4
    assert by["revise_rounds"]["easy"]["claude-sonnet-5"]["value"] == 1.0
    assert by["lead_time"]["easy"]["claude-sonnet-5"]["value"] == 2.0 and by["lead_time"]["easy"]["claude-opus-4-8"]["value"] == 4.0
    # the same computation is what `garden metrics` carries
    assert metrics(events, tasks)["by_difficulty_model"]["models"] == out["models"]


def _six_easy_tasks(a_cost=1.0, b_cost=2.0):
    tasks = {f"T-{i}": SimpleNamespace(difficulty="easy") for i in range(6)}
    events = []
    for i in range(6):
        model, cost = ("a", a_cost) if i < 3 else ("b", b_cost)
        events += [_ev("dispatch", f"T-{i}", "2026-09-05T10:00:00+00:00", mode="work"),
                   _ev("run_finished", f"T-{i}", "2026-09-05T10:10:00+00:00", mode="work", model=model, cost_usd=cost),
                   _ev("transition", f"T-{i}", "2026-09-05T11:00:00+00:00", to="done")]
    return tasks, events


def test_shading_marks_best_and_worst_per_row_in_the_metrics_direction():
    tasks, events = _six_easy_tasks()
    out = difficulty_by_model(events, tasks, since="2026-09-05T00:00:00+00:00")
    cost = next(m for m in out["metrics"] if m["key"] == "cost_per_accepted")["rows"]["easy"]
    assert cost["a"]["best"] and cost["a"]["heat"] == 0.0 and not cost["a"]["thin"]      # lower cost: green
    assert cost["b"]["worst"] and cost["b"]["heat"] == 1.0 and not cost["b"]["best"]
    # a thin cell (under three samples) is marked and never best or worst
    thin = difficulty_by_model(events[3:], tasks, since="2026-09-05T00:00:00+00:00")
    cost = next(m for m in thin["metrics"] if m["key"] == "cost_per_accepted")["rows"]["easy"]
    assert cost["a"]["n"] == 2 and cost["a"]["thin"] and not cost["a"]["best"] and not cost["b"]["worst"]
    # first-pass approval runs the other way: the higher share is the green end
    events += [_ev("review", f"T-{i}", "2026-09-05T10:20:00+00:00", verdict="approve" if i >= 3 else "request_changes") for i in range(6)]
    out = difficulty_by_model(events, tasks, since="2026-09-05T00:00:00+00:00")
    first = next(m for m in out["metrics"] if m["key"] == "first_pass")["rows"]["easy"]
    assert first["b"]["best"] and first["b"]["heat"] == 0.0 and first["a"]["worst"] and first["a"]["heat"] == 1.0


# ---- the harness's progress reader ----------------------------------------------------

def test_runs_by_model_is_a_shaded_table_of_mode_by_who():
    finished = [
        *({"kind": "run_finished", "mode": "work", "harness": "claude", "model": "opus", "cost_usd": c} for c in (4.0, 5.0, 6.0)),
        *({"kind": "run_finished", "mode": "work", "harness": "codex", "model": "terra", "cost_usd": c} for c in (1.0, 2.0, 3.0)),
        {"kind": "run_finished", "mode": "review", "harness": "claude", "model": "opus", "cost_usd": 0.5},
        {"kind": "run_finished", "mode": "check", "harness": "", "model": "", "cost_usd": 0.0},
        {"kind": "run_finished", "mode": "rebase", "cost_usd": 0.0},
    ]
    t = now1.runs_by_model(finished)
    assert t["columns"] == ["claude:opus", "codex:terra", "garden"]  # by total cost, then runs, then name
    assert list(t["rows"]) == ["work", "rebase", "check", "review"]  # the loop's order: writing, mechanical, reading
    assert t["heads"] == {"claude:opus": "$15.50 · 4 runs", "codex:terra": "$6.00 · 3 runs", "garden": "$0.00 · 2 runs"}
    work = t["rows"]["work"]
    assert work["codex:terra"] == {"value": 2.0, "n": 3, "heat": 0.0, "thin": False, "best": True, "worst": False}
    assert work["claude:opus"] == {"value": 5.0, "n": 3, "heat": 1.0, "thin": False, "best": False, "worst": True}
    review = t["rows"]["review"]  # one sample: thin, and never best or worst
    assert review["claude:opus"]["thin"] and not review["claude:opus"]["best"] and "codex:terra" not in review
    assert t["label"] == "cost per run" and t["better"] == "low" and t["n_word"] == "runs"
    assert now1.runs_by_model([]) == {"label": "cost per run", "unit": "usd", "better": "low", "n_word": "runs",
                                      "columns": [], "rows": {}, "heads": {}, "thin": 3}


def test_harness_progress_reads_a_partial_stream():
    lines = [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the brief.\nmore"}], "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 20}}}),
             json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}], "usage": {"input_tokens": 50, "cache_read_input_tokens": 950, "output_tokens": 10}}}),
             json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Tests pass."}], "usage": {"input_tokens": 10, "output_tokens": 5}}})]
    p = Harness("claude", {}).progress("\n".join(lines), model="claude-sonnet-5")
    assert p["said"] == "Tests pass."
    assert p["tokens"] == 100 + 900 + 20 + 50 + 950 + 10 + 10 + 5
    assert p["cost_usd"] is None  # claude ships no price table: the page shows tokens, never an estimate
    codex = "\n".join([json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Committed."}}),
                       json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 500_000, "output_tokens": 1000}})])
    p = Harness("codex", {}).progress(codex, model="gpt-5.6-luna")
    assert p["said"] == "Committed." and p["tokens"] == 1_001_000
    assert p["cost_usd"] == (500_000 * 0.2 + 500_000 * 0.02 + 1000 * 1.2) / 1_000_000
    assert Harness("claude", {}).progress("") == {"said": "", "usage": {}, "cost_usd": None, "tokens": 0}


# ---- the page --------------------------------------------------------------------------

def test_now1_page_renders_the_four_regions_and_the_nav(garden):
    page = _client(garden).get("/now1").text
    for region in ('id="now"', 'id="next"', 'id="where"', 'id="period"'):
        assert region in page
    assert '<a href="/now1" class="on">Now 1</a>' in page and 'href="/now2"' in page
    assert 'class="page-now"' in page and re.search(r'data-server-now="\d{4}-\d\d-\d\dT', page)
    assert "The garden is quiet." not in page  # two ready tasks are queued
    assert "Nothing running." in page and "it will dispatch DM-001" in page
    assert "DM-001" in page and "priority 1 · work · medium →" in page
    assert "p1, seed" in page and "0 of 2 merged" in page
    assert "No runs finished in this window." in page
    for region in ("head", "now", "next", "where", "period"):
        assert _client(garden).get(f"/partials/now1/{region}").status_code == 200
    assert _client(garden).get("/partials/now1/nope").status_code == 404


def test_running_card_carries_the_start_time_the_clock_reads(garden):
    stdout = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Found the bug."}], "usage": {"input_tokens": 1000}}})
    run = _record_running(garden, stdout=stdout, started_minutes_ago=7)
    c = _client(garden)
    page = c.get("/now1").text
    card = re.search(r'<article class="specimen strip[^"]*"[^>]*data-task="DM-001"[^>]*>', page).group(0)
    assert f'data-started="{now1.iso_utc(run.started_at)}"' in card
    assert re.search(r'data-started="2\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00"', card)  # UTC, whole seconds, explicit offset
    assert "data-stopped" not in card
    strip = re.search(r'data-task="DM-001".*?</article>', page, re.S).group(0)
    assert re.search(r"<span data-elapsed>7:0\d</span>", strip)  # the server's first reading in the clock's format
    assert "“Found the bug.”" in strip and "1k tokens so far" in strip  # tokens, since claude has no price table
    assert "work · claude sonnet" in strip
    assert "1 run in flight" in page and "1 of 2 worker slots" in page
    # the same attributes on the Board's running card and the task page's run row, and the clock script once
    assert 'data-started="' in c.get("/board").text
    assert 'data-started="' in c.get("/tasks/DM-001").text
    assert page.count("window.gardenClock = {") == 1 and "[data-started]:not([data-stopped])" in page


def test_typical_and_longer_than_usual_on_the_strip(garden):
    rs = RunStore(Store(garden).config.garden_dir)
    for i in range(3):
        r = rs.new_run("DM-002", "local", "work")  # ids stay distinct within a second (-2, -3)
        r.harness, r.difficulty, r.status = "claude", "easy", "done"
        r.started_at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30 + i)).isoformat()
        r.finished_at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=20)).isoformat()
        r.save()
    _record_running(garden, started_minutes_ago=20)  # twice the typical ten minutes
    page = _client(garden).get("/now1").text
    strip = re.search(r'data-task="DM-001".*?</article>', page, re.S).group(0)
    assert 'data-typical="6' in strip and "typically 1" in strip
    assert "data-longer> · longer than usual" in strip  # past typical: the words show, no colour change


def test_no_process_record_and_hands_are_visible(garden):
    _record_running(garden, pid=None)  # a record written at dispatch and never launched
    store = Store(garden)
    sched = Scheduler(store, github=FakeGitHub())
    sched.state.get("DM-002")["needs_human"] = {"kind": "stall", "reason": "the loop stalled on a repeated finding"}
    sched.state.get("DM-002")["question"] = "Which fixture: Go or Node?"
    sched.state.save()
    sched.pause_harness("codex", "usage limit reached")
    sched.pause("cli", "quota on both accounts")
    t = store.task("DM-002")
    t.status = now1.Status.WAITING_HUMAN
    store.save(t)
    page = _client(garden).get("/now1").text
    assert "no process recorded" in page and "(1 without a process)" in page
    assert 'class="stamp">needs you</span>' in page and "Which fixture: Go or Node?" in page
    assert 'class="stamp">paused</span>' in page and "codex harness paused" in page and "usage limit reached" in page
    assert "Dispatch paused by cli since" in page and "quota on both accounts" in page
    assert "2 cards waiting on you" in page  # the question and the paused harness


def test_next_region_is_the_schedulers_dispatch_order_with_reasons(garden):
    store = Store(garden)
    sched = Scheduler(store, github=FakeGitHub())
    # DM-001 is ready; make it also carry a revise round so the queue has both kinds
    t = store.task("DM-001")
    t.status = now1.Status.CHANGES_REQUESTED
    t.pr = "https://example.com/pull/1"
    store.save(t)
    sched.state.get("DM-001")["pending_feedback"] = "fix the test"
    sched.state.get("DM-001")["revisions"] = 1
    sched.state.save()
    queue = sched.dispatch_queue()
    assert [(t.id, mode, why) for t, mode, why in queue] == [("DM-001", "revise", "revise round 2 of 2")]
    lines = now1.dispatch_lines(sched)
    assert lines[0]["task"] == "DM-001" and lines[0]["harness"] == "claude" and lines[0]["model"] == "sonnet" and not lines[0]["skip"]
    sched.pause_harness("claude", "quota")
    assert now1.dispatch_lines(sched)[0]["skip"] == "harness paused"
    page = _client(garden).get("/now1").text
    assert "revise round 2 of 2 · revise · medium →" in page and '<span class="skip">harness paused</span>' in page
    assert "in review" not in page.lower() or "no open PR" in page


def test_merge_queue_and_reviews_waiting_show_their_facts(garden):
    store = Store(garden)
    sched = Scheduler(store, github=FakeGitHub())
    for tid in ("DM-001", "DM-002"):
        t = store.task(tid)
        t.status = now1.Status.IN_REVIEW
        t.pr = f"https://example.com/pull/{tid[-1]}"
        store.save(t)
    st = sched.state.get("DM-001")
    for k, v in (("merge_head", True), ("automerge_candidate", True), ("checks", "PENDING")):
        st[k] = v  # item by item: the state file tracks writes through __setitem__, not dict.update
    st2 = sched.state.get("DM-002")
    for k, v in (("review_rounds", 1), ("checks", "SUCCESS"),
                 ("automerge_blocked", "the automated review verdict is request_changes, not approve"),
                 ("pending_reviews", [{"kind": "persona", "name": "security"}])):
        st2[k] = v
    sched.state.save()
    page = _client(garden).get("/now1").text
    assert "rebased, waiting for its rollup · CI pending" in page
    assert "round 1 of 2 · CI success · the automated review verdict is request_changes" in page
    assert "persona:security" in page and "no review slot (0 of 2 busy)" in page


def test_phase_sheet_grows_with_merges_and_closed_phases_are_specimens(garden):
    store = Store(garden)
    (garden / "demo" / "p1" / "goals.md").write_text("# p1\n\n## Goals\n\n1. **First.** DM-001 lands.\n2. **Second.** DM-002 after it.\n")
    (garden / "demo" / "p0").mkdir()
    (garden / "demo" / "p0" / "goals.md").write_text("---\nclosed: '2026-09-01'\n---\n# p0\n")
    t = store.task("DM-001")
    t.status = now1.Status.DONE
    store.save(t)
    page = _client(garden).get("/now1").text
    assert "1 of 2 merged" in page and 'style="--grown:50.0%"' in page and "p1, in bud" in page
    assert "1 of 1 · merged" in page and "0 of 1 · not started" in page
    assert 'class="stamp ink">pressed</span>' in page and "/phases/demo/p0" in page


def test_last_period_reads_the_windows_events(garden):
    store = Store(garden)
    log = EventLog(store.config.garden_dir / "events.jsonl")
    at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).isoformat()
    for i, (task, model, cost) in enumerate((("DM-001", "sonnet", 1.0), ("DM-002", "opus", 3.0))):
        log.emit("dispatch", task, mode="work", model=model, harness="claude")
        for ev in (("run_finished", {"mode": "work", "model": model, "harness": "claude", "cost_usd": cost, "status": "done"}),
                   ("review", {"verdict": "approve" if i == 0 else "request_changes"}),
                   ("transition", {"from": "in_review", "to": "done"})):
            log.emit(ev[0], task, **ev[1])
    log.emit("answer", "DM-001", question="q", answer="a")
    log.emit("profile_changed", "", **{"from": "", "to": "steady"})
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(json.dumps({**json.loads(ln), "at": at}) for ln in lines) + "\n")
    c = _client(garden)
    page = c.get("/now1?window=hour").text
    assert 'href="/now1?window=hour#period" class="on"' in page
    assert "merged · DM-001, DM-002" in page and "50 %<small>1 of 2</small>" in page
    assert "$4.00<small>2 runs" in page and "$2.00</div><div class=\"l\">per accepted task" in page
    assert "claude:opus" in page and "claude:sonnet" in page and "hand steps: 1 (answer 1)" in page
    assert "profile changed: (none) → steady" in page and 'class="annotation"' in page
    for label in ("cost per accepted task", "first-pass approval", "work-run cost", "revise rounds", "median lead time"):
        assert f"<caption>{label}" in page
    # the shading: a green ground at the row's best value, a red one at its worst; with one sample
    # each the cells are thin, so neither is marked best or worst, only faintly shaded and marked thin
    assert 'class="heat thin" style="--g:100%"' in page and 'class="heat thin" style="--g:0%"' in page
    assert "n 1 · thin" in page and "· best" not in page
    # the runs table reads the same way: a row per mode, a column per harness:model with its total
    # in the head, each cell the mean cost per run shaded within the row and marked
    assert "<caption>cost per run<span class=\"dir\">lower is better · n = runs</span>" in page
    assert '<span class="vendor">claude:</span>opus<span class="tot">$3.00 · 1 run</span>' in page
    assert re.search(r'<th>work</th>.*?class="heat thin" style="--g:100%"[^>]*>\s*<b>\$1\.00</b>', page, re.S)
    for window in ("today", "24h", "phase"):
        assert c.get(f"/now1?window={window}").status_code == 200


# ---- the stream ------------------------------------------------------------------------

def _sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln and not ln.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_stream_drives_a_dispatch_event_to_the_strip_fragment(garden):
    stdout = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Starting."}]}})
    run = _record_running(garden, stdout=stdout, started_minutes_ago=0)
    store = Store(garden)
    log = EventLog(store.config.garden_dir / "events.jsonl")
    log.emit("dispatch", "DM-001", run=run.run_id, mode="work", model="sonnet", harness="claude")
    c = _client(garden)
    body = c.get("/now1/stream?start=0&limit=1").text
    messages = _sse(body)
    assert messages[0][0] == "event" and messages[0][1]["kind"] == "dispatch" and messages[0][1]["run"] == run.run_id
    # the page fetches the strip the event names and inserts it: the fragment is one article
    frag = c.get(f"/partials/now1/strip/DM-001/{run.run_id}").text.strip()
    assert frag.startswith('<article class="specimen strip') and frag.endswith("</article>")
    assert f'data-run="{run.run_id}"' in frag and 'data-started="' in frag and "“Starting.”" in frag
    assert c.get("/partials/now1/strip/DM-001/nope").status_code == 404
    # a finished run's fragment carries its verdict and stops the clock
    run.status, run.cost_usd, run.finished_at = "done", 1.42, dt.datetime.now(dt.UTC).isoformat()
    run.save()
    frag = c.get(f"/partials/now1/strip/DM-001/{run.run_id}").text
    assert 'data-stopped="' in frag and '<span class="verdict">done · $1.42</span>' in frag


def test_stream_carries_progress_and_the_tick_and_never_takes_the_hub_lock(garden):
    stdout = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working."}], "usage": {"input_tokens": 5}}})
    run = _record_running(garden, stdout=stdout)
    app_ = create_app(Store(garden), watch=False, host="testserver")
    hub = app_.state.hub
    hub.tick_seq, hub.tick_record = 3, {"seq": 3, "at": "2026-09-06T02:00:00+00:00", "duration_s": 0.4, "summary": "quiet"}
    with hub.lock:  # a pass in flight: the stream must not wait for it
        with TestClient(app_) as c:
            r = c.get("/now1/stream?limit=2&seconds=0.5")
    assert r.headers["content-type"].startswith("text/event-stream")
    kinds = {k: d for k, d in _sse(r.text)}
    assert kinds["progress"]["run"] == run.run_id and kinds["progress"]["said"] == "Working." and kinds["progress"]["tokens"] == 5
    assert "tick" not in kinds  # the seq did not move while the stream was open
    # the tail picks up only what lands after it opened, and a tick message when the seq moves
    calls = []
    seq = {"n": 0}

    def tick_state():
        seq["n"] += 1
        return {"seq": seq["n"]}

    def sleep(_s):
        calls.append(1)
        EventLog(Store(garden).config.garden_dir / "events.jsonl").emit("transition", "DM-001", to="done")

    msgs = list(now1.stream(Store(garden), tick_state, limit=3, progress_every=10_000, sleep=sleep))
    parsed = _sse("".join(msgs))
    assert parsed[0][0] == "tick" and parsed[1][0] == "event" and parsed[1][1]["kind"] == "transition"
    assert len(calls) >= 1
    # the page itself never polls: no data-poll hook and no setInterval fetch of a region
    page = c.get("/now1").text
    assert 'data-poll="' not in page and 'new EventSource("/now1/stream")' in page


def test_tail_lines_leaves_a_partial_line_for_the_next_read(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text('{"kind": "a"}\n{"kind": "b"')
    lines, offset = now1.tail_lines(p, 0)
    assert [ln["kind"] for ln in lines] == ["a"] and offset == len('{"kind": "a"}\n')
    p.write_text('{"kind": "a"}\n{"kind": "b"}\n')
    lines, offset = now1.tail_lines(p, offset)
    assert [ln["kind"] for ln in lines] == ["b"] and offset == p.stat().st_size
    assert now1.tail_lines(tmp_path / "missing.jsonl", 0) == ([], 0)


# ---- the text view, the walkthrough ----------------------------------------------------

def test_garden_now_prints_the_four_regions(garden):
    _record_running(garden)
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        r = CliRunner().invoke(app, ["now", "--page", "1"])
    finally:
        os.chdir(cwd)
    assert r.exit_code == 0, r.output
    for head in ("NOW  1 run in flight on 1 of 2 worker slots", "NEXT ", "WHERE  demo/p1", "THE LAST PERIOD  last hour"):
        assert head in r.output
    assert "DM-001   work     claude sonnet" in r.output
    assert "1. DM-001" in r.output and "priority 1 · work · medium → claude sonnet" in r.output  # DM-002 waits on it
    assert "No runs finished in this window." in r.output


def test_garden_now_page_2_says_so_when_now_2_is_not_built(garden):
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        r = CliRunner().invoke(app, ["now", "--page", "2"])
    finally:
        os.chdir(cwd)
    assert r.exit_code == 1 and "Now 2" in r.output


def test_walkthrough_captures_now1(garden):
    from garden.walkthrough import capture, pages_for

    store = Store(garden)
    ph = store.phase("demo", "p1")
    assert pages_for(store, ph)[0].url == "/now1"
    out = Path(garden) / "cap"
    result = capture(store, ph, out, screenshots=False)
    now_page = next(pr for pr in result.pages if pr.spec.slug == "now1")
    assert now_page.status == 200 and (out / "now1.html").exists()
    assert "every run in flight" in (out / "now1.txt").read_text()
