"""Private multiplayer identity registry and shared authorization decisions.

Membership is runtime state, not garden context: the registry lives below ``.garden`` and
contains only salted credential verifiers.  A bearer credential binds a garden, member and
installation together, so callers never get to assert a separate trusted user id.
"""

from __future__ import annotations

import base64
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

Role = Literal["administrator", "member", "viewer"]
Visibility = Literal["all", "assigned"]
ROLES = frozenset({"administrator", "member", "viewer"})
VISIBILITIES = frozenset({"all", "assigned"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


_Result = TypeVar("_Result")


def _locked_mutation(method: Callable[..., _Result]) -> Callable[..., _Result]:
    """Serialize registry read/modify/write operations across local processes."""
    @functools.wraps(method)
    def locked(self: Any, *args: Any, **kwargs: Any) -> _Result:
        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return method(self, *args, **kwargs)
    return locked


@dataclass(frozen=True)
class Principal:
    garden_id: str
    member_id: str
    installation_id: str
    role: Role
    project_visibility: Visibility
    projects: frozenset[str] = frozenset()


def authorize(principal: Principal, operation: str, *, owner_id: str = "",
              project: str = "") -> bool:
    """One role vocabulary for coordinator, clients and HTTP actions.

    Administration is deliberately separate from authority over assigned work: even an
    administrator cannot mutate work owned by somebody else.
    """
    if operation == "read":
        return (principal.project_visibility == "all"
                or bool(project and project in principal.projects))
    if operation == "administer":
        return principal.role == "administrator"
    if operation == "mutate_work":
        return principal.role in {"administrator", "member"} and (
            not owner_id or owner_id == principal.member_id
        )
    return False


class MemberRegistry:
    """Process-safe, host-private identity state for one garden coordinator."""

    def __init__(self, garden_dir: Path):
        self.path = garden_dir / "members.json"

    @staticmethod
    def _valid_id(value: str, label: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError(f"{label} must be a stable 1-128 character identifier")
        return value

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "garden_id": "", "members": {}, "installations": {}}
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("unsupported multiplayer member registry")
        return value

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, sort_keys=True)
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

    @staticmethod
    def _verifier(secret: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 210_000).hex()

    @_locked_mutation
    def enroll_administrator(self, garden_id: str, member_id: str, installation_id: str) -> str:
        """Bootstrap an empty registry, returning the only copy of the new credential."""
        state = self._read()
        if state["members"] or state["installations"]:
            raise PermissionError("administrator enrollment is only allowed for an empty registry")
        state["garden_id"] = self._valid_id(garden_id, "garden_id")
        self._add_member(state, member_id, "administrator", "all")
        token = self._add_installation(state, member_id, installation_id)
        self._write(state)
        return token

    @_locked_mutation
    def add_member(self, actor: Principal, member_id: str, role: Role,
                   project_visibility: Visibility = "all",
                   projects: tuple[str, ...] = ()) -> None:
        state = self._authorized_state(actor)
        if not authorize(actor, "administer"):
            raise PermissionError("administrator role required")
        self._add_member(state, member_id, role, project_visibility, projects)
        self._write(state)

    @_locked_mutation
    def issue_installation(self, actor: Principal, member_id: str, installation_id: str) -> str:
        state = self._authorized_state(actor)
        if actor.member_id != member_id and not authorize(actor, "administer"):
            raise PermissionError("cannot issue another member's installation")
        token = self._add_installation(state, member_id, installation_id)
        self._write(state)
        return token

    @_locked_mutation
    def rotate_installation(self, actor: Principal, installation_id: str) -> str:
        state = self._authorized_state(actor)
        row = state["installations"].get(installation_id)
        if not row:
            raise KeyError(installation_id)
        if row["member_id"] != actor.member_id and not authorize(actor, "administer"):
            raise PermissionError("cannot rotate another member's installation")
        token = self._set_secret(state, installation_id)
        self._write(state)
        return token

    @_locked_mutation
    def revoke_installation(self, actor: Principal, installation_id: str) -> None:
        state = self._authorized_state(actor)
        row = state["installations"].get(installation_id)
        if not row:
            raise KeyError(installation_id)
        if row["member_id"] != actor.member_id and not authorize(actor, "administer"):
            raise PermissionError("cannot revoke another member's installation")
        row["revoked"] = True
        self._write(state)

    @_locked_mutation
    def set_member_active(self, actor: Principal, member_id: str, active: bool) -> None:
        state = self._authorized_state(actor)
        if not authorize(actor, "administer"):
            raise PermissionError("administrator role required")
        if member_id not in state["members"]:
            raise KeyError(member_id)
        state["members"][member_id]["active"] = bool(active)
        self._write(state)

    def authenticate(self, token: str) -> Principal | None:
        try:
            version, encoded_garden, encoded_installation, secret = token.split(".", 3)
            if version != "v1":
                return None
            garden_id = self._decode_id(encoded_garden)
            installation_id = self._decode_id(encoded_installation)
            state = self._read()
            installation = state["installations"][installation_id]
            member = state["members"][installation["member_id"]]
            salt = base64.urlsafe_b64decode(installation["salt"] + "==")
            actual = self._verifier(secret, salt)
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError, OSError):
            return None
        if garden_id != state["garden_id"] or installation.get("revoked") or not member.get("active"):
            return None
        if not secrets.compare_digest(actual, installation["verifier"]):
            return None
        return Principal(garden_id, installation["member_id"], installation_id,
                         member["role"], member["project_visibility"],
                         frozenset(member.get("projects") or ()))

    @staticmethod
    def _decode_id(value: str) -> str:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding).decode()

    def _authorized_state(self, actor: Principal) -> dict:
        state = self._read()
        current = state["members"].get(actor.member_id)
        installation = state["installations"].get(actor.installation_id)
        if (actor.garden_id != state["garden_id"] or not current or not current.get("active")
                or not installation or installation.get("revoked")
                or installation.get("member_id") != actor.member_id
                or actor.role != current.get("role")
                or actor.project_visibility != current.get("project_visibility")
                or actor.projects != frozenset(current.get("projects") or ())):
            raise PermissionError("caller is no longer an active garden member")
        # Use current registry capabilities, never stale caller-supplied role fields.
        return state

    def _add_member(self, state: dict, member_id: str, role: str, visibility: str,
                    projects: tuple[str, ...] = ()) -> None:
        member_id = self._valid_id(member_id, "member_id")
        if role not in ROLES:
            raise ValueError("role must be administrator, member, or viewer")
        if visibility not in VISIBILITIES:
            raise ValueError("project_visibility must be all or assigned")
        normalized_projects = sorted({self._valid_id(value, "project") for value in projects})
        if visibility == "all" and normalized_projects:
            raise ValueError("projects may only be supplied with assigned visibility")
        if member_id in state["members"]:
            raise ValueError("member already exists")
        state["members"][member_id] = {"role": role, "active": True,
                                        "project_visibility": visibility,
                                        "projects": normalized_projects}

    def _add_installation(self, state: dict, member_id: str, installation_id: str) -> str:
        installation_id = self._valid_id(installation_id, "installation_id")
        if member_id not in state["members"]:
            raise KeyError(member_id)
        if installation_id in state["installations"]:
            raise ValueError("installation already exists")
        state["installations"][installation_id] = {"member_id": member_id, "revoked": False}
        return self._set_secret(state, installation_id)

    def _set_secret(self, state: dict, installation_id: str) -> str:
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        state["installations"][installation_id].update({
            "salt": base64.urlsafe_b64encode(salt).decode().rstrip("="),
            "verifier": self._verifier(secret, salt), "revoked": False,
        })
        garden = base64.urlsafe_b64encode(state["garden_id"].encode()).decode().rstrip("=")
        installation = base64.urlsafe_b64encode(installation_id.encode()).decode().rstrip("=")
        return f"v1.{garden}.{installation}.{secret}"
