"""Typed, durable notification delivery through configured destinations.

Configuration chooses a logical destination and its adapter.  Event text is data all the
way to an adapter: the only built-in process adapter receives a JSON document on stdin and
uses a configured argv list, never a shell command.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .host_identity import scrub_shared_text

VERSION = 1
LOGGER = logging.getLogger("garden.notify")


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


@dataclass(frozen=True)
class DeliveryPolicy:
    """Normalized, bounded values used for one destination delivery."""

    timeout: float
    max_attempts: int
    backoff: float


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
        if not isinstance(raw, dict):
            return {}
        records = {key: value for key, value in raw.items()
                   if isinstance(key, str) and self._valid_record(value)}
        if len(records) != len(raw):
            # Durable content may have been edited or partially written by an older
            # version. Keep diagnostics bounded and content-free: records can contain
            # private event fields even though current writers redact them.
            LOGGER.warning("notification delivery ledger contains malformed records; ignored")
        return records

    @staticmethod
    def _valid_record(record: Any) -> bool:
        """Accept only record fields the delivery paths can inspect safely."""
        if not isinstance(record, dict):
            return False
        if record.get("outcome") not in {"delivered", "failed", "permanent"}:
            return False
        if not isinstance(record.get("destination"), str):
            return False
        if not isinstance(record.get("event"), dict):
            return False
        retryable = record.get("retryable")
        if retryable is not None and not isinstance(retryable, bool):
            return False
        next_attempt = record.get("next_attempt", 0)
        return (isinstance(next_attempt, (int, float)) and not isinstance(next_attempt, bool)
                and math.isfinite(next_attempt))

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
        try:
            records = self.store.read()
        except (OSError, UnicodeError):
            LOGGER.warning("notification delivery ledger could not be read; delivery skipped")
            return []
        results: list[str] = []
        now = self.clock()
        for name, raw in destinations.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            # The ledger is durable and can be read by an operator later.  Its retry
            # payload must cross the same disclosure boundary as an external adapter.
            safe_event = self._safe_event(cfg, event)
            key = _identity(name, safe_event)
            record = records.get(key, {})
            if (record.get("outcome") in ("delivered", "permanent")
                    or record.get("retryable") is False
                    or record.get("next_attempt", 0) > now):
                continue
            attempts = self._attempts(record) + 1
            try:
                policy = self._policy(raw)
            except PermanentDeliveryError:
                # Configuration belongs to the trusted operator, but a typo must be
                # contained just like an adapter error: preserve a visible terminal
                # ledger outcome and leave the task transition untouched.
                records[key] = {
                    "destination": name,
                    "outcome": "permanent",
                    "reason": "invalid destination policy",
                    "attempts": attempts,
                    "event": asdict(safe_event),
                    "updated_at": now,
                }
                results.append(f"{name}: permanent failure")
                continue
            try:
                fields = self._fields(raw, safe_event)
                if fields is None:
                    records[key] = {"destination": name, "outcome": "permanent", "reason": "destination revoked",
                                    "event": asdict(safe_event), "updated_at": now}
                    results.append(f"{name}: revoked")
                    continue
                adapter = self.adapters.get(str(raw.get("adapter") or ""))
                if adapter is None or adapter.version != VERSION:
                    raise PermanentDeliveryError("unapproved adapter")
                adapter.deliver(raw, fields, policy.timeout)
            except PermanentDeliveryError:
                records[key] = {"destination": name, "outcome": "permanent", "reason": "adapter rejected", "attempts": attempts,
                                "event": asdict(safe_event), "updated_at": now}
                results.append(f"{name}: permanent failure")
            except Exception:  # adapters must not corrupt a task transition
                records[key] = {"destination": name, "outcome": "failed", "reason": "delivery failed", "attempts": attempts,
                                "next_attempt": now + (policy.backoff * attempts if attempts < policy.max_attempts else 0),
                                "event": asdict(safe_event), "updated_at": now, "retryable": attempts < policy.max_attempts}
                results.append(f"{name}: failed")
            else:
                records[key] = {"destination": name, "outcome": "delivered", "attempts": attempts,
                                "event": asdict(safe_event), "updated_at": now}
                results.append(f"{name}: delivered")
        try:
            self.store.write(records)
        except (OSError, UnicodeError):
            LOGGER.warning("notification delivery ledger could not be written")
        return results

    def retry_pending(self, cfg: dict[str, Any]) -> list[str]:
        """Retry only durable, due failures after a scheduler restart or later tick."""
        results: list[str] = []
        try:
            records = self.store.read()
        except (OSError, UnicodeError):
            LOGGER.warning("notification delivery ledger could not be read; retries skipped")
            return results
        for record in records.values():
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
    def _safe_event(cfg: dict[str, Any], event: NotificationEvent) -> NotificationEvent:
        """Return the only event representation allowed beyond this trust boundary."""
        fields = asdict(event)
        return NotificationEvent(**{
            name: scrub_shared_text(value, cfg) if isinstance(value, str) else value
            for name, value in fields.items()
        })

    @staticmethod
    def _attempts(record: dict[str, Any]) -> int:
        """Read a prior ledger count defensively so bad durable data stays nonfatal."""
        try:
            return max(int(record.get("attempts", 0)), 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _policy(cls, destination: dict[str, Any]) -> DeliveryPolicy:
        """Validate policy before delivery so malformed config cannot escape a tick."""
        return DeliveryPolicy(
            timeout=cls._bounded_number(destination.get("timeout_seconds", 10), "timeout_seconds", 0.1, 60.0),
            max_attempts=cls._bounded_attempts(destination.get("max_attempts", 3)),
            backoff=cls._bounded_number(destination.get("backoff_seconds", 5), "backoff_seconds", 0, 3600.0),
        )

    @staticmethod
    def _bounded_number(value: Any, name: str, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise PermanentDeliveryError(f"invalid {name}") from exc
        if not math.isfinite(number):
            raise PermanentDeliveryError(f"invalid {name}")
        return min(max(number, minimum), maximum)

    @classmethod
    def _bounded_attempts(cls, value: Any) -> int:
        number = cls._bounded_number(value, "max_attempts", 1, 10)
        if not number.is_integer():
            raise PermanentDeliveryError("invalid max_attempts")
        return int(number)

    @staticmethod
    def _fields(destination: dict[str, Any], event: NotificationEvent) -> dict[str, Any] | None:
        if destination.get("revoked") is True:
            return None
        allowed = destination.get("fields", ["task_id", "status", "message", "pr_url", "kind"])
        if not isinstance(allowed, list):
            raise PermanentDeliveryError("destination fields must be a list")
        safe = asdict(event)
        return {name: safe[name] for name in allowed if name in safe}
