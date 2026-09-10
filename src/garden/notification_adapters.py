"""Typed, durable notification delivery through configured destinations.

Configuration chooses a logical destination and its adapter.  Event text is data all the
way to an adapter: the only built-in process adapter receives a JSON document on stdin and
uses a configured argv list, never a shell command.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .host_identity import scrub_shared_text

VERSION = 1


class TransientDeliveryError(Exception):
    """A destination may succeed when retried."""


class PermanentDeliveryError(Exception):
    """A destination was revoked or cannot accept this event."""


@dataclass(frozen=True)
class NotificationEvent:
    task_id: str
    status: str
    message: str
    pr_url: str = ""
    kind: str = "required_action"
    version: int = VERSION


class DestinationAdapter(Protocol):
    version: int

    def deliver(self, destination: dict[str, Any], fields: dict[str, Any], timeout: float) -> None: ...


class ArgvAdapter:
    """A static argv destination; JSON is supplied on stdin, not interpolated into argv."""

    version = VERSION

    def deliver(self, destination: dict[str, Any], fields: dict[str, Any], timeout: float) -> None:
        argv = destination.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise PermanentDeliveryError("destination has no safe argv")
        try:
            result = subprocess.run(argv, input=json.dumps(fields), text=True, capture_output=True,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TransientDeliveryError(f"timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise TransientDeliveryError(f"could not start adapter: {exc}") from exc
        if result.returncode:
            raise TransientDeliveryError(f"adapter exited {result.returncode}")


def _identity(destination: str, event: NotificationEvent) -> str:
    body = json.dumps([VERSION, destination, event.task_id, event.status, event.kind,
                       event.message, event.pr_url], separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


class DeliveryStore:
    """Separate, restart-safe delivery ledger; task state is never touched."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def write(self, records: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(records, indent=2, sort_keys=True))
        os.replace(tmp, self.path)


class NotificationDelivery:
    """Apply destination policy and bounded retry/coalescing to typed events."""

    def __init__(self, path: Path, adapters: dict[str, DestinationAdapter] | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = DeliveryStore(path)
        self.adapters = {"argv": ArgvAdapter(), **(adapters or {})}
        self.clock = clock

    def deliver(self, cfg: dict[str, Any], event: NotificationEvent) -> list[str]:
        notify = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
        destinations = notify.get("destinations") if isinstance(notify, dict) else {}
        if not isinstance(destinations, dict):
            return []
        records, results, now = self.store.read(), [], self.clock()
        for name, raw in destinations.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            key = _identity(name, event)
            record = records.get(key, {})
            if (record.get("outcome") in ("delivered", "permanent")
                    or record.get("retryable") is False
                    or record.get("next_attempt", 0) > now):
                continue
            attempts = int(record.get("attempts", 0)) + 1
            timeout = min(max(float(raw.get("timeout_seconds", 10)), 0.1), 60.0)
            try:
                fields = self._fields(cfg, raw, event)
                if fields is None:
                    records[key] = {"destination": name, "outcome": "permanent", "reason": "destination revoked",
                                    "event": asdict(event), "updated_at": now}
                    results.append(f"{name}: revoked")
                    continue
                adapter = self.adapters.get(str(raw.get("adapter") or ""))
                if adapter is None or adapter.version != VERSION:
                    raise PermanentDeliveryError("unapproved adapter")
                adapter.deliver(raw, fields, timeout)
            except PermanentDeliveryError as exc:
                records[key] = {"destination": name, "outcome": "permanent", "reason": str(exc), "attempts": attempts,
                                "event": asdict(event), "updated_at": now}
                results.append(f"{name}: permanent failure")
            except Exception as exc:  # adapters must not corrupt a task transition
                maximum = min(max(int(raw.get("max_attempts", 3)), 1), 10)
                backoff = min(max(float(raw.get("backoff_seconds", 5)), 0), 3600.0)
                records[key] = {"destination": name, "outcome": "failed", "reason": str(exc), "attempts": attempts,
                                "next_attempt": now + (backoff * attempts if attempts < maximum else 0),
                                "event": asdict(event), "updated_at": now, "retryable": attempts < maximum}
                results.append(f"{name}: failed")
            else:
                records[key] = {"destination": name, "outcome": "delivered", "attempts": attempts,
                                "event": asdict(event), "updated_at": now}
                results.append(f"{name}: delivered")
        self.store.write(records)
        return results

    def retry_pending(self, cfg: dict[str, Any]) -> list[str]:
        """Retry only durable, due failures after a scheduler restart or later tick."""
        results: list[str] = []
        for record in self.store.read().values():
            if record.get("outcome") != "failed" or not record.get("retryable"):
                continue
            raw_event = record.get("event")
            if not isinstance(raw_event, dict):
                continue
            try:
                event = NotificationEvent(**raw_event)
            except TypeError:
                continue
            results.extend(self.deliver(cfg, event))
        return results

    @staticmethod
    def _fields(cfg: dict[str, Any], destination: dict[str, Any], event: NotificationEvent) -> dict[str, Any] | None:
        if destination.get("revoked") is True:
            return None
        allowed = destination.get("fields", ["task_id", "status", "message", "pr_url", "kind"])
        if not isinstance(allowed, list):
            raise PermanentDeliveryError("destination fields must be a list")
        safe = asdict(event)
        safe["message"] = scrub_shared_text(event.message, cfg)
        return {name: safe[name] for name in allowed if name in safe}
