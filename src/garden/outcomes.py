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
EFFORT_ACTION_KINDS = {
    "operator_repair", "requeue", "retry", "set_status", "redispatch", "mark_done",
    "answer", "triaged", "decision_accepted", "decision_resolved", "dispatch_paused",
    "dispatch_resumed", "resumed", "moved", "budget_set", "config_override", "suggestion",
    "automerged",
}
EFFORT_ACTORS = ("human_owner", "delegated_operator", "automated_scheduler", "unknown")


def canonical_phase_key(product: str, phase: str) -> str:
    """Canonicalize an attributed phase without guessing its product.

    Operator ledgers support both ``phase-05`` and ``context-garden/phase-05``. A
    short name is meaningful only with its product, keeping same-named phases in
    different products distinct.
    """
    product, phase = str(product or ""), str(phase or "")
    if not phase or "/" in phase:
        return phase
    return f"{product}/{phase}" if product else phase


def attributed_phase_key(event: dict[str, Any], task: Any | None = None) -> str:
    """Return the canonical phase key for a task run or attributed taskless event."""
    if task is not None:
        return str(getattr(task, "key", ""))
    return canonical_phase_key(str(event.get("product") or ""), str(event.get("phase") or ""))


def acceptance_cohort(
    events: list[dict[str, Any]], tasks: dict[str, Any], *, since: str = "", until: str = "",
    product: str = "", phase: str = "", difficulty: str = "", model: str = "", harness: str = "",
) -> dict[str, Any]:
    """Return the common accepted-task cost cohort used by every reporting surface.

    Membership is determined by a proven base-branch acceptance in the half-open
    completion window ``[since, until)``.  Product, phase and difficulty describe the
    current task; model and harness match any implementation route used before acceptance.
    The numerator contains every run for a member task through its acceptance, including
    review/check/rebase runs.  One missing run price makes that task, and therefore the
    cohort average, incomplete instead of silently contributing zero dollars.
    """
    start = timestamp(since) or dt.datetime.min.replace(tzinfo=dt.UTC)
    end = timestamp(until) or dt.datetime.max.replace(tzinfo=dt.UTC)
    history = sorted((e for e in events if timestamp(e.get("at")) and timestamp(e["at"]) < end),
                     key=lambda e: timestamp(e["at"]))
    merge_facts = {str(e.get("task")) for e in history
                   if e.get("kind") == "automerged" and e.get("task")}
    accepted_at: dict[str, dt.datetime] = {}
    for event in history:
        tid = str(event.get("task") or "")
        at = timestamp(event.get("at"))
        if tid in tasks and at is not None and start <= at < end and base_acceptance(event, merge_facts):
            accepted_at.setdefault(tid, at)

    members: list[dict[str, Any]] = []
    for tid, accepted in accepted_at.items():
        task = tasks[tid]
        if product and getattr(task, "product", "") != product:
            continue
        if phase and getattr(task, "key", "") != canonical_phase_key(product, phase):
            continue
        if difficulty and getattr(task, "difficulty", "") != difficulty:
            continue
        life = [e for e in history if str(e.get("task") or "") == tid
                and timestamp(e.get("at")) <= accepted]
        implementation = [e for e in life if e.get("kind") in ("dispatch", "run_finished")
                          and e.get("mode") in IMPLEMENTATION]
        models = {str(e.get("model") or "unknown") for e in implementation}
        harnesses = {str(e.get("harness") or "unknown") for e in implementation}
        if model and model not in models:
            continue
        if harness and harness not in harnesses:
            continue
        runs = [e for e in life if e.get("kind") == "run_finished"]
        first_review = next((str(e.get("verdict") or "") for e in life
                             if e.get("kind") == "review"
                             and e.get("verdict") in ("approve", "request_changes")), "")
        unpriced = sum(not isinstance(e.get("cost_usd"), (int, float))
                       or isinstance(e.get("cost_usd"), bool) for e in runs)
        known_cost = sum(float(e["cost_usd"]) for e in runs
                         if isinstance(e.get("cost_usd"), (int, float))
                         and not isinstance(e.get("cost_usd"), bool))
        members.append({"id": tid, "accepted_at": accepted.isoformat(), "runs": len(runs),
                        "priced_runs": len(runs) - unpriced, "unpriced_runs": unpriced,
                        "known_cost_usd": round(known_cost, 4),
                        "cost_usd": round(known_cost, 4) if runs and not unpriced else None,
                        "models": sorted(models), "harnesses": sorted(harnesses),
                        "difficulty": getattr(task, "difficulty", "") or "medium",
                        "first_review": first_review})
    known = sum(m["known_cost_usd"] for m in members)
    priced_tasks = sum(m["cost_usd"] is not None for m in members)
    return {"accepted": len(members), "priced_tasks": priced_tasks,
            "unpriced_tasks": len(members) - priced_tasks,
            "priced_runs": sum(m["priced_runs"] for m in members),
            "unpriced_runs": sum(m["unpriced_runs"] for m in members),
            "known_cost_usd": round(known, 4),
            "cost_complete": priced_tasks == len(members),
            "cost_per_accepted_task": (round(known / len(members), 4)
                                       if members and priced_tasks == len(members) else None),
            "tasks": members,
            "contract": "acceptance completion in [since, until); all task runs through acceptance"}


