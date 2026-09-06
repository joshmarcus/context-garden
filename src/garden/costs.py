"""Spend over time, sliced one way and filtered by the rest: `cost_series` is the single
aggregation behind both `garden costs` and the `/costs` page, so an operator staring at a
graph and a manager reading a printout see the same numbers.

Every `run_finished` event carries `cost_usd`, `mode`, `harness`, `model`, `task` and
`usage`; a task's *current* `difficulty`, `product` and `phase` come from the task file via
`tasks` (id -> Task, e.g. `Store.tasks()`). A run dispatched under a tier the task no longer
carries (its difficulty was changed since) is grouped under the task's current value, not
the one that actually picked its model — the same trade-off `events.metrics` already makes.
A run whose task id isn't in `tasks` (a retro or persona run, dispatched against a synthetic
probe task, never a real one) can still be grouped by activity, model or harness, but reads
as the "unknown" difficulty/phase/task group and is naturally excluded by a difficulty/phase/
task filter — it was never scoped to one.
"""

from __future__ import annotations

from typing import Any

from .model import Task

# The fixed activity vocabulary the costs chart names in order (CG-214): every other mode
# a run can carry (trial, compare, edit, and any future one) folds into "other"
# rather than growing the chart's own categorical order. "operator" (CG-223) is not a
# scheduler run mode: it is synthesized from docs/operator-spend.jsonl by
# `operator_spend.to_cost_events` before the events reach `cost_series`.
ACTIVITIES = ("work", "resume", "revise", "rebase", "review", "persona", "retro", "check", "operator")
GROUP_BY_CHOICES = ("activity", "difficulty", "model", "harness", "pool_member", "phase", "task", "session")
BUCKET_CHOICES = ("hour", "day")


def bucket_key(at: str, bucket: str) -> str:
    """The bucket an ISO timestamp falls into for `bucket` ("hour" or "day"); shared with
    `charts.cost_stack_svg` so a `profile_changed` annotation lines up with the same bar a
    run's cost landed in."""
    return at[:13] + ":00" if bucket == "hour" else at[:10]


def _group_key(ev: dict[str, Any], task: Task | None, group_by: str) -> str:
    if group_by == "activity":
        mode = str(ev.get("mode") or "unknown")
        return mode if mode in ACTIVITIES else "other"
    if group_by == "model":
        return str(ev.get("model") or "unknown")
    if group_by == "harness":
        return str(ev.get("harness") or "unknown")
    if group_by == "pool_member":
        return str(ev.get("pool_member") or "unpooled")
    if group_by == "difficulty":
        return str(task.difficulty) if task else "unknown"
    if group_by == "phase":
        return task.key if task else "unknown"
    if group_by == "task":
        return str(ev.get("task") or "unknown")
    if group_by == "session":
        return str(ev.get("session") or "unknown")
    raise ValueError(f"unknown group_by: {group_by}")


def _zero_row() -> dict[str, Any]:
    return {
        "runs": 0, "cost_usd": 0.0, "priced_runs": 0, "unpriced_runs": 0,
        "task_ids": set(), "tasked_cost_usd": 0.0, "tasked_unpriced_runs": 0,
        "taskless_runs": 0, "taskless_cost_usd": 0.0, "taskless_unpriced_runs": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }


def _add(row: dict[str, Any], ev: dict[str, Any]) -> None:
    row["runs"] += 1
    cost = ev.get("cost_usd")
    priced = isinstance(cost, (int, float)) and not isinstance(cost, bool)
    if priced:
        row["cost_usd"] += float(cost)
        row["priced_runs"] += 1
    else:
        row["unpriced_runs"] += 1
    task_id = str(ev.get("task") or "")
    if task_id:
        row["task_ids"].add(task_id)
        if priced:
            row["tasked_cost_usd"] += float(cost)
        else:
            row["tasked_unpriced_runs"] += 1
    else:
        row["taskless_runs"] += 1
        if priced:
            row["taskless_cost_usd"] += float(cost)
        else:
            row["taskless_unpriced_runs"] += 1
    usage = ev.get("usage") or {}
    row["cache_read_tokens"] += int(usage.get("cache_read_input_tokens", 0) or 0)
    row["cache_write_tokens"] += int(usage.get("cache_creation_input_tokens", 0) or 0)


