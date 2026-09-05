"""The Timeline and metrics."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...charts import tier_bars_svg
from ...events import EventLog, digest, metrics, parse_since
from ..common import Site, tier_rows


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/events", response_class=HTMLResponse)
    def events_page(request: Request, since: str = "24h"):
        s = hub.fresh()
        since_iso = parse_since(since) if since else ""
        evs = EventLog(s.config.garden_dir / "events.jsonl").read(since=since_iso)
        d = digest(evs)
        tasks = s.tasks()
        return templates.TemplateResponse(request, "events.html", ctx(
            request, page="events", events=list(reversed(evs))[:300], digest=d, since=since, tasks=tasks,
            metrics=metrics(EventLog(s.config.garden_dir / "events.jsonl").read(), tasks), tiers=tier_bars_svg(tier_rows(s, tasks))))
