"""Now 2: render the shared read model and stream keyed fragments."""
from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from ...now2 import snapshot
from ...now2_stream import Fragments, stream, versions
from ...outcomes import format_cell
from ...store import Store
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    def data(window: str, phase: str) -> dict:
        # A separate task cache and accepted config: never invalidate the hub's store.
        s = Store(site.hub.store.root, config=site.hub.store.config)
        return snapshot(s, window, phase)

    def render_fragments(window: str, phase: str) -> dict[str, str]:
        d = data(window, phase)
        context = {"d": d, "fmt": format_cell, "watch": site.hub.watch}
        result = {f"now2-{name}": site.templates.env.get_template(f"_now2_{name}.html").render(**context)
                  for name in ("summary", "attention", "next", "where", "period")}
        for row in d["running"]:
            r = row["run"]
            result[f"run-{r.task_id}-{r.run_id}"] = site.templates.env.get_template("_now2_run.html").render(row=row)
        return result

    @app.get("/now2", response_class=HTMLResponse)
    def now2(request: Request, window: str = "hour", phase: str = ""):
        return site.templates.TemplateResponse(request, "now2.html", site.ctx(
            request, page="now2", d=data(window, phase), fmt=format_cell))

    @app.get("/now2/time")
    def now2_time():
        return {"now": dt.datetime.now(dt.UTC).isoformat()}

    @app.get("/api/events/now2")
    def now2_events(request: Request, window: str = "hour", phase: str = ""):
        fragments = Fragments(lambda: render_fragments(window, phase))
        return StreamingResponse(stream(fragments, lambda: versions(site.hub.store.root, site.hub.store.config.garden_dir),
                                        request.is_disconnected), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/now2/period", response_class=HTMLResponse)
    def now2_period(request: Request, window: str = "hour", phase: str = ""):
        return HTMLResponse(site.templates.env.get_template("_now2_period.html").render(d=data(window, phase), fmt=format_cell))