def _finish_row(row: dict[str, Any], grand_cost: float | None = None) -> None:
    """Make an internal aggregation row safe for callers and calculate task-based values."""
    task_count = len(row.pop("task_ids"))
    row["task_count"] = task_count
    row["cost_usd"] = round(row["cost_usd"], 4)
    row["tasked_cost_usd"] = round(row["tasked_cost_usd"], 4)
    row["taskless_cost_usd"] = round(row["taskless_cost_usd"], 4)
    row["cost_complete"] = row["unpriced_runs"] == 0
    row["tasked_cost_complete"] = row["tasked_unpriced_runs"] == 0
    row["mean_cost_usd"] = round(row["cost_usd"] / row["runs"], 4) if row["runs"] else None
    # Taskless activity remains in the total, but cannot belong in a per-task numerator.
    # Only an unpriced tasked run makes a tasked average incomplete.
    row["cost_per_task_usd"] = (
        round(row["tasked_cost_usd"] / task_count, 4)
        if task_count and row["tasked_cost_complete"] else None
    )
    if grand_cost is not None:
        row["share"] = round(row["cost_usd"] / grand_cost, 4) if grand_cost else None


def cost_series(
    events: list[dict[str, Any]], tasks: dict[str, Task], *,
    since: str = "", until: str = "", bucket: str = "day", group_by: str = "activity",
    difficulty: str = "", model: str = "", harness: str = "", phase: str = "", product: str = "", task: str = "",
    session: str = "",
) -> dict[str, Any]:
    """Bucket `run_finished` events by time (day or hour), grouped by one dimension, with the
    rest of the dimensions available as equality filters.

    Returns `{"buckets": [{"bucket": <key>, "groups": {group: row}}, ...], "totals": {group:
    row}, "grand_total": row, "groups": [group, ...] ordered by descending cost, "group_by":
    group_by, "bucket": bucket}`, where a `row` is `{runs, cost_usd, cache_read_tokens,
    cache_write_tokens}`. Rows also carry their distinct `task_count`,
    `cost_per_task_usd`, pricing completeness, and `share` (for totals). Missing prices
    stay missing rather than becoming zero-cost runs.
    """
    if group_by not in GROUP_BY_CHOICES:
        raise ValueError(f"unknown group_by: {group_by}")
    if bucket not in BUCKET_CHOICES:
        raise ValueError(f"unknown bucket: {bucket}")
    buckets: dict[str, dict[str, dict[str, Any]]] = {}
    totals: dict[str, dict[str, Any]] = {}
    grand = _zero_row()
    for ev in events:
        if ev.get("kind") != "run_finished":
            continue
        at = str(ev.get("at") or "")
        if not at:
            continue
        if since and at < since:
            continue
        if until and at >= until:
            continue
        tid = str(ev.get("task") or "")
        t = tasks.get(tid)
        if difficulty and (not t or t.difficulty != difficulty):
            continue
        if model and str(ev.get("model") or "") != model:
            continue
        if harness and str(ev.get("harness") or "") != harness:
            continue
        if phase and (not t or t.key != phase):
            continue
        if product and (not t or t.product != product):
            continue
        if task and tid != task:
            continue
        if session and str(ev.get("session") or "") != session:
            continue
        group = _group_key(ev, t, group_by)
        _add(buckets.setdefault(bucket_key(at, bucket), {}).setdefault(group, _zero_row()), ev)
        _add(totals.setdefault(group, _zero_row()), ev)
        _add(grand, ev)
    _finish_row(grand)
    for row in totals.values():
        _finish_row(row, grand["cost_usd"])
    ordered_buckets = [{"bucket": b, "groups": buckets[b]} for b in sorted(buckets)]
    for row_set in ordered_buckets:
        for row in row_set["groups"].values():
            _finish_row(row)
    return {
        "buckets": ordered_buckets,
        "totals": totals,
        "grand_total": grand,
        "groups": sorted(totals, key=lambda g: -totals[g]["cost_usd"]),
        "group_by": group_by,
        "bucket": bucket,
    }
