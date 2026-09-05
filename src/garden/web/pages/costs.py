"""The Costs page: spend over time, sliceable by activity, difficulty, model, harness,
phase and task — the same numbers `garden costs` prints for the same filters."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...charts import cost_stack_svg
from ...costs import GROUP_BY_CHOICES, cost_series
from ...events import EventLog, parse_since
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
        difficulty: str = "", model: str = "", harness: str = "", phase: str = "", task: str = "",
    ):
        s = hub.fresh()
        tasks = s.tasks()
        events = EventLog(s.config.garden_dir / "events.jsonl").read()
        by = by if by in GROUP_BY_CHOICES else "activity"
        bucket = bucket if bucket in ("day", "hour") else "day"
        series = cost_series(events, tasks, since=resolve_since(since), bucket=bucket, group_by=by,
                             difficulty=difficulty, model=model, harness=harness, phase=phase, task=task)
        runs = [e for e in events if e.get("kind") == "run_finished"]
        models = sorted({str(e["model"]) for e in runs if e.get("model")})
        harnesses = sorted({str(e["harness"]) for e in runs if e.get("harness")})
        task_ids = sorted({str(e["task"]) for e in runs if e.get("task")})
        phase_keys = [ph.key for p in s.products() for ph in p.phases]
        return templates.TemplateResponse(request, "costs.html", ctx(
            request, page="costs", series=series, chart=cost_stack_svg(series),
            since=since, bucket=bucket, by=by, difficulty=difficulty, model=model, harness=harness,
            phase=phase, task=task, models=models, harnesses=harnesses, task_ids=task_ids, phase_keys=phase_keys))
