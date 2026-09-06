from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from garden.events import difficulty_by_model, metrics
from garden.now2 import period_data, typical_durations, window_bounds
from garden.outcomes import rank_row
from garden.runs import RunStore
from garden.store import Store

NOW = dt.datetime(2026, 9, 6, 12, tzinfo=dt.UTC)


def event(kind, at, task="D-1", **kw):
    return {"kind": kind, "at": f"2026-09-06T{at}+00:00", "task": task, **kw}


def history():
    return [event("dispatch", "09:00:00", mode="work", model="alpha", run="w"),
            event("run_finished", "10:00:00", mode="work", model="alpha", run="w", cost_usd=2),
            event("review", "10:20:00", verdict="request_changes"),
            event("dispatch", "10:30:00", mode="revise", model="beta", run="r"),
            event("run_finished", "11:15:00", mode="revise", model="beta", run="r", cost_usd=3),
            event("run_finished", "11:30:00", mode="review", model="judge", run="j", cost_usd=1),
            event("transition", "11:45:00", to="done", note="PR merged by the garden")]


def test_windowed_matrices_keep_lifetime_cost_and_acceptance_provenance():
    tasks = {"D-1": SimpleNamespace(difficulty="easy", status="done", key="demo/p1")}
    events = history() + [event("transition", "11:50:00", to="done", note="PR merged by the garden")]
    data = difficulty_by_model(events, tasks, "2026-09-06T11:00:00+00:00", NOW.isoformat())
    assert data["accepted_count"] == 1
    assert data["models"] == ["alpha", "beta"]
    for model in data["models"]:
        assert data["metrics"]["total_cost"]["rows"]["easy"][model]["value"] == 6
        assert data["metrics"]["first_pass"]["rows"]["easy"][model]["value"] == 0
        assert data["metrics"]["revise_rounds"]["rows"]["easy"][model]["value"] == 1
        assert data["metrics"]["lead_time"]["rows"]["easy"][model]["value"] == 9900
        assert data["metrics"]["work_cost"]["rows"]["easy"][model]["n"] == 0
    assert metrics(events, tasks, "2026-09-06T11:00:00+00:00", NOW.isoformat())["difficulty_by_model"] == data
    events[-2]["note"] = "marked done"
    events[-1]["note"] = "marked done"
    assert difficulty_by_model(events, tasks)["accepted_count"] == 0


def test_missing_prices_and_reviews_are_not_zero():
    tasks = {"D-1": SimpleNamespace(difficulty="easy")}
    events = [e for e in history() if e["kind"] != "review"]
    events[1]["cost_usd"] = None
    d = difficulty_by_model(events, tasks, until=NOW.isoformat())
    assert d["total_cost"]["value"] is None and d["total_cost"]["missing"] == 1
    assert d["first_pass"]["value"] is None and d["first_pass"]["n"] == 0


def test_windows_and_typical_duration_with_fake_harness_records(garden):
    assert window_bounds("today", NOW)[0] == "2026-09-06T00:00:00+00:00"
    assert window_bounds("hour", NOW)[0] == "2026-09-06T11:00:00+00:00"
    assert window_bounds("24h", NOW)[0] == "2026-09-05T12:00:00+00:00"
    assert window_bounds("phase", NOW, "2026-09-01T00:00:00Z")[0] == "2026-09-01T00:00:00+00:00"
    runs = RunStore(Store(garden).config.garden_dir)
    rows = []
    for i, seconds in enumerate((60, 180, 300)):
        r = runs.new_run("D-1", "local", run_id=f"fake-{i}")
        r.harness, r.mode, r.difficulty, r.status = "fake", "work", "easy", "done"
        r.started_at = (NOW-dt.timedelta(seconds=seconds+10)).isoformat()
        r.finished_at = (NOW-dt.timedelta(seconds=10)).isoformat()
        r.save()
        rows.append(r)
    assert typical_durations(rows[:2], NOW)["work", "easy"] == {"seconds": None, "n": 2}
    assert typical_durations(rows, NOW)["work", "easy"] == {"seconds": 180, "n": 3}
    rows[-1].status = "failed"
    assert typical_durations(rows, NOW)["work", "easy"]["n"] == 2


def test_rank_direction_ties_zero_and_small_samples():
    cells = {str(i): {"value": v, "n": n, "shade": 0, "direction": "lower", "rank": ""}
             for i, (v, n) in enumerate(((0, 3), (2, 1), (None, 0)))}
    rank_row(cells)
    assert cells["0"]["rank"] == "best" and cells["0"]["shade"] == 1
    assert cells["1"]["rank"] == "worst" and cells["1"]["shade"] == -.25
    assert cells["2"]["shade"] == 0
    cells["1"]["value"] = 0
    rank_row(cells)
    assert cells["0"]["rank"] == "equal" and cells["0"]["shade"] == 0
    cells["1"]["value"] = 100
    for c in cells.values():
        c["direction"] = "higher"
    rank_row(cells)
    assert cells["1"]["rank"] == "best" and cells["0"]["rank"] == "worst"


def test_period_operator_annotations_rebases_and_throughput():
    tasks = {"D-1": SimpleNamespace(difficulty="easy", status="done", key="demo/p1")}
    records = [{"at": "2026-09-06T10:00:00+00:00", "session": "s", "list_price_usd": 10},
               {"at": "2026-09-06T11:30:00+00:00", "session": "s", "list_price_usd": 12},
               {"at": "2026-09-06T11:35:00+00:00", "session": "s", "kind": "compacted"}]
    evs = history() + [event("profile_changed", "11:05:00", **{"from": "fast", "to": "efficient"}),
                       event("run_finished", "11:06:00", mode="rebase", run="b", how="mechanical", cost_usd=0)]
    p = period_data(evs, tasks, records, "2026-09-06T11:00:00+00:00", NOW.isoformat())
    assert p["operator"] == 2 and p["operator_share"] == 2/6
    assert p["rebase"] == {"mechanical": 1, "agent": 0}
    assert sum(p["buckets"]) == 1 and len(p["annotations"]) == 2
    assert p["hand_unknown"] == 0


def test_phase_window_keeps_global_operating_marks_without_other_phase_cost():
    tasks = {"D-1": SimpleNamespace(difficulty="easy", status="done", key="demo/p1"),
             "D-2": SimpleNamespace(difficulty="hard", status="done", key="demo/p2")}
    events = history() + [
        event("profile_changed", "11:05:00", task="", **{"from": "fast", "to": "efficient"}),
        event("upgraded", "11:10:00", task=""),
        event("config_override", "11:15:00", task="", phase="demo/p2"),
        event("run_finished", "11:20:00", task="D-2", mode="work", cost_usd=100),
        event("profile_changed", "12:00:00", task=""),
    ]
    p = period_data(events, tasks, [], "2026-09-06T11:00:00+00:00", NOW.isoformat(), "demo/p1")
    assert [a["kind"] for a in p["annotations"]] == ["profile_changed", "upgraded"]
    assert p["series"]["grand_total"]["cost_usd"] == 4
    assert p["matrices"]["accepted_count"] == 1
    assert all("position" not in e for e in events)

