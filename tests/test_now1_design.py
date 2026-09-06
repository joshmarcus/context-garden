"""The Now 1 design's mock (docs/design/now_1_mock.py): the contracts the build inherits.

The mock is a design tool, not part of the package, so it is loaded from its file. What is
held here is what the design fixes for the build: the data attribute the live clock reads on
a running card, the clock's format, and the difficulty-by-model tables' computation.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _ev(kind, task, at, **kw):
    return {"kind": kind, "task": task, "at": at, **kw}


def test_difficulty_by_model_tables(mock):
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
    out = mock.difficulty_by_model(events, tasks, since="2026-09-05T00:00:00+00:00")
    assert out["models"] == ["claude-sonnet-5", "claude-opus-4-8"]
    by = {m["key"]: m["rows"] for m in out["metrics"]}
    assert [m["key"] for m in out["metrics"]] == ["cost_per_accepted", "first_pass", "work_run_cost", "revise_rounds", "lead_time"]
    # T-1's whole cost (work, review, revise) to sonnet; T-2's whole cost to opus, the model that got it accepted
    assert by["cost_per_accepted"]["easy"]["claude-sonnet-5"] == {"value": 3.5, "n": 1, "best": False}
    assert by["cost_per_accepted"]["easy"]["claude-opus-4-8"] == {"value": 6.0, "n": 1, "best": False}
    assert by["cost_per_accepted"]["medium"] == {}  # T-3 was accepted before the window
    # first pass: both first reviews happened on sonnet's work; one approve of two
    assert by["first_pass"]["easy"] == {"claude-sonnet-5": {"value": 0.5, "n": 2, "best": False}}
    # work runs in the window, per run, by the run's own model: sonnet 2.0, 1.0, 1.0, 3.0; opus 5.0
    assert by["work_run_cost"]["easy"]["claude-sonnet-5"] == {"value": 1.75, "n": 4, "best": False}
    assert by["work_run_cost"]["easy"]["claude-opus-4-8"] == {"value": 5.0, "n": 1, "best": False}
    assert by["revise_rounds"]["easy"]["claude-sonnet-5"]["value"] == 1.0
    assert by["lead_time"]["easy"]["claude-sonnet-5"] == {"value": 2.0, "n": 1, "best": False}
    assert by["lead_time"]["easy"]["claude-opus-4-8"]["value"] == 4.0


def test_best_cell_needs_three_samples(mock):
    tasks = {f"T-{i}": SimpleNamespace(difficulty="easy") for i in range(6)}
    events = []
    for i in range(6):
        model, cost = ("a", 1.0) if i < 3 else ("b", 2.0)
        events += [_ev("dispatch", f"T-{i}", "2026-09-05T10:00:00+00:00", mode="work"),
                   _ev("run_finished", f"T-{i}", "2026-09-05T10:10:00+00:00", mode="work", model=model, cost_usd=cost),
                   _ev("transition", f"T-{i}", "2026-09-05T11:00:00+00:00", to="done")]
    out = mock.difficulty_by_model(events, tasks, since="2026-09-05T00:00:00+00:00")
    cost = next(m for m in out["metrics"] if m["key"] == "cost_per_accepted")["rows"]["easy"]
    assert cost["a"]["best"] and not cost["b"]["best"]
    # drop one of a's tasks below the threshold: nothing is marked best
    thin = mock.difficulty_by_model(events[3:], tasks, since="2026-09-05T00:00:00+00:00")
    cost = next(m for m in thin["metrics"] if m["key"] == "cost_per_accepted")["rows"]["easy"]
    assert cost["a"]["n"] == 2 and not cost["a"]["best"] and not cost["b"]["best"]


def test_mock_renders_the_five_tables_per_window(mock, snapshot):
    html = mock.render(snapshot)
    for label in ("cost per accepted task", "first-pass approval", "work-run cost", "revise rounds", "median lead time"):
        assert f"<caption>{label}</caption>" in html
    assert html.count('<table class="tier">') == 5 * len(snapshot["period"])
    assert re.search(r"<small>n \d+</small>", html)
