"""Versioned JSON Lines boundary for isolated plugin capabilities."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "garden.plugin-command/v1"
MAX_STDERR_BYTES = 64 * 1024


class CommandProtocolError(RuntimeError):
    """Base class for actionable isolated-plugin failures."""


class UnsupportedProtocol(CommandProtocolError):
    pass


class UnsupportedCapabilities(CommandProtocolError):
    pass


class MalformedOutput(CommandProtocolError):
    pass


class TransportLost(CommandProtocolError):
    pass


class CommandTimedOut(CommandProtocolError):
    pass


class CommandCancelled(CommandProtocolError):
    pass


@dataclass(frozen=True)
class CommandReply:
    result: Any
    stderr: str
    idempotency_key: str


class JsonLinesCommand:
    """Run one handshake and typed request over a bounded subprocess exchange.

    stdout is reserved for two JSON protocol records. stderr is captured as logs and
    never parsed as protocol. A fresh process per request makes timeout/cancellation a
    core-owned boundary and makes retry identity explicit rather than process-local.
    """

    def __init__(self, command: list[str], *, capability: str,
                 timeout_seconds: float = 60, max_stderr_bytes: int = MAX_STDERR_BYTES,
                 cancelled: Callable[[], bool] | None = None,
                 audit_path: Path | None = None,
                 redact: Callable[[str], str] | None = None):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("plugin command must be a non-empty argv list")
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("plugin command timeout_seconds must be in (0, 3600]")
        self.command = tuple(command)
        self.capability = capability
        self.timeout_seconds = timeout_seconds
        self.max_stderr_bytes = max_stderr_bytes
        self.cancelled = cancelled or (lambda: False)
        self.audit_path = audit_path
        self.redact = redact or (lambda value: value)

    def invoke(self, operation: str, payload: dict[str, Any], *,
               idempotency_key: str | None = None) -> CommandReply:
        key = idempotency_key or str(uuid.uuid4())
        try:
            reply = self._exchange(operation, payload, key)
        except CommandProtocolError as exc:
            self._audit(operation, key, "error", str(exc))
            raise
        self._audit(operation, key, "ok", "")
        return reply

    def _exchange(self, operation: str, payload: dict[str, Any], key: str) -> CommandReply:
        if self.cancelled():
            raise CommandCancelled(f"plugin command {operation} was cancelled before launch")
        handshake = {"type": "handshake", "protocol_version": PROTOCOL_VERSION,
                     "required_capabilities": [self.capability]}
        request = {"type": "request", "operation": operation,
                   "idempotency_key": key, "payload": payload}
        stdin = "".join(json.dumps(row, separators=(",", ":")) + "\n"
                        for row in (handshake, request))
        try:
            completed = subprocess.run(
                self.command, input=stdin, text=True, capture_output=True,
                timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimedOut(
                f"plugin command {operation} timed out after {self.timeout_seconds:g}s"
            ) from exc
        except OSError as exc:
            raise TransportLost(f"plugin command could not start: {exc}") from exc
        stderr = completed.stderr[-self.max_stderr_bytes:]
        rows = self._rows(completed.stdout)
        if completed.returncode != 0:
            detail = stderr.strip()
            raise TransportLost(
                f"plugin command exited {completed.returncode}" + (f": {detail}" if detail else "")
            )
        if len(rows) != 2:
            raise MalformedOutput("plugin command must write one handshake and one response to stdout")
        hello, response = rows
        if hello.get("type") != "handshake" or hello.get("protocol_version") != PROTOCOL_VERSION:
            raise UnsupportedProtocol(
                f"plugin command does not support protocol {PROTOCOL_VERSION!r}"
            )
        declared = hello.get("capabilities")
        if not isinstance(declared, list) or self.capability not in declared:
            raise UnsupportedCapabilities(
                f"plugin command does not declare required capability {self.capability!r}"
            )
        if response.get("type") != "response" or response.get("idempotency_key") != key:
            raise MalformedOutput("plugin command response has the wrong type or idempotency key")
        if response.get("cancelled") is True:
            raise CommandCancelled(f"plugin command {operation} was cancelled")
        if "error" in response:
            raise CommandProtocolError(f"plugin command {operation} failed: {response['error']}")
        if "result" not in response:
            raise MalformedOutput("plugin command response is missing result")
        return CommandReply(response["result"], stderr, key)

    def _audit(self, operation: str, key: str, status: str, error: str) -> None:
        """Append a secret-scrubbed recovery record when the caller supplies durable storage."""
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"protocol_version": PROTOCOL_VERSION, "capability": self.capability,
               "operation": operation, "idempotency_key": key, "status": status}
        if error:
            row["error"] = self.redact(error)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")

    @staticmethod
    def _rows(stdout: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            for line in stdout.splitlines():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError
                rows.append(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedOutput("plugin command wrote malformed JSON Lines output") from exc
        return rows
