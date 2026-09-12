"""Fail-closed local client and conflict-aware Markdown projection cache."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .coordination import PROTOCOL_VERSION


class MultiplayerUnavailable(RuntimeError):
    """The authoritative service cannot currently authorize an operation."""


class ProjectionConflict(MultiplayerUnavailable):
    """A local authored edit and an authoritative projection need reconciliation."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class AuthoritativeView:
    snapshot: dict[str, Any]
    stale: bool
    error: str = ""

    def revision(self, kind: str, scope: str) -> int:
        for row in self.snapshot.get("authority", []):
            if row.get("kind") == kind and row.get("scope") == scope:
                return int(row["version"])
        raise MultiplayerUnavailable(f"no authoritative revision for {kind} {scope}")

    def projection_lag(self) -> list[str]:
        projected = {
            (row.get("kind"), row.get("scope")): int(row["version"])
            for row in self.snapshot.get("projections", [])
        }
        return [
            f"{row['kind']}:{row['scope']}@{row['version']}"
            for row in self.snapshot.get("authority", [])
            if projected.get((row.get("kind"), row.get("scope")), 0) < int(row["version"])
        ]


class MultiplayerClient:
    """The authenticated boundary shared by local schedulers and web readers.

    Reads may use the last authenticated response during an outage. Mutations never do:
    each command refreshes authority and names the revision it was based on.
    """

    def __init__(self, *, root: Path, garden_id: str, endpoint: str, credential: str,
                 member_id: str, installation_id: str,
                 request: Callable[..., httpx.Response] | None = None):
        if not all((garden_id, endpoint, credential, member_id, installation_id)):
            raise MultiplayerUnavailable(
                "multiplayer enrollment requires garden, endpoint, member, installation, and credential"
            )
        self.root = root.resolve()
        self.garden_id = garden_id
        self.endpoint = endpoint.rstrip("/")
        self.member_id = member_id
        self.installation_id = installation_id
        self._headers = {"Authorization": f"Bearer {credential}"}
        self._request = request or httpx.request
        self._cache_path = self.root / ".garden" / "authoritative-snapshot.json"
        self._projection_path = self.root / ".garden" / "authoritative-projections.json"

    @classmethod
    def from_config(cls, config: Config, **kwargs: Any) -> MultiplayerClient | None:
        if not config.get("multiplayer.enabled", False):
            return None
        credential_env = str(config.get("multiplayer.credential_env", ""))
        credential = os.environ.get(credential_env, "") if credential_env else ""
        return cls(
            root=config.root, garden_id=str(config.get("multiplayer.garden_id", "")),
            endpoint=str(config.get("multiplayer.coordinator_url", "")),
            member_id=str(config.get("multiplayer.member_id", "")),
            installation_id=str(config.get("multiplayer.installation_id", "")),
            credential=credential, **kwargs,
        )

    def _url(self, suffix: str) -> str:
        return f"{self.endpoint}/v1/gardens/{self.garden_id}{suffix}"

    def _load_cache(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self._cache_path.read_text())
            if (value.get("garden_id") != self.garden_id
                    or value.get("protocol_version") != PROTOCOL_VERSION):
                return None
            return value
        except (OSError, ValueError, AttributeError):
            return None

    def refresh(self, *, allow_stale: bool = True) -> AuthoritativeView:
        try:
            response = self._request(
                "GET", self._url("/snapshot"), headers=self._headers,
                params={"protocol_version": PROTOCOL_VERSION}, timeout=10,
            )
            response.raise_for_status()
            snapshot = response.json()
            if snapshot.get("garden_id") != self.garden_id:
                raise MultiplayerUnavailable("coordinator returned a different garden")
            if snapshot.get("protocol_version") != PROTOCOL_VERSION:
                raise MultiplayerUnavailable("coordinator protocol version does not match this client")
            if (snapshot.get("member_id") != self.member_id
                    or snapshot.get("installation_id") != self.installation_id):
                raise MultiplayerUnavailable(
                    "coordinator credential belongs to a different member or installation"
                )
            _atomic_json(self._cache_path, snapshot)
            return AuthoritativeView(snapshot, False)
        except (httpx.HTTPError, ValueError, MultiplayerUnavailable) as exc:
            cached = self._load_cache() if allow_stale else None
            if cached is not None:
                return AuthoritativeView(cached, True, str(exc))
            raise MultiplayerUnavailable(f"authoritative coordinator unavailable: {exc}") from exc

    def command(self, path: str, body: dict[str, Any], *, kind: str, scope: str,
                expected_version: int) -> dict[str, Any]:
        current = self.refresh(allow_stale=False)
        actual = current.revision(kind, scope)
        if actual != expected_version:
            raise MultiplayerUnavailable(
                f"stale {kind} revision: requested {expected_version}, authoritative {actual}"
            )
        payload = dict(body)
        payload.setdefault("expected_version", expected_version)
        try:
            response = self._request(
                "POST", self._url(path), headers=self._headers, json=payload, timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise MultiplayerUnavailable(f"authoritative command rejected: {exc}") from exc

    def synchronize(self, snapshot: dict[str, Any] | None = None) -> list[str]:
        """Apply projections only when their recorded local base is unchanged."""
        snapshot = snapshot or self.refresh(allow_stale=False).snapshot
        try:
            ledger = json.loads(self._projection_path.read_text())
        except (OSError, ValueError):
            ledger = {}
        updates: list[tuple[str, Path, str, int]] = []
        conflicts: list[str] = []
        for row in snapshot.get("projections", []):
            relative = str(row.get("path", ""))
            target = (self.root / relative).resolve()
            if not relative or self.root not in target.parents:
                raise ProjectionConflict(f"unsafe authoritative projection path {relative!r}")
            content = str(row.get("markdown", ""))
            current = target.read_text() if target.exists() else ""
            prior = ledger.get(relative, {})
            allowed_hashes = {str(prior.get("content_hash", "")), str(row.get("base_revision", ""))}
            if current != content and _digest(current) not in allowed_hashes:
                conflicts.append(relative)
                continue
            updates.append((relative, target, content, int(row["version"])))
        if conflicts:
            raise ProjectionConflict("local authored content conflicts with authority: " + ", ".join(conflicts))
        changed: list[str] = []
        for relative, target, content, version in updates:
            if not target.exists() or target.read_text() != content:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                changed.append(relative)
            ledger[relative] = {"version": version, "content_hash": _digest(content)}
        _atomic_json(self._projection_path, ledger)
        return changed
