"""Isolated anonymous viewer application for a public projection directory."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from ..publication import SCHEMA_VERSION

PUBLIC_ROUTES = frozenset({
    "/", "/healthz", "/api/projects", "/api/search", "/events", "/export.json",
})
PUBLIC_PREFIXES = ("/projects/", "/phases/", "/tasks/")


class ProjectionUnavailable(RuntimeError):
    pass


def _load(root: Path) -> tuple[dict[str, Any], str]:
    path = root / "projection.json"
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionUnavailable("public garden is temporarily unavailable") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION or not isinstance(data.get("projects"), list):
        raise ProjectionUnavailable("public garden projection is incompatible")
    return data, hashlib.sha256(raw).hexdigest()


def _find(data: dict[str, Any], kind: str, identifiers: tuple[str, ...]) -> dict[str, Any] | None:
    projects = data["projects"]
    project = next((item for item in projects if item.get("id") == identifiers[0]), None)
    if kind == "project" or project is None:
        return project
    phase = next((item for item in project.get("phases", []) if item.get("id") == identifiers[1]), None)
    if kind == "phase" or phase is None:
        return phase
    return next((item for item in phase.get("tasks", []) if item.get("id") == identifiers[2]), None)


def _page(title: str, item: dict[str, Any], links: list[tuple[str, str]] | None = None) -> str:
    fields = [f"<h1>{html.escape(title)}</h1>"]
    for key in ("summary", "title", "status", "content"):
        if item.get(key):
            fields.append(f"<p>{html.escape(str(item[key]))}</p>")
    fields.extend(f'<p><a href="{html.escape(url)}">{html.escape(label)}</a></p>' for label, url in (links or []))
    return "<!doctype html><meta name=viewport content='width=device-width'><title>" + html.escape(title) + "</title>" + "".join(fields)


async def projection_events(root: Path, interval: float = 1) -> AsyncIterator[str]:
    """Stream replacements; a subscriber sees revocation rather than a cached old view."""
    previous = ""
    while True:
        try:
            data, revision = _load(root)
            if revision != previous:
                yield f"event: projection\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
                previous = revision
        except ProjectionUnavailable:
            yield "event: unavailable\ndata: {}\n\n"
            return
        await asyncio.sleep(interval)


def create_public_app(projection_dir: Path) -> FastAPI:
    """Create a viewer with no Store, Hub, scheduler, worker ingress, or private mounts."""
    root = projection_dir.resolve()
    app = FastAPI(title="public context-garden", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def positive_route_policy(request: Request, call_next: Any):
        path = request.url.path
        allowed_path = path in PUBLIC_ROUTES or path.startswith(PUBLIC_PREFIXES)
        if request.method not in {"GET", "HEAD"} or not allowed_path:
            return PlainTextResponse("not found", status_code=404)
        # Identity/scope selectors have no meaning for anonymous publication and must not
        # become an accidental authority channel. Search owns the sole accepted query key.
        allowed_query = {"q"} if path == "/api/search" else set()
        if set(request.query_params) - allowed_query:
            return PlainTextResponse("not found", status_code=404)
        return await call_next(request)

    def snapshot() -> tuple[dict[str, Any], str]:
        return _load(root)

    @app.exception_handler(ProjectionUnavailable)
    async def unavailable(_request: Request, exc: ProjectionUnavailable):
        return PlainTextResponse(str(exc), status_code=503)

    @app.get("/healthz")
    async def healthz():
        snapshot()
        return {"ok": True, "mode": "public-viewer"}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        data, _ = snapshot()
        links = [(str(p["id"]), f"/projects/{p['id']}") for p in data["projects"]]
        message = "Nothing is published." if not links else "Published projects"
        return HTMLResponse(_page(message, {}, links))

    @app.get("/projects/{project}", response_class=HTMLResponse)
    async def project_page(project: str):
        data, _ = snapshot()
        item = _find(data, "project", (project,))
        if item is None:
            return PlainTextResponse("not found", status_code=404)
        links = [(str(p["id"]), f"/phases/{project}/{p['id']}") for p in item["phases"]]
        return HTMLResponse(_page(project, item, links))

    @app.get("/phases/{project}/{phase}", response_class=HTMLResponse)
    async def phase_page(project: str, phase: str):
        data, _ = snapshot()
        item = _find(data, "phase", (project, phase))
        if item is None:
            return PlainTextResponse("not found", status_code=404)
        links = [(str(t["id"]), f"/tasks/{project}/{phase}/{t['id']}") for t in item["tasks"]]
        return HTMLResponse(_page(phase, item, links))

    @app.get("/tasks/{project}/{phase}/{task}", response_class=HTMLResponse)
    async def task_page(project: str, phase: str, task: str):
        data, _ = snapshot()
        item = _find(data, "task", (project, phase, task))
        return (HTMLResponse(_page(task, item)) if item is not None
                else PlainTextResponse("not found", status_code=404))

    @app.get("/api/projects")
    async def projects_api():
        return JSONResponse(snapshot()[0])

    @app.get("/api/search")
    async def search_api(q: str = ""):
        data, _ = snapshot()
        needle = q.casefold().strip()
        results = []
        for project in data["projects"]:
            for phase in project["phases"]:
                for task in phase["tasks"]:
                    searchable = " ".join(str(task.get(k, "")) for k in ("id", "title", "content"))
                    if needle and needle in searchable.casefold():
                        results.append(task)
        return {"results": results}

    @app.get("/export.json")
    async def export():
        return JSONResponse(snapshot()[0], headers={"Cache-Control": "no-store"})

    @app.get("/events")
    async def event_stream():
        return StreamingResponse(
            projection_events(root), media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    return app
