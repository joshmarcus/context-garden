"""Authenticated HTTP protocol for the shared multiplayer coordinator."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

from .coordination import Claim, CoordinationError, Coordinator, ProtocolMismatch
from .members import MemberRegistry, Principal


def create_coordination_app(garden_dir: Path) -> FastAPI:
    """Create the separately deployable coordinator service for one garden.

    TLS is an operator deployment concern, as with the existing web listener. Bearer secrets
    are verified by the private member registry and are never passed into the ledger.
    """
    app = FastAPI(title="context-garden coordinator", version="1")
    coordinator = Coordinator(garden_dir / "coordination.db")
    registry = MemberRegistry(garden_dir)

    def principal(authorization: str = Header(default="")) -> Principal:
        actor = registry.authenticate(authorization.removeprefix("Bearer "))
        if not authorization.startswith("Bearer ") or actor is None:
            raise HTTPException(401, "valid member bearer credential required")
        return actor

    @app.exception_handler(CoordinationError)
    async def coordination_error(_request: Any, exc: CoordinationError):
        from fastapi.responses import JSONResponse

        status = 426 if isinstance(exc, ProtocolMismatch) else 409
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.get("/v1/gardens/{garden_id}/snapshot")
    def snapshot(garden_id: str, actor: Principal = Depends(principal),
                 protocol_version: int = 1):
        value = coordinator.snapshot(actor, garden_id, protocol_version=protocol_version)
        assignment = registry.assignment(actor.member_id)
        value["assignment"] = asdict(assignment) if assignment is not None else None
        return value

    @app.post("/v1/gardens/{garden_id}/authority")
    def set_authority(garden_id: str, body: dict[str, Any],
                      actor: Principal = Depends(principal)):
        try:
            return coordinator.set_authority(actor, garden_id=garden_id, **body)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(403 if isinstance(exc, PermissionError) else 422, str(exc)) from None

    @app.post("/v1/gardens/{garden_id}/claims")
    def claim(garden_id: str, body: dict[str, Any],
              actor: Principal = Depends(principal)):
        try:
            return coordinator.claim(actor, garden_id=garden_id, **body)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(403 if isinstance(exc, PermissionError) else 422, str(exc)) from None

    @app.post("/v1/gardens/{garden_id}/transitions")
    def transition(garden_id: str, body: dict[str, Any],
                   actor: Principal = Depends(principal)):
        try:
            claim_value = Claim(**body.pop("claim"))
            if claim_value.garden_id != garden_id:
                raise PermissionError("claim belongs to another garden")
            return coordinator.transition(actor, claim_value, **body)
        except (PermissionError, ValueError, TypeError) as exc:
            raise HTTPException(403 if isinstance(exc, PermissionError) else 422, str(exc)) from None

    @app.post("/v1/gardens/{garden_id}/effects")
    def effect(garden_id: str, body: dict[str, Any],
               actor: Principal = Depends(principal)):
        try:
            claim_value = Claim(**body.pop("claim"))
            if claim_value.garden_id != garden_id:
                raise PermissionError("claim belongs to another garden")
            return coordinator.begin_effect(actor, claim_value, **body)
        except (PermissionError, ValueError, TypeError) as exc:
            raise HTTPException(403 if isinstance(exc, PermissionError) else 422, str(exc)) from None

    @app.post("/v1/gardens/{garden_id}/effects/{operation_id}/finish")
    def finish_effect(garden_id: str, operation_id: str, body: dict[str, Any],
                      actor: Principal = Depends(principal)):
        try:
            coordinator.finish_effect(actor, garden_id, operation_id, **body)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(403 if isinstance(exc, PermissionError) else 422, str(exc)) from None
        return {"status": body.get("outcome")}

    @app.post("/v1/gardens/{garden_id}/outbox/{outbox_id}/finish")
    def finish_outbox(garden_id: str, outbox_id: int, body: dict[str, Any],
                      actor: Principal = Depends(principal)):
        try:
            coordinator.finish_outbox(actor, garden_id=garden_id, outbox_id=outbox_id, **body)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(403 if isinstance(exc, PermissionError) else 422, str(exc)) from None
        return {"status": "done" if body.get("success") else "pending"}

    @app.post("/v1/gardens/{garden_id}/reservations")
    def reserve(garden_id: str, body: dict[str, Any],
                actor: Principal = Depends(principal)):
        try:
            claim_body = body.pop("claim", None)
            if claim_body is None:
                return coordinator.reserve(actor, garden_id=garden_id, **body)
            claim_value = Claim(**claim_body)
            if claim_value.garden_id != garden_id:
                raise PermissionError("claim belongs to another garden")
            return coordinator.reserve_phase(actor, claim_value, **body)
        except (PermissionError, ValueError, TypeError) as exc:
            raise HTTPException(403 if isinstance(exc, PermissionError) else 422, str(exc)) from None

    app.state.coordinator = coordinator
    return app
