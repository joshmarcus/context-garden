"""Windowed acceptance cohorts shared by metrics and operational views.

Keep lifecycle history before the window: accepting a task costs all of its runs,
not just the last hour's work. Never treat a hand-marked done task as a base merge.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import mean, median
from typing import Any

METRICS = {
    "total_cost": ("Total cost / accepted task", "USD", "lower"),
    "work_cost": ("Mean work-run cost", "USD", "lower"),
    "first_pass": ("First-pass approval", "%", "higher"),
    "revise_rounds": ("Mean revise rounds", "rounds", "lower"),
    "lead_time": ("Median lead time", "seconds", "lower"),
}
IMPLEMENTATION = {"work", "revise", "resume"}


def timestamp(value: Any) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t.replace(tzinfo=dt.UTC) if t.tzinfo is None else t.astimezone(dt.UTC)
    except (ValueError, TypeError):
        return None


def base_acceptance(event: dict) -> bool:
    note = str(event.get("note") or "")
    return event.get("kind") == "transition" and event.get("to") == "done" and (
        event.get("base_merged") is True or note.startswith("PR merged")
        or (note.startswith("parent ") and "this task's commits are now on " in note))


def cell(values: list[float], missing: int, unit: str, direction: str, med: bool = False) -> dict:
    return {"value": (median(values) if med else mean(values)) if values else None,
            "n": len(values), "missing": missing, "unit": unit, "direction": direction,
            "rank": "", "shade": 0.0}


def rank_row(cells: dict[str, dict]) -> None:
    values = [c["value"] for c in cells.values() if c["value"] is not None]
    if not values:
        return
    low, high = min(values), max(values)
    for c in cells.values():
        value = c["value"]
        if value is None:
            continue
        if low == high:
            c["shade"] = 0.0
            c["rank"] = "equal"
            continue
        quality = (value - low) / (high - low)
        if c["direction"] == "lower":
            quality = 1 - quality
        c["shade"] = (2 * quality - 1) * (0.25 if c["n"] < 3 else 1)
        c["rank"] = "best" if quality == 1 else "worst" if quality == 0 else ""


def difficulty_by_model(events: list[dict], tasks: dict[str, Any], since: str = "",
                        until: str = "") -> dict:
    """Five matrices with explicit priced/reviewed denominators and missing counts."""
    end = timestamp(until) or dt.datetime.now(dt.UTC)
    start = timestamp(since) or dt.datetime.min.replace(tzinfo=dt.UTC)
    history = sorted((e for e in events if timestamp(e.get("at")) and timestamp(e["at"]) < end),
                     key=lambda e: timestamp(e["at"]))
    lives: dict[str, list[dict]] = defaultdict(list)
    accepted: dict[str, dt.datetime] = {}
    finished: dict[tuple, dict] = {}
    for e in history:
        tid = e.get("task", "")
        if tid in tasks:
            lives[tid].append(e)
            at = timestamp(e["at"])
            if base_acceptance(e) and start <= at:
                accepted.setdefault(tid, at)
        if e.get("kind") == "run_finished":
            finished[(tid, e.get("run") or e["at"], e.get("mode"))] = e
    finish_by_run = {(e.get("task"), e.get("run")): e for e in finished.values() if e.get("run")}
    models: set[str] = set()
    members = []
    for tid, at in accepted.items():
        life = [e for e in lives[tid] if timestamp(e["at"]) <= at]
        routes = {str(e.get("model") or "unknown model") for e in life
                  if e.get("kind") in ("dispatch", "run_finished") and e.get("mode") in IMPLEMENTATION}
        routes = routes or {"unknown model"}
        models.update(routes)
        runs = [e for e in finished.values() if e.get("task") == tid and timestamp(e["at"]) <= at]
        dispatch = [e for e in life if e.get("kind") == "dispatch"]
        review = next((e for e in life if e.get("kind") == "review" and e.get("verdict") in ("approve", "request_changes")), None)
        completed_ids = {e.get("run") for e in runs}
        complete = (bool(runs) and all(e.get("cost_usd") is not None for e in runs)
                    and all(not e.get("run") or e["run"] in completed_ids for e in dispatch))
        lead = (at - timestamp(dispatch[0]["at"])).total_seconds() if dispatch else None
        members.append({"id": tid, "models": routes, "difficulty": tasks[tid].difficulty or "medium",
                        "total_cost": sum(float(e["cost_usd"]) for e in runs) if complete else None,
                        "first_pass": (100.0 if review["verdict"] == "approve" else 0.0) if review else None,
                        "revise_rounds": sum(e.get("mode") == "revise" for e in dispatch) if dispatch else None,
                        "lead_time": lead if lead is not None and lead >= 0 else None})
    window_runs = [e for e in finished.values() if start <= timestamp(e["at"]) < end]
    # Include implementation models still running across the boundary, even without an acceptance.
    for tid, life in lives.items():
        for e in life:
            if e.get("mode") not in IMPLEMENTATION or e.get("kind") not in ("dispatch", "run_finished"):
                continue
            finish = finish_by_run.get((tid, e.get("run")))
            if timestamp(e["at"]) >= start or (e.get("kind") == "dispatch" and (not finish or timestamp(finish["at"]) >= start)):
                models.add(str(e.get("model") or "unknown model"))
    columns = sorted(models)
    matrices = {}
    for key, (label, unit, direction) in METRICS.items():
        rows = {}
        for tier in ("easy", "medium", "hard"):
            row = {}
            for model in columns:
                if key == "work_cost":
                    values = [e.get("cost_usd") for e in window_runs if e.get("mode") == "work"
                              and e.get("task") in tasks and tasks[e["task"]].difficulty == tier
                              and str(e.get("model") or "unknown model") == model]
                else:
                    values = [m[key] for m in members if m["difficulty"] == tier and model in m["models"]]
                row[model] = cell([v for v in values if v is not None], values.count(None), unit, direction, key == "lead_time")
            rank_row(row)
            rows[tier] = row
        matrices[key] = {"label": label, "unit": unit, "direction": direction, "rows": rows}
    reviewed = [m["first_pass"] for m in members if m["first_pass"] is not None]
    priced = [m["total_cost"] for m in members if m["total_cost"] is not None]
    return {"models": columns, "metrics": matrices, "accepted": {k: v.isoformat() for k, v in accepted.items()},
            "accepted_count": len(accepted), "first_pass": cell(reviewed, len(members)-len(reviewed), "%", "higher"),
            "total_cost": cell(priced, len(members)-len(priced), "USD", "lower")}


def format_cell(c: dict) -> str:
    value = c["value"]
    if value is None:
        return "—"
    if c["unit"] == "USD":
        return "<$0.01" if 0 < value < .01 else f"${value:.2f}"
    if c["unit"] == "%":
        return f"{value:.0f}%"
    if c["unit"] == "seconds":
        return f"{value / 3600:.1f} h" if value >= 3600 else f"{value / 60:.1f} min" if value >= 60 else f"{value:.0f} s"
    return f"{value:.1f}"
