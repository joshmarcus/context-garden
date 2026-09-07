"""JSON endpoints."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ...events import DECISION_KINDS, EventLog, decision_notifications
from ...graph import effective_status
from ...runs import Run
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.get("/api/tasks")
    def api_tasks():
        s = hub.fresh()
        tasks = s.tasks()
        stack = bool(s.config.get("stack", True))
        return JSONResponse([{**t.to_frontmatter(), "effective_status": effective_status(t, tasks, stack)} for t in tasks.values()])

    @app.get("/api/operations/{task_id}/{run_id}")
    def api_operation(task_id: str, run_id: str):
        """Read one durable launch identity directly; never scan run history."""
        if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in (task_id, run_id)):
            raise HTTPException(404)
        path = hub.store.config.garden_dir / "runs" / task_id / run_id
        try:
            run = Run.load(path)
        except (OSError, ValueError, TypeError):
            raise HTTPException(404) from None
        return JSONResponse({"operation_id": run.run_id, "task_id": run.task_id,
                             "state": run.lifecycle_state, "status": run.status,
                             "pid": run.pid if run.status == "running" else None,
                             "requested_at": run.started_at, "finished_at": run.finished_at,
                             "error": run.error})

    @app.get("/api/events")
    def api_events():
        return JSONResponse(hub.events[-50:])

    @app.get("/api/decisions")
    def api_decisions(since: str = ""):
        """The decision-kind events since a timestamp, each with a one-line title and the URL
        to open — what an open browser tab polls to notify a person that the loop needs them
        (CG-208). Notices never appear here; on `since` in the future it returns nothing."""
        s = hub.fresh()
        evs = EventLog(s.config.garden_dir / "events.jsonl").read(since=since, kinds=DECISION_KINDS)
        titles = {t.id: t.title for t in s.tasks().values()}
        return JSONResponse(decision_notifications(evs, titles))
