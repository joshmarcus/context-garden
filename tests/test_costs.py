"""cost_series (the aggregation behind both `garden costs` and /costs) and their parity."""

from __future__ import annotations

import json

import pytest

from garden.charts import cost_stack_svg
from garden.costs import cost_series
from garden.model import Status, Task


def _tasks() -> dict[str, Task]:
    return {
        "DM-001": Task(path=None, id="DM-001", title="A", status=Status.DONE, product="demo", phase="p1", difficulty="easy"),
        "DM-002": Task(path=None, id="DM-002", title="B", status=Status.DONE, product="demo", phase="p2", difficulty="hard"),
    }


def _events() -> list[dict]:
    return [
        {"at": "2026-09-04T10:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "sonnet", "harness": "claude", "cost_usd": 1.0,
         "usage": {"cache_read_input_tokens": 100, "cache_creation_input_tokens": 10}},
        {"at": "2026-09-04T11:00:00+00:00", "kind": "run_finished", "task": "DM-002", "mode": "revise",
         "model": "opus", "harness": "claude", "cost_usd": 2.0,
         "usage": {"cache_read_input_tokens": 50, "cache_creation_input_tokens": 5}},
        {"at": "2026-09-05T09:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "sonnet", "harness": "codex", "cost_usd": 0.5, "usage": {}},
        {"at": "2026-09-05T09:30:00+00:00", "kind": "run_finished", "task": "DM-002", "mode": "check",
         "model": "opus", "harness": "codex", "cost_usd": 0.25, "usage": {}},
        # an unlisted mode folds into "other" rather than growing the activity vocabulary
        {"at": "2026-09-05T09:32:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "trial",
         "model": "sonnet", "harness": "claude", "cost_usd": 0.1, "usage": {}},
        # a run against a synthetic probe task (a retro run, never a real task file): counts
        # for activity/model/harness, reads as "unknown" for difficulty/phase/task, and a
        # difficulty/phase/task filter naturally excludes it
        {"at": "2026-09-05T09:40:00+00:00", "kind": "run_finished", "task": "_retro-demo-p1", "mode": "retro",
         "model": "opus", "harness": "claude", "cost_usd": 0.3, "usage": {}},
        # not a run_finished event: must never be counted
        {"at": "2026-09-05T09:35:00+00:00", "kind": "dispatch", "task": "DM-001", "mode": "work"},
    ]


def test_group_by_activity_orders_by_cost_and_folds_unknown_modes():
    series = cost_series(_events(), _tasks(), group_by="activity", bucket="day")
    assert series["groups"] == ["revise", "work", "retro", "check", "other"]
    assert series["totals"]["work"]["cost_usd"] == 1.5  # 1.0 + 0.5
    assert series["totals"]["work"]["runs"] == 2
    assert series["totals"]["other"]["cost_usd"] == 0.1  # the "trial" mode run
    assert series["totals"]["retro"]["cost_usd"] == 0.3
    assert series["grand_total"]["cost_usd"] == 4.15  # 1.0+2.0+0.5+0.25+0.1+0.3
    assert series["grand_total"]["runs"] == 6
    # mean and share are computed on the totals, not the grand total
    assert series["totals"]["work"]["mean_cost_usd"] == 0.75
    assert series["totals"]["revise"]["share"] == round(2.0 / 4.15, 4)


def test_group_by_buckets_by_day():
    series = cost_series(_events(), _tasks(), group_by="activity", bucket="day")
    by_bucket = {b["bucket"]: b["groups"] for b in series["buckets"]}
    assert set(by_bucket) == {"2026-09-04", "2026-09-05"}
    assert by_bucket["2026-09-04"]["work"]["cost_usd"] == 1.0
    assert by_bucket["2026-09-04"]["revise"]["cost_usd"] == 2.0
    assert by_bucket["2026-09-05"]["work"]["cost_usd"] == 0.5


def test_group_by_difficulty_and_cache_usage():
    series = cost_series(_events(), _tasks(), group_by="difficulty", bucket="day")
    assert series["totals"]["easy"]["cost_usd"] == 1.6  # DM-001: 1.0 + 0.5 + 0.1
    assert series["totals"]["hard"]["cost_usd"] == 2.25  # DM-002: 2.0 + 0.25
    assert series["totals"]["unknown"]["cost_usd"] == 0.3  # the retro run, no real task
    assert series["totals"]["easy"]["cache_read_tokens"] == 100
    assert series["totals"]["easy"]["cache_write_tokens"] == 10


def test_group_by_model_and_harness():
    series = cost_series(_events(), _tasks(), group_by="model", bucket="day")
    assert series["totals"]["sonnet"]["cost_usd"] == 1.6
    assert series["totals"]["opus"]["cost_usd"] == 2.55
    series = cost_series(_events(), _tasks(), group_by="harness", bucket="day")
    assert series["totals"]["claude"]["cost_usd"] == 3.4  # 1.0 + 2.0 + 0.1 + 0.3
    assert series["totals"]["codex"]["cost_usd"] == 0.75


def test_group_by_phase_and_task():
    series = cost_series(_events(), _tasks(), group_by="phase", bucket="day")
    assert series["totals"]["demo/p1"]["cost_usd"] == 1.6
    assert series["totals"]["demo/p2"]["cost_usd"] == 2.25
    assert series["totals"]["unknown"]["cost_usd"] == 0.3
    series = cost_series(_events(), _tasks(), group_by="task", bucket="day")
    assert series["totals"]["DM-001"]["cost_usd"] == 1.6
    assert series["totals"]["DM-002"]["cost_usd"] == 2.25


def test_filters_by_difficulty_model_harness_phase_task_and_product():
    tasks = _tasks()
    assert cost_series(_events(), tasks, difficulty="easy")["grand_total"]["cost_usd"] == 1.6
    assert cost_series(_events(), tasks, model="opus")["grand_total"]["cost_usd"] == 2.55
    assert cost_series(_events(), tasks, harness="codex")["grand_total"]["cost_usd"] == 0.75
    assert cost_series(_events(), tasks, phase="demo/p2")["grand_total"]["cost_usd"] == 2.25
    assert cost_series(_events(), tasks, task="DM-001")["grand_total"]["cost_usd"] == 1.6
    # the retro run's task isn't a real task file, so no product/phase/difficulty/task
    # filter can ever match it — it only shows up in an unfiltered or activity/model/harness view
    assert cost_series(_events(), tasks, product="demo")["grand_total"]["cost_usd"] == 3.85
    assert cost_series(_events(), tasks, product="other")["grand_total"]["cost_usd"] == 0.0
    assert cost_series(_events(), tasks, product="other")["groups"] == []


def test_since_and_until_scope_to_a_window():
    series = cost_series(_events(), _tasks(), since="2026-09-05T00:00:00+00:00")
    assert series["grand_total"]["cost_usd"] == 1.15  # only the 09-05 events (0.5+0.25+0.1+0.3)
    series = cost_series(_events(), _tasks(), until="2026-09-05T00:00:00+00:00")
    assert series["grand_total"]["cost_usd"] == 3.0  # only the 09-04 events


# ---- CLI/web parity, and today's question (CG-214) --------------------------------------


def _write_events(garden, events: list[dict]) -> None:
    path = garden / ".garden" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_cli_and_web_costs_agree_on_a_fixture_log(garden):
    from tests.test_cli import run
    from tests.test_web import client

    _write_events(garden, _events())

    r = run(garden, "costs", "--by", "model", "--json")
    assert r.exit_code == 0, r.output
    cli_series = json.loads(r.output)
    assert cli_series["totals"]["opus"]["cost_usd"] == 2.55
    assert cli_series["totals"]["sonnet"]["cost_usd"] == 1.6

    page = client(garden).get("/costs?by=model").text
    assert "$2.55" in page and "$1.60" in page
    assert '<option value="model" selected>' in page


def test_backfill_recomputes_codex_cost_from_stored_transcript(garden):
    """CG-233: a codex run recorded before costs were priced (cost_usd null, usage never
    computed from its transcript) gets a real cost_usd on `garden costs --backfill`, and the
    matching run_finished event is corrected so `garden costs` picks it up too."""
    from tests.test_cli import run as cli_run

    run_dir = garden / ".garden" / "runs" / "DM-001" / "20260101T000000Z-work"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "task_id": "DM-001", "run_id": "20260101T000000Z-work", "dir": str(run_dir),
        "runner": "local", "mode": "work", "harness": "codex", "model": "gpt-5.6-terra",
        "status": "done", "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:05:00+00:00",
        "usage": {}, "cost_usd": None,
    }))
    (run_dir / "stdout.json").write_text(
        json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n"
        + json.dumps({"type": "turn.completed",
                     "usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 200}}) + "\n"
    )
    _write_events(garden, [{"at": "2026-01-01T00:05:00+00:00", "kind": "run_finished", "task": "DM-001",
                           "run": "20260101T000000Z-work", "mode": "work", "harness": "codex",
                           "model": "gpt-5.6-terra", "status": "done", "cost_usd": None, "usage": {}}])

    r = cli_run(garden, "costs", "--backfill")
    assert r.exit_code == 0, r.output
    assert "1 codex run" in r.output

    expected_cost = (900 * 2.0 + 100 * 0.2 + 200 * 12.0) / 1_000_000
    reloaded = json.loads((run_dir / "run.json").read_text())
    assert reloaded["cost_usd"] == pytest.approx(expected_cost)
    assert reloaded["usage"]["input_tokens"] == 900

    patched = json.loads((garden / ".garden" / "events.jsonl").read_text().splitlines()[0])
    assert patched["cost_usd"] == pytest.approx(expected_cost)

    # a second backfill is a no-op: the run already carries the recomputed cost
    r = cli_run(garden, "costs", "--backfill")
    assert "0 codex run" in r.output


def test_costs_page_shows_hourly_spend_dropping_after_the_tier_change(garden):
    """CG-214's motivating day: a tier-map change at 14:50 on 2026-09-05 should show up as a
    drop in spend per hour from then on."""
    from tests.test_web import client

    _write_events(garden, [
        {"at": "2026-09-05T13:05:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "opus", "harness": "claude", "cost_usd": 2.00, "usage": {}},
        {"at": "2026-09-05T13:40:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "opus", "harness": "claude", "cost_usd": 1.50, "usage": {}},
        {"at": "2026-09-05T15:05:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "sonnet", "harness": "claude", "cost_usd": 0.20, "usage": {}},
        {"at": "2026-09-05T15:40:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "sonnet", "harness": "claude", "cost_usd": 0.15, "usage": {}},
    ])
    series = cost_series(
        [json.loads(line) for line in (garden / ".garden" / "events.jsonl").read_text().splitlines()],
        {}, since="2026-09-05T00:00:00+00:00", bucket="hour",
    )
    by_bucket = {b["bucket"]: sum(r["cost_usd"] for r in b["groups"].values()) for b in series["buckets"]}
    assert round(by_bucket["2026-09-05T13:00"], 2) == 3.50
    assert round(by_bucket["2026-09-05T15:00"], 2) == 0.35
    assert by_bucket["2026-09-05T15:00"] < by_bucket["2026-09-05T13:00"]

    page = client(garden).get("/costs?since=2026-09-05T00%3A00%3A00%2B00%3A00&bucket=hour").text
    assert page.count("<svg") >= 1  # the stacked chart rendered, not the empty state
    assert "$3.50" in page and "$0.35" in page


# ---- the operator activity, read from docs/operator-spend.jsonl (CG-223) ----------------


def _write_operator_records(garden, records: list[dict]) -> None:
    path = garden / "docs" / "operator-spend.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_operator_activity_appears_in_cli_and_web_costs(garden):
    from tests.test_cli import run
    from tests.test_web import client

    _write_events(garden, _events())
    _write_operator_records(garden, [
        {"at": "2026-09-05T09:00:00+00:00", "session": "sess-a", "list_price_usd": 1.5, "turns": 3, "avg_context": 100},
        {"at": "2026-09-05T09:30:00+00:00", "session": "sess-a", "list_price_usd": 2.0, "turns": 5, "avg_context": 200},
    ])

    r = run(garden, "costs", "--by", "activity", "--json")
    assert r.exit_code == 0, r.output
    series = json.loads(r.output)
    assert "operator" in series["groups"]
    assert series["totals"]["operator"]["cost_usd"] == 2.0  # 1.5, then delta of 0.5

    page = client(garden).get("/costs?by=activity").text
    assert "operator" in page
    assert "$2.00" in page


def test_operator_activity_is_sliceable_by_session(garden):
    from tests.test_cli import run

    _write_operator_records(garden, [
        {"at": "2026-09-05T09:00:00+00:00", "session": "sess-a", "list_price_usd": 1.0},
        {"at": "2026-09-05T09:00:00+00:00", "session": "sess-b", "list_price_usd": 4.0},
    ])
    r = run(garden, "costs", "--by", "session", "--json")
    series = json.loads(r.output)
    assert series["totals"]["sess-a"]["cost_usd"] == 1.0
    assert series["totals"]["sess-b"]["cost_usd"] == 4.0

    r = run(garden, "costs", "--session", "sess-b", "--json")
    only_b = json.loads(r.output)
    assert only_b["grand_total"]["cost_usd"] == 4.0


def test_costs_page_draws_a_compaction_annotation(garden):
    from tests.test_web import client

    _write_operator_records(garden, [
        {"at": "2026-09-05T09:00:00+00:00", "session": "sess-a", "list_price_usd": 1.0},
        {"at": "2026-09-05T09:00:00+00:00", "session": "sess-a", "kind": "compacted"},
    ])
    page = client(garden).get("/costs?since=2026-09-05T00%3A00%3A00%2B00%3A00").text
    assert "compacted" in page


# ---- profile_changed annotations on the chart (CG-221) -----------------------------------


def test_cost_stack_svg_marks_a_profile_change_on_its_bucket():
    series = cost_series(_events(), _tasks(), group_by="activity", bucket="day")
    svg = cost_stack_svg(series, annotations=[{"at": "2026-09-05T09:15:00+00:00", "from": "economy", "to": "fast"}])
    assert "annotation" in svg
    assert "profile changed economy → fast" in svg


def test_cost_stack_svg_skips_an_annotation_with_no_bar_to_mark():
    series = cost_series(_events(), _tasks(), group_by="activity", bucket="day")
    svg = cost_stack_svg(series, annotations=[{"at": "2020-01-01T00:00:00+00:00", "from": "economy", "to": "fast"}])
    assert "annotation" not in svg


def test_costs_page_shows_a_profile_change_as_an_annotation(garden):
    from tests.test_web import client

    _write_events(garden, [
        {"at": "2026-09-05T09:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "sonnet", "harness": "claude", "cost_usd": 1.0, "usage": {}},
        {"at": "2026-09-05T09:30:00+00:00", "kind": "profile_changed", "from": "economy", "to": "fast"},
    ])
    page = client(garden).get("/costs?since=2026-09-05T00%3A00%3A00%2B00%3A00").text
    assert "profile changed economy → fast" in page
