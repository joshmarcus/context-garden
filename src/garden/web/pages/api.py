"""JSON endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ...graph import effective_status
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.get("/api/tasks")
    def api_tasks():
        s = hub.fresh()
        tasks = s.tasks()
        stack = bool(s.config.get("stack", True))
        return JSONResponse([{**t.to_frontmatter(), "effective_status": effective_status(t, tasks, stack)} for t in tasks.values()])

    @app.get("/api/events")
    def api_events():
        return JSONResponse(hub.events[-50:])
