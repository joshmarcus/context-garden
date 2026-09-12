"""Fail-closed local client and conflict-aware Markdown projection cache."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Callable
from contextlib import contextmanager
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


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
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

    CLAIM_RENEWAL_MARGIN = dt.timedelta(seconds=10)

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
        self._claims: dict[tuple[str, str], dict[str, Any]] = {}

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
                    or value.get("protocol_version") != PROTOCOL_VERSION
                    or value.get("member_id") != self.member_id
                    or value.get("installation_id") != self.installation_id):
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
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    detail = f": {exc.response.json().get('detail', '')}"
                except (ValueError, AttributeError):
                    detail = f": {exc.response.text}"
            raise MultiplayerUnavailable(f"authoritative command rejected: {exc}{detail}") from exc

    def claim(self, *, kind: str, scope: str, owner_id: str,
              authority_generation: int, expected_version: int) -> dict[str, Any]:
        """Acquire (or reuse) this installation's fenced lifecycle lease."""
        key = (kind, scope)
        current = self._claims.get(key)
        if (current and current.get("owner_id") == owner_id
                and int(current.get("authority_generation", -1)) == authority_generation
                and self._claim_is_current(current)):
            return current
        operation_id = f"claim:{self.installation_id}:{kind}:{scope}:{uuid.uuid4().hex}"
        claim = self.command(
            "/claims", {
                "kind": kind, "scope": scope, "accepted_owner": owner_id,
                "authority_generation": authority_generation,
                "operation_id": operation_id,
                "replaces_operation_id": str(current.get("operation_id", "")) if current else "",
            }, kind=kind, scope=scope, expected_version=expected_version,
        )
        self._claims[key] = claim
        return claim

    def _claim_is_current(self, claim: dict[str, Any]) -> bool:
        try:
            expires = dt.datetime.fromisoformat(str(claim["lease_expires_at"]))
            if expires.tzinfo is None:
                return False
            return expires > dt.datetime.now(dt.UTC) + self.CLAIM_RENEWAL_MARGIN
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _is_stale_claim_rejection(exc: MultiplayerUnavailable) -> bool:
        message = str(exc).lower()
        return "stale" in message and ("expired" in message or "fencing lease" in message)

    @contextmanager
    def effect(self, *, kind: str, scope: str, owner_id: str,
               authority_generation: int, expected_version: int,
               effect_key: str, provider: str = "scheduler"):
        """Fence one mutation and leave uncertain effects blocked for reconciliation."""
        operation_id = f"effect:{self.installation_id}:{uuid.uuid4().hex}"
        key = (kind, scope)
        for attempt in range(2):
            claim = self.claim(kind=kind, scope=scope, owner_id=owner_id,
                               authority_generation=authority_generation,
                               expected_version=expected_version)
            try:
                self.command(
                    "/effects", {
                        "claim": claim, "provider": provider, "effect_key": effect_key,
                        "operation_id": operation_id, "credential_scope": f"{provider}:write",
                        "precondition": f"authority={expected_version}",
                        "request": {"kind": kind, "scope": scope},
                    }, kind=kind, scope=scope, expected_version=expected_version,
                )
                break
            except MultiplayerUnavailable as exc:
                if attempt or not self._is_stale_claim_rejection(exc):
                    raise
                self._claims.pop(key, None)
        try:
            yield claim
        except BaseException:
            self._finish_effect(operation_id, "unknown")
            raise
        else:
            self._finish_effect(operation_id, "succeeded")

    def _finish_effect(self, operation_id: str, outcome: str) -> None:
        try:
            response = self._request(
                "POST", self._url(f"/effects/{operation_id}/finish"),
                headers=self._headers, json={"outcome": outcome}, timeout=10,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MultiplayerUnavailable(f"could not record authoritative effect outcome: {exc}") from exc

    def acknowledge_cancellations(
        self, snapshot: dict[str, Any], cancel: Callable[[str, str], bool]
    ) -> list[str]:
        """Stop locally owned work before acknowledging a coordinator handoff request."""
        acknowledged: list[str] = []
        for request in snapshot.get("cancellation_requests", []):
            if request.get("installation") != self.installation_id:
                continue
            kind, scope = str(request["kind"]), str(request["scope"])
            if not cancel(kind, scope):
                continue
            try:
                response = self._request(
                    "POST", self._url("/cancellations/acknowledge"), headers=self._headers,
                    json={"kind": kind, "scope": scope, "fence": int(request["fence"])},
                    timeout=10,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise MultiplayerUnavailable(
                    f"could not acknowledge fenced worker cancellation: {exc}"
                ) from exc
            acknowledged.append(f"{kind}:{scope}")
            self._claims.pop((kind, scope), None)
        return acknowledged

    def retain_stale_evidence(self, *, kind: str, scope: str, evidence_id: str,
                              operation_id: str, payload: dict[str, Any]) -> None:
        try:
            response = self._request(
                "POST", self._url("/stale-evidence"), headers=self._headers,
                json={"kind": kind, "scope": scope, "evidence_id": evidence_id,
                      "operation_id": operation_id, "payload": payload}, timeout=10,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MultiplayerUnavailable(f"could not retain stale evidence: {exc}") from exc

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
                _atomic_text(target, content)
                changed.append(relative)
            ledger[relative] = {"version": version, "content_hash": _digest(content)}
        _atomic_json(self._projection_path, ledger)
        return changed

    def projection_lag(self, snapshot: dict[str, Any]) -> list[str]:
        """Name server projections this checkout has not applied at their current version."""
        try:
            ledger = json.loads(self._projection_path.read_text())
        except (OSError, ValueError):
            ledger = {}
        return [
            f"{row['kind']}:{row['scope']}@{row['version']}"
            for row in snapshot.get("projections", [])
            if int(ledger.get(str(row.get("path", "")), {}).get("version", 0)) < int(row["version"])
        ]
