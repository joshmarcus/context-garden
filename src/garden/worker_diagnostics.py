"""Bounded, secret-free transport history for pull-based workers."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

MAX_EVENTS = 2000
MAX_BYTES = 2 * 1024 * 1024


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def endpoint_class(path: str) -> str:
    if path == "/api/runs/claim":
        return "claim"
    if path.endswith("/heartbeat"):
        return "heartbeat"
    if path.endswith("/finish"):
        return "result"
    return "other"


class WorkerEventLog:
    """Append a compact JSONL record and retain a useful tail across restarts."""

    def __init__(self, path: Path, *, worker_id: str = "", generation: str = ""):
        self.path = path
        self.worker_id = worker_id
        self.generation = generation
        self._lock = threading.Lock()

    def emit(self, event: str, **data: Any) -> dict[str, Any]:
        record = {
            "at": utc_now(), "event": event, "worker_id": self.worker_id,
            "process_generation": self.generation, **data,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
            self._trim()
        return record

    def _trim(self) -> None:
        try:
            if self.path.stat().st_size <= MAX_BYTES:
                return
        except OSError:
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) > MAX_EVENTS:
            self.path.write_text("\n".join(lines[-MAX_EVENTS:]) + "\n", encoding="utf-8")

    def read(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 1000)):]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return result


def durable_worker_identity(root: Path, configured: str = "") -> str:
    """Return an operator-safe stable identity without exposing a physical host id."""
    path = root / "worker-id"
    if configured:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", configured):
            raise ValueError("worker_id must be 1-128 safe identifier characters")
        identity = configured
    elif path.exists():
        identity = path.read_text().strip()
    else:
        identity = "worker-" + uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", identity):
        raise ValueError("saved worker identity is invalid")
    if not path.exists():
        path.write_text(identity + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return identity
