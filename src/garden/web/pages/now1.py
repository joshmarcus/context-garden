"""Now: the live view of what is running, what is next, where the phase is and the last
period (docs/design/now-1.md). The route renders `now1.snapshot`; each region is also a
partial the page re-fetches when the stream says it changed; the stream is server-sent
events off the tick's path."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from markupsafe import Markup
from starlette.requests import ClientDisconnect

from ... import now1
from ...charts import cost_stack_svg, sparkline_svg
from ...runs import RunStore
from ...workers import snapshot as worker_snapshot
from ..common import Site

WINDOW_KEYS = {key for key, _ in now1.WINDOWS}
REGIONS = ("head", "now", "next", "where", "period")
_monotonic = time.monotonic


class SSEStreamingResponse(StreamingResponse):
    """End an SSE response quietly when its client or server goes away.

    Streaming responses run after the route handler has returned, so these expected
    lifecycle signals do not pass through the app's ordinary exception handler. Keep
    the exception boundary narrow: application errors from the iterator or response
    still need to reach the server's error reporting.
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        except (asyncio.CancelledError, ClientDisconnect):
            return


def _chart(p: dict[str, Any], width: int = 640) -> Markup:
    marks = [a for a in p["annotations"] if a["kind"] == "profile_changed"]
    return Markup(cost_stack_svg(p["series"], width=width, annotations=marks))


# The formatters the templates use, passed in the context rather than registered on the
# shared environment, so this page's names never collide with another's.
FORMAT = SimpleNamespace(
    clock=now1.clock, minutes=now1.minutes, money=now1.money, ktok=now1.ktok, per_merge=now1.per_merge,
    cell=now1.format_cell, short=now1.short_title, chart=_chart, quiet_period=now1.QUIET_PERIOD,
    spark=lambda values: Markup(sparkline_svg([float(v) for v in values], width=100, height=26)),
)


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx
    # One stream event can make the browser request several regions at once.  They are all
    # views of the same page reading, so build that reading once and let the burst share it.
    # The short completed-at TTL only joins overlapping requests; a later event gets a fresh
    # reading, while head and period cannot drift onto different run/event snapshots.
    partial_lock = threading.Lock()
    partial_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
    typical_lock = threading.Lock()
    typical_cache: tuple[float, dict[str, float]] | None = None
    burst_seconds = 0.25

    def snap(request: Request, window: str) -> dict[str, Any]:
        s = hub.fresh()
        projects = site.allowed_projects(request)
        return now1.snapshot(s, hub.reader(), window=window if window in WINDOW_KEYS else "hour",
                             tick=hub.tick_state(), projects=projects)

    def page_ctx(request: Request, window: str, **kw: Any) -> dict[str, Any]:
        return ctx(request, page="now", f=FORMAT, snap=snap(request, window), **kw)

    def partial_snap(request: Request, window: str, burst: str) -> dict[str, Any]:
        with partial_lock:
            # Sample after acquiring the lock.  If another event arrived while this request
            # waited for an earlier build, its log/state signature forces a follow-up build
            # instead of serving the earlier event's completed-at cache entry.
            signatures = []
            for path in (hub.store.config.garden_dir / "events.jsonl",
                         hub.store.config.garden_dir / "state.json"):
                try:
                    stat = path.stat()
                    signatures.extend((stat.st_mtime_ns, stat.st_size))
                except OSError:
                    signatures.extend((0, 0))
            selected = window if window in WINDOW_KEYS else "hour"
            # The browser gives every event fanout an explicit id.  That is the operation
            # boundary: all regions for one event share a reading even when the server cannot
            # schedule their requests inside the fallback TTL, while a later event cannot
            # reuse it merely because it arrived quickly.
            allowed = site.allowed_projects(request)
            key = (selected, burst, tuple(sorted(allowed)) if allowed is not None else None, *signatures)
            cached = partial_cache.get(key)
            if cached is not None and (bool(burst) or cached[0] >= _monotonic()):
                return cached[1]
            reading = snap(request, selected)
            partial_cache.clear()
            partial_cache[key] = (_monotonic() + burst_seconds, reading)
            return reading

    def partial_ctx(request: Request, window: str, burst: str, **kw: Any) -> dict[str, Any]:
        # Region templates use only request, the Now formatters and the snapshot.  Rebuilding
        # Site.ctx here would also rebuild the global rail and Inbox even though neither is in
        # a partial response.
        return {"request": request, "f": FORMAT, "snap": partial_snap(request, window, burst), **kw}

    def cached_typical(runs: RunStore) -> dict[str, float]:
        nonlocal typical_cache
        with typical_lock:
            if typical_cache is not None and typical_cache[0] >= _monotonic():
                return typical_cache[1]
            import datetime as dt

            reading = now1.typical_seconds(runs.all_runs(), dt.datetime.now(dt.UTC))
            typical_cache = (_monotonic() + burst_seconds, reading)
            return reading

    @app.get("/now", response_class=HTMLResponse)
    def now_page(request: Request, window: str = "hour"):
        return templates.TemplateResponse(request, "now1.html", page_ctx(request, window))

    @app.get("/now/workers", response_class=HTMLResponse)
    def workers_page(request: Request):
        s = hub.fresh()
        fleet = worker_snapshot(s.config, RunStore(s.config.garden_dir))
        return templates.TemplateResponse(
            request, "now_workers.html", ctx(request, page="now", fleet=fleet)
        )

    @app.get("/now1", include_in_schema=False)
    @app.get("/now2", include_in_schema=False)
    def legacy_now_page(request: Request) -> RedirectResponse:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/now{query}", status_code=308)

    @app.get("/partials/now/{region}", response_class=HTMLResponse)
    def now1_partial(request: Request, region: str, window: str = "hour", burst: str = ""):
        if region not in REGIONS:
            raise HTTPException(404)
        return templates.TemplateResponse(request, f"_now1_{region}.html", partial_ctx(request, window, burst))

    @app.get("/partials/now/strip/{task_id}/{run_id}", response_class=HTMLResponse)
    def now1_strip(request: Request, task_id: str, run_id: str):
        """One strip, for a run the stream said arrived or finished; rendered from the run
        record, so a finished run carries its verdict and `data-stopped`."""
        s = hub.fresh()
        runs = RunStore(s.config.garden_dir)
        run = next((r for r in runs.runs_for(task_id) if r.run_id == run_id), None)
        if run is None:
            raise HTTPException(404)
        task = s.tasks().get(task_id)
        allowed = site.allowed_projects(request)
        if allowed is not None and (task is None or task.product not in allowed):
            raise HTTPException(403, "project is not visible to this member")
        typical = cached_typical(runs)
        strip = now1.strip_for_run(run, s.tasks(), s, typical)
        return templates.TemplateResponse(request, "_now1_strip.html",
                                          {"request": request, "f": FORMAT, "s": strip})

    @app.get("/now/stream")
    def now1_stream(request: Request, start: int | None = None, limit: int | None = None, seconds: float | None = None):
        """Server-sent events for the page: each new event log line, run progress and the
        tick. `start` (a byte offset into events.jsonl, 0 to replay), `limit` and `seconds`
        bound the stream for a test; a browser opens it unbounded and the server ends it when
        the tab goes away. Never holds the hub lock."""
        s = hub.fresh()
        deadline = time.monotonic() + seconds if seconds is not None else None
        body = now1.stream(s, hub.tick_state, start=start, limit=limit, deadline=deadline,
                           projects=site.allowed_projects(request))
        return SSEStreamingResponse(body, media_type="text/event-stream",
                                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
