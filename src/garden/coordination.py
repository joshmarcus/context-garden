"""Transactional authority and shared-effect coordination for multiplayer gardens.

The coordinator owns a SQLite database on one server.  Local schedulers talk to that
server (the HTTP client is introduced in CG-632); they must never copy this database or
infer authority from their Markdown checkout.  Every mutating request is authenticated
before it reaches this module and carries an idempotency identity.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .members import Principal, authorize

PROTOCOL_VERSION = 1


class CoordinationError(RuntimeError):
    """A safe, actionable coordinator refusal."""


class ProtocolMismatch(CoordinationError):
    pass


class Conflict(CoordinationError):
    """The caller's snapshot or fence is stale, or another operation is unresolved."""


@dataclass(frozen=True)
class Claim:
    garden_id: str
    kind: Literal["task", "phase"]
    scope: str
    owner_id: str
    authority_generation: int
    installation_id: str
    operation_id: str
    fence: int
    lease_expires_at: str


def _iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


class Coordinator:
    """One durable, transactional authority for independently running schedulers."""

    def __init__(self, path: Path, *, clock: Any | None = None):
        self.path = path
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS authority (
                    garden TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    version INTEGER NOT NULL, owner TEXT NOT NULL,
                    authority_generation INTEGER NOT NULL, next_fence INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (garden, kind, scope));
                CREATE TABLE IF NOT EXISTS claims (
                    garden TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    owner TEXT NOT NULL, authority_generation INTEGER NOT NULL,
                    installation TEXT NOT NULL, operation_id TEXT NOT NULL,
                    fence INTEGER NOT NULL, lease_expires_at TEXT NOT NULL,
                    PRIMARY KEY (garden, kind, scope), UNIQUE (garden, operation_id));
                CREATE TABLE IF NOT EXISTS operations (
                    garden TEXT NOT NULL, operation_id TEXT NOT NULL, operation_kind TEXT NOT NULL,
                    actor TEXT NOT NULL, installation TEXT NOT NULL, request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (garden, operation_id));
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, garden TEXT NOT NULL,
                    operation_id TEXT NOT NULL, effect_kind TEXT NOT NULL, scope TEXT NOT NULL,
                    authority_version INTEGER NOT NULL, fence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE (garden, operation_id, effect_kind));
                CREATE TABLE IF NOT EXISTS effects (
                    garden TEXT NOT NULL, provider TEXT NOT NULL, effect_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL, actor TEXT NOT NULL, installation TEXT NOT NULL,
                    fence INTEGER NOT NULL, credential_scope TEXT NOT NULL,
                    precondition_value TEXT NOT NULL, request_json TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL, PRIMARY KEY (garden, provider, effect_key),
                    UNIQUE (garden, operation_id));
                CREATE TABLE IF NOT EXISTS reservations (
                    garden TEXT NOT NULL, pool TEXT NOT NULL, operation_id TEXT NOT NULL,
                    units INTEGER NOT NULL, spend_micros INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (garden, pool, operation_id));
                CREATE TABLE IF NOT EXISTS evidence (
                    garden TEXT NOT NULL, evidence_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (garden, evidence_id));
                CREATE TABLE IF NOT EXISTS handoffs (
                    garden TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    from_owner TEXT NOT NULL, to_owner TEXT NOT NULL,
                    from_generation INTEGER NOT NULL, to_generation INTEGER NOT NULL,
                    authority_version INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (garden, kind, scope, to_generation));
                CREATE TABLE IF NOT EXISTS cancellations (
                    garden TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    installation TEXT NOT NULL, fence INTEGER NOT NULL,
                    status TEXT NOT NULL, requested_at TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (garden, kind, scope, installation, fence));
                CREATE TABLE IF NOT EXISTS projections (
                    garden TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    version INTEGER NOT NULL, path TEXT NOT NULL, markdown TEXT NOT NULL,
                    base_revision TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (garden, kind, scope));
            """)
            self._add_column(db, "effects", "kind", "TEXT NOT NULL DEFAULT 'task'")
            self._add_column(db, "effects", "scope", "TEXT NOT NULL DEFAULT ''")
            self._add_column(db, "effects", "authority_generation", "INTEGER NOT NULL DEFAULT 0")
            db.execute("""UPDATE effects SET
                kind=COALESCE((SELECT kind FROM claims WHERE claims.garden=effects.garden
                    AND claims.installation=effects.installation AND claims.fence=effects.fence),kind),
                scope=COALESCE((SELECT scope FROM claims WHERE claims.garden=effects.garden
                    AND claims.installation=effects.installation AND claims.fence=effects.fence),scope),
                authority_generation=COALESCE((SELECT authority_generation FROM claims
                    WHERE claims.garden=effects.garden AND claims.installation=effects.installation
                    AND claims.fence=effects.fence),authority_generation)
                WHERE scope=''""")

    @staticmethod
    def _add_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        if name not in {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _protocol(version: int) -> None:
        if version != PROTOCOL_VERSION:
            raise ProtocolMismatch(
                f"unsupported coordination protocol {version}; server requires {PROTOCOL_VERSION}"
            )

    @staticmethod
    def _garden(principal: Principal, garden_id: str) -> None:
        if principal.garden_id != garden_id:
            raise PermissionError("credential does not belong to this garden")

    def set_authority(self, principal: Principal, *, garden_id: str, kind: str, scope: str,
                      owner_id: str, authority_generation: int, expected_version: int,
                      operation_id: str, protocol_version: int = PROTOCOL_VERSION) -> dict[str, Any]:
        """CAS the owner projection after the membership registry has authorized its change."""
        self._protocol(protocol_version)
        self._garden(principal, garden_id)
        if not authorize(principal, "administer"):
            raise PermissionError("administrator role required")
        if kind not in {"task", "phase"} or not scope:
            raise ValueError("authority requires a valid kind and scope")
        request = {"kind": kind, "scope": scope, "owner_id": owner_id,
                   "authority_generation": authority_generation, "expected_version": expected_version}
        with self._transaction() as db:
            repeated = self._repeat(db, principal, garden_id, operation_id, "set_authority", request)
            if repeated is not None:
                return repeated
            row = db.execute("SELECT * FROM authority WHERE garden=? AND kind=? AND scope=?",
                             (garden_id, kind, scope)).fetchone()
            version = int(row["version"]) if row else 0
            if version != expected_version:
                raise Conflict(f"stale {kind} version: expected {expected_version}, current {version}")
            if row and authority_generation <= int(row["authority_generation"]):
                raise Conflict("stale authority generation")
            new_version = version + 1
            db.execute("""INSERT INTO authority(garden,kind,scope,version,owner,authority_generation)
                VALUES(?,?,?,?,?,?) ON CONFLICT(garden,kind,scope) DO UPDATE SET
                version=excluded.version,owner=excluded.owner,
                authority_generation=excluded.authority_generation""",
                (garden_id, kind, scope, new_version, owner_id, authority_generation))
            # Changing the generation fences the old claim immediately.  Admission remains
            # closed until the old installation and its externally visible effects have been
            # reconciled; this is deliberately separate from the new owner projection.
            if row:
                active = db.execute(
                    "SELECT * FROM claims WHERE garden=? AND kind=? AND scope=?",
                    (garden_id, kind, scope),
                ).fetchone()
                db.execute("""INSERT INTO handoffs VALUES(?,?,?,?,?,?,?,?,?,?,'')
                    ON CONFLICT(garden,kind,scope,to_generation) DO NOTHING""",
                    (garden_id, kind, scope, row["owner"], owner_id,
                     int(row["authority_generation"]), authority_generation, new_version,
                     "reconciling", _iso(self.clock())))
                if active:
                    db.execute("""INSERT OR REPLACE INTO cancellations
                        VALUES(?,?,?,?,?,'requested',?,'')""",
                        (garden_id, kind, scope, active["installation"], int(active["fence"]),
                         _iso(self.clock())))
            db.execute("DELETE FROM claims WHERE garden=? AND kind=? AND scope=?",
                       (garden_id, kind, scope))
            response = {"version": new_version, "owner_id": owner_id,
                        "authority_generation": authority_generation,
                        "handoff_status": "reconciling" if row else "ready"}
            self._record(db, principal, garden_id, operation_id, "set_authority", request, response)
            return response

    def claim(self, principal: Principal, *, garden_id: str, kind: Literal["task", "phase"],
              scope: str, expected_version: int, accepted_owner: str,
              authority_generation: int, operation_id: str, lease_seconds: int = 120,
              protocol_version: int = PROTOCOL_VERSION) -> Claim:
        """Acquire one fenced lease; expiration is evaluated only against server time."""
        self._protocol(protocol_version)
        self._garden(principal, garden_id)
        if principal.member_id != accepted_owner or principal.role == "viewer":
            raise PermissionError("claim owner must be the authenticated executing member")
        if kind not in {"task", "phase"} or not scope or not operation_id:
            raise ValueError("claim requires a valid kind, scope and operation id")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        request = {"kind": kind, "scope": scope, "expected_version": expected_version,
                   "accepted_owner": accepted_owner, "authority_generation": authority_generation,
                   "lease_seconds": lease_seconds}
        now = self.clock()
        with self._transaction() as db:
            repeated = self._repeat(db, principal, garden_id, operation_id, "claim", request)
            if repeated is not None:
                return Claim(**repeated)
            authority = db.execute(
                "SELECT * FROM authority WHERE garden=? AND kind=? AND scope=?",
                (garden_id, kind, scope),
            ).fetchone()
            if not authority:
                raise Conflict("authority has not been established")
            if (int(authority["version"]) != expected_version
                    or authority["owner"] != accepted_owner
                    or int(authority["authority_generation"]) != authority_generation):
                raise Conflict("stale authority snapshot")
            if not accepted_owner:
                raise Conflict("scope is unassigned")
            handoffs = db.execute(
                "SELECT * FROM handoffs WHERE garden=? AND kind=? AND scope=? "
                "AND status!='ready' ORDER BY to_generation",
                (garden_id, kind, scope),
            ).fetchall()
            for handoff in handoffs:
                blockers = self._handoff_blockers(db, handoff)
                if blockers:
                    raise Conflict("scope is in handoff reconciliation: " + ", ".join(blockers))
                db.execute("UPDATE handoffs SET status='ready',completed_at=? "
                           "WHERE garden=? AND kind=? AND scope=? AND to_generation=?",
                           (_iso(now), garden_id, kind, scope, handoff["to_generation"]))
            active = db.execute("SELECT * FROM claims WHERE garden=? AND kind=? AND scope=?",
                                (garden_id, kind, scope)).fetchone()
            if active and dt.datetime.fromisoformat(active["lease_expires_at"]) > now:
                raise Conflict("scope is waiting on another fenced operation")
            fence = int(authority["next_fence"]) + 1
            expires = _iso(now + dt.timedelta(seconds=lease_seconds))
            db.execute("UPDATE authority SET next_fence=? WHERE garden=? AND kind=? AND scope=?",
                       (fence, garden_id, kind, scope))
            db.execute("""INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(garden,kind,scope) DO UPDATE SET owner=excluded.owner,
                authority_generation=excluded.authority_generation,
                installation=excluded.installation,operation_id=excluded.operation_id,
                fence=excluded.fence,lease_expires_at=excluded.lease_expires_at""",
                (garden_id, kind, scope, accepted_owner, authority_generation,
                 principal.installation_id, operation_id, fence, expires))
            claim = Claim(garden_id, kind, scope, accepted_owner, authority_generation,
                          principal.installation_id, operation_id, fence, expires)
            self._record(db, principal, garden_id, operation_id, "claim", request, asdict(claim))
            return claim

    def snapshot(self, principal: Principal, garden_id: str, *,
                 protocol_version: int = PROTOCOL_VERSION) -> dict[str, Any]:
        """Return an authenticated versioned authority snapshot with useful wait states."""
        self._protocol(protocol_version)
        self._garden(principal, garden_id)
        with self._connect() as db:
            authority = [dict(row) for row in db.execute(
                "SELECT * FROM authority WHERE garden=? ORDER BY kind,scope", (garden_id,))]
            claims = [dict(row) for row in db.execute(
                "SELECT kind,scope,owner,authority_generation,installation,fence,lease_expires_at "
                "FROM claims WHERE garden=? ORDER BY kind,scope", (garden_id,))]
            pending = [dict(row) for row in db.execute(
                "SELECT effect_kind,scope,status,last_error FROM outbox WHERE garden=? AND status!='done'",
                (garden_id,))]
            effects = [dict(row) for row in db.execute(
                "SELECT provider,effect_key,status FROM effects WHERE garden=? AND status IN ('pending','unknown')",
                (garden_id,))]
            handoffs = [dict(row) for row in db.execute(
                "SELECT * FROM handoffs WHERE garden=? ORDER BY kind,scope", (garden_id,))]
            cancellations = [dict(row) for row in db.execute(
                "SELECT * FROM cancellations WHERE garden=? AND status='requested' "
                "ORDER BY kind,scope", (garden_id,))]
            evidence = [dict(row) for row in db.execute(
                "SELECT evidence_id,operation_id,payload_json,created_at FROM evidence "
                "WHERE garden=? ORDER BY created_at,evidence_id", (garden_id,))]
            projections = [dict(row) for row in db.execute(
                "SELECT kind,scope,version,path,markdown,base_revision FROM projections "
                "WHERE garden=? ORDER BY kind,scope", (garden_id,))]
        return {"protocol_version": PROTOCOL_VERSION, "garden_id": garden_id,
                "member_id": principal.member_id, "installation_id": principal.installation_id,
                "role": principal.role,
                "authority": authority, "active_claims": claims, "projections": projections,
                "pending_outbox": pending, "blocking_effects": effects,
                "handoffs": handoffs, "cancellation_requests": cancellations,
                "evidence": evidence}

    def transition(self, principal: Principal, claim: Claim, *, expected_version: int,
                   new_state: str, markdown: str, operation_id: str,
                   evidence: dict[str, Any] | None = None, path: str = "",
                   canonical_revision: str = "") -> dict[str, Any]:
        """Commit authority plus its Git projection journal atomically."""
        request = {"claim_operation_id": claim.operation_id, "expected_version": expected_version,
                   "new_state": new_state, "markdown": markdown, "evidence": evidence or {},
                   "path": path, "canonical_revision": canonical_revision}
        if path and not canonical_revision:
            raise ValueError("a projected transition requires its canonical revision")
        with self._transaction() as db:
            repeated = self._repeat(db, principal, claim.garden_id, operation_id, "transition", request)
            if repeated is not None:
                return repeated
            authority = self._validate_claim(db, principal, claim, expected_version)
            version = int(authority["version"]) + 1
            db.execute("UPDATE authority SET version=? WHERE garden=? AND kind=? AND scope=?",
                       (version, claim.garden_id, claim.kind, claim.scope))
            payload = {"state": new_state, "markdown": markdown, "owner": claim.owner_id,
                       "authority_generation": claim.authority_generation}
            if path:
                db.execute("""INSERT INTO projections VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(garden,kind,scope) DO UPDATE SET version=excluded.version,
                    path=excluded.path,markdown=excluded.markdown,
                    base_revision=excluded.base_revision,updated_at=excluded.updated_at""",
                    (claim.garden_id, claim.kind, claim.scope, version, path, markdown,
                     canonical_revision, _iso(self.clock())))
            for effect_kind in ("task_transition", "git_projection"):
                db.execute("""INSERT INTO outbox
                    (garden,operation_id,effect_kind,scope,authority_version,fence,payload_json,status,created_at)
                    VALUES(?,?,?,?,?,?,?,'pending',?)""",
                    (claim.garden_id, operation_id, effect_kind, claim.scope, version,
                     claim.fence, json.dumps(payload, sort_keys=True), _iso(self.clock())))
            if evidence:
                evidence_id = str(evidence.get("id") or operation_id)
                db.execute("INSERT INTO evidence VALUES(?,?,?,?,?)",
                           (claim.garden_id, evidence_id, operation_id,
                            json.dumps(evidence, sort_keys=True), _iso(self.clock())))
            response = {"version": version, "outbox_status": "pending"}
            self._record(db, principal, claim.garden_id, operation_id, "transition", request, response)
            return response

    def finish_outbox(self, principal: Principal, *, garden_id: str, outbox_id: int,
                      authority_version: int, success: bool, error: str = "") -> None:
        self._garden(principal, garden_id)
        if not authorize(principal, "administer"):
            raise PermissionError("projection gateway authority required")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=? AND garden=?",
                             (outbox_id, garden_id)).fetchone()
            if not row:
                raise KeyError(outbox_id)
            if int(row["authority_version"]) != authority_version:
                raise Conflict("stale projection completion")
            current = db.execute("SELECT version FROM authority WHERE garden=? AND kind='task' AND scope=?",
                                 (garden_id, row["scope"])).fetchone()
            if row["effect_kind"] == "git_projection" and current and int(current[0]) != authority_version:
                raise Conflict("stale Markdown cannot overwrite newer authority")
            status = "done" if success else "pending"
            db.execute("UPDATE outbox SET status=?,attempts=attempts+1,last_error=?,completed_at=? WHERE id=?",
                       (status, error, _iso(self.clock()) if success else "", outbox_id))

    def supersede_outbox(self, principal: Principal, *, garden_id: str,
                         outbox_id: int, authority_version: int) -> None:
        """Resolve a fenced old projection without ever publishing its payload."""
        self._garden(principal, garden_id)
        if not authorize(principal, "administer"):
            raise PermissionError("projection recovery authority required")
        with self._transaction() as db:
            row = db.execute("SELECT authority_version,status FROM outbox WHERE id=? AND garden=?",
                             (outbox_id, garden_id)).fetchone()
            if not row:
                raise KeyError(outbox_id)
            if int(row["authority_version"]) != authority_version:
                raise Conflict("stale projection reconciliation request")
            db.execute("UPDATE outbox SET status='superseded',attempts=attempts+1,"
                       "completed_at=? WHERE id=?", (_iso(self.clock()), outbox_id))

    def begin_effect(self, principal: Principal, claim: Claim, *, provider: str, effect_key: str,
                     operation_id: str, credential_scope: str, precondition: str,
                     request: dict[str, Any]) -> dict[str, Any]:
        """Serialize one provider mutation without persisting its delegated credential."""
        payload = {"provider": provider, "effect_key": effect_key, "credential_scope": credential_scope,
                   "precondition": precondition, "request": request}
        with self._transaction() as db:
            repeated = self._repeat(db, principal, claim.garden_id, operation_id, "effect", payload)
            if repeated is not None:
                return repeated
            self._validate_claim(db, principal, claim)
            existing = db.execute("SELECT * FROM effects WHERE garden=? AND provider=? AND effect_key=?",
                                  (claim.garden_id, provider, effect_key)).fetchone()
            if existing and existing["status"] in {"pending", "unknown"}:
                raise Conflict(f"provider effect is {existing['status']}; reconciliation required")
            now = _iso(self.clock())
            db.execute("""INSERT INTO effects
                (garden,provider,effect_key,operation_id,actor,installation,fence,
                 credential_scope,precondition_value,request_json,status,result_json,updated_at,
                 kind,scope,authority_generation)
                VALUES(?,?,?,?,?,?,?,?,?,?,'pending','{}',?,?,?,?)
                ON CONFLICT(garden,provider,effect_key) DO UPDATE SET
                operation_id=excluded.operation_id,actor=excluded.actor,installation=excluded.installation,
                fence=excluded.fence,credential_scope=excluded.credential_scope,
                precondition_value=excluded.precondition_value,request_json=excluded.request_json,
                status='pending',result_json='{}',updated_at=excluded.updated_at,
                kind=excluded.kind,scope=excluded.scope,
                authority_generation=excluded.authority_generation""",
                (claim.garden_id, provider, effect_key, operation_id, principal.member_id,
                 principal.installation_id, claim.fence, credential_scope,
                 precondition, json.dumps(request, sort_keys=True), now, claim.kind, claim.scope,
                 claim.authority_generation))
            response = {"status": "pending", "operation_id": operation_id}
            self._record(db, principal, claim.garden_id, operation_id, "effect", payload, response)
            return response

    def finish_effect(self, principal: Principal, garden_id: str, operation_id: str, *,
                      outcome: Literal["succeeded", "failed", "unknown"],
                      result: dict[str, Any] | None = None) -> None:
        self._garden(principal, garden_id)
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid provider outcome")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM effects WHERE garden=? AND operation_id=?",
                             (garden_id, operation_id)).fetchone()
            if not row or (row["actor"] != principal.member_id
                           and not authorize(principal, "administer")):
                raise PermissionError("provider operation does not belong to actor")
            db.execute("UPDATE effects SET status=?,result_json=?,updated_at=? WHERE garden=? AND operation_id=?",
                       (outcome, json.dumps(result or {}, sort_keys=True), _iso(self.clock()),
                        garden_id, operation_id))

    def acknowledge_cancellation(self, principal: Principal, *, garden_id: str, kind: str,
                                 scope: str, fence: int) -> None:
        """Confirm that this installation's fenced local workers have stopped."""
        self._garden(principal, garden_id)
        with self._transaction() as db:
            allowed_installation = principal.installation_id
            if authorize(principal, "administer"):
                row = db.execute("""SELECT installation FROM cancellations
                    WHERE garden=? AND kind=? AND scope=? AND fence=? AND status='requested'""",
                    (garden_id, kind, scope, fence)).fetchone()
                allowed_installation = str(row["installation"]) if row else allowed_installation
            changed = db.execute("""UPDATE cancellations
                SET status='acknowledged',acknowledged_at=?
                WHERE garden=? AND kind=? AND scope=? AND installation=? AND fence=?
                  AND status='requested'""",
                (_iso(self.clock()), garden_id, kind, scope, allowed_installation, fence),
            ).rowcount
            if not changed:
                raise Conflict("cancellation request is stale or belongs to another installation")

    def retain_stale_evidence(self, principal: Principal, *, garden_id: str, kind: str,
                              scope: str, evidence_id: str, operation_id: str,
                              payload: dict[str, Any]) -> None:
        """Archive a late result without applying its state or source side effects."""
        self._garden(principal, garden_id)
        record = {"kind": kind, "scope": scope, "stale": True,
                  "actor": principal.member_id, "installation": principal.installation_id,
                  "payload": payload}
        with self._transaction() as db:
            existing = db.execute(
                "SELECT payload_json FROM evidence WHERE garden=? AND evidence_id=?",
                (garden_id, evidence_id),
            ).fetchone()
            encoded = json.dumps(record, sort_keys=True)
            if existing:
                if existing["payload_json"] != encoded:
                    raise Conflict("evidence id was reused with different content")
                return
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?)", (
                garden_id, evidence_id, operation_id, encoded, _iso(self.clock()),
            ))

    @staticmethod
    def _handoff_blockers(db: sqlite3.Connection, handoff: sqlite3.Row) -> list[str]:
        key = (handoff["garden"], handoff["kind"], handoff["scope"])
        cancellations = db.execute("""SELECT COUNT(*) FROM cancellations
            WHERE garden=? AND kind=? AND scope=? AND status='requested'""", key).fetchone()[0]
        effects = db.execute("""SELECT COUNT(*) FROM effects
            WHERE garden=? AND kind=? AND scope=? AND authority_generation<=?
              AND status IN ('pending','unknown')""", (*key, handoff["from_generation"])).fetchone()[0]
        outbox = db.execute("""SELECT COUNT(*) FROM outbox
            WHERE garden=? AND scope=? AND authority_version<? AND status='pending'""",
            (handoff["garden"], handoff["scope"], handoff["authority_version"]),
        ).fetchone()[0]
        blockers = []
        if cancellations:
            blockers.append("old workers have not acknowledged cancellation")
        if effects:
            blockers.append("old provider outcomes are unknown")
        if outbox:
            blockers.append("old projections are pending")
        return blockers

    def reserve(self, principal: Principal, *, garden_id: str, pool: str, operation_id: str,
                units: int, spend_micros: int, unit_limit: int, spend_limit_micros: int) -> dict[str, Any]:
        """Atomically reserve shared concurrency and spend; host caps remain additional gates."""
        self._garden(principal, garden_id)
        if principal.role == "viewer" or min(units, spend_micros) < 0:
            raise PermissionError("reservation is not authorized")
        if pool.startswith("global:") and not authorize(principal, "administer"):
            raise PermissionError("global reservation requires administrator authority")
        with self._transaction() as db:
            old = db.execute("SELECT * FROM reservations WHERE garden=? AND pool=? AND operation_id=?",
                             (garden_id, pool, operation_id)).fetchone()
            if old:
                if int(old["units"]) != units or int(old["spend_micros"]) != spend_micros:
                    raise Conflict("operation id was reused with a different reservation")
                return {"status": old["status"], "units": units, "spend_micros": spend_micros}
            totals = db.execute("""SELECT COALESCE(SUM(units),0),COALESCE(SUM(spend_micros),0)
                FROM reservations WHERE garden=? AND pool=? AND status='active'""",
                (garden_id, pool)).fetchone()
            if int(totals[0]) + units > unit_limit or int(totals[1]) + spend_micros > spend_limit_micros:
                raise Conflict("shared capacity or spending limit reached; waiting")
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,'active',?)",
                       (garden_id, pool, operation_id, units, spend_micros, _iso(self.clock())))
            return {"status": "active", "units": units, "spend_micros": spend_micros}

    def release_reservation(self, principal: Principal, garden_id: str, pool: str,
                            operation_id: str) -> None:
        self._garden(principal, garden_id)
        with self._transaction() as db:
            db.execute("UPDATE reservations SET status='released' WHERE garden=? AND pool=? AND operation_id=?",
                       (garden_id, pool, operation_id))

    def pending_outbox(self, garden_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM outbox WHERE garden=? AND status='pending' ORDER BY id", (garden_id,))]

    def reserve_phase(self, principal: Principal, claim: Claim, *, pool: str, operation_id: str,
                      units: int, spend_micros: int, unit_limit: int,
                      spend_limit_micros: int) -> dict[str, Any]:
        """Reserve phase-wide resources only beneath a current exclusive phase claim."""
        if claim.kind != "phase":
            raise PermissionError("phase reservation requires a phase-operation claim")
        with self._transaction() as db:
            self._validate_claim(db, principal, claim)
        return self.reserve(
            principal, garden_id=claim.garden_id, pool=f"phase:{claim.scope}:{pool}",
            operation_id=operation_id, units=units, spend_micros=spend_micros,
            unit_limit=unit_limit, spend_limit_micros=spend_limit_micros,
        )

    def _validate_claim(self, db: sqlite3.Connection, principal: Principal, claim: Claim,
                        expected_version: int | None = None) -> sqlite3.Row:
        self._garden(principal, claim.garden_id)
        row = db.execute("SELECT * FROM claims WHERE garden=? AND kind=? AND scope=?",
                         (claim.garden_id, claim.kind, claim.scope)).fetchone()
        now = self.clock()
        if (not row or row["operation_id"] != claim.operation_id or int(row["fence"]) != claim.fence
                or row["installation"] != principal.installation_id
                or dt.datetime.fromisoformat(row["lease_expires_at"]) <= now):
            raise Conflict("stale or expired fencing lease")
        authority = db.execute("SELECT * FROM authority WHERE garden=? AND kind=? AND scope=?",
                               (claim.garden_id, claim.kind, claim.scope)).fetchone()
        if (not authority or authority["owner"] != principal.member_id
                or int(authority["authority_generation"]) != claim.authority_generation):
            raise Conflict("claim was invalidated by reassignment")
        if expected_version is not None and int(authority["version"]) != expected_version:
            raise Conflict("stale authority version")
        return authority

    @staticmethod
    def _repeat(db: sqlite3.Connection, principal: Principal, garden: str, operation_id: str,
                kind: str, request: dict[str, Any]) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM operations WHERE garden=? AND operation_id=?",
                         (garden, operation_id)).fetchone()
        if not row:
            return None
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        if (row["operation_kind"] != kind or row["actor"] != principal.member_id
                or row["installation"] != principal.installation_id
                or row["request_json"] != encoded):
            raise Conflict("operation id was reused with a different identity or request")
        return json.loads(row["response_json"])

    def _record(self, db: sqlite3.Connection, principal: Principal, garden: str,
                operation_id: str, kind: str, request: dict[str, Any], response: dict[str, Any]) -> None:
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (
            garden, operation_id, kind, principal.member_id, principal.installation_id,
            json.dumps(request, sort_keys=True, separators=(",", ":")),
            json.dumps(response, sort_keys=True, separators=(",", ":")), _iso(self.clock()),
        ))
