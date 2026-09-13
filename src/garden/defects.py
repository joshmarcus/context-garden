"""Durable defect annotations for closed tasks.

Defects are later observations, not task state.  They live in a separate controller-local
ledger so creating or correcting one cannot rewrite a task's completion evidence.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Any

from .model import Task, now_iso

SEVERITIES = frozenset({"minor", "major"})
DISPOSITIONS = frozenset({"unreviewed", "reviewed"})
TEXT_FIELDS = (
    "expected", "observed", "impact", "affected_source",
    "affected_release", "affected_run", "follow_up", "known_facts", "hypotheses",
    "unknowns", "could_have_caught", "prevention", "proposed_follow_up",
)
LIST_FIELDS = ("evidence_links",)


class DefectConflict(RuntimeError):
    """A retry key or optimistic revision did not describe the stored mutation."""


class DefectStore:
    """A locked, atomically replaced JSON ledger below ``.garden``."""

    def __init__(self, garden_dir: Path):
        self.path = garden_dir / "defects.json"
        self.lock_path = garden_dir / "defects.lock"

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "defects": []}
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("version") != 1 \
                or not isinstance(value.get("defects"), list):
            raise ValueError("unsupported defect ledger")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.lock_path.open("a")
        os.chmod(self.lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return lock

    @staticmethod
    def _clean(fields: dict[str, Any]) -> dict[str, Any]:
        clean = {name: str(fields.get(name) or "").strip() for name in TEXT_FIELDS}
        for name in LIST_FIELDS:
            value = fields.get(name) or []
            if isinstance(value, str):
                value = value.splitlines()
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{name} must be a list of strings")
            clean[name] = [item.strip() for item in value if item.strip()]
        return clean

    def create(self, task: Task, severity: str, description: str, reporter: str, *,
               idempotency_key: str = "", **fields: Any) -> tuple[dict[str, Any], bool]:
        severity, description, reporter = severity.strip(), description.strip(), reporter.strip()
        if not task.status.terminal:
            raise ValueError("defects can only be recorded against a closed task")
        if severity not in SEVERITIES:
            raise ValueError("severity must be minor or major")
        if not description:
            raise ValueError("description is required")
        if not reporter:
            raise ValueError("reporter is required")
        if len(description) > 1000:
            raise ValueError("description must be at most 1000 characters")
        key = idempotency_key.strip()
        if len(key) > 200:
            raise ValueError("idempotency_key must be at most 200 characters")
        clean = self._clean(fields)
        fingerprint = {"severity": severity, "description": description, **clean}
        with self._locked():
            state = self._read()
            if key:
                existing = next((row for row in state["defects"]
                                 if row.get("task_id") == task.id
                                 and row.get("reporter") == reporter
                                 and row.get("idempotency_key") == key), None)
                if existing:
                    current = {name: existing.get(name, [] if name in LIST_FIELDS else "")
                               for name in (*TEXT_FIELDS, *LIST_FIELDS)}
                    if {"severity": existing["severity"], "description": existing["description"],
                        **current} != fingerprint:
                        raise DefectConflict("idempotency key was already used for different defect data")
                    return dict(existing), False
            defect = {
                "id": f"DEF-{uuid.uuid4().hex}", "task_id": task.id,
                "product": task.product, "phase": task.phase, "severity": severity,
                "description": description, "discovered_at": now_iso(), "reporter": reporter,
                "disposition": "unreviewed", "revision": 1, "history": [],
                "idempotency_key": key, **clean,
            }
            state["defects"].append(defect)
            self._write(state)
            return dict(defect), True

    def update(self, defect_id: str, actor: str, expected_revision: int, **changes: Any) -> dict[str, Any]:
        actor = actor.strip()
        if not actor:
            raise ValueError("actor is required")
        allowed = {"severity", "disposition", "description", *TEXT_FIELDS, *LIST_FIELDS}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown defect fields: {', '.join(sorted(unknown))}")
        with self._locked():
            state = self._read()
            row = next((item for item in state["defects"] if item.get("id") == defect_id), None)
            if row is None:
                raise KeyError(defect_id)
            if row.get("revision") != expected_revision:
                raise DefectConflict(f"defect changed; current revision is {row.get('revision')}")
            normalized = self._clean(changes)
            normalized = {name: value for name, value in normalized.items() if name in changes}
            if "description" in changes:
                normalized["description"] = str(changes["description"]).strip()
            if "severity" in changes:
                severity = str(changes["severity"]).strip()
                if severity not in SEVERITIES:
                    raise ValueError("severity must be minor or major")
                normalized["severity"] = severity
            if "disposition" in changes:
                disposition = str(changes["disposition"]).strip()
                if disposition not in DISPOSITIONS:
                    raise ValueError("disposition must be unreviewed or reviewed")
                normalized["disposition"] = disposition
            if "description" in normalized and not normalized["description"]:
                raise ValueError("description is required")
            if "description" in normalized and len(normalized["description"]) > 1000:
                raise ValueError("description must be at most 1000 characters")
            changed = {name: value for name, value in normalized.items() if row.get(name) != value}
            if not changed:
                return dict(row)
            row.setdefault("history", []).append({
                "revision": row["revision"], "changed_at": now_iso(), "changed_by": actor,
                "prior": {name: row.get(name) for name in changed},
            })
            row.update(changed)
            row["revision"] += 1
            row["updated_at"] = now_iso()
            row["updated_by"] = actor
            self._write(state)
            return dict(row)

    def get(self, defect_id: str) -> dict[str, Any]:
        row = next((item for item in self._read()["defects"] if item.get("id") == defect_id), None)
        if row is None:
            raise KeyError(defect_id)
        return dict(row)

    def list(self, *, task_id: str = "", product: str = "", phase: str = "",
             severity: str = "", disposition: str = "", discovered_from: str = "",
             discovered_to: str = "", allowed_projects: frozenset[str] | None = None) -> list[dict[str, Any]]:
        rows = self._read()["defects"]
        filters = {"task_id": task_id, "product": product, "phase": phase,
                   "severity": severity, "disposition": disposition}
        out = [dict(row) for row in rows
               if all(not value or row.get(name) == value for name, value in filters.items())
               and (allowed_projects is None or row.get("product") in allowed_projects)
               and (not discovered_from or row.get("discovered_at", "") >= discovered_from)
               and (not discovered_to or row.get("discovered_at", "") <= discovered_to)]
        return sorted(out, key=lambda row: (row.get("discovered_at", ""), row.get("id", "")), reverse=True)

    def summary(self, **filters: Any) -> dict[str, Any]:
        rows = self.list(**filters)
        return {
            "total": len(rows),
            "minor": sum(row.get("severity") == "minor" for row in rows),
            "major": sum(row.get("severity") == "major" for row in rows),
            "unreviewed": sum(row.get("disposition") == "unreviewed" for row in rows),
            "reviewed": sum(row.get("disposition") == "reviewed" for row in rows),
        }
