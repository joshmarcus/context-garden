"""Accept or reject a worker's duplicate/cancel decision card, or answer/dismiss a kickoff
question card (CG-224)."""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.post("/decisions/{decision_id}/{action}")
    def decision_action(request: Request, decision_id: str, action: str, answer: str = Form("")):
        if action not in ("accept", "reject", "answer", "dismiss"):
            raise HTTPException(400, f"unknown action {action}")
        back = request.headers.get("referer", "")
        redirect_to = back if back.endswith("/") or back.endswith("/inbox") else "/inbox"
        with hub.action_lock:
            sched = hub.scheduler()
            try:
                if action == "answer":
                    sched.answer_question(decision_id, answer, by="web")
                elif action == "dismiss":
                    sched.dismiss_question(decision_id, by="web")
                else:
                    sched.resolve_decision(decision_id, accept=(action == "accept"))
            except RuntimeError as e:
                raise HTTPException(409, str(e)) from None
            except KeyError:
                raise HTTPException(404) from None
        return RedirectResponse(redirect_to, status_code=303)
