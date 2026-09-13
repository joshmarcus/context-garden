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

from .graph import blockers
from .model import Phase, Status, Task, effective_owner, phase_refusal

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


@dataclass(frozen=True)
class WorkAssignment:
    """One member's explicit execution cursor; absence means no executable scope."""

    member_id: str
    project: str
    phase: str
    generation: int
    enabled: bool
    advance: bool


@dataclass(frozen=True)
class PhaseOwner:
    """Versioned authority for phase-wide operations, including explicit vacancy."""

    project: str
    phase: str
    owner_id: str
    generation: int
    changed_by: str


@dataclass(frozen=True)
class OwnerChangePreview:
    """A reviewable ownership mutation plan; applying it is intentionally separate."""

    change: str
    before: str
    after: str
    affected_issues: tuple[str, ...]


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
        return (principal.role in {"administrator", "member"}
                and bool(owner_id) and owner_id == principal.member_id
                and authorize(principal, "read", project=project))
    return False


class MemberRegistry:
    """Process-safe, host-private identity state for one garden coordinator."""

    def __init__(self, garden_dir: Path, coordinator: Any | None = None):
        self.path = garden_dir / "members.json"
        self.coordinator = coordinator

    def _fence_member_claims(self, actor: Principal, state: dict, member_id: str, *,
                             mutation: str, generation: int,
                             installation_id: str = "") -> None:
        """Fence claims invalidated by a membership mutation before acknowledging it."""
        if self.coordinator is None:
            return
        self.coordinator.fence_member_claims(
            actor, garden_id=actor.garden_id, member_id=member_id,
            installation_id=installation_id,
            operation_id=f"membership:{mutation}:{member_id}:{generation}",
        )

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

    def active_member_ids(self, project: str = "") -> frozenset[str]:
        """Return active members allowed to see ``project`` (viewers included for reads)."""
        state = self._read()
        return frozenset(member_id for member_id, member in state["members"].items()
                         if member.get("active") and (not project
                         or member.get("project_visibility") == "all"
                         or project in (member.get("projects") or ())))

    def active_execution_member_ids(self, project: str) -> frozenset[str]:
        """Active non-viewers with access to a project may own executable work."""
        state = self._read()
        return frozenset(member_id for member_id, member in state["members"].items()
                         if member.get("active") and member.get("role") != "viewer"
                         and (member.get("project_visibility") == "all"
                         or project in (member.get("projects") or ())))

    def effective_task_owner(self, task: Task, phase: Phase) -> tuple[str, str]:
        """Resolve metadata precedence, but confer authority only on active project members."""
        owner_id, source = effective_owner(task, phase)
        if not owner_id:
            return "", source
        if owner_id not in self.active_execution_member_ids(task.product):
            return "", "invalid"
        return owner_id, source

    def preview_default_owner_change(self, phase: Phase, tasks: list[Task],
                                     owner_id: str | None) -> OwnerChangePreview:
        """Show only inherited issues affected by a proposed default-owner bulk change."""
        after = owner_id or ""
        if after and after not in self.active_execution_member_ids(phase.product):
            raise ValueError("default owner must be an active project member")
        affected = tuple(sorted(task.id for task in tasks
                                if task.product == phase.product and task.phase == phase.name
                                and not task.owner and not task.owner_unassigned
                                and phase.default_owner != after))
        return OwnerChangePreview("default_owner", phase.default_owner, after, affected)

    def preview_task_owner_change(self, task: Task, phase: Phase,
                                  owner_id: str | None) -> OwnerChangePreview:
        """Show an explicit issue override separately from changing a phase default."""
        after = owner_id or ""
        if after and after not in self.active_execution_member_ids(task.product):
            raise ValueError("task owner must be an active project member")
        before = self.effective_task_owner(task, phase)[0]
        affected = (task.id,) if before != after else ()
        return OwnerChangePreview("task_owner", before, after, affected)

    @staticmethod
    def dependency_information(task: Task, tasks: dict[str, Task],
                               permitted_projects: frozenset[str]) -> dict[str, object]:
        """Expose permitted blocker ids while retaining an opaque count across boundaries."""
        visible: list[str] = []
        hidden = 0
        for dependency_id in blockers(task, tasks, stack=True):
            dependency = tasks.get(dependency_id)
            if dependency is not None and dependency.product in permitted_projects:
                visible.append(dependency_id)
            else:
                hidden += 1
        return {"blockers": tuple(sorted(visible)), "inaccessible_blocker_count": hidden}

    def assignment(self, member_id: str) -> WorkAssignment | None:
        row = (self._read().get("assignments") or {}).get(member_id)
        if not row:
            return None
        return WorkAssignment(member_id, str(row["project"]), str(row["phase"]),
                              int(row["generation"]), bool(row["enabled"]),
                              bool(row.get("advance", False)))

    @_locked_mutation
    def set_assignment(self, actor: Principal, member_id: str, project: str, phase: str, *,
                       enabled: bool = True, advance: bool = False,
                       expected_generation: int = 0) -> WorkAssignment:
        """Create or replace a cursor using optimistic concurrency and administrator authority."""
        state = self._authorized_state(actor)
        if not authorize(actor, "administer"):
            raise PermissionError("administrator role required")
        if member_id not in self.active_execution_member_ids(project):
            raise ValueError("assignment owner must be an active garden member")
        self._valid_id(project, "project")
        self._valid_id(phase, "phase")
        rows = state.setdefault("assignments", {})
        current = rows.get(member_id)
        generations = state.setdefault("assignment_generations", {})
        generation = (int(current.get("generation", 0)) if current
                      else int(generations.get(member_id, 0)))
        if generation != expected_generation:
            raise RuntimeError("stale assignment generation")
        row = {"project": project, "phase": phase, "generation": generation + 1,
               "enabled": bool(enabled), "advance": bool(advance),
               "changed_by": actor.member_id}
        self._fence_member_claims(actor, state, member_id, mutation="assignment",
                                  generation=generation + 1)
        rows[member_id] = row
        generations[member_id] = generation + 1
        self._write(state)
        return WorkAssignment(member_id, project, phase, generation + 1,
                              bool(enabled), bool(advance))

    @_locked_mutation
    def clear_assignment(self, actor: Principal, member_id: str, *,
                         expected_generation: int) -> None:
        state = self._authorized_state(actor)
        if not authorize(actor, "administer"):
            raise PermissionError("administrator role required")
        rows = state.setdefault("assignments", {})
        current = rows.get(member_id)
        if not current or int(current.get("generation", 0)) != expected_generation:
            raise RuntimeError("stale assignment generation")
        generation = int(current["generation"]) + 1
        self._fence_member_claims(actor, state, member_id, mutation="assignment",
                                  generation=generation)
        del rows[member_id]
        state.setdefault("assignment_generations", {})[member_id] = generation
        self._write(state)

    def phase_owner(self, project: str, phase: str) -> PhaseOwner | None:
        row = (self._read().get("phase_owners") or {}).get(f"{project}/{phase}")
        if row is None:
            return None
        return PhaseOwner(project, phase, str(row.get("owner_id") or ""),
                          int(row["generation"]), str(row["changed_by"]))

    @_locked_mutation
    def set_phase_owner(self, actor: Principal, project: str, phase: str,
                        owner_id: str | None, *, expected_generation: int = 0) -> PhaseOwner:
        """Assign/transfer/vacate phase-operation authority without inferring a replacement."""
        state = self._authorized_state(actor)
        if not authorize(actor, "administer"):
            raise PermissionError("administrator role required")
        self._valid_id(project, "project")
        self._valid_id(phase, "phase")
        if owner_id is not None and owner_id not in self.active_execution_member_ids(project):
            raise ValueError("phase owner must be an active garden member")
        rows = state.setdefault("phase_owners", {})
        key = f"{project}/{phase}"
        current = rows.get(key)
        generation = int(current.get("generation", 0)) if current else 0
        if generation != expected_generation:
            raise RuntimeError("stale phase owner generation")
        row = {"owner_id": owner_id or "", "generation": generation + 1,
               "changed_by": actor.member_id}
        rows[key] = row
        self._write(state)
        return PhaseOwner(project, phase, row["owner_id"], generation + 1, actor.member_id)

    def authorize_phase_operation(self, principal: Principal, project: str, phase: str) -> bool:
        """Only the current, active, project-visible explicit owner may operate a phase."""
        try:
            self._authorized_state(principal)
        except PermissionError:
            return False
        owner = self.phase_owner(project, phase)
        return bool(owner and owner.owner_id == principal.member_id
                    and principal.member_id in self.active_execution_member_ids(project)
                    and authorize(principal, "read", project=project))

    def executable_tasks(self, member_id: str, tasks: dict[str, Task],
                         phases: dict[str, Phase]) -> list[Task]:
        """Apply cursor, ownership, status, holds and dependency gates to executable work."""
        cursor = self.assignment(member_id)
        if (not cursor or not cursor.enabled
                or member_id not in self.active_execution_member_ids(cursor.project)):
            return []
        return [task for task in tasks.values()
                if task.product == cursor.project and task.phase == cursor.phase
                and task.status == Status.READY and not blockers(task, tasks, stack=True)
                and not phase_refusal(phases[task.key], task)
                and self.effective_task_owner(task, phases[task.key])[0] == member_id]

    def can_advance_assignment(self, member_id: str, tasks: dict[str, Task],
                               phases: dict[str, Phase]) -> bool:
        """Configured advance remains blocked until everybody's current phase work is done."""
        cursor = self.assignment(member_id)
        if not cursor or not cursor.enabled or not cursor.advance:
            return False
        for task in tasks.values():
            if task.product != cursor.project or task.phase != cursor.phase or task.status.terminal:
                continue
            return False
        return True

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
        generation = int(row.get("credential_generation", 0)) + 1
        self._fence_member_claims(
            actor, state, str(row["member_id"]), mutation=f"credential:{installation_id}",
            generation=generation, installation_id=installation_id,
        )
        token = self._set_secret(state, installation_id)
        row["credential_generation"] = generation
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
        generation = int(row.get("credential_generation", 0)) + 1
        self._fence_member_claims(
            actor, state, str(row["member_id"]), mutation=f"credential:{installation_id}",
            generation=generation, installation_id=installation_id,
        )
        row["revoked"] = True
        row["credential_generation"] = generation
        self._write(state)

    @_locked_mutation
    def set_member_active(self, actor: Principal, member_id: str, active: bool) -> None:
        state = self._authorized_state(actor)
        if not authorize(actor, "administer"):
            raise PermissionError("administrator role required")
        if member_id not in state["members"]:
            raise KeyError(member_id)
        row = state["members"][member_id]
        generation = int(row.get("active_generation", 0)) + 1
        if not active:
            self._fence_member_claims(actor, state, member_id, mutation="disable",
                                      generation=generation)
        row["active"] = bool(active)
        row["active_generation"] = generation
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
