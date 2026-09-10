"""cost_series (the aggregation behind both `garden costs` and /costs) and their parity."""

from __future__ import annotations

import json

import pytest

from garden import now1
from garden import operator_spend as ops
from garden.charts import cost_stack_svg
from garden.costs import cost_series
from garden.events import metrics
from garden.model import Status, Task
from garden.outcomes import acceptance_cohort, attributed_phase_key, cohort_subset, delegated_effort
from garden.store import Store


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


def test_delegated_effort_uses_the_accepted_cohort_and_preserves_unknowns():
    tasks = {"DM-001": _tasks()["DM-001"]}
    events = [
        {"at": "2026-09-01T10:00:00+00:00", "kind": "dispatch", "task": "DM-001", "mode": "work"},
        {"at": "2026-09-01T10:30:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "work", "cost_usd": 2.0},
        {"at": "2026-09-01T11:00:00+00:00", "kind": "retry", "task": "DM-001",
         "actor": "delegated_operator", "reason": "failed check"},
        {"at": "2026-09-01T12:00:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "review", "cost_usd": None},
        {"at": "2026-09-01T13:00:00+00:00", "kind": "transition", "task": "DM-001",
         "to": "done", "base_merged": True},
        {"at": "2026-09-01T12:30:00+00:00", "kind": "run_finished", "task": "", "mode": "operator",
         "product": "demo", "phase": "p1", "cost_usd": 1.0},
        {"at": "2026-09-01T12:40:00+00:00", "kind": "run_finished", "task": "", "mode": "operator",
         "cost_usd": 9.0},
    ]

    effort = delegated_effort(events, tasks, since="2026-09-01T00:00:00+00:00")

    assert effort["accepted"] == 1
    assert effort["elapsed"] == {"tasks_with_lead_time": 1, "median_lead_hours": 3.0,
                                  "total_lead_hours": 3.0}
    assert effort["actions"]["delegated_operator"]["causes"] == {"failed check": 1}
    assert effort["actions"]["human_owner"]["hours"] is None
    assert effort["cost"] == {"known_usd": 3.0, "priced_records": 2, "unpriced_records": 1,
                              "complete": False, "per_accepted_change": None}
    assert effort["operator"]["unattributed_records"] == 1
    assert effort["savings"] is None


def test_delegated_effort_normalizes_established_action_provenance():
    tasks = {"DM-001": _tasks()["DM-001"]}
    events = [
        {"at": "2026-09-01T10:00:00+00:00", "kind": "dispatch", "task": "DM-001"},
        {"at": "2026-09-01T10:10:00+00:00", "kind": "answer", "task": "DM-001"},
        {"at": "2026-09-01T10:20:00+00:00", "kind": "triaged", "task": "DM-001", "by": "human"},
        {"at": "2026-09-01T10:30:00+00:00", "kind": "retry", "task": "DM-001",
         "actor": "delegated_operator"},
        {"at": "2026-09-01T10:40:00+00:00", "kind": "triaged", "task": "DM-001", "by": "github"},
        {"at": "2026-09-01T10:50:00+00:00", "kind": "automerged", "task": "DM-001"},
        {"at": "2026-09-01T11:00:00+00:00", "kind": "requeue", "task": "DM-001"},
        {"at": "2026-09-01T12:00:00+00:00", "kind": "transition", "task": "DM-001",
         "to": "done", "base_merged": True},
    ]

    actions = delegated_effort(events, tasks)["actions"]

    assert actions["human_owner"]["actions"] == 2
    assert actions["delegated_operator"]["actions"] == 1
    assert actions["automated_scheduler"]["actions"] == 2
    assert actions["unknown"]["actions"] == 1


def test_delegated_effort_attributes_phase_and_global_taskless_actions():
    tasks = {"DM-001": _tasks()["DM-001"]}
    events = [
        {"at": "2026-09-01T10:00:00+00:00", "kind": "dispatch", "task": "DM-001"},
        {"at": "2026-09-01T10:10:00+00:00", "kind": "budget_set", "task": "",
         "phase": "demo/p1", "by": "web"},
        {"at": "2026-09-01T10:20:00+00:00", "kind": "dispatch_paused", "task": "",
         "by": "web", "reason": "owner pause"},
        {"at": "2026-09-01T10:30:00+00:00", "kind": "config_override", "task": "",
         "scope": "global", "by": "operator"},
        {"at": "2026-09-01T10:40:00+00:00", "kind": "budget_set", "task": "",
         "phase": "other/p1", "by": "web"},
        {"at": "2026-09-01T10:50:00+00:00", "kind": "budget_set", "task": "", "by": "web"},
        {"at": "2026-09-01T12:00:00+00:00", "kind": "transition", "task": "DM-001",
         "to": "done", "base_merged": True},
        {"at": "2026-09-01T12:10:00+00:00", "kind": "dispatch_resumed", "task": "", "by": "web"},
    ]

    effort = delegated_effort(events, tasks)

    assert effort["actions"]["human_owner"]["actions"] == 2
    assert effort["actions"]["delegated_operator"]["actions"] == 1
    assert effort["action_coverage"] == {"attributed_taskless": 3, "unattributed_taskless": 2}


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


def test_group_by_activity_names_resume():
    events = [{"at": "2026-09-05T10:00:00+00:00", "kind": "run_finished", "task": "DM-001",
               "mode": "resume", "cost_usd": 0.2, "usage": {}}]
    series = cost_series(events, _tasks(), group_by="activity")
    assert series["groups"] == ["resume"]


def test_per_task_cost_uses_distinct_tasks_and_keeps_unpriced_and_taskless_runs_separate():
    events = [
        {"at": "2026-09-05T10:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work", "cost_usd": 4.0},
        {"at": "2026-09-05T10:01:00+00:00", "kind": "run_finished", "task": "DM-002", "mode": "work", "cost_usd": 4.0},
        # A revision raises this task's spend but not the denominator.
        {"at": "2026-09-05T10:02:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "revise", "cost_usd": 2.0},
        # A recorded zero is priced activity, while None is not.
        {"at": "2026-09-05T10:03:00+00:00", "kind": "run_finished", "task": "DM-002", "mode": "revise", "cost_usd": 0.0},
        {"at": "2026-09-05T10:04:00+00:00", "kind": "run_finished", "task": "DM-002", "mode": "revise", "cost_usd": None},
        {"at": "2026-09-05T10:05:00+00:00", "kind": "run_finished", "mode": "review", "cost_usd": 3.0},
    ]
    series = cost_series(events, _tasks(), group_by="activity")

    assert series["totals"]["work"]["task_count"] == 2
    assert series["totals"]["work"]["cost_per_task_usd"] == 4.0
    assert series["totals"]["revise"]["task_count"] == 2
    assert series["totals"]["revise"]["cost_usd"] == 2.0
    assert series["totals"]["revise"]["unpriced_runs"] == 1
    assert series["totals"]["revise"]["cost_per_task_usd"] is None
    assert series["totals"]["review"]["taskless_runs"] == 1
    assert series["totals"]["review"]["cost_per_task_usd"] is None
    # Overall is total / the union of task IDs, never an average of activity averages.
    assert series["grand_total"]["task_count"] == 2
    assert series["grand_total"]["cost_per_task_usd"] is None
    complete = cost_series(events[:3], _tasks(), group_by="activity")["grand_total"]
    assert complete["cost_per_task_usd"] == 5.0


def test_taskless_spend_is_excluded_from_tasked_average_but_kept_in_total():
    events = [
        {"at": "2026-09-05T10:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work", "cost_usd": 2.0},
        {"at": "2026-09-05T10:01:00+00:00", "kind": "run_finished", "mode": "work", "cost_usd": 3.0},
    ]

    row = cost_series(events, _tasks(), group_by="activity")["grand_total"]

    assert row["cost_usd"] == 5.0
    assert row["tasked_cost_usd"] == 2.0
    assert row["taskless_cost_usd"] == 3.0
    assert row["task_count"] == 1
    assert row["cost_per_task_usd"] == 2.0


def test_per_task_average_stays_constant_when_equal_spend_is_doubled():
    one_task = [{"at": "2026-09-05T10:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work", "cost_usd": 5.0}]
    two_tasks = one_task + [{"at": "2026-09-05T10:01:00+00:00", "kind": "run_finished", "task": "DM-002", "mode": "work", "cost_usd": 5.0}]
    assert cost_series(one_task, _tasks())["grand_total"]["cost_usd"] == 5.0
    doubled = cost_series(two_tasks, _tasks())["grand_total"]
    assert doubled["cost_usd"] == 10.0
    assert doubled["cost_per_task_usd"] == 5.0


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


def test_group_by_pool_member():
    events = _events()
    events[0]["pool_member"] = "claude:sonnet"
    events[2]["pool_member"] = "codex:gpt-std"
    series = cost_series(events, _tasks(), group_by="pool_member", bucket="day")
    assert series["totals"]["claude:sonnet"]["cost_usd"] == 1.0
    assert series["totals"]["codex:gpt-std"]["cost_usd"] == 0.5
    assert series["totals"]["unpooled"]["cost_usd"] == 2.65


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


def test_accepted_cohort_uses_completion_window_and_full_history_with_missing_prices():
    events = [
        {"at": "2026-09-01T00:00:00+00:00", "kind": "dispatch", "task": "DM-001",
         "mode": "work", "model": "sonnet", "harness": "claude"},
        {"at": "2026-09-01T00:01:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "work", "model": "sonnet", "harness": "claude", "cost_usd": 2.0},
        {"at": "2026-09-02T00:01:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "review", "cost_usd": None},
        {"at": "2026-09-03T00:00:00+00:00", "kind": "transition", "task": "DM-001",
         "to": "done", "base_merged": True},
        # A forced completion is explicitly not acceptance provenance.
        {"at": "2026-09-03T01:00:00+00:00", "kind": "transition", "task": "DM-002",
         "to": "done", "base_merged": False},
    ]
    accepted = cost_series(events, _tasks(), since="2026-09-03T00:00:00+00:00",
                           until="2026-09-04T00:00:00+00:00", model="sonnet")["accepted"]
    assert accepted["accepted"] == 1
    assert accepted["priced_tasks"] == 0 and accepted["unpriced_tasks"] == 1
    assert accepted["known_cost_usd"] == 2.0
    assert accepted["cost_per_accepted_task"] is None
    short_phase = cost_series(events, _tasks(), since="2026-09-03T00:00:00+00:00",
                              product="demo", phase="p1")["accepted"]
    canonical_phase = cost_series(events, _tasks(), since="2026-09-03T00:00:00+00:00",
                                  phase="demo/p1")["accepted"]
    assert short_phase == canonical_phase == accepted


def test_accepted_cohort_prepares_realistic_history_once(monkeypatch):
    """Current-scale history must not become a full scan for every accepted task."""
    tasks = {
        f"DM-{index:03d}": Task(path=None, id=f"DM-{index:03d}", title="Task",
                                status=Status.DONE, product="demo", phase="p1",
                                difficulty=("easy", "medium", "hard")[index % 3])
        for index in range(596)
    }
    events = []
    for index, tid in enumerate(tasks):
        minute = index % 60
        events.append({"at": f"2026-09-{1 + index % 8:02d}T00:{minute:02d}:00+00:00",
                       "kind": "dispatch", "task": tid, "mode": "work",
                       "model": f"model-{index % 4}", "harness": f"harness-{index % 2}"})
        for run in range(6):
            events.append({"at": f"2026-09-{1 + index % 8:02d}T{run + 1:02d}:{minute:02d}:00+00:00",
                           "kind": "run_finished", "task": tid, "mode": "work",
                           "model": f"model-{index % 4}", "harness": f"harness-{index % 2}",
                           "cost_usd": 0.25})
        events.append({"at": f"2026-09-{1 + index % 8:02d}T10:{minute:02d}:00+00:00",
                       "kind": "transition", "task": tid, "to": "done", "base_merged": True})
    events.extend({"at": f"2026-08-31T00:{index % 60:02d}:00+00:00", "kind": "tick"}
                  for index in range(131))

    from garden import outcomes

    calls = 0
    real_timestamp = outcomes.timestamp

    def counted_timestamp(value):
        nonlocal calls
        calls += 1
        return real_timestamp(value)

    monkeypatch.setattr(outcomes, "timestamp", counted_timestamp)
    report = metrics(events, tasks)

    assert len(events) == 4899
    assert sum(row["accepted"] for row in report["by_difficulty"].values()) == 596
    assert sum(row["priced_runs"] for row in report["by_difficulty"].values()) == 3576
    # Several independent metric families consume timestamps, but dimension rows and
    # accepted tasks no longer multiply full-history preparation or scans.
    assert calls <= len(events) * 15


def test_prepared_cohort_subgroups_preserve_filters_prices_and_first_reviews():
    tasks = _tasks()
    events = [
        {"at": "2026-09-01T00:00:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "work", "model": "sonnet", "harness": "codex", "cost_usd": 2.0},
        {"at": "2026-09-01T00:01:00+00:00", "kind": "review", "task": "DM-001",
         "verdict": "approve"},
        {"at": "2026-09-02T00:00:00+00:00", "kind": "transition", "task": "DM-001",
         "to": "done", "base_merged": True},
        {"at": "2026-09-01T00:00:00+00:00", "kind": "run_finished", "task": "DM-002",
         "mode": "work", "model": "opus", "harness": "claude", "cost_usd": None},
        {"at": "2026-09-02T00:00:00+00:00", "kind": "transition", "task": "DM-002",
         "to": "done", "base_merged": True},
    ]
    whole = acceptance_cohort(events, tasks)

    assert cohort_subset(whole, difficulty="easy") == acceptance_cohort(
        events, tasks, difficulty="easy")
    assert cohort_subset(whole, model="opus") == acceptance_cohort(events, tasks, model="opus")
    assert cohort_subset(whole, harness="codex") == acceptance_cohort(
        events, tasks, harness="codex")


def test_phase_filter_excludes_other_spend_and_separates_unattributed_operator_cost():
    events = _events() + [
        {"at": "2026-09-05T10:00:00+00:00", "kind": "run_finished", "mode": "operator",
         "cost_usd": 7.0, "product": "demo", "phase": "demo/p2"},
        {"at": "2026-09-05T10:01:00+00:00", "kind": "run_finished", "mode": "operator",
         "cost_usd": 11.0, "product": "", "phase": ""},
    ]
    series = cost_series(events, _tasks(), phase="demo/p1")
    assert series["grand_total"]["cost_usd"] == 1.6
    assert series["unattributed_operator"] == {
        "runs": 1, "priced_runs": 1, "unpriced_runs": 0, "cost_usd": 11.0}


def test_operator_phase_attribution_agrees_across_costs_metrics_now_and_retro():
    """Short and canonical names select one product's phase on every reporting surface."""
    selected = {
        "CG-001": Task(path=None, id="CG-001", title="A", status=Status.DONE,
                       product="context-garden", phase="phase-05", difficulty="easy"),
    }
    all_tasks = {
        **selected,
        "OT-001": Task(path=None, id="OT-001", title="B", status=Status.DONE,
                       product="other", phase="phase-05", difficulty="easy"),
    }
    operator_records = [
        {"list_price_usd": 1.0, "turns": 1, "session": "short",
         "product": "context-garden", "phase": "phase-05", "at": "2026-09-06T01:00:00+00:00"},
        {"list_price_usd": 2.0, "turns": 2, "session": "canonical",
         "product": "context-garden", "phase": "context-garden/phase-05",
         "at": "2026-09-06T01:01:00+00:00"},
        {"list_price_usd": 40.0, "turns": 3, "session": "other",
         "product": "other", "phase": "phase-05", "at": "2026-09-06T01:02:00+00:00"},
        {"list_price_usd": 8.0, "turns": 4, "session": "unknown",
         "product": "", "phase": "", "at": "2026-09-06T01:03:00+00:00"},
        {"list_price_usd": 5.0, "turns": 1, "session": "product-only",
         "product": "context-garden", "phase": "", "at": "2026-09-06T01:04:00+00:00"},
    ]
    operator_events = ops.to_cost_events(operator_records)

    costs = cost_series(operator_events, all_tasks, product="context-garden", phase="phase-05")
    cli_metrics = metrics(operator_events, selected)
    now = now1.period([], operator_events, selected, "2026-09-06T00:00:00+00:00", "hour")
    retro_cost, retro_turns = ops.attributed_totals(
        operator_records,
        include=lambda record: record["product"] == "context-garden"
        and attributed_phase_key(record) == "context-garden/phase-05",
    )
    retro_unattributed = ops.attributed_summary(
        operator_records, include=lambda record: not record.get("product") or not record.get("phase"))

    assert costs["grand_total"]["cost_usd"] == 3.0
    assert costs["unattributed_operator"] == {
        "runs": 2, "priced_runs": 2, "unpriced_runs": 0, "cost_usd": 13.0}
    assert cli_metrics["operator"] == {
        "spend": 3.0, "share": 1.0, "priced_records": 2, "unpriced_records": 0,
        "cost_complete": True, "unattributed_spend": 13.0,
        "unattributed_priced_records": 2, "unattributed_unpriced_records": 0,
        "unattributed_cost_complete": True}
    assert now["cost"] == 3.0
    assert now["operator"] == {"spend": 3.0, "share": 1.0, "sessions": 2,
                               "priced_records": 2, "unpriced_records": 0, "cost_complete": True}
    assert now["unattributed_operator"] == {"spend": 13.0, "share": None, "sessions": 2,
                                            "priced_records": 2, "unpriced_records": 0,
                                            "cost_complete": True}
    assert (retro_cost, retro_turns) == (3.0, 3)
    assert retro_unattributed == {"known_cost_usd": 13.0, "turns": 5,
                                  "priced_records": 2, "unpriced_records": 0,
                                  "cost_complete": True}


def test_outcomes_count_only_base_branch_merges_as_accepted():
    """CG-251: a completed-looking task is not accepted until the scheduler records its
    base-branch `done` transition (CG-228); its run cost must not lower the denominator."""
    tasks = _tasks()
    events = [
        {"at": "2026-09-04T10:00:00+00:00", "kind": "dispatch", "task": "DM-001", "mode": "work", "model": "sonnet", "harness": "claude"},
        {"at": "2026-09-04T10:01:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work", "model": "sonnet", "harness": "claude", "cost_usd": 2.0},
        {"at": "2026-09-04T10:02:00+00:00", "kind": "review", "task": "DM-001", "verdict": "approve"},
        {"at": "2026-09-04T10:03:00+00:00", "kind": "transition", "task": "DM-001", "to": "done", "base_merged": True},
        # Its task file can say done, but without the merge transition it was never accepted.
        {"at": "2026-09-04T11:00:00+00:00", "kind": "dispatch", "task": "DM-002", "mode": "work", "model": "opus", "harness": "codex"},
        {"at": "2026-09-04T11:01:00+00:00", "kind": "run_finished", "task": "DM-002", "mode": "work", "model": "opus", "harness": "codex", "cost_usd": 9.0},
        {"at": "2026-09-04T11:02:00+00:00", "kind": "review", "task": "DM-002", "verdict": "request_changes"},
    ]

    outcome = metrics(events, tasks)
    sonnet = outcome["by_model"]["sonnet"]
    assert sonnet["accepted"] == 1
    assert sonnet["mean_cost_usd"] == 2.0
    assert sonnet["cost_per_accepted_task"] == 2.0
    assert sonnet["first_pass_rate"] == 1.0
    assert outcome["by_model"]["opus"]["accepted"] == 0
    assert outcome["by_model"]["opus"]["cost_per_accepted_task"] is None
    assert outcome["by_model"]["opus"]["first_pass_rate"] == 0.0
    assert outcome["by_difficulty"]["easy"]["cost_per_accepted_task"] == 2.0
    assert outcome["by_harness"]["claude"]["first_pass_rate"] == 1.0


def test_outcomes_attribute_supporting_run_costs_to_the_task_route():
    """CG-251: a model's accepted-task bill includes untagged review and edit runs."""
    tasks = _tasks()
    events = [
        {"at": "2026-09-04T10:00:00+00:00", "kind": "dispatch", "task": "DM-001", "mode": "work", "model": "sonnet", "harness": "claude"},
        {"at": "2026-09-04T10:01:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work", "model": "sonnet", "harness": "claude", "cost_usd": 1.0},
        # These runs support the same task, but their events identify only their mode.
        {"at": "2026-09-04T10:02:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "review", "cost_usd": 0.5},
        {"at": "2026-09-04T10:03:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "edit", "cost_usd": 0.25},
        {"at": "2026-09-04T10:04:00+00:00", "kind": "transition", "task": "DM-001", "to": "done", "base_merged": True},
    ]

    outcome = metrics(events, tasks)
    assert outcome["by_model"]["sonnet"]["mean_cost_usd"] == 1.0
    assert outcome["by_model"]["sonnet"]["cost_per_accepted_task"] == 1.75
    assert outcome["by_harness"]["claude"]["cost_per_accepted_task"] == 1.75


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


def test_costs_page_shares_now_comparisons_and_last_week_label(garden):
    from tests.test_web import client

    _write_events(garden, _events())

    page = client(garden).get("/costs").text

    assert '<option value="7d">Last week</option>' in page
    assert "Cost comparisons for this cohort" in page
    assert "Runs by harness and model" in page
    assert "By difficulty and model" in page


def test_costs_comparisons_honor_session_and_mark_unpriced_runs(garden):
    from garden.web.pages.costs import comparison_data
    from tests.test_web import client

    events = [
        {"at": "2026-09-05T09:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "sonnet", "harness": "claude", "session": "included", "cost_usd": 2.0},
        {"at": "2026-09-05T09:01:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work",
         "model": "opus", "harness": "claude", "session": "excluded", "cost_usd": 9.0},
        {"at": "2026-09-05T09:02:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "review",
         "model": "sonnet", "harness": "claude", "session": "included", "cost_usd": None},
    ]
    _write_events(garden, events)

    page = client(garden).get("/costs?session=included").text
    comparison = comparison_data(events, Store(garden).tasks(), "", session="included")

    assert "partial $2.00 · 2 runs" in page
    assert "one or more samples have no recorded price" in page
    assert comparison["by_model"]["columns"] == ["claude:sonnet"]
    assert comparison["by_model"]["rows"]["review"]["claude:sonnet"]["cost_complete"] is False


def test_costs_page_switches_to_average_per_task_and_preserves_total_context(garden):
    from tests.test_web import client

    _write_events(garden, [
        {"at": "2026-09-05T09:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work", "cost_usd": 2.0},
        {"at": "2026-09-05T09:01:00+00:00", "kind": "run_finished", "task": "DM-002", "mode": "work", "cost_usd": 4.0},
        {"at": "2026-09-05T09:02:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "revise", "cost_usd": 2.0},
    ])

    page = client(garden).get("/costs?metric=per_task&by=activity").text
    assert '<option value="per_task" selected>' in page
    assert "Average cost per participating task over time, by activity" in page
    assert "8.00 over 3 runs and 2 participating tasks" in page
    assert "Overall average: $4.00 per participating task" in page
    assert "Work and revise can have different task cohorts" in page


def test_costs_page_separates_taskless_spend_from_task_average(garden):
    from tests.test_web import client

    _write_events(garden, [
        {"at": "2026-09-05T09:00:00+00:00", "kind": "run_finished", "task": "DM-001", "mode": "work", "cost_usd": 2.0},
        {"at": "2026-09-05T09:01:00+00:00", "kind": "run_finished", "mode": "work", "cost_usd": 3.0},
    ])

    page = client(garden).get("/costs?metric=per_task&by=activity").text

    assert "5.00 over 2 runs and 1 participating task" in page
    assert "Overall average: $2.00 per participating task" in page
    assert "taskless spend" in page
    assert "included in total cost" in page


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


def test_operator_price_completeness_and_short_phase_cohort_match_cli_web_now_and_retro(garden):
    """A measured zero remains priced while an unknown operator price stays visible and
    makes totals partial on every surface; a short phase selector uses the same task cohort."""
    from garden.retro import numbers_section
    from tests.test_cli import run
    from tests.test_web import client

    at = "2026-09-05T09:00:00+00:00"
    _write_events(garden, [
        {"at": at, "kind": "dispatch", "task": "DM-001", "mode": "work",
         "model": "sonnet", "harness": "claude"},
        {"at": "2026-09-05T09:01:00+00:00", "kind": "run_finished", "task": "DM-001",
         "mode": "work", "model": "sonnet", "harness": "claude", "cost_usd": 2.0},
        {"at": "2026-09-05T09:02:00+00:00", "kind": "review", "task": "DM-001",
         "verdict": "approve"},
        {"at": "2026-09-05T09:03:00+00:00", "kind": "transition", "task": "DM-001",
         "to": "done", "base_merged": True},
    ])
    records = [
        {"at": "2026-09-05T09:04:00+00:00", "session": "zero", "turns": 1,
         "list_price_usd": 0.0, "product": "demo", "phase": "p1"},
        {"at": "2026-09-05T09:05:00+00:00", "session": "unknown", "turns": 2,
         "list_price_usd": None, "product": "demo", "phase": "demo/p1"},
        {"at": "2026-09-05T09:06:00+00:00", "session": "other", "turns": 3,
         "list_price_usd": 40.0, "product": "other", "phase": "p1"},
        {"at": "2026-09-05T09:07:00+00:00", "session": "unattributed", "turns": 4,
         "list_price_usd": None, "product": "", "phase": ""},
    ]
    _write_operator_records(garden, records)

    cli = json.loads(run(garden, "costs", "--product", "demo", "--phase", "p1", "--json").output)
    assert cli["grand_total"]["cost_usd"] == 2.0
    assert cli["grand_total"]["priced_runs"] == 2 and cli["grand_total"]["unpriced_runs"] == 1
    assert cli["grand_total"]["cost_complete"] is False
    assert cli["unattributed_operator"] == {
        "runs": 1, "priced_runs": 0, "unpriced_runs": 1, "cost_usd": 0.0}

    events = [json.loads(line) for line in (garden / ".garden/events.jsonl").read_text().splitlines()]
    operator_events = ops.to_cost_events(records)
    selected = {"DM-001": Store(garden).tasks()["DM-001"]}
    now = now1.period(events, operator_events, selected, at, "hour")
    assert now["cost"] == 2.0 and now["cost_complete"] is False
    assert now["operator"]["spend"] == 0.0
    assert now["operator"]["priced_records"] == 1 and now["operator"]["unpriced_records"] == 1
    assert now["operator"]["share"] is None

    operator = ops.attributed_summary(
        records, include=lambda row: row.get("product") == "demo"
        and attributed_phase_key(row) == "demo/p1")
    retro = numbers_section(2.0, operator["known_cost_usd"], operator_priced_records=1,
                            operator_unpriced_records=1, operator_turns=operator["turns"])
    assert "operator: partial known spend $0.00" in retro
    assert "1 priced, 1 unpriced records" in retro
    assert "total: $2.00 partial known spend" in retro

    page = client(garden).get("/costs?product=demo&phase=p1").text
    assert "partial $2.00 over 3 runs" in page
    assert "1</b> accepted task" in page and "sonnet" in page
    assert "partial known spend $0.00 over 1 record (0 priced, 1 unpriced)" in page


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
