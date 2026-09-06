"""Now 1: the live view of what is running, what is next, where the phase is and the last
period (docs/design/now-1.md). The route renders `now1.snapshot`; each region is also a
partial the page re-fetches when the stream says it changed; the stream is server-sent
events off the tick's path."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from markupsafe import Markup

from ... import now1
from ...charts import cost_stack_svg, sparkline_svg
from ...runs import RunStore
from ..common import Site

WINDOW_KEYS = {key for key, _ in now1.WINDOWS}
REGIONS = ("head", "now", "next", "where", "period")


def _chart(p: dict[str, Any], width: int = 640) -> Markup:
    marks = [a for a in p["annotations"] if a["kind"] == "profile_changed"]
    return Markup(cost_stack_svg(p["series"], width=width, annotations=marks))


# The formatters the templates use, passed in the context rather than registered on the
# shared environment, so this page's names never collide with another's.
FORMAT = SimpleNamespace(
    clock=now1.clock, minutes=now1.minutes, money=now1.money, ktok=now1.ktok, per_merge=now1.per_merge,
    cell=now1.format_cell, short=now1.short_title, chart=_chart,
    spark=lambda values: Markup(sparkline_svg([float(v) for v in values], width=100, height=26)),
)


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    def snap(window: str) -> dict[str, Any]:
        s = hub.fresh()
        return now1.snapshot(s, hub.reader(), window=window if window in WINDOW_KEYS else "hour", tick=hub.tick_state())

    def page_ctx(request: Request, window: str, **kw: Any) -> dict[str, Any]:
        return ctx(request, page="now1", f=FORMAT, snap=snap(window), **kw)

    @app.get("/now1", response_class=HTMLResponse)
    def now1_page(request: Request, window: str = "hour"):
        return templates.TemplateResponse(request, "now1.html", page_ctx(request, window))

    @app.get("/partials/now1/{region}", response_class=HTMLResponse)
    def now1_partial(request: Request, region: str, window: str = "hour"):
        if region not in REGIONS:
            raise HTTPException(404)
        return templates.TemplateResponse(request, f"_now1_{region}.html", page_ctx(request, window))

    @app.get("/partials/now1/strip/{task_id}/{run_id}", response_class=HTMLResponse)
    def now1_strip(request: Request, task_id: str, run_id: str):
        """One strip, for a run the stream said arrived or finished; rendered from the run
        record, so a finished run carries its verdict and `data-stopped`."""
        s = hub.fresh()
        runs = RunStore(s.config.garden_dir)
        run = next((r for r in runs.runs_for(task_id) if r.run_id == run_id), None)
        if run is None:
            raise HTTPException(404)
        import datetime as dt

        typical = now1.typical_seconds(runs.all_runs(), dt.datetime.now(dt.UTC))
        strip = now1.strip_for_run(run, s.tasks(), s, typical)
        return templates.TemplateResponse(request, "_now1_strip.html", ctx(request, page="now1", f=FORMAT, s=strip))

    @app.get("/now1/stream")
    def now1_stream(request: Request, start: int | None = None, limit: int | None = None, seconds: float | None = None):
        """Server-sent events for the page: each new event log line, run progress and the
        tick. `start` (a byte offset into events.jsonl, 0 to replay), `limit` and `seconds`
        bound the stream for a test; a browser opens it unbounded and the server ends it when
        the tab goes away. Never holds the hub lock."""
        s = hub.fresh()
        deadline = time.monotonic() + seconds if seconds is not None else None
        body = now1.stream(s, hub.tick_state, start=start, limit=limit, deadline=deadline)
        return StreamingResponse(body, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
