"""The Now 1 design's mock (docs/design/now_1_mock.py): the contracts the build inherits.

The mock is a design tool, not part of the package, so it is loaded from its file. What is
held here is what the design fixes for the build: the data attribute the live clock reads on
a running card, the clock's format, the difficulty-by-model tables (computed once, in
`garden.events`, for `garden metrics` and the page alike) and how a table row is ranked and
marked, and the reason a queued review gives for waiting.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from garden.cli.loop import _tier_cell
from garden.events import difficulty_by_model, metrics

DESIGN = Path(__file__).resolve().parent.parent / "docs" / "design"


@pytest.fixture(scope="module")
def mock():
    spec = importlib.util.spec_from_file_location("now_1_mock", DESIGN / "now_1_mock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def snapshot():
    return json.loads((DESIGN / "now-1-snapshot.json").read_text())


def _strip(**over):
    base = {"kind": "run", "state": "running", "task": "CG-236", "title": "A trial winner's PR enters the review queue",
            "run": "20260906T011000Z-work", "mode": "work", "stage": "", "harness": "claude", "model": "claude-sonnet-5",
            "difficulty": "easy", "started_at": "2026-09-06T01:10:00+00:00", "elapsed_s": 420, "typical_s": 1080,
            "said": "Reading the brief.", "spend_usd": 0.41, "tokens_so_far": 0, "no_process": False,
            "glyph": "running", "dot": "running"}
    return {**base, **over}


# ---- the live clock -------------------------------------------------------------------

def test_running_card_carries_the_start_time_the_clock_reads(mock, snapshot):
    """A running card names its start time and its typical duration as data attributes, the
    page names the server's clock, and the server-rendered digits are the clock's own format
    (the owner's rule: seconds, then m:ss, then h:mm:ss)."""
    snap = {**snapshot, "captured_at": "2026-09-06T01:17:00+00:00", "now": [_strip()]}
    html = mock.render(snap)
    card = re.search(r'<article class="specimen strip[^"]*"[^>]*data-task="CG-236"[^>]*>', html).group(0)
    assert 'data-started="2026-09-06T01:10:00+00:00"' in card
    assert 'data-typical="1080"' in card
    assert "data-stopped" not in card
    assert '<body class="page-now" data-server-now="2026-09-06T01:17:00+00:00">' in html
    assert "<span data-elapsed>7:00</span>" in html
    assert 'data-longer hidden> · longer than usual' in html  # under typical: the words wait


def test_past_typical_says_so_and_a_finished_card_stops(mock, snapshot):
    snap = {**snapshot, "now": [_strip(elapsed_s=1860), _strip(task="CG-250", elapsed_s=1140, verdict="done · $1.42", state="finishing")]}
    html = mock.render(snap)
    over = re.search(r'data-task="CG-236".*?</article>', html, re.S).group(0)
    assert "data-longer> · longer than usual" in over
    assert "<span data-elapsed>31:00</span>" in over
    done = re.search(r'data-task="CG-250".*?</article>', html, re.S).group(0)
    assert 'data-stopped="1140"' in done  # the browser clock leaves it alone


def test_clock_format(mock):
    assert mock.clock(42) == "42 s"
    assert mock.clock(59.9) == "59 s"
    assert mock.clock(60) == "1:00"
    assert mock.clock(420) == "7:00"
    assert mock.clock(3725) == "1:02:05"
    assert mock.clock(36000) == "10:00:00"


# ---- the strips and the sentence ------------------------------------------------------

def test_running_card_links_to_its_run_and_the_sentence_names_titles(mock, snapshot):
    """A card's title is the link to the task and the card links to its exact run, so two runs
    of one task are told apart; the five-second sentence says what comes next by title, not by
    id (the designer persona's finding)."""
    queue = [{"pos": 1, "task": "CG-215", "title": "Onboarding: garden onboard drafts a garden from an existing repository, with a planner pass",
              "mode": "work", "why": "priority 1", "difficulty": "hard", "harness": "claude", "model": "claude-opus-5", "skip": "", "status": "ready", "product": "context-garden"},
             {"pos": 2, "task": "CG-216", "title": "Workers on independent remote hosts", "mode": "work", "why": "priority 1",
              "difficulty": "hard", "harness": "codex", "model": "gpt-5.6-sol", "skip": "", "status": "ready", "product": "context-garden"}]
    snap = {**snapshot, "now": [_strip()], "next": {**snapshot["next"], "dispatch": queue}}
    html = mock.render(snap)
    card = re.search(r'data-task="CG-236".*?</article>', html, re.S).group(0)
    assert '<a class="t" href="/tasks/CG-236">A trial winner&#39;s PR enters the review queue</a>' in card
    assert '<a class="open-run" href="/runs/CG-236/20260906T011000Z-work">open run</a>' in card
    sentence = re.search(r'<p class="now-five">.*?</p>', html, re.S).group(0)
    assert "Next</a>: Onboarding: garden onboard drafts a garden from an…, then Workers on independent remote hosts." in sentence
    assert "CG-215" not in sentence
    # in the Next list the title is the link and the id is secondary, in the why line
    assert '<a class="lt" href="/tasks/CG-215">Onboarding: garden onboard' in html
    assert '<span class="why"><span class="id">CG-215</span> · priority 1 · work · hard → claude claude-opus-5</span>' in html


def _gates(**over):
    base = {"dispatch_paused": False, "paused_harnesses": set(), "review_harness": "claude", "review_busy": 1,
            "review_slots": 3, "last_tick": "2026-09-06T03:00:00+00:00", "last_moved": {"T-1": "2026-09-06T03:05:00+00:00"}}
    return {**base, **over}


def test_review_wait_reason_follows_the_scheduler_gates(mock):
    """The reason a queued review waits comes from the gates the tick applies, in the tick's
    order; a free slot never reads as a full one (the usability and designer personas' finding)."""
    reason = mock.review_wait_reason
    assert reason("T-1", [], _gates(dispatch_paused=True)) == ("paused", "dispatch paused: reviews start again with dispatch")
    worker = {"task": "T-1", "mode": "revise", "no_process": False}
    assert reason("T-1", [worker], _gates()) == ("worker", "waits for its revise run to finish")
    ghost = {"task": "T-1", "mode": "revise", "no_process": True}
    assert reason("T-1", [ghost], _gates()) == ("worker", "its revise record has no process; the tick that reaps it starts the review")
    assert reason("T-1", [{"task": "T-2", "mode": "work", "no_process": False}], _gates(paused_harnesses={"claude"})) == ("harness", "claude harness paused")
    assert reason("T-1", [], _gates(review_busy=3)) == ("slots", "no review slot (3 of 3 busy)")
    assert reason("T-1", [], _gates(review_busy=0)) == ("tick", "queued: the next tick starts it")
    # a tick has passed since the task last moved and nothing holds it: the page says so and
    # sends the person to the log rather than promising a recovery
    assert reason("T-1", [], _gates(last_tick="2026-09-06T03:06:00+00:00")) == ("overdue", "still queued after a tick and no gate explains it: see the task's log")


def test_waiting_review_renders_its_gate(mock, snapshot):
    waiting = [{"task": "CG-242", "title": "Hold untrusted config changes", "what": "review", "gate": "overdue",
                "why": "still queued after a tick and no gate explains it: see the task's log"}]
    snap = {**snapshot, "next": {**snapshot["next"], "merge": {**snapshot["next"]["merge"], "waiting": waiting}}}
    html = mock.render(snap)
    assert 'class="fact held">still queued after a tick and no gate explains it: see the task&#39;s log · <a href="/tasks/CG-242#log">open the log</a>' in html


# ---- the difficulty-by-model tables ----------------------------------------------------

def _ev(kind, task, at, **kw):
    return {"kind": kind, "task": task, "at": at, **kw}


def _cell(value, n, **over):
    return {"value": value, "n": n, "thin": n < 3, "rank": None, "best": False, "worst": False, **over}


def test_difficulty_by_model_tables():
    tasks = {"T-1": SimpleNamespace(difficulty="easy"), "T-2": SimpleNamespace(difficulty="easy"),
             "T-3": SimpleNamespace(difficulty="medium"), "T-4": SimpleNamespace(difficulty="easy")}
    events = [
        # T-1: sonnet, one revise round, approved first pass, accepted in the window
        _ev("dispatch", "T-1", "2026-09-05T10:00:00+00:00", mode="work"),
        _ev("run_finished", "T-1", "2026-09-05T10:20:00+00:00", mode="work", model="claude-sonnet-5", cost_usd=2.0),
        _ev("review", "T-1", "2026-09-05T10:30:00+00:00", verdict="approve"),
        _ev("run_finished", "T-1", "2026-09-05T10:30:00+00:00", mode="review", cost_usd=0.5),
        _ev("dispatch", "T-1", "2026-09-05T10:40:00+00:00", mode="revise"),
        _ev("run_finished", "T-1", "2026-09-05T10:50:00+00:00", mode="revise", model="claude-sonnet-5", cost_usd=1.0),
        _ev("transition", "T-1", "2026-09-05T12:00:00+00:00", to="done"),
        # T-2: started on sonnet, escalated to opus, which got it accepted; first review was request_changes
        _ev("dispatch", "T-2", "2026-09-05T10:00:00+00:00", mode="work"),
        _ev("run_finished", "T-2", "2026-09-05T10:20:00+00:00", mode="work", model="claude-sonnet-5", cost_usd=1.0),
        _ev("review", "T-2", "2026-09-05T10:30:00+00:00", verdict="request_changes"),
        _ev("dispatch", "T-2", "2026-09-05T11:00:00+00:00", mode="revise"),
        _ev("run_finished", "T-2", "2026-09-05T11:30:00+00:00", mode="revise", model="claude-opus-4-8", cost_usd=5.0),
        _ev("transition", "T-2", "2026-09-05T14:00:00+00:00", to="done"),
        # T-3: accepted before the window; only its work run counts nowhere
        _ev("dispatch", "T-3", "2026-09-04T10:00:00+00:00", mode="work"),
        _ev("run_finished", "T-3", "2026-09-04T10:20:00+00:00", mode="work", model="claude-sonnet-5", cost_usd=9.0),
        _ev("transition", "T-3", "2026-09-04T12:00:00+00:00", to="done"),
        # T-4: a work run in the window, not yet reviewed or accepted
        _ev("dispatch", "T-4", "2026-09-05T13:00:00+00:00", mode="work"),
        _ev("run_finished", "T-4", "2026-09-05T13:20:00+00:00", mode="work", model="claude-sonnet-5", cost_usd=3.0),
    ]
    out = difficulty_by_model(events, tasks, since="2026-09-05T00:00:00+00:00")
    assert out["models"] == ["claude-sonnet-5", "claude-opus-4-8"]
    by = {m["key"]: m["rows"] for m in out["metrics"]}
    assert [m["key"] for m in out["metrics"]] == ["cost_per_accepted", "first_pass", "work_run_cost", "revise_rounds", "lead_time"]
    assert [m["n_unit"] for m in out["metrics"]] == ["accepted tasks", "tasks first reviewed", "work runs", "accepted tasks", "accepted tasks"]
    # T-1's whole cost (work, review, revise) to sonnet; T-2's whole cost to opus, the model that got it accepted
    assert by["cost_per_accepted"]["easy"]["claude-sonnet-5"] == _cell(3.5, 1)
    assert by["cost_per_accepted"]["easy"]["claude-opus-4-8"] == _cell(6.0, 1)
    assert by["cost_per_accepted"]["medium"] == {}  # T-3 was accepted before the window
    # first pass: both first reviews happened on sonnet's work; one approve of two
    assert by["first_pass"]["easy"] == {"claude-sonnet-5": _cell(0.5, 2)}
    # work runs in the window, per run, by the run's own model: sonnet 2.0, 1.0, 1.0, 3.0; opus 5.0
    assert by["work_run_cost"]["easy"]["claude-sonnet-5"] == _cell(1.75, 4)
    assert by["work_run_cost"]["easy"]["claude-opus-4-8"] == _cell(5.0, 1)
    assert by["revise_rounds"]["easy"]["claude-sonnet-5"]["value"] == 1.0
    assert by["lead_time"]["easy"]["claude-sonnet-5"] == _cell(2.0, 1)
    assert by["lead_time"]["easy"]["claude-opus-4-8"]["value"] == 4.0
    # the whole window is the default, and `garden metrics` carries the same tables
    assert difficulty_by_model(events, tasks) == difficulty_by_model(events, tasks, since="")
    assert metrics(events, tasks)["by_difficulty_model"] == difficulty_by_model(events, tasks)


def _accepted(task, model, cost, at="2026-09-05T11:00:00+00:00"):
    return [_ev("dispatch", task, "2026-09-05T10:00:00+00:00", mode="work"),
            _ev("run_finished", task, "2026-09-05T10:10:00+00:00", mode="work", model=model, cost_usd=cost),
            _ev("transition", task, at, to="done")]


def test_rows_are_ranked_from_best_to_worst_and_marked():
    """The owner's heat-map rule (2026-09-06 02:45Z): within a row, rank 0 at the best value and
    1 at the worst, direction per metric; best and worst carry a mark; a cell under three
    samples sits on the scale faintly but is never an end."""
    tasks = {f"T-{i}": SimpleNamespace(difficulty="easy") for i in range(11)}
    events = []
    for i in range(3):
        events += _accepted(f"T-{i}", "a", 1.0)      # a: mean 1.0, n 3
    for i in range(3, 6):
        events += _accepted(f"T-{i}", "b", 3.0)      # b: mean 3.0, n 3
    for i in range(6, 9):
        events += _accepted(f"T-{i}", "c", 5.0)      # c: mean 5.0, n 3
    events += _accepted("T-9", "d", 0.5) + _accepted("T-10", "d", 0.5)  # d: 0.5 but n 2: thin, never best
    out = difficulty_by_model(events, tasks)
    cost = next(m for m in out["metrics"] if m["key"] == "cost_per_accepted")["rows"]["easy"]
    assert cost["a"] == _cell(1.0, 3, rank=0.0, best=True)
    assert cost["b"] == _cell(3.0, 3, rank=0.5)
    assert cost["c"] == _cell(5.0, 3, rank=1.0, worst=True)
    assert cost["d"] == _cell(0.5, 2, rank=0.0)  # clamped onto the scale, faint, no mark
    # higher is better for first-pass approval: the direction flips
    reviewed = [_ev("review", f"T-{i}", "2026-09-05T10:20:00+00:00", verdict="approve" if i < 3 else "request_changes") for i in range(6)]
    rows = difficulty_by_model(events + reviewed, tasks)
    first = next(m for m in rows["metrics"] if m["key"] == "first_pass")["rows"]["easy"]
    assert first["a"] == _cell(1.0, 3, rank=0.0, best=True)
    assert first["b"] == _cell(0.0, 3, rank=1.0, worst=True)


def test_a_comparison_needs_two_solid_cells():
    tasks = {f"T-{i}": SimpleNamespace(difficulty="easy") for i in range(5)}
    events = []
    for i in range(3):
        events += _accepted(f"T-{i}", "a", 1.0)
    events += _accepted("T-3", "b", 2.0) + _accepted("T-4", "b", 2.0)
    cost = next(m for m in difficulty_by_model(events, tasks)["metrics"] if m["key"] == "cost_per_accepted")["rows"]["easy"]
    assert cost["a"] == _cell(1.0, 3) and cost["b"] == _cell(2.0, 2)  # one solid cell: no scale, no marks


def test_mock_renders_the_five_tables_with_the_scale_and_marks(mock, snapshot):
    html = mock.render(snapshot)
    for label in ("cost per accepted task", "first-pass approval", "work-run cost", "revise rounds", "median lead time"):
        assert f"<caption>{label} <small>" in html
    assert "n = tasks first reviewed</small>" in html and "higher is better" in html
    assert html.count('<table class="tier">') == 5 * len(snapshot["period"])
    # the row scale and the marks are decided in the data; the template only styles them
    assert re.search(r'<td class="scaled best  " style="--k:0.0"><b><span class="mark best" title="best of the row">▲</span>', html)
    assert re.search(r'<td class="scaled  worst " style="--k:1.0"><b><span class="mark worst" title="worst of the row">▽</span>', html)
    assert re.search(r'<small><span class="mark thin" title="fewer than 3 samples">~</span>n [12]</small>', html)


def test_metrics_cli_cell_reads_like_the_page():
    assert _tier_cell(None, "usd") == "—"
    assert _tier_cell(_cell(4.62, 50, rank=0.0, best=True), "usd") == "▲ $4.62 (n 50)"
    assert _tier_cell(_cell(0.14, 7, rank=1.0, worst=True), "pct") == "▽ 14% (n 7)"
    assert _tier_cell(_cell(0.914, 1, rank=0.1), "hours") == "0.9 h (~n 1)"
    assert _tier_cell(_cell(1.4, 15), "rounds") == "1.4 (n 15)"
