"""Durable, lease-scoped worker transcript storage.

The canonical stream is newline-delimited JSON.  Each record is an observable harness
event (stdout, stderr, or a worker lifecycle event); it is inert data and is never
evaluated by this module.  ``stdout.json`` and ``stderr.log`` remain presentation views.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024


class TranscriptError(RuntimeError):
    """A transcript chunk or finalization request cannot be accepted."""


def attempt_id(lease_token: str) -> str:
    return hashlib.sha256(lease_token.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Receipt:
    offset: int
    sha256: str


class TranscriptStore:
    """Append and finalize one attempt without loading its transcript into memory."""

    def __init__(self, run_path: Path, lease_token: str, *, max_bytes: int = DEFAULT_MAX_BYTES):
        self.attempt = attempt_id(lease_token)
        self.root = run_path / "transcripts" / self.attempt
        self.stream = self.root / "events.jsonl"
        self.metadata = self.root / "metadata.json"
        self.max_bytes = max_bytes

    def receipt(self) -> Receipt:
        size = self.stream.stat().st_size if self.stream.exists() else 0
        digest = hashlib.sha256()
        if self.stream.exists():
            with self.stream.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
        return Receipt(size, digest.hexdigest())

    def append(self, offset: int, payload: bytes, chunk_sha256: str) -> Receipt:
        if len(payload) > MAX_CHUNK_BYTES:
            raise TranscriptError("transcript chunk exceeds 1 MiB")
        if hashlib.sha256(payload).hexdigest() != chunk_sha256:
            raise TranscriptError("transcript chunk checksum mismatch")
        self.root.mkdir(parents=True, exist_ok=True)
        current = self.stream.stat().st_size if self.stream.exists() else 0
        if offset < current:
            with self.stream.open("rb") as source:
                source.seek(offset)
                if source.read(len(payload)) != payload:
                    raise TranscriptError("transcript chunk conflicts with durable data")
        elif offset > current:
            raise TranscriptError(f"transcript offset {offset} is ahead of durable offset {current}")
        elif current + len(payload) > self.max_bytes:
            raise TranscriptError("transcript storage limit exceeded")
        elif payload:
            try:
                with self.stream.open("ab") as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
            except OSError as exc:
                raise TranscriptError(f"transcript storage failed: {exc}") from exc
        return self.receipt()

    def finalize(self, expected_bytes: int, expected_sha256: str,
                 metadata: dict[str, Any]) -> dict[str, Any]:
        receipt = self.receipt()
        if receipt.offset != expected_bytes or receipt.sha256 != expected_sha256:
            raise TranscriptError("transcript finalization does not match durable data")
        record = {
            "schema_version": SCHEMA_VERSION,
            **metadata,
            "attempt_id": self.attempt,
            "byte_count": receipt.offset,
            "sha256": receipt.sha256,
            "status": "complete",
        }
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2))
        with temporary.open("rb") as source:
            os.fsync(source.fileno())
        temporary.replace(self.metadata)
        return record

    def state(self) -> dict[str, Any]:
        if self.metadata.exists():
            return json.loads(self.metadata.read_text())
        receipt = self.receipt()
        return {"schema_version": SCHEMA_VERSION, "attempt_id": self.attempt,
                "byte_count": receipt.offset, "sha256": receipt.sha256,
                "status": "partial" if receipt.offset else "missing"}
