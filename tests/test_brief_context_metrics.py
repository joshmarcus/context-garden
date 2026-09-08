"""Brief/context aggregates stay based on indexed run metadata."""

from __future__ import annotations

import datetime as dt
import json

from garden.events import brief_context_metrics
from garden.model import Status, Task
from garden.runs import Run, RunStore


def _tasks() -> dict[str, Task]:
    return {"CG-001": Task(path=None, id="CG-001", title="Context", status=Status.DONE,
                            product="garden", phase="phase-07", difficulty="hard")}


def test_brief_context_aggregates_known_mixed_runs_and_flags_comparable_growth(tmp_path):
    store = RunStore(tmp_path)
    for number in range(10):
        run = store.new_run("CG-001", "local", run_id=f"run-{number}")
        run.mode, run.model, run.difficulty = "work", "model-a", "hard"
        run.started_at = f"2026-01-{number + 1:02d}T00:00:00+00:00"
        run.finished_at = run.started_at
        run.status = "done"
        run.brief_tokens = 100 if number < 5 else 130
        run.usage = {"input_tokens": 200 + number, "cache_read_input_tokens": 500 + number}
        run.save()

    row = brief_context_metrics(store.all_runs(), _tasks())["groups"][0]

    assert (row["product"], row["phase"], row["mode"], row["model"], row["tier"]) == (
        "garden", "phase-07", "work", "model-a", "hard")
    assert row["runs"] == 10
    assert row["estimated_tokens"] == {"known": 10, "mean": 115.0, "p50": 100, "p95": 130, "max": 130}
    assert row["measured_input_tokens"]["known"] == 10
    assert row["measured_cache_read_tokens"]["p95"] == 509
    assert row["comparison"] == {"baseline_runs": 5, "recent_runs": 5,
                                 "baseline_mean_estimated_tokens": 100.0,
                                 "recent_mean_estimated_tokens": 130.0,
                                 "minimum_samples": 5, "regression": True}


def test_brief_context_treats_missing_historical_values_as_unknown(tmp_path):
    missing = Run(task_id="CG-001", run_id="missing", dir=str(tmp_path / "missing"), runner="local",
                  mode="work", model="model-a", difficulty="hard")
    row = brief_context_metrics([missing], _tasks())["groups"][0]

    assert row["runs"] == 1
    assert row["estimated_tokens"]["mean"] is None
    assert row["measured_input_tokens"]["mean"] is None
    assert row["comparison"]["regression"] is False


def test_brief_context_archive_restore_and_duplicate_continuation_do_not_double_count(tmp_path):
    store = RunStore(tmp_path)
    run = store.new_run("CG-001", "local", run_id="one")
    run.mode, run.model, run.difficulty = "resume", "model-a", "hard"
    run.started_at = run.finished_at = "2026-01-01T00:00:00+00:00"
    run.status, run.brief_tokens = "done", 123
    run.usage = {"input_tokens": 456}
    run.save()

    assert store.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC)) == 1
    archived = store.all_runs()
    duplicate_event_view = Run(**{**archived[0].__dict__})
    assert brief_context_metrics(archived + [duplicate_event_view], _tasks())["groups"][0]["runs"] == 1

    assert store.restore_archived("CG-001", "one")
    restored = brief_context_metrics(store.all_runs(), _tasks())["groups"][0]
    assert restored["runs"] == 1
    assert restored["estimated_tokens"]["mean"] == 123.0


def test_metrics_cli_renders_brief_context_aggregation(garden):
    from tests.test_cli import run as cli_run

    run_dir = garden / ".garden" / "runs" / "DM-001" / "brief-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "task_id": "DM-001", "run_id": "brief-run", "dir": str(run_dir), "runner": "local",
        "mode": "work", "model": "model-a", "difficulty": "easy", "status": "done",
        "started_at": "2026-01-01T00:00:00+00:00", "brief_tokens": 123,
        "usage": {"input_tokens": 456, "cache_read_input_tokens": 789},
    }))

    result = cli_run(garden, "metrics")

    assert result.exit_code == 0, result.output
    assert "brief and startup context by product / phase / mode / model / tier" in result.output
    assert "model-a" in result.output
    assert "123" in result.output
    assert "456" in result.output
