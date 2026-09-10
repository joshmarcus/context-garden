"""The Costs page: spend over time, sliceable by activity, difficulty, model, harness,
phase and task — the same numbers `garden costs` prints for the same filters."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ... import operator_spend as ops
from ...charts import cost_per_task_svg, cost_stack_svg
from ...costs import GROUP_BY_CHOICES, cost_series
from ...events import EventLog, metrics, parse_since
from ...outcomes import canonical_phase_key
from ..common import Site


def resolve_since(since: str) -> str:
    """A window select's value -> an ISO timestamp `cost_series` filters on: 'today' is
    today's UTC midnight (not a relative offset, so it survives across ticks), anything
    else is `events.parse_since` ('24h', '3d', an ISO timestamp, or '' for all time)."""
    if since == "today":
        return dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return parse_since(since) if since else ""


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
        tasks = s.tasks()
        events = EventLog(s.config.garden_dir / "events.jsonl").read()
        operator_records = ops.read_records(ops.default_path(s.root, s.config))
        events = events + ops.to_cost_events(operator_records)
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
        outcomes = metrics(events, selected_tasks, since=window_since)
        runs = [e for e in events if e.get("kind") == "run_finished"]
        models = sorted({str(e["model"]) for e in runs if e.get("model")})
        harnesses = sorted({str(e["harness"]) for e in runs if e.get("harness")})
        task_ids = sorted({str(e["task"]) for e in runs if e.get("task")})
        session_ids = sorted({str(e["session"]) for e in runs if e.get("session")})
        phase_keys = [ph.key for p in s.products() for ph in p.phases]
        product_names = [p.name for p in s.products()]
        compactions = ops.compaction_marks(operator_records)
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
            request, page="costs", series=series,
            outcomes=outcomes,
            chart=chart,
            since=since, bucket=bucket, by=by, difficulty=difficulty, model=model, harness=harness,
            phase=phase, product=product, task=task, session=session, metric=metric, models=models,
            harnesses=harnesses, task_ids=task_ids,
            session_ids=session_ids, phase_keys=phase_keys, product_names=product_names))
