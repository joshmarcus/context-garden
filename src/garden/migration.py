"""Recoverable enrollment of a standalone garden in multiplayer authority."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .coordination import Coordinator
from .git_coordination import VERSION as GIT_PROTOCOL_VERSION
from .git_coordination import GitCoordinationError, GitStateStore
from .hosts.registry import enrolled_hosts, revoke_enrolled_hosts, worker_configuration
from .members import MemberRegistry, Principal
from .model import effective_owner, now_iso
from .runs import RunStore
from .store import Store

MIGRATION_VERSION = 1


class MigrationRefused(RuntimeError):
    """The requested cutover is not safe to perform yet."""


def _write_json(path: Path, value: dict[str, Any]) -> None:
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


def _plan_id(value: dict[str, Any]) -> str:
    body = {key: item for key, item in value.items() if key not in {"preview_id", "created_at"}}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def standalone_fence(root: Path) -> dict[str, Any] | None:
    """Return the durable cutover fence, independent of editable configuration."""
    path = root / ".garden" / "authority-mode.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if value.get("mode") == "multiplayer" else None


@dataclass
class GardenMigration:
    store: Store

    @property
    def directory(self) -> Path:
        return self.store.config.garden_dir / "migration"

    def preview(self, choices: dict[str, Any]) -> dict[str, Any]:
        """Inspect legacy state and persist a non-mutating, explicitly selectable plan."""
        registry = MemberRegistry(self.store.config.garden_dir)
        owner_map = choices.get("owner_map") or {}
        phase_choices = choices.get("phase_owners") or {}
        installation_map = choices.get("installations") or {}
        worker_map = choices.get("worker_enrollments") or {}
        if not all(isinstance(value, dict)
                   for value in (owner_map, phase_choices, installation_map, worker_map)):
            raise MigrationRefused(
                "owner_map, phase_owners, installations and worker_enrollments must be mappings"
            )

        active_members = registry.active_execution_member_ids("")
        tasks: list[dict[str, Any]] = []
        unknown: set[str] = set()
        for task in sorted(self.store.tasks().values(), key=lambda item: item.id):
            phase = self.store.phase(task.product, task.phase)
            legacy, source = effective_owner(task, phase)
            mapped = legacy in owner_map
            member = str(owner_map.get(legacy) or "") if legacy else ""
            eligible = registry.active_execution_member_ids(task.product)
            if legacy and (not mapped or (member and member not in eligible)):
                unknown.add(legacy)
            tasks.append({"id": task.id, "project": task.product, "phase": task.phase,
                          "legacy_owner": legacy, "owner_source": source,
                          "member_id": member if member in eligible else ""})

        phases: list[dict[str, Any]] = []
        for product in self.store.products():
            for phase in product.phases:
                chosen = str(phase_choices.get(phase.key, ""))
                if chosen and chosen not in registry.active_execution_member_ids(product.name):
                    raise MigrationRefused(f"phase owner {chosen!r} is not active for {product.name}")
                phases.append({"scope": phase.key, "legacy_default_owner": phase.default_owner,
                               "member_id": chosen})

        bindings: list[dict[str, str]] = []
        seen_installations: set[str] = set()
        registry_state = registry._read()
        enrolled_installations = {
            key: str(row.get("member_id", ""))
            for key, row in registry_state.get("installations", {}).items()
            if not row.get("revoked")
        }
        for installation, member in sorted(installation_map.items()):
            installation, member = str(installation), str(member)
            if (installation in seen_installations
                    or member not in active_members
                    or enrolled_installations.get(installation) != member):
                raise MigrationRefused("each installation must bind unambiguously to one active member")
            seen_installations.add(installation)
            bindings.append({"installation_id": installation, "member_id": member})

        runs = RunStore(self.store.config.garden_dir)
        active = [{"task_id": run.task_id, "run_id": run.run_id, "status": run.status}
                  for run in runs.active()]
        dirty = self._dirty_paths()
        pending = self._authority_snapshot_for_preview(registry)
        blockers = []
        if unknown:
            blockers.append("unknown legacy owners must be mapped or explicitly made unassigned")
        if active:
            blockers.append("active attempts must drain or be cancelled")
        if dirty:
            blockers.append("local edits must be committed, stashed, or discarded")
        if pending["pending_outbox"] or pending["blocking_effects"]:
            blockers.append("pending coordinator effects must be reconciled")
        if pending["active_claims"]:
            blockers.append("active coordinator claims must drain or be cancelled")
        if not bindings:
            blockers.append("each legacy server/operator installation must be bound to one member")
        missing_installations = sorted(set(enrolled_installations) - seen_installations)
        if missing_installations:
            blockers.append("all active operator installations must be assigned: "
                            + ", ".join(missing_installations))
        try:
            legacy_workers = {str(row["name"]): row
                              for row in enrolled_hosts(worker_configuration(self.store.config))}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationRefused(f"cannot inventory legacy worker enrollments: {exc}") from exc
        worker_bindings: list[dict[str, str]] = []
        worker_members: set[str] = set()
        for worker_name, member in sorted(worker_map.items()):
            worker_name, member = str(worker_name), str(member)
            if (worker_name not in legacy_workers or member not in active_members
                    or member in worker_members):
                raise MigrationRefused(
                    "each legacy worker enrollment must bind to one distinct active member"
                )
            worker_members.add(member)
            worker_bindings.append({"worker_name": worker_name, "member_id": member})
        missing_workers = sorted(set(legacy_workers) - set(worker_map))
        if missing_workers:
            blockers.append("all legacy worker enrollments must be assigned: "
                            + ", ".join(missing_workers))
        connection = self.store.config.get("multiplayer", {}) or {}
        git_settings = connection.get("git", {}) if isinstance(connection, dict) else {}
        if (not connection.get("enabled") or connection.get("garden_id") != self._garden_id(registry)
                or not connection.get("member_id") or not connection.get("installation_id")
                or not isinstance(git_settings, dict) or not git_settings.get("remote")
                or not git_settings.get("state_ref")):
            blockers.append("configure this installation for Garden Git before cutover")
        elif installation_map.get(connection["installation_id"]) != connection["member_id"]:
            blockers.append("configured Git installation must match its selected member binding")
        plan = {"version": MIGRATION_VERSION, "protocol_version": GIT_PROTOCOL_VERSION,
                "garden_id": self._garden_id(registry), "tasks": tasks, "phases": phases,
                "installations": bindings, "unknown_owners": sorted(unknown),
                "worker_enrollments": worker_bindings,
                "active_attempts": active, "pending_effects": pending["blocking_effects"],
                "active_claims": pending["active_claims"],
                "pending_projections": pending["pending_outbox"], "local_edits": dirty,
                "required_setup": blockers, "ready": not blockers}
        plan["preview_id"] = _plan_id(plan)
        plan["created_at"] = now_iso()
        _write_json(self.directory / "previews" / f"{plan['preview_id']}.json", plan)
        return plan

    def commit(self, preview_id: str, actor: Principal) -> dict[str, Any]:
        """Commit the exact selected plan, resumably, after rechecking every blocker."""
        path = self.directory / "previews" / f"{preview_id}.json"
        try:
            plan = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise MigrationRefused("chosen migration preview does not exist") from exc
        if plan.get("preview_id") != preview_id or _plan_id(plan) != preview_id:
            raise MigrationRefused("migration preview is corrupt or was edited")
        journal_path = self.directory / "cutover.json"
        journal = self._read(journal_path)
        resuming_revocation = bool(journal and journal.get("worker_revocation_pending"))
        choices = {"owner_map": {row["legacy_owner"]: row["member_id"] for row in plan["tasks"]
                                 if row["legacy_owner"]},
                   "phase_owners": {row["scope"]: row["member_id"] for row in plan["phases"]},
                   "installations": {row["installation_id"]: row["member_id"]
                                     for row in plan["installations"]},
                   "worker_enrollments": {row["worker_name"]: row["member_id"]
                                          for row in plan.get("worker_enrollments", [])}}
        if not resuming_revocation:
            current = self.preview(choices)
            if current["preview_id"] != preview_id or not current["ready"]:
                raise MigrationRefused("garden changed since preview; inspect and choose a new preview")
        journal = journal or {"version": MIGRATION_VERSION, "preview_id": preview_id,
                              "garden_id": plan["garden_id"], "status": "prepared",
                              "created_at": now_iso()}
        if journal.get("preview_id") != preview_id:
            raise MigrationRefused("another migration is already in progress")
        if "snapshot" not in journal:
            journal["snapshot"] = str(self._snapshot(preview_id))
            _write_json(journal_path, journal)
        rows = [("task", row["id"], row["member_id"]) for row in plan["tasks"]]
        rows += [("phase", row["scope"], row["member_id"]) for row in plan["phases"]]
        try:
            self._initialize_git_authority(
                plan, actor, operation_id=f"migration:{preview_id}:authority"
            )
        except (GitCoordinationError, PermissionError) as exc:
            raise MigrationRefused(str(exc)) from exc
        journal["authority"] = sorted(f"{kind}:{scope}" for kind, scope, _owner in rows)
        journal["worker_revocation_pending"] = True
        _write_json(journal_path, journal)
        worker_names = {row["worker_name"] for row in plan.get("worker_enrollments", [])}
        try:
            revoke_enrolled_hosts(worker_configuration(self.store.config), worker_names)
        except (OSError, ValueError) as exc:
            raise MigrationRefused(str(exc)) from exc
        journal["revoked_worker_enrollments"] = sorted(worker_names)
        journal.pop("worker_revocation_pending", None)
        _write_json(journal_path, journal)
        fence = {"version": MIGRATION_VERSION, "mode": "multiplayer", "garden_id": plan["garden_id"],
                 "preview_id": preview_id, "protocol_version": plan["protocol_version"],
                 "snapshot": journal["snapshot"], "committed_at": now_iso()}
        _write_json(self.store.config.garden_dir / "authority-mode.json", fence)
        journal["status"] = "committed"
        journal["completed_at"] = now_iso()
        _write_json(journal_path, journal)
        return journal

    def export_standalone(self, destination: Path) -> Path:
        """Export a quiescent standalone authority without rewriting historical evidence."""
        try:
            destination.resolve().relative_to(self.store.root)
        except ValueError:
            pass
        else:
            raise MigrationRefused("standalone export destination must be outside the live garden")
        if RunStore(self.store.config.garden_dir).active():
            raise MigrationRefused("active attempts must drain before standalone export")
        coordinator_path: Path | None = None
        if standalone_fence(self.store.root) or self.store.config.get("multiplayer.enabled", False):
            try:
                state = self._git_store().read()[1]
            except (GitCoordinationError, ValueError) as exc:
                raise MigrationRefused(
                    f"authoritative Git state is unavailable; standalone export refused: {exc}"
                ) from exc
            unresolved_effects = any(
                row.get("outcome", "pending") in {"pending", "unknown"}
                for row in state["effects"].values()
            )
            unresolved_permits = any(
                row.get("status", "pending") not in {"terminal", "fenced"}
                for row in state["permits"].values()
            )
            if (state["claims"] or unresolved_permits or unresolved_effects
                    or state["reservations"] or state["handoffs"]
                    or state["authority_changes"]):
                raise MigrationRefused(
                    "authoritative Git claims and effects must be quiescent before standalone export"
                )
        else:
            coordinator_path = self.store.config.garden_dir / "coordination.db"
            if not coordinator_path.exists():
                coordinator_path = None
        if coordinator_path:
            registry = MemberRegistry(self.store.config.garden_dir)
            snapshot = Coordinator(coordinator_path).snapshot(
                self._admin_for_preview(registry), self._garden_id(registry))
            if snapshot["active_claims"] or snapshot["pending_outbox"] or snapshot["blocking_effects"]:
                raise MigrationRefused("claims and effects must be quiescent before standalone export")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._archive(destination, standalone=True)
        return destination

    def _authority_snapshot_for_preview(self, registry: MemberRegistry) -> dict[str, Any]:
        """Inspect configured authority without creating either authority backend."""
        if self.store.config.get("multiplayer.enabled", False):
            try:
                git_store = self._git_store()
                if not git_store._remote_oid():
                    return {"active_claims": [], "pending_outbox": [], "blocking_effects": []}
                state = git_store.read()[1]
            except (GitCoordinationError, ValueError) as exc:
                raise MigrationRefused(f"cannot inspect Garden Git authority: {exc}") from exc
            effects = [
                {"operation_id": key, **row} for key, row in state["effects"].items()
                if row.get("outcome", "pending") in {"pending", "unknown"}
            ]
            permits = [
                {"operation_id": key, **row} for key, row in state["permits"].items()
                if row.get("status", "pending") not in {"terminal", "fenced"}
            ]
            return {
                "active_claims": list(state["claims"].values()),
                "pending_outbox": permits,
                "blocking_effects": effects,
            }
        coordinator_path = self.store.config.garden_dir / "coordination.db"
        if coordinator_path.exists():
            return Coordinator(coordinator_path).snapshot(
                self._admin_for_preview(registry), self._garden_id(registry)
            )
        return {"active_claims": [], "pending_outbox": [], "blocking_effects": []}

    def _git_store(self) -> GitStateStore:
        settings = self.store.config.get("multiplayer.git", {}) or {}
        return GitStateStore(
            self.store.root,
            garden_id=str(self.store.config.get("multiplayer.garden_id", "")),
            remote=str(settings.get("remote", "origin")),
            state_ref=str(settings.get("state_ref", "refs/heads/garden-state")),
        )

    def _initialize_git_authority(
        self, plan: dict[str, Any], actor: Principal, *, operation_id: str
    ) -> None:
        registry = MemberRegistry(self.store.config.garden_dir)
        local = registry._read()
        actor_member = local.get("members", {}).get(actor.member_id) or {}
        actor_installation = local.get("installations", {}).get(actor.installation_id) or {}
        if (actor.garden_id != plan["garden_id"] or actor.role != "administrator"
                or not actor_member.get("active")
                or actor_installation.get("member_id") != actor.member_id
                or actor_installation.get("revoked")):
            raise PermissionError("an active administrator installation must commit migration")
        members = {}
        assignments = local.get("assignments", {})
        for member_id, row in local.get("members", {}).items():
            member = {
                "active": bool(row.get("active")),
                "role": row.get("role", "member"),
                "project_visibility": row.get("project_visibility", "assigned"),
                "projects": list(row.get("projects") or ()),
            }
            if member_id in assignments:
                assignment = assignments[member_id]
                member["assignment"] = {
                    key: assignment[key]
                    for key in ("project", "phase", "generation", "enabled", "advance")
                    if key in assignment
                }
            members[member_id] = member
        installations = {
            row["installation_id"]: row["member_id"] for row in plan["installations"]
        }
        revoked_installations = {
            installation_id: row.get("member_id", "")
            for installation_id, row in local.get("installations", {}).items()
            if row.get("revoked")
        }
        entities = {}
        for row in plan["tasks"]:
            entities[f"task:{row['id']}"] = {
                "kind": "task", "project": row["project"], "scope": row["id"],
                "owner": row["member_id"], "authority_generation": 1, "version": 0,
            }
        for row in plan["phases"]:
            project = row["scope"].split("/", 1)[0]
            entities[f"phase:{row['scope']}"] = {
                "kind": "phase", "project": project, "scope": row["scope"],
                "owner": row["member_id"], "authority_generation": 1, "version": 0,
            }
        settings = self.store.config.get("multiplayer.git", {}) or {}
        GitStateStore.initialize_authority(
            self.store.root, garden_id=plan["garden_id"], operation_id=operation_id,
            members=members, installations=installations,
            revoked_installations=revoked_installations, entities=entities,
            remote=str(settings.get("remote", "origin")),
            state_ref=str(settings.get("state_ref", "refs/heads/garden-state")),
        )
        state = self._git_store().read()[1]
        if (state["claims"] or state["permits"] or state["effects"]
                or state["reservations"] or state["handoffs"]
                or state["authority_changes"]):
            raise GitCoordinationError("claims and effects must be quiescent during migration")

    def _snapshot(self, preview_id: str) -> Path:
        path = self.directory / "snapshots" / f"standalone-{preview_id}.tar.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._archive(path, standalone=True)
        return path

    def _archive(self, path: Path, *, standalone: bool) -> None:
        with tarfile.open(path, "w:gz") as archive:
            for source in sorted(self.store.root.rglob("*")):
                relative = source.relative_to(self.store.root)
                if (".git" in relative.parts or relative == Path(".garden/migration")
                        or Path(".garden/migration") in relative.parents
                        or relative == Path(".garden/worktrees")
                        or Path(".garden/worktrees") in relative.parents):
                    continue
                if standalone:
                    private = {"members.json", "members.lock", "coordination.db",
                               "coordination.db-wal", "coordination.db-shm",
                               "authority-mode.json", "authoritative-snapshot.json",
                               "authoritative-projections.json"}
                    if relative.parent == Path(".garden") and relative.name in private:
                        continue
                    if relative.parts[:2] == (".garden", "hosts"):
                        continue
                    if relative.name in {"garden.yaml", "garden.local.yaml"}:
                        self._add_standalone_config(archive, source, relative)
                        continue
                archive.add(source, arcname=relative, recursive=False)
            marker = json.dumps({"version": MIGRATION_VERSION, "mode": "standalone",
                                 "exported_at": now_iso()}).encode()
            info = tarfile.TarInfo(".garden/authority-mode.json")
            info.size = len(marker)
            archive.addfile(info, io.BytesIO(marker))

    @staticmethod
    def _add_standalone_config(archive: tarfile.TarFile, source: Path, relative: Path) -> None:
        value = yaml.safe_load(source.read_text()) or {}
        if not isinstance(value, dict):
            raise MigrationRefused(f"{relative} must contain a mapping")
        multiplayer = value.setdefault("multiplayer", {})
        if not isinstance(multiplayer, dict):
            raise MigrationRefused(f"{relative}: multiplayer must contain a mapping")
        multiplayer.clear()
        multiplayer["enabled"] = False
        body = yaml.safe_dump(value, sort_keys=False).encode()
        info = tarfile.TarInfo(str(relative))
        info.size = len(body)
        info.mode = source.stat().st_mode & 0o777
        archive.addfile(info, io.BytesIO(body))

    def _dirty_paths(self) -> list[str]:
        repositories = [("garden", self.store.root)]
        for product in self.store.products():
            configured = self.store.config.product(product.name)
            repo = configured.get("repo") if isinstance(configured, dict) else None
            if repo:
                repositories.append((product.name, (self.store.root / str(repo)).resolve()))
        dirty: list[str] = []
        for label, repository in repositories:
            proc = subprocess.run(["git", "status", "--porcelain"], cwd=repository,
                                  text=True, capture_output=True, check=False)
            if proc.returncode not in {0, 128}:
                raise MigrationRefused(f"could not inspect local edits in {label}")
            dirty.extend(f"{label}:{line[3:]}" for line in proc.stdout.splitlines() if line)
        return dirty

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _garden_id(registry: MemberRegistry) -> str:
        value = registry._read().get("garden_id", "")
        if not value:
            raise MigrationRefused("enroll a Garden administrator before previewing migration")
        return str(value)

    @staticmethod
    def _admin_for_preview(registry: MemberRegistry) -> Principal:
        state = registry._read()
        for member_id, row in state.get("members", {}).items():
            if row.get("active") and row.get("role") == "administrator":
                return Principal(str(state["garden_id"]), member_id, "migration-preview",
                                 "administrator", row.get("project_visibility", "all"),
                                 frozenset(row.get("projects") or ()))
        raise MigrationRefused("an active Garden administrator is required")
