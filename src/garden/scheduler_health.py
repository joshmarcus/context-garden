"""Durable, bounded health evidence for standalone scheduler watch processes."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import now_iso

MAX_WATCHERS = 64


def _process_identity(pid: int) -> str:
    """Return Linux's stable identity for a process, or an empty portable fallback."""
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError):
        return ""


def _process_matches(pid: int, identity: str) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    current = _process_identity(pid)
    return not identity or not current or current == identity


@dataclass
class WatchHeartbeat:
    """One watch process's atomic lease in ``.garden/watchers``."""

    garden_dir: Path
    interval: int

    def __post_init__(self) -> None:
        self.pid = os.getpid()
        self.identity = _process_identity(self.pid)
        self.instance = uuid.uuid4().hex
        self.started_at = now_iso()
        self.path = self.garden_dir / "watchers" / f"{self.pid}-{self.instance}.json"

    def write(self, state: str, *, last_tick: str = "", error: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "pid": self.pid,
            "process_identity": self.identity,
            "instance": self.instance,
            "started_at": self.started_at,
            "heartbeat_at": now_iso(),
            "interval_seconds": self.interval,
            "state": state,
            "last_tick": last_tick,
            "error": error[:500],
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True) + "\n")
        temporary.replace(self.path)

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


def scheduler_health(
    garden_dir: Path,
    *,
    now: dt.datetime | None = None,
    process_matches: Callable[[int, str], bool] = _process_matches,
) -> dict[str, Any]:
    """Summarise at most ``MAX_WATCHERS`` auditable watch leases."""
    now = now or dt.datetime.now(dt.UTC)
    records: list[dict[str, Any]] = []
    directory = garden_dir / "watchers"
    paths = sorted(directory.glob("*.json"))[:MAX_WATCHERS] if directory.is_dir() else []
    for path in paths:
        try:
            record = json.loads(path.read_text())
            heartbeat = dt.datetime.fromisoformat(str(record["heartbeat_at"]))
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=dt.UTC)
            record["age_seconds"] = max(0, int((now - heartbeat).total_seconds()))
            record["source"] = path.name
            records.append(record)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue

    live: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for record in records:
        interval = max(1, int(record.get("interval_seconds") or 60))
        stale_after = max(75, interval * 2 + 15)
        age = int(record["age_seconds"])
        alive = process_matches(int(record.get("pid") or 0), str(record.get("process_identity") or ""))
        if age > stale_after:
            if alive or age <= stale_after * 4:
                stale.append(record)
        elif record.get("state") == "failed" or not alive:
            failed.append(record)
        else:
            live.append(record)

    if len(live) > 1:
        kind, label = "duplicated", f"duplicate standalone watchers ({len(live)})"
    elif len(live) == 1:
        kind, label = "healthy", "standalone watcher healthy"
    elif failed:
        kind, label = "failed", "standalone watcher failed"
    elif stale:
        kind, label = "stale", "standalone watcher stale"
    else:
        kind, label = "missing", "no standalone watcher detected"
    evidence = live or failed or stale
    return {"kind": kind, "label": label, "records": evidence, "checked_at": now.isoformat()}
