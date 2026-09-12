"""Web and JSON operations for closed-task defect annotations."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ...defects import DefectConflict, DefectStore
from ...members import Principal
from ..common import Site, _flash_url


def _actor(request: Request, fallback: str) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.member_id if isinstance(principal, Principal) else fallback


def _create(store: Any, task_id: str, severity: str, description: str, reporter: str,
            idempotency_key: str, fields: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    task = store.task(task_id)
    return DefectStore(store.config.garden_dir).create(
        task, severity, description, reporter, idempotency_key=idempotency_key, **fields
    )


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.post("/tasks/{task_id}/defects")
    def create_defect_form(
        request: Request, task_id: str, severity: str = Form(...),
        description: str = Form(...), expected: str = Form(""), observed: str = Form(""),
        impact: str = Form(""), evidence_links: str = Form(""),
        affected_source: str = Form(""), affected_release: str = Form(""),
        affected_run: str = Form(""), follow_up: str = Form(""),
        idempotency_key: str = Form(""),
    ) -> RedirectResponse:
        try:
            with hub.action_lock:
                defect, created = _create(
                    hub.fresh(), task_id, severity, description, _actor(request, "web"),
                    idempotency_key or uuid.uuid4().hex,
                    {"expected": expected, "observed": observed, "impact": impact,
                     "evidence_links": evidence_links, "affected_source": affected_source,
                     "affected_release": affected_release, "affected_run": affected_run,
                     "follow_up": follow_up},
                )
        except KeyError:
            raise HTTPException(404) from None
        except (ValueError, DefectConflict) as exc:
            return RedirectResponse(_flash_url(f"/tasks/{task_id}", str(exc)), status_code=303)
        message = f"recorded {defect['id']}" if created else f"already recorded {defect['id']}"
        return RedirectResponse(_flash_url(f"/tasks/{task_id}", message), status_code=303)

    @app.get("/api/defects")
    def list_defects(
        request: Request, severity: str = "", product: str = "", phase: str = "",
        task_id: str = "", disposition: str = "", discovered_from: str = "",
        discovered_to: str = "",
    ) -> dict[str, Any]:
        ledger = DefectStore(hub.fresh().config.garden_dir)
        filters = {"severity": severity, "product": product, "phase": phase,
                   "task_id": task_id, "disposition": disposition,
                   "discovered_from": discovered_from, "discovered_to": discovered_to,
                   "allowed_projects": site.allowed_projects(request)}
        rows = ledger.list(**filters)
        return {"defects": rows, "summary": ledger.summary(**filters)}

    @app.post("/api/tasks/{task_id}/defects", status_code=201)
    def create_defect_api(request: Request, task_id: str,
                          payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            with hub.action_lock:
                defect, created = _create(
                    hub.fresh(), task_id, str(payload.get("severity") or ""),
                    str(payload.get("description") or ""), _actor(request, "api"),
                    str(payload.get("idempotency_key") or ""),
                    {name: value for name, value in payload.items()
                     if name not in {"severity", "description", "idempotency_key", "reporter"}},
                )
        except KeyError:
            raise HTTPException(404) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except DefectConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return {"defect": defect, "created": created}

    @app.patch("/api/tasks/{task_id}/defects/{defect_id}")
    def update_defect_api(request: Request, task_id: str, defect_id: str,
                          payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            expected = payload.get("expected_revision")
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise ValueError("expected_revision must be an integer")
            changes = {name: value for name, value in payload.items()
                       if name not in {"expected_revision", "actor"}}
            with hub.action_lock:
                ledger = DefectStore(hub.fresh().config.garden_dir)
                current = ledger.get(defect_id)
                if current.get("task_id") != task_id:
                    raise KeyError(defect_id)
                defect = ledger.update(defect_id, _actor(request, "api"), expected, **changes)
        except KeyError:
            raise HTTPException(404) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except DefectConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return {"defect": defect}
