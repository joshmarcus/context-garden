"""Accept or reject a worker's duplicate/cancel decision card."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.post("/decisions/{decision_id}/{action}")
    def decision_action(request: Request, decision_id: str, action: str):
        if action not in ("accept", "reject"):
            raise HTTPException(400, f"unknown action {action}")
        with hub.action_lock:
            sched = hub.scheduler()
            try:
                sched.resolve_decision(decision_id, accept=(action == "accept"))
            except KeyError:
                raise HTTPException(404) from None
        back = request.headers.get("referer", "")
        return RedirectResponse(back if back.endswith("/") or back.endswith("/inbox") else "/inbox", status_code=303)