def delegated_effort(
    events: list[dict[str, Any]], tasks: dict[str, Any], *, since: str = "", until: str = "",
) -> dict[str, Any]:
    """Operating effort for the accepted cohort, without manufacturing human hours.

    Task actions and runs are included through each member's acceptance. Taskless operator
    ledger rows must name the cohort's product and phase and fall inside its first-dispatch
    to last-acceptance envelope. That is the narrowest attribution available for historical
    session records; rows without both labels remain visible as unattributed coverage.
    """
    cohort = acceptance_cohort(events, tasks, since=since, until=until)
    members = {row["id"]: timestamp(row["accepted_at"]) for row in cohort["tasks"]}
    histories: dict[str, list[dict[str, Any]]] = {}
    starts: list[dt.datetime] = []
    leads: list[float] = []
    for tid, accepted in members.items():
        life = [event for event in events if str(event.get("task") or "") == tid
                and (at := timestamp(event.get("at"))) is not None and at <= accepted]
        histories[tid] = life
        dispatched = [timestamp(event.get("at")) for event in life if event.get("kind") == "dispatch"]
        dispatched = [at for at in dispatched if at is not None]
        if dispatched:
            start = min(dispatched)
            starts.append(start)
            leads.append((accepted - start).total_seconds())

    actions = [event for life in histories.values() for event in life
               if event.get("kind") in EFFORT_ACTION_KINDS]
    action_rows: dict[str, dict[str, Any]] = {}
    for actor in EFFORT_ACTORS:
        selected = [event for event in actions
                    if str(event.get("actor") or
                           ("automated_scheduler" if event.get("kind") == "automerged" else "unknown")) == actor]
        causes = defaultdict(int)
        for event in selected:
            causes[str(event.get("reason") or event.get("kind") or "unknown")] += 1
        action_rows[actor] = {"actions": len(selected), "hours": None,
                              "hours_status": "unavailable", "causes": dict(sorted(causes.items()))}

    end = max((at for at in members.values() if at is not None), default=None)
    start = min(starts, default=None)
    products = {str(getattr(tasks[tid], "product", "")) for tid in members}
    phases = {str(getattr(tasks[tid], "key", "")) for tid in members}
    operator_rows: list[dict[str, Any]] = []
    unattributed_rows: list[dict[str, Any]] = []
    if start is not None and end is not None:
        for event in events:
            at = timestamp(event.get("at"))
            if (event.get("kind") != "run_finished" or event.get("mode") != "operator"
                    or at is None or not start <= at <= end):
                continue
            if not event.get("product") or not event.get("phase"):
                unattributed_rows.append(event)
            elif (str(event.get("product")) in products
                  and attributed_phase_key(event) in phases):
                operator_rows.append(event)

    task_runs = [event for life in histories.values() for event in life
                 if event.get("kind") == "run_finished"]
    priced = [event for event in task_runs + operator_rows
              if isinstance(event.get("cost_usd"), (int, float))
              and not isinstance(event.get("cost_usd"), bool)]
    all_cost_rows = task_runs + operator_rows
    known_cost = round(sum(float(event["cost_usd"]) for event in priced), 4)
    return {
        "accepted": len(members), "task_ids": sorted(members),
        "elapsed": {"tasks_with_lead_time": len(leads),
                    "median_lead_hours": round(median(leads) / 3600, 2) if leads else None,
                    "total_lead_hours": round(sum(leads) / 3600, 2) if leads else None},
        "actions": action_rows,
        "cost": {"known_usd": known_cost, "priced_records": len(priced),
                 "unpriced_records": len(all_cost_rows) - len(priced),
                 "complete": len(priced) == len(all_cost_rows),
                 "per_accepted_change": (round(known_cost / len(members), 4)
                                         if members and len(priced) == len(all_cost_rows) else None)},
        "operator": {"priced_records": sum(event in priced for event in operator_rows),
                     "unpriced_records": sum(event not in priced for event in operator_rows),
                     "unattributed_records": len(unattributed_rows)},
        "savings": None,
        "savings_status": "not_estimated_without_a_human_effort_baseline",
        "contract": cohort["contract"] + "; attributed operator rows in cohort effort envelope",
    }


def timestamp(value: Any) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t.replace(tzinfo=dt.UTC) if t.tzinfo is None else t.astimezone(dt.UTC)
    except (ValueError, TypeError):
        return None


def base_acceptance(event: dict, merged_tasks: set[str] | None = None) -> bool:
    """Return whether a done transition proves the task reached its base branch.

    New transitions carry explicit provenance.  The note forms are retained only for logs
    written before that field existed; an otherwise unannotated transition is accepted only
    when the event stream also contains the garden's merge fact for that task.
    """
    if event.get("kind") != "transition" or event.get("to") != "done":
        return False
    if event.get("base_merged") is not None:
        return event.get("base_merged") is True
    note = str(event.get("note") or "")
    if note.startswith("PR merged") or (note.startswith("parent ") and
                                        "this task's commits are now on " in note):
        return True
    # Before base_merged was recorded, merge transitions sometimes had no note at all.
    # That shape is only a merge when the event stream supplies its matching merge fact.
    return not note and bool(merged_tasks and str(event.get("task") or "") in merged_tasks)


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
    merge_facts = {str(e.get("task")) for e in history if e.get("kind") == "automerged" and e.get("task")}
    for e in history:
        tid = e.get("task", "")
        if tid in tasks:
            lives[tid].append(e)
            at = timestamp(e["at"])
            if base_acceptance(e, merge_facts) and start <= at:
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
