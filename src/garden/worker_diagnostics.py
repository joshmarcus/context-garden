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
MAX_FIELD_CHARS = 256
MAX_RECORD_BYTES = 4096


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


def safe_correlation_id(value: Any) -> str:
    """Accept opaque correlation metadata only in the documented secret-safe alphabet."""
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", text) else ""


class WorkerEventLog:
    """Append a compact JSONL record and retain a useful tail across restarts."""

    def __init__(self, path: Path, *, worker_id: str = "", generation: str = ""):
        self.path = path
        self.worker_id = worker_id
        self.generation = generation
        self._lock = threading.Lock()
        try:
            with self.path.open("rb") as stream:
                self._line_count = sum(1 for _line in stream)
        except OSError:
            self._line_count = 0

    def emit(self, event: str, **data: Any) -> dict[str, Any]:
        record = _bounded_record({
            "at": utc_now(), "event": event, "worker_id": self.worker_id,
            "process_generation": self.generation, **data,
        })
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            self._line_count += 1
            self._trim()
        return record

    def _trim(self) -> None:
        try:
            if self._line_count <= MAX_EVENTS and self.path.stat().st_size <= MAX_BYTES:
                return
        except OSError:
            return
        try:
            raw_lines = self.path.read_bytes().splitlines(keepends=True)
        except OSError:
            return
        if len(raw_lines) <= MAX_EVENTS and sum(map(len, raw_lines)) <= MAX_BYTES:
            return
        target_events = max(1, MAX_EVENTS * 3 // 4)
        target_bytes = max(1, MAX_BYTES * 3 // 4)
        kept: list[bytes] = []
        size = 0
        for line in reversed(raw_lines[-target_events:]):
            if size + len(line) > target_bytes:
                break
            kept.append(line)
            size += len(line)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(b"".join(reversed(kept)))
        temporary.replace(self.path)
        self._line_count = len(kept)

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


def _bounded_record(record: dict[str, Any]) -> dict[str, Any]:
    """Keep diagnostic metadata scalar and small, even when supplied by a peer."""
    bounded: dict[str, Any] = {}
    identifiers = {"request_id", "claim_request_id", "worker_id", "process_generation",
                   "run_id", "task_id"}
    for key, value in record.items():
        safe_key = str(key)[:64]
        if safe_key in identifiers:
            bounded[safe_key] = safe_correlation_id(value)
        elif isinstance(value, str):
            bounded[safe_key] = value[:MAX_FIELD_CHARS]
        elif value is None or isinstance(value, (bool, int, float)):
            bounded[safe_key] = value
        else:
            bounded[safe_key] = str(value)[:MAX_FIELD_CHARS]
    encoded = json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_RECORD_BYTES:
        # Required correlation fields are inserted first by callers and survive this
        # conservative last-resort cap; optional tail fields are discarded.
        while len(encoded) > MAX_RECORD_BYTES and bounded:
            bounded.pop(next(reversed(bounded)))
            encoded = json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode()
    return bounded


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
