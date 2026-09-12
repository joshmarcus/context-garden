"""The Costs page: spend over time, sliceable by activity, difficulty, model, harness,
phase and task — the same numbers `garden costs` prints for the same filters."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import now1
from ... import operator_spend as ops
from ...charts import cost_per_task_svg, cost_stack_svg
from ...costs import GROUP_BY_CHOICES, cost_series
from ...events import EventLog, difficulty_by_model, metrics, parse_since
from ...outcomes import canonical_phase_key
from ..common import Site

FORMAT = SimpleNamespace(cell=now1.format_cell)


def resolve_since(since: str) -> str:
    """A window select's value -> an ISO timestamp `cost_series` filters on: 'today' is
    today's UTC midnight (not a relative offset, so it survives across ticks), anything
    else is `events.parse_since` ('24h', '3d', an ISO timestamp, or '' for all time)."""
    if since == "today":
        return dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return parse_since(since) if since else ""


def comparison_events(events: list[dict[str, Any]], tasks: dict[str, Any],
                      *, model: str = "", harness: str = "", session: str = "") -> list[dict[str, Any]]:
    """Keep a comparison cohort's lifecycle facts while filtering its completed runs."""
    scoped = [event for event in events if str(event.get("task") or "") in tasks]
    if not (model or harness or session):
        return scoped
    return [
        event for event in scoped
        if event.get("kind") != "run_finished"
        or (not model or str(event.get("model") or "") == model)
        and (not harness or str(event.get("harness") or "") == harness)
        and (not session or str(event.get("session") or "") == session)
    ]


def comparison_data(events: list[dict[str, Any]], tasks: dict[str, Any], since: str,
                    *, model: str = "", harness: str = "", session: str = "") -> dict[str, object]:
    """The Now comparison tables, scoped to the Costs cohort and its run filters.

    Lifecycle facts stay with their task so accepted-task comparisons retain their history;
    model, harness and session restrict the runs that supply spend and model credit.
    """
    scoped = comparison_events(events, tasks, model=model, harness=harness, session=session)
    finished = [
        event for event in scoped
        if event.get("kind") == "run_finished" and str(event.get("at") or "") >= since
    ]
    return {"by_model": now1.runs_by_model(finished),
            "tiers": difficulty_by_model(scoped, tasks, since)}


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/costs", response_class=HTMLResponse)
    def costs_page(
        request: Request, since: str = "", bucket: str = "day", by: str = "activity",
        difficulty: str = "", model: str = "", harness: str = "", phase: str = "", product: str = "",
        task: str = "", session: str = "",
        metric: str = "total",
    ):
        s = hub.fresh()
        allowed = site.allowed_projects(request)
        if product and allowed is not None and product not in allowed:
            raise HTTPException(403, "project is not visible to this member")
        tasks = site.visible_tasks(request, s)
        events = EventLog(s.config.garden_dir / "events.jsonl").read()
        operator_records = ops.read_records(ops.default_path(s.root, s.config))
        events = events + ops.to_cost_events(operator_records)
        events = site.visible_events(request, events, tasks)
        by = by if by in GROUP_BY_CHOICES else "activity"
        bucket = bucket if bucket in ("day", "hour") else "day"
        metric = metric if metric in ("total", "per_task") else "total"
        window_since = resolve_since(since)
        series = cost_series(events, tasks, since=window_since, bucket=bucket, group_by=by,
                             difficulty=difficulty, model=model, harness=harness, phase=phase, product=product, task=task,
                             session=session)
        selected_phase = canonical_phase_key(product, phase)
        selected_tasks = {tid: t for tid, t in tasks.items()
                          if (not product or t.product == product)
                          and (not selected_phase or t.key == selected_phase)}
        if difficulty:
            selected_tasks = {tid: t for tid, t in selected_tasks.items() if t.difficulty == difficulty}
        if task:
            selected_tasks = {tid: t for tid, t in selected_tasks.items() if tid == task}
        filtered_comparison_events = comparison_events(
            events, selected_tasks, model=model, harness=harness, session=session,
        )
        comparison = comparison_data(
            events, selected_tasks, window_since, model=model, harness=harness, session=session,
        )
        outcomes = metrics(filtered_comparison_events, selected_tasks, since=window_since)
        runs = [e for e in events if e.get("kind") == "run_finished"]
        models = sorted({str(e["model"]) for e in runs if e.get("model")})
        harnesses = sorted({str(e["harness"]) for e in runs if e.get("harness")})
        task_ids = sorted({str(e["task"]) for e in runs if e.get("task")})
        session_ids = sorted({str(e["session"]) for e in runs if e.get("session")})
        visible_products = [p for p in s.products() if allowed is None or p.name in allowed]
        phase_keys = [ph.key for p in visible_products for ph in p.phases]
        product_names = [p.name for p in visible_products]
        compactions = ops.compaction_marks(operator_records) if allowed is None else []
        annotations = [
            {"at": e.get("at"), "from": e.get("from"), "to": e.get("to")}
            for e in events
            if e.get("kind") == "profile_changed" and (not window_since or str(e.get("at") or "") >= window_since)
        ]
        chart = (
            cost_per_task_svg(series)
            if metric == "per_task"
            else cost_stack_svg(series, compactions=compactions, annotations=annotations)
        )
        return templates.TemplateResponse(request, "costs.html", ctx(
            request, page="costs", f=FORMAT, series=series,
            outcomes=outcomes,
            comparison=comparison,
            chart=chart,
            since=since, bucket=bucket, by=by, difficulty=difficulty, model=model, harness=harness,
            phase=phase, product=product, task=task, session=session, metric=metric, models=models,
            harnesses=harnesses, task_ids=task_ids,
            session_ids=session_ids, phase_keys=phase_keys, product_names=product_names))
