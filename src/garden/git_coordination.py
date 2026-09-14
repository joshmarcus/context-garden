"""Service-free multiplayer coordination over a dedicated Garden Git ref."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL = "context-garden/git-coordination"
VERSION = 1
HANDOFF_ENTITY_FIELDS = frozenset(
    {"owner", "authority_generation", "kind", "scope", "draining", "pending_owner"}
)
CLAIM_IDENTITY_FIELDS = (
    "operation_id",
    "kind",
    "scope",
    "owner_id",
    "authority_generation",
    "actor",
    "installation",
)
EFFECT_OUTCOMES = {"pending", "unknown", "succeeded", "failed"}
DEFAULT_TRANSPORT_TIMEOUT_SECONDS = 30.0


class GitCoordinationError(RuntimeError):
    """The shared state is unavailable, invalid, or rejected."""


class GitContention(GitCoordinationError):
    """Another installation accepted a transaction first."""


class GitTransportTimeout(GitCoordinationError):
    """A Git operation exceeded its deadline and its process tree was stopped."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _run_process(
    cwd: Path,
    args: list[str],
    *,
    timeout_seconds: float = DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise GitTransportTimeout(
            f"git operation timed out after {timeout_seconds:g} seconds: "
            f"{' '.join(args[1:4])}"
        ) from exc
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _run(
    cwd: Path,
    *args: str,
    input_text: str | None = None,
    timeout_seconds: float = DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
) -> str:
    result = _run_process(
        cwd, ["git", *args], timeout_seconds=timeout_seconds, input_text=input_text
    )
    if result.returncode:
        raise GitCoordinationError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def empty_state(garden_id: str) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "protocol_version": VERSION,
        "garden_id": garden_id,
        "sequence": 0,
        "members": {},
        "installations": {},
        "revoked_installations": {},
        "entities": {},
        "claims": {},
        "operations": {},
        "permits": {},
        "effects": {},
        "handoffs": {},
        "authority_changes": {},
        "reservations": {},
        "policy": {"pools": {}},
        "recovery": [],
    }


def validate_state(state: dict[str, Any], garden_id: str) -> None:
    if state.get("protocol") != PROTOCOL or state.get("protocol_version") != VERSION:
        raise GitCoordinationError("unsupported coordination protocol")
    if state.get("garden_id") != garden_id:
        raise GitCoordinationError("coordination state belongs to a different garden")
    if isinstance(state.get("sequence"), bool) or not isinstance(state.get("sequence"), int):
        raise GitCoordinationError("invalid coordination sequence")
    for name in (
        "members",
        "installations",
        "revoked_installations",
        "entities",
        "claims",
        "operations",
        "permits",
        "effects",
        "handoffs",
        "authority_changes",
        "reservations",
    ):
        if not isinstance(state.get(name), dict):
            raise GitCoordinationError(f"invalid coordination {name}")
    if not isinstance(state.get("recovery"), list):
        raise GitCoordinationError("invalid coordination recovery history")
    if not isinstance(state.get("policy"), dict) or not isinstance(
        state["policy"].get("pools"), dict
    ):
        raise GitCoordinationError("invalid coordination policy")


@dataclass(frozen=True)
class AcceptedTransaction:
    operation_id: str
    commit: str
    result: dict[str, Any]
    replayed: bool = False


class GitStateStore:
    """Validate and conditionally advance one linear remote coordination ref."""

    def __init__(
        self,
        repo: Path,
        *,
        garden_id: str,
        remote: str = "origin",
        state_ref: str = "refs/heads/garden-state",
        retries: int = 5,
        timeout_seconds: float = DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
    ):
        if not state_ref.startswith("refs/heads/") or any(c.isspace() for c in state_ref):
            raise ValueError("multiplayer.git.state_ref must be a full branch ref")
        self.repo = repo.resolve()
        self.garden_id, self.remote, self.state_ref = garden_id, remote, state_ref
        self.retries = retries
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("multiplayer.git.timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.evidence_dir = self.repo / ".garden" / "coordination-recovery"
        self.observed_path = self.repo / ".garden" / "coordination-observed"

    @classmethod
    def initialize(
        cls,
        repo: Path,
        *,
        garden_id: str,
        remote: str = "origin",
        state_ref: str = "refs/heads/garden-state",
    ) -> str:
        """Explicitly create a missing state ref; normal mutation never bootstraps it."""
        store = cls(repo, garden_id=garden_id, remote=remote, state_ref=state_ref)
        if store._remote_oid():
            raise GitCoordinationError("coordination state ref already exists")
        commit = store._commit(empty_state(garden_id), None, "initialize")
        store._push(commit, "")
        return commit

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitTransportTimeout(
                f"git operation timed out after {self.timeout_seconds:g} seconds: fetch"
            )
        return remaining

    def _remote_oid(self, timeout_seconds: float | None = None) -> str:
        output = _run(
            self.repo, "ls-remote", "--refs", self.remote, self.state_ref,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
        )
        return output.split()[0] if output else ""

    def _fetch(self, deadline: float) -> str:
        observed = ""
        for _ in range(3):
            before = self._remote_oid(self._remaining(deadline))
            if not before:
                self._preserve("unexpected-ref-deletion", {"state_ref": self.state_ref})
                raise GitCoordinationError(
                    "coordination state ref is missing; initialize it explicitly"
                )
            _run(
                self.repo,
                "fetch",
                "--no-tags",
                self.remote,
                f"+{self.state_ref}:refs/garden/state-observed",
                timeout_seconds=self._remaining(deadline),
            )
            after = self._remote_oid(self._remaining(deadline))
            observed = _run(
                self.repo,
                "rev-parse",
                "refs/garden/state-observed",
                timeout_seconds=self._remaining(deadline),
            )
            if before == after == observed:
                break
        else:
            raise GitContention("coordination ref kept changing during fetch")
        try:
            prior = self.observed_path.read_text().strip()
        except OSError:
            prior = ""
        if prior:
            ancestry = _run_process(
                self.repo,
                ["git", "merge-base", "--is-ancestor", prior, observed],
                timeout_seconds=self._remaining(deadline),
            )
            if ancestry.returncode:
                self._preserve(
                    "unexpected-ref-rewrite", {"previous": prior, "observed": observed}
                )
                raise GitCoordinationError("coordination ref was unexpectedly rewritten")
        self._validate_history(observed, deadline)
        self.observed_path.parent.mkdir(parents=True, exist_ok=True)
        self.observed_path.write_text(observed + "\n")
        return observed

    def _validate_history(self, head: str, deadline: float) -> None:
        commits = _run(
            self.repo,
            "rev-list",
            "--reverse",
            head,
            timeout_seconds=self._remaining(deadline),
        ).splitlines()
        previous = ""
        sequence = -1
        for commit in commits:
            parents = _run(
                self.repo,
                "show",
                "-s",
                "--format=%P",
                commit,
                timeout_seconds=self._remaining(deadline),
            ).split()
            if previous and parents != [previous]:
                self._preserve("non-linear-history", {"commit": commit, "parents": parents})
                raise GitCoordinationError("coordination history is not a single-parent chain")
            if not previous and parents:
                self._preserve("rewritten-root", {"commit": commit, "parents": parents})
                raise GitCoordinationError("coordination history has an unexpected root")
            state = self._read(commit, deadline=deadline)
            validate_state(state, self.garden_id)
            if state["sequence"] != sequence + 1:
                raise GitCoordinationError("coordination history sequence is corrupt")
            sequence = state["sequence"]
            previous = commit

    def _read(self, commit: str, *, deadline: float | None = None) -> dict[str, Any]:
        def timeout() -> float:
            return self._remaining(deadline) if deadline is not None else self.timeout_seconds

        try:
            names = _run(
                self.repo,
                "ls-tree",
                "--name-only",
                commit,
                timeout_seconds=timeout(),
            ).splitlines()
            if names != ["state.json"]:
                raise GitCoordinationError("coordination commit must contain only state.json")
            state = json.loads(
                _run(
                    self.repo,
                    "show",
                    f"{commit}:state.json",
                    timeout_seconds=timeout(),
                )
            )
            # Version-1 refs created before acknowledged handoffs remain readable. The
            # first accepted handoff transaction materializes the additive table.
            state.setdefault("handoffs", {})
            state.setdefault("authority_changes", {})
            state.setdefault("revoked_installations", {})
            return state
        except (ValueError, TypeError) as exc:
            raise GitCoordinationError("coordination state is not valid JSON") from exc

    def read(self) -> tuple[str, dict[str, Any]]:
        deadline = time.monotonic() + self.timeout_seconds
        head = self._fetch(deadline)
        return head, self._read(head, deadline=deadline)

    def _commit(self, state: dict[str, Any], parent: str | None, operation_id: str) -> str:
        content = (_canonical(state) + "\n").encode()
        blob = _run(self.repo, "hash-object", "-w", "--stdin", input_text=content.decode())
        tree = _run(self.repo, "mktree", input_text=f"100644 blob {blob}\tstate.json\n")
        args = ["commit-tree", tree, "-m", f"garden transaction {operation_id}"]
        if parent:
            args[2:2] = ["-p", parent]
        return _run(self.repo, *args)

    def _push(self, commit: str, expected: str) -> None:
        lease = f"--force-with-lease={self.state_ref}:{expected}"
        result = _run_process(
            self.repo,
            ["git", "push", "--porcelain", lease, self.remote, f"{commit}:{self.state_ref}"],
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode:
            if "stale info" in result.stderr or "rejected" in result.stdout + result.stderr:
                raise GitContention("coordination transaction lost remote compare-and-swap")
            raise GitCoordinationError(result.stderr.strip() or "coordination push failed")

    def _preserve(self, reason: str, detail: dict[str, Any]) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "detail": detail, "observed_at": time.time()}
        name = hashlib.sha256(_canonical(payload).encode()).hexdigest()[:16]
        (self.evidence_dir / f"{name}.json").write_text(_canonical(payload) + "\n")

    def _obligation_path(self, operation_id: str) -> Path:
        name = hashlib.sha256(operation_id.encode()).hexdigest()
        return self.evidence_dir / f"pending-push-{name}.json"

    def _write_obligation(
        self, operation_id: str, fingerprint: str, commit: str, expected: str
    ) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "operation_id": operation_id,
            "fingerprint": fingerprint,
            "commit": commit,
            "expected": expected,
        }
        path = self._obligation_path(operation_id)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.evidence_dir, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(_canonical(payload) + "\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)

    def _clear_obligation(self, operation_id: str) -> None:
        self._obligation_path(operation_id).unlink(missing_ok=True)

    def transact(
        self,
        operation_id: str,
        inputs: dict[str, Any],
        mutate: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> AcceptedTransaction:
        if not operation_id or len(operation_id) > 200:
            raise ValueError("operation_id must be non-empty and bounded")
        fingerprint = hashlib.sha256(_canonical(inputs).encode()).hexdigest()
        for attempt in range(self.retries):
            head, state = self.read()
            old = state["operations"].get(operation_id)
            if old:
                if old.get("fingerprint") != fingerprint:
                    self._preserve("duplicate-operation-mismatch", {"operation_id": operation_id})
                    raise GitCoordinationError(
                        "operation ID was already used with different inputs"
                    )
                self._clear_obligation(operation_id)
                return AcceptedTransaction(
                    operation_id, head, deepcopy(old.get("result", {})), True
                )
            obligation = self._obligation_path(operation_id)
            if obligation.exists():
                try:
                    pending = json.loads(obligation.read_text())
                except (OSError, ValueError, TypeError) as exc:
                    raise GitCoordinationError("invalid pending push obligation") from exc
                if pending.get("fingerprint") != fingerprint:
                    raise GitCoordinationError(
                        "operation ID has an unresolved push with different inputs"
                    )
                # The successful read above established that the prior candidate was not
                # accepted, so it is now safe to retry the same logical operation.
                self._clear_obligation(operation_id)
            next_state = deepcopy(state)
            result = mutate(next_state)
            next_state["sequence"] += 1
            record = {
                "fingerprint": fingerprint,
                "inputs": deepcopy(inputs),
                "actor": inputs.get("actor"),
                "installation": inputs.get("installation"),
                "result": deepcopy(result),
            }
            next_state["operations"][operation_id] = record
            validate_state(next_state, self.garden_id)
            commit = self._commit(next_state, head, operation_id)
            self._write_obligation(operation_id, fingerprint, commit, head)
            try:
                self._push(commit, head)
                self._clear_obligation(operation_id)
                return AcceptedTransaction(operation_id, commit, result)
            except GitContention:
                self._clear_obligation(operation_id)
                if attempt + 1 == self.retries:
                    raise
                time.sleep(min(0.01 * (2**attempt), 0.1))
            except GitCoordinationError:
                # A transport can lose the acknowledgement after the server accepted it.
                # Resolve the stable ID against accepted history before allowing a retry.
                resolved_head, resolved = self.read()
                accepted = resolved["operations"].get(operation_id)
                if accepted and accepted.get("fingerprint") == fingerprint:
                    self._clear_obligation(operation_id)
                    return AcceptedTransaction(
                        operation_id, resolved_head, deepcopy(accepted.get("result", {})), True
                    )
                self._clear_obligation(operation_id)
                raise
        raise GitContention("coordination contention retry limit reached")

    def apply(
        self,
        operation_id: str,
        *,
        actor: str,
        installation: str,
        expected_versions: dict[str, int],
        changes: dict[str, Any],
    ) -> AcceptedTransaction:
        """Atomically apply validated entity, claim, permit, effect, and reservation changes."""
        inputs = {
            "actor": actor,
            "installation": installation,
            "expected_versions": expected_versions,
            "changes": changes,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            pending_changes = state.get("authority_changes", {}).values()
            if any(
                change.get("status") == "draining"
                and (
                    change.get("member_id") == actor
                    or change.get("installation_id") == installation
                )
                for change in pending_changes
            ):
                raise PermissionError("member or installation authority is draining")
            binding = state["installations"].get(installation)
            binding_owner = (
                binding.get("member_id")
                if isinstance(binding, dict) and not binding.get("revoked")
                else binding
            )
            if binding_owner != actor or actor not in state["members"]:
                raise PermissionError("installation is not bound to an active member")
            if not state["members"][actor].get("active", False):
                raise PermissionError("member is disabled")
            for entity, version in expected_versions.items():
                if int(state["entities"].get(entity, {}).get("version", 0)) != version:
                    raise GitContention(f"stale entity version for {entity}")
            effect_changes = changes.get("effects", {})
            for key, value in changes.get("permits", {}).items():
                current = state["permits"].get(key)
                if not current:
                    continue
                if value is not None and value.get("claim", current.get("claim")) != current.get(
                    "claim"
                ):
                    raise GitCoordinationError("permit claim is immutable after admission")
                if value is None:
                    effect = effect_changes.get(key, state["effects"].get(key))
                    if not effect or effect.get("outcome", "pending") in {"pending", "unknown"}:
                        raise GitCoordinationError(
                            f"unresolved permit cannot be released without terminal evidence: {key}"
                        )
            for key, value in effect_changes.items():
                if value is None and key in state["effects"]:
                    raise GitCoordinationError(
                        f"effect recovery evidence cannot be deleted: {key}"
                    )
            for entity, patch in changes.get("entities", {}).items():
                current = state["entities"].get(entity)
                if current is None:
                    current = state["entities"].setdefault(entity, {"version": 0})
                else:
                    changed_authority = sorted(
                        field
                        for field in HANDOFF_ENTITY_FIELDS.intersection(patch)
                        if patch[field] != current.get(field)
                    )
                    if changed_authority:
                        raise GitCoordinationError(
                            "authority changes require an acknowledged handoff: "
                            + ", ".join(changed_authority)
                        )
                current.update(deepcopy(patch))
                current["version"] += 1
            for table in ("claims", "permits", "effects", "reservations"):
                for key, value in changes.get(table, {}).items():
                    current = state[table].get(key)
                    if value is None:
                        if current and (
                            current.get("actor"), current.get("installation")
                        ) != (actor, installation):
                            raise PermissionError(f"cannot release another member's {table[:-1]}")
                        if current and table in {"claims", "permits"}:
                            raise GitCoordinationError(
                                f"{table[:-1]} release requires a validated lifecycle transition"
                            )
                        state[table].pop(key, None)
                    else:
                        row = deepcopy(value)
                        row.setdefault("actor", actor)
                        row.setdefault("installation", installation)
                        if table == "claims":
                            if row.get("actor") != actor or row.get("installation") != installation:
                                raise PermissionError(
                                    "claim identity must match the authenticated installation"
                                )
                            if not row.get("operation_id"):
                                raise GitCoordinationError(
                                    "claim operation_id must be non-empty"
                                )
                            if current:
                                for field in CLAIM_IDENTITY_FIELDS:
                                    if row.get(field) != current.get(field):
                                        raise GitContention(
                                            f"claim {field} is immutable after admission"
                                        )
                            entity = state["entities"].get(key)
                            if not entity:
                                raise GitCoordinationError(f"claim has no authoritative entity: {key}")
                            if row.get("kind") != entity.get("kind") or row.get(
                                "scope"
                            ) != entity.get("scope"):
                                raise GitCoordinationError("claim scope does not match authority")
                            if row.get("owner_id") != entity.get("owner"):
                                raise PermissionError("claim owner does not match authority")
                            if row.get("owner_id") != actor:
                                raise PermissionError("only the authoritative owner may claim")
                            if int(row.get("authority_generation", -1)) != int(
                                entity.get("authority_generation", -1)
                            ):
                                raise GitContention("claim generation does not match authority")
                            if entity.get("draining"):
                                raise GitCoordinationError("authority is draining; new claims are blocked")
                        if table in {"permits", "effects"}:
                            claim_entities = {
                                claim.get("operation_id"): entity_key
                                for entity_key, claim in state["claims"].items()
                            }
                            if row.get("claim") not in claim_entities:
                                raise GitCoordinationError(
                                    f"{table[:-1]} does not reference an active claim"
                                )
                            claim_entity = state["entities"].get(claim_entities[row["claim"]], {})
                            if claim_entity.get("draining") and current is None:
                                raise GitCoordinationError(
                                    "authority is draining; new permits and effects are blocked"
                                )
                        if table == "permits":
                            if current is None and row.get("status", "pending") != "pending":
                                raise GitCoordinationError(
                                    "new execution permits must start pending"
                                )
                            if current and row.get(
                                "status", current.get("status", "pending")
                            ) != current.get("status", "pending"):
                                raise GitCoordinationError(
                                    "permit status requires validated terminal or fencing evidence"
                                )
                        if table == "effects" and current:
                            prior_outcome = current.get("outcome", "pending")
                            next_outcome = row.get("outcome", "pending")
                            if (
                                prior_outcome not in {"pending", "unknown"}
                                and next_outcome != prior_outcome
                            ):
                                raise GitCoordinationError("terminal effect outcome is immutable")
                            for field in ("claim", "provider", "scope", "effect_key"):
                                if row.get(field, current.get(field)) != current.get(field):
                                    raise GitCoordinationError(
                                        f"effect {field} is immutable after admission"
                                    )
                            row = {**current, **row}
                        if table == "effects" and row.get("outcome", "pending") not in EFFECT_OUTCOMES:
                            raise GitCoordinationError("unrecognized effect outcome")
                        if current and (current.get("actor"), current.get("installation")) != (
                            actor,
                            installation,
                        ):
                            raise GitContention(f"conflicting {table[:-1]} {key}")
                        state[table][key] = row
            for pool, limit in state["policy"]["pools"].items():
                reservations = [
                    row for row in state["reservations"].values() if row.get("pool") == pool
                ]
                if sum(int(row.get("units", 0)) for row in reservations) > int(limit["units"]):
                    raise GitContention(f"shared concurrency limit reached for {pool}")
                if sum(int(row.get("spend_micros", 0)) for row in reservations) > int(
                    limit["spend_micros"]
                ):
                    raise GitContention(f"shared budget limit reached for {pool}")
            for entity, patch in changes.get("entities", {}).items():
                if "owner" not in patch:
                    continue
                blocked = [
                    key
                    for key, effect in state["effects"].items()
                    if effect.get("scope") == entity
                    and effect.get("outcome", "pending") in {"pending", "unknown"}
                ]
                claim_operations = {
                    claim.get("operation_id")
                    for claim in state["claims"].values()
                    if f"{claim.get('kind')}:{claim.get('scope')}" == entity
                }
                blocked.extend(
                    key
                    for key, permit in state["permits"].items()
                    if permit.get("claim") in claim_operations
                    and (state["effects"].get(key) or {}).get("outcome", "pending")
                    in {"pending", "unknown"}
                )
                if blocked:
                    raise GitCoordinationError(
                        f"ownership handoff blocked by unresolved permits or effects: "
                        f"{', '.join(sorted(set(blocked)))}"
                    )
            unresolved = {
                key
                for key, value in state["effects"].items()
                if value.get("outcome", "pending") in {"pending", "unknown"}
            }
            result = {"sequence": state["sequence"] + 1, "unresolved_effects": sorted(unresolved)}
            return result

        return self.transact(operation_id, inputs, mutate)

    @staticmethod
    def _handoff_blockers(state: dict[str, Any], entity_key: str) -> list[str]:
        claim_operations = {
            claim.get("operation_id")
            for key, claim in state["claims"].items()
            if key == entity_key
        }
        blockers = [
            key
            for key, permit in state["permits"].items()
            if permit.get("claim") in claim_operations
            and permit.get("status", "pending") not in {"terminal", "fenced"}
            and (state["effects"].get(key) or {}).get("outcome", "pending")
            in {"pending", "unknown"}
        ]
        blockers.extend(
            key
            for key, effect in state["effects"].items()
            if effect.get("scope") == entity_key
            and effect.get("outcome", "pending") in {"pending", "unknown"}
        )
        return sorted(set(blockers))

    @staticmethod
    def _require_active_installation(
        state: dict[str, Any], actor: str, installation: str
    ) -> dict[str, Any]:
        member = state["members"].get(actor) or {}
        installation_row = state["installations"].get(installation)
        installation_owner = (
            installation_row.get("member_id")
            if isinstance(installation_row, dict) and not installation_row.get("revoked")
            else installation_row
        )
        if installation_owner != actor or not member.get("active"):
            raise PermissionError("handoff installation is not bound to an active member")
        return member

    @staticmethod
    def _require_authorized_owner(
        state: dict[str, Any], entity: dict[str, Any], owner: str
    ) -> None:
        """Require a nonempty handoff target that may own work in this project."""
        if not owner:
            return
        member = state["members"].get(owner) or {}
        if not member.get("active") or member.get("role", "member") not in {
            "owner", "admin", "administrator", "member",
        }:
            raise PermissionError("handoff target is not an active execution member")
        project = str(entity.get("project", ""))
        if not project and entity.get("kind") == "phase":
            project = str(entity.get("scope", "")).split("/", 1)[0]
        visibility = member.get("project_visibility", member.get("visibility", "assigned"))
        if not project or (
            visibility != "all" and project not in (member.get("projects") or ())
        ):
            raise PermissionError("handoff target is not authorized for the entity project")

    def begin_handoff(
        self, operation_id: str, *, actor: str, installation: str,
        entity_key: str, expected_version: int, pending_owner: str,
        projection: dict[str, Any] | None = None,
        assignment: dict[str, Any] | None = None,
    ) -> AcceptedTransaction:
        """Accept draining intent without changing effective ownership or generation."""
        inputs = {
            "actor": actor, "installation": installation, "entity": entity_key,
            "expected_version": expected_version, "pending_owner": pending_owner,
            "projection": projection, "assignment": assignment,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            entity = state["entities"].get(entity_key)
            if not entity or int(entity.get("version", 0)) != expected_version:
                raise GitContention(f"stale entity version for {entity_key}")
            member = self._require_active_installation(state, actor, installation)
            effective_owner = str(entity.get("owner", ""))
            administrator = member.get("role") in {"owner", "admin", "administrator"}
            if actor != effective_owner and not administrator:
                raise PermissionError("only the effective owner or an administrator may begin handoff")
            if entity_key in state["handoffs"]:
                raise GitCoordinationError("authority already has a pending handoff")
            self._require_authorized_owner(state, entity, pending_owner)
            entity["draining"] = True
            entity["pending_owner"] = pending_owner
            entity["version"] = expected_version + 1
            prior_claim = state["claims"].get(entity_key) or {}
            blockers = self._handoff_blockers(state, entity_key)
            state["handoffs"][entity_key] = {
                "from_owner": effective_owner,
                "from_generation": int(entity.get("authority_generation", 0)),
                "from_installation": str(prior_claim.get("installation", "")),
                "pending_owner": pending_owner,
                "status": "ready" if not prior_claim and not blockers else "blocked",
                "stop_acknowledged": not prior_claim and not blockers,
                "external_fence": None,
                "projection": deepcopy(projection),
                "assignment": deepcopy(assignment),
            }
            return {
                "status": state["handoffs"][entity_key]["status"],
                "effective_owner": effective_owner,
                "pending_owner": pending_owner, "blockers": blockers,
            }

        return self.transact(operation_id, inputs, mutate)

    def reconcile_inherited_task_owners(
        self, operation_id: str, *, actor: str, installation: str,
        project: str, phase: str, owner: str, tasks: list[str],
    ) -> AcceptedTransaction:
        """Create or safely transfer task authority implied by one phase owner change."""
        inputs = {
            "actor": actor, "installation": installation, "project": project,
            "phase": phase, "owner": owner, "tasks": sorted(tasks),
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            member = self._require_active_installation(state, actor, installation)
            if member.get("role") not in {"owner", "admin", "administrator"}:
                raise PermissionError("phase ownership changes require an administrator")
            affected: list[str] = []
            pending: list[str] = []
            for task_id in sorted(set(tasks)):
                key = f"task:{task_id}"
                entity = state["entities"].get(key)
                if entity is None:
                    state["entities"][key] = {
                        "kind": "task", "scope": task_id, "project": project,
                        "owner": owner, "authority_generation": 1, "version": 0,
                    }
                    affected.append(task_id)
                    continue
                if str(entity.get("owner", "")) == owner:
                    continue
                if key in state["handoffs"]:
                    if state["handoffs"][key].get("pending_owner") != owner:
                        raise GitCoordinationError(f"authority already has a pending handoff: {key}")
                    pending.append(task_id)
                    continue
                self._require_authorized_owner(state, entity, owner)
                self._stage_lifecycle_handoff(state, key, pending_owner=owner)
                affected.append(task_id)
                handoff = state["handoffs"][key]
                if handoff["status"] == "ready":
                    entity.update({
                        "owner": owner,
                        "authority_generation": int(handoff["from_generation"]) + 1,
                        "draining": False, "pending_owner": "",
                        "version": int(entity.get("version", 0)) + 1,
                    })
                    state["claims"].pop(key, None)
                    state["recovery"].append({
                        "kind": "handoff", "entity": key, **deepcopy(handoff),
                    })
                    del state["handoffs"][key]
                else:
                    pending.append(task_id)
            return {"affected": affected, "pending": pending}

        return self.transact(operation_id, inputs, mutate)

    @staticmethod
    def _stage_lifecycle_handoff(
        state: dict[str, Any], entity_key: str, *, pending_owner: str = ""
    ) -> None:
        """Fence one authority while retaining its effective owner and generation."""
        entity = state["entities"][entity_key]
        if entity_key in state["handoffs"]:
            raise GitCoordinationError(f"authority already has a pending handoff: {entity_key}")
        entity["draining"] = True
        entity["pending_owner"] = pending_owner
        entity["version"] = int(entity.get("version", 0)) + 1
        claim = state["claims"].get(entity_key) or {}
        claim_installation = str(claim.get("installation", ""))
        state["handoffs"][entity_key] = {
            "from_owner": str(entity.get("owner", "")),
            "from_generation": int(entity.get("authority_generation", 0)),
            "from_installation": claim_installation,
            "pending_owner": pending_owner,
            "status": "ready" if not claim_installation else "blocked",
            "stop_acknowledged": not claim_installation,
            "external_fence": None,
        }

    def begin_member_authority_change(
        self, operation_id: str, *, actor: str, installation: str, member_id: str,
        active: bool | None = None, projects: list[str] | None = None,
    ) -> AcceptedTransaction:
        """Fence authorities before disabling a member or narrowing project access."""
        inputs = {
            "actor": actor, "installation": installation, "member_id": member_id,
            "active": active, "projects": projects,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            administrator = self._require_active_installation(state, actor, installation)
            if administrator.get("role") not in {"owner", "admin", "administrator"}:
                raise PermissionError("member authority changes require an administrator")
            member = state["members"].get(member_id)
            if member is None:
                raise KeyError(member_id)
            if active is not False and projects is None:
                raise GitCoordinationError("authority change must disable or change project scope")
            change_key = f"member:{member_id}"
            if change_key in state["authority_changes"]:
                raise GitCoordinationError("member already has a pending authority change")
            retained_projects = None if projects is None else sorted(set(projects))
            affected = []
            for entity_key, entity in state["entities"].items():
                if entity.get("owner") != member_id:
                    continue
                project = str(entity.get("project", ""))
                if active is False or (retained_projects is not None and project not in retained_projects):
                    self._stage_lifecycle_handoff(state, entity_key)
                    affected.append(entity_key)
            state["authority_changes"][change_key] = {
                "status": "draining", "kind": "member", "member_id": member_id,
                "active": active, "projects": retained_projects, "entities": sorted(affected),
                "requested_by": actor,
            }
            return {"status": "draining", "entities": sorted(affected)}

        return self.transact(operation_id, inputs, mutate)

    def begin_installation_revocation(
        self, operation_id: str, *, actor: str, installation: str,
        installation_id: str,
    ) -> AcceptedTransaction:
        """Immediately deny new work and drain claims bound to an installation."""
        inputs = {
            "actor": actor, "installation": installation,
            "installation_id": installation_id,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            administrator = self._require_active_installation(state, actor, installation)
            if administrator.get("role") not in {"owner", "admin", "administrator"}:
                raise PermissionError("installation revocation requires an administrator")
            member_id = state["installations"].get(installation_id)
            if member_id is None:
                raise KeyError(installation_id)
            change_key = f"installation:{installation_id}"
            if change_key in state["authority_changes"]:
                raise GitCoordinationError("installation already has a pending revocation")
            affected = []
            for entity_key, claim in state["claims"].items():
                if claim.get("installation") == installation_id:
                    self._stage_lifecycle_handoff(state, entity_key)
                    affected.append(entity_key)
            state["authority_changes"][change_key] = {
                "status": "draining", "kind": "installation",
                "member_id": member_id, "installation_id": installation_id,
                "entities": sorted(affected), "requested_by": actor,
            }
            return {"status": "draining", "entities": sorted(affected)}

        return self.transact(operation_id, inputs, mutate)

    def enroll_installation(
        self, operation_id: str, *, actor: str, installation: str,
        installation_id: str, member_id: str,
    ) -> AcceptedTransaction:
        """Explicitly bind an unused installation; live enrollments are immutable."""
        inputs = {
            "actor": actor, "installation": installation,
            "installation_id": installation_id, "member_id": member_id,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            administrator = self._require_active_installation(state, actor, installation)
            if administrator.get("role") not in {"owner", "admin", "administrator"}:
                raise PermissionError("installation enrollment requires an administrator")
            member = state["members"].get(member_id) or {}
            if not member.get("active"):
                raise PermissionError("installation owner must be an active member")
            if installation_id in state["installations"]:
                raise GitCoordinationError(
                    "live installation ownership is immutable; revoke before re-enrollment"
                )
            if f"installation:{installation_id}" in state["authority_changes"]:
                raise GitCoordinationError("installation revocation is not complete")
            state["installations"][installation_id] = member_id
            state["revoked_installations"].pop(installation_id, None)
            return {"installation_id": installation_id, "member_id": member_id}

        return self.transact(operation_id, inputs, mutate)

    def complete_authority_change(
        self, operation_id: str, *, actor: str, installation: str, change_key: str,
    ) -> AcceptedTransaction:
        """Atomically finish every fenced authority before changing membership or enrollment."""
        inputs = {
            "actor": actor, "installation": installation, "change_key": change_key,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            administrator = self._require_active_installation(state, actor, installation)
            if administrator.get("role") not in {"owner", "admin", "administrator"}:
                raise PermissionError("authority change completion requires an administrator")
            change = state["authority_changes"].get(change_key)
            if not change:
                raise GitCoordinationError("no pending authority change")
            for entity_key in change["entities"]:
                handoff = state["handoffs"].get(entity_key)
                blockers = self._handoff_blockers(state, entity_key)
                if blockers:
                    raise GitCoordinationError(
                        "authority change remains blocked by: " + ", ".join(blockers)
                    )
                if not handoff or (
                    not handoff.get("stop_acknowledged") and not handoff.get("external_fence")
                ):
                    raise GitCoordinationError(
                        f"authority change requires acknowledgement or external fence: {entity_key}"
                    )
            for entity_key in change["entities"]:
                handoff = state["handoffs"].pop(entity_key)
                entity = state["entities"][entity_key]
                state["claims"].pop(entity_key, None)
                entity.update({
                    "owner": "", "authority_generation": int(handoff["from_generation"]) + 1,
                    "draining": False, "pending_owner": "",
                    "version": int(entity.get("version", 0)) + 1,
                })
                state["recovery"].append({
                    "kind": "handoff", "entity": entity_key, **deepcopy(handoff),
                })
            if change["kind"] == "member":
                member = state["members"][change["member_id"]]
                if change.get("active") is not None:
                    member["active"] = bool(change["active"])
                if change.get("projects") is not None:
                    member["projects"] = deepcopy(change["projects"])
                    member["project_visibility"] = "assigned"
            else:
                revoked_id = change["installation_id"]
                prior_member = state["installations"].pop(revoked_id, None)
                state["revoked_installations"][revoked_id] = prior_member
            state["recovery"].append({"kind": "authority-change", **deepcopy(change)})
            del state["authority_changes"][change_key]
            return {"status": "complete", "change": change_key}

        return self.transact(operation_id, inputs, mutate)

    def acknowledge_stop(
        self, operation_id: str, *, actor: str, installation: str, entity_key: str,
    ) -> AcceptedTransaction:
        """Bind a stop/release acknowledgement to the installation being handed off."""
        inputs = {"actor": actor, "installation": installation, "entity": entity_key}

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            handoff = state["handoffs"].get(entity_key)
            if not handoff or (handoff["from_owner"], handoff["from_installation"]) != (
                actor, installation,
            ):
                raise PermissionError("stop acknowledgement is not bound to the prior installation")
            blockers = self._handoff_blockers(state, entity_key)
            if blockers:
                raise GitCoordinationError("stop acknowledgement blocked by: " + ", ".join(blockers))
            handoff["stop_acknowledged"] = True
            handoff["status"] = "ready"
            return {"status": "ready"}

        return self.transact(operation_id, inputs, mutate)

    def record_external_fence(
        self, operation_id: str, *, actor: str, installation: str, entity_key: str,
        proof: dict[str, Any],
    ) -> AcceptedTransaction:
        """Record an administrator's durable, specific proof of independent fencing."""
        inputs = {
            "actor": actor, "installation": installation, "entity": entity_key, "proof": proof,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            member = state["members"].get(actor) or {}
            if state["installations"].get(installation) != actor or member.get("role") not in {
                "owner", "admin", "administrator",
            }:
                raise PermissionError("external fencing requires an enrolled administrator")
            if not proof.get("execution") or not proof.get("publication"):
                raise GitCoordinationError("external fence must cover execution and publication")
            handoff = state["handoffs"].get(entity_key)
            if not handoff:
                raise GitCoordinationError("no pending handoff")
            claim_operations = {
                claim.get("operation_id")
                for key, claim in state["claims"].items()
                if key == entity_key
            }
            unresolved_effects = [
                key for key, effect in state["effects"].items()
                if effect.get("scope") == entity_key
                and effect.get("outcome", "pending") in {"pending", "unknown"}
            ]
            if unresolved_effects:
                raise GitCoordinationError(
                    "external fence blocked by unresolved effects: " + ", ".join(unresolved_effects)
                )
            for permit in state["permits"].values():
                if permit.get("claim") in claim_operations and permit.get(
                    "status", "pending"
                ) not in {"terminal", "fenced"}:
                    permit["status"] = "fenced"
            handoff["external_fence"] = {"actor": actor, "installation": installation, **deepcopy(proof)}
            handoff["status"] = "ready"
            return {"status": "ready"}

        return self.transact(operation_id, inputs, mutate)

    def retain_late_evidence(
        self, operation_id: str, *, actor: str, installation: str, entity_key: str,
        prior_generation: int, evidence_id: str, payload: dict[str, Any],
    ) -> AcceptedTransaction:
        """Archive late output as attributed evidence without applying lifecycle state."""
        inputs = {
            "actor": actor, "installation": installation, "entity": entity_key,
            "prior_generation": prior_generation, "evidence_id": evidence_id, "payload": payload,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            enrolled_actor = state["installations"].get(installation)
            revoked_actor = state.get("revoked_installations", {}).get(installation)
            if actor not in {enrolled_actor, revoked_actor}:
                raise PermissionError("evidence installation is not enrolled")
            entity = state["entities"].get(entity_key)
            if not entity or int(entity.get("authority_generation", 0)) <= prior_generation:
                raise GitCoordinationError("evidence is not from a fenced prior generation")
            state["recovery"].append({
                "kind": "late-evidence", "entity": entity_key,
                "generation": prior_generation, "evidence_id": evidence_id,
                "actor": actor, "installation": installation, "payload": deepcopy(payload),
            })
            return {"retained": evidence_id, "applied": False}

        return self.transact(operation_id, inputs, mutate)

    def reconcile_effect(
        self, operation_id: str, *, actor: str, installation: str,
        effect_operation_id: str, outcome: str, evidence: dict[str, Any],
    ) -> AcceptedTransaction:
        """Resolve the original provider operation by ID without issuing a replacement effect."""
        if outcome not in {"succeeded", "failed", "fenced"}:
            raise ValueError("reconciled outcome must be terminal")
        inputs = {
            "actor": actor, "installation": installation, "effect": effect_operation_id,
            "outcome": outcome, "evidence": evidence,
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if state["installations"].get(installation) != actor:
                raise PermissionError("recovery installation is not enrolled")
            effect = state["effects"].get(effect_operation_id)
            if not effect:
                raise GitCoordinationError("original provider effect was not found")
            if effect.get("outcome", "pending") not in {"pending", "unknown", outcome}:
                raise GitCoordinationError("provider effect already has a different terminal outcome")
            effect.update({"outcome": outcome, "reconciled_by": actor, "evidence": deepcopy(evidence)})
            permit = state["permits"].get(effect_operation_id)
            if permit is not None:
                permit["status"] = "fenced" if outcome == "fenced" else "terminal"
            return {"effect": effect_operation_id, "outcome": outcome}

        return self.transact(operation_id, inputs, mutate)

    def complete_handoff(
        self, operation_id: str, *, actor: str, installation: str, entity_key: str,
    ) -> AcceptedTransaction:
        """Atomically release old authority, assign the pending owner, and advance generation."""
        inputs = {"actor": actor, "installation": installation, "entity": entity_key}

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            member = self._require_active_installation(state, actor, installation)
            handoff = state["handoffs"].get(entity_key)
            if not handoff:
                raise GitCoordinationError("no pending handoff")
            administrator = member.get("role") in {"owner", "admin", "administrator"}
            if actor not in {handoff["from_owner"], handoff["pending_owner"]} and not administrator:
                raise PermissionError("handoff completion requires a participating owner or administrator")
            blockers = self._handoff_blockers(state, entity_key)
            if blockers:
                raise GitCoordinationError("handoff remains blocked by: " + ", ".join(blockers))
            if not handoff.get("stop_acknowledged") and not handoff.get("external_fence"):
                raise GitCoordinationError("handoff requires bound stop acknowledgement or external fence")
            entity = state["entities"][entity_key]
            self._require_authorized_owner(state, entity, handoff["pending_owner"])
            state["claims"].pop(entity_key, None)
            entity.update({
                "owner": handoff["pending_owner"],
                "authority_generation": int(handoff["from_generation"]) + 1,
                "draining": False,
                "pending_owner": "",
                "version": int(entity.get("version", 0)) + 1,
            })
            projection = handoff.get("projection")
            if projection:
                state.setdefault("projections", []).append(deepcopy(projection))
            assignment = handoff.get("assignment")
            if assignment and handoff["pending_owner"]:
                member = state["members"][handoff["pending_owner"]]
                generation = int((member.get("assignment") or {}).get("generation", 0)) + 1
                member["assignment"] = {
                    "member_id": handoff["pending_owner"], **deepcopy(assignment),
                    "generation": generation, "enabled": True,
                }
            state["recovery"].append({"kind": "handoff", "entity": entity_key, **deepcopy(handoff)})
            del state["handoffs"][entity_key]
            return {
                "status": "complete", "owner": entity["owner"],
                "authority_generation": entity["authority_generation"],
            }

        return self.transact(operation_id, inputs, mutate)


class GitMultiplayerClient:
    """Production adapter selected by ``multiplayer.git`` configuration."""

    def __init__(self, store: GitStateStore, member_id: str, installation_id: str):
        self.store, self.member_id, self.installation_id = store, member_id, installation_id
        self.root = store.repo
        self._cache_path = self.root / ".garden" / "authoritative-snapshot.json"
        self._projection_path = self.root / ".garden" / "authoritative-projections.json"

    @classmethod
    def connect(
        cls,
        root: Path,
        *,
        garden_id: str,
        member_id: str,
        installation_id: str,
        remote: str = "origin",
        state_ref: str = "refs/heads/garden-state",
        authentication: str = "credential",
        timeout_seconds: float = DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
    ) -> GitMultiplayerClient:
        if authentication not in {"credential", "temporary-username"}:
            raise GitCoordinationError("unsupported multiplayer authentication mode")
        if authentication == "temporary-username":
            from .members import operating_system_username

            if member_id != operating_system_username():
                raise GitCoordinationError(
                    "temporary username enrollment belongs to a different operating-system account"
                )
        if not all((remote, state_ref, garden_id, member_id, installation_id)):
            raise GitCoordinationError("multiplayer Git enrollment is incomplete")
        return cls(
            GitStateStore(
                root, garden_id=garden_id, remote=remote, state_ref=state_ref,
                timeout_seconds=timeout_seconds,
            ),
            member_id,
            installation_id,
        )

    @classmethod
    def from_config(cls, config: Any) -> GitMultiplayerClient | None:
        if not config.get("multiplayer.enabled", False):
            return None
        remote = str(config.get("multiplayer.git.remote", "origin"))
        ref = str(config.get("multiplayer.git.state_ref", "refs/heads/garden-state"))
        garden_id = str(config.get("multiplayer.garden_id", ""))
        member = str(config.get("multiplayer.member_id", ""))
        installation = str(config.get("multiplayer.installation_id", ""))
        authentication = str(config.get("multiplayer.authentication", "credential"))
        timeout_seconds = float(config.get("multiplayer.git.timeout_seconds", 30))
        return cls.connect(
            config.root,
            garden_id=garden_id,
            member_id=member,
            installation_id=installation,
            remote=remote,
            state_ref=ref,
            authentication=authentication,
            timeout_seconds=timeout_seconds,
        )

    def refresh(self, *, allow_stale: bool = True) -> Any:
        from .multiplayer_client import AuthoritativeView, _atomic_json

        try:
            revision, state = self.store.read()
            snapshot = deepcopy(state)
            snapshot.update({"member_id": self.member_id, "installation_id": self.installation_id})
            snapshot["observed_revision"] = revision
            snapshot["role"] = (state["members"].get(self.member_id) or {}).get("role", "member")
            snapshot["projects"] = (state["members"].get(self.member_id) or {}).get("projects", [])
            snapshot["assignment"] = (state["members"].get(self.member_id) or {}).get("assignment")
            snapshot["authority"] = list(state["entities"].values())
            snapshot["projections"] = state.get("projections", [])
            _atomic_json(self._cache_path, snapshot)
            return AuthoritativeView(snapshot, False)
        except GitCoordinationError as exc:
            from .multiplayer_client import MultiplayerUnavailable

            if allow_stale:
                try:
                    cached = json.loads(self._cache_path.read_text())
                    if (
                        cached.get("garden_id") == self.store.garden_id
                        and cached.get("member_id") == self.member_id
                        and cached.get("installation_id") == self.installation_id
                    ):
                        return AuthoritativeView(cached, True, str(exc))
                except (OSError, ValueError, AttributeError):
                    pass
            raise MultiplayerUnavailable(f"authoritative Git state unavailable: {exc}") from exc

    def synchronize(self, snapshot: dict[str, Any] | None = None) -> list[str]:
        """Update derived Markdown without touching unrelated authored checkout state."""
        from .members import MemberRegistry
        from .multiplayer_client import ProjectionConflict, _atomic_json, _atomic_text, _digest

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
            allowed = {str(prior.get("content_hash", "")), str(row.get("base_revision", ""))}
            if current != content and _digest(current) not in allowed:
                conflicts.append(relative)
            else:
                updates.append((relative, target, content, int(row["version"])))
        if conflicts:
            raise ProjectionConflict(
                "local authored content conflicts with authority: " + ", ".join(conflicts)
            )
        changed = []
        for relative, target, content, version in updates:
            if not target.exists() or target.read_text() != content:
                _atomic_text(target, content)
                changed.append(relative)
            ledger[relative] = {"version": version, "content_hash": _digest(content)}
        _atomic_json(self._projection_path, ledger)
        MemberRegistry(self.root / ".garden").synchronize_authority(snapshot)
        return changed

    def projection_lag(self, snapshot: dict[str, Any]) -> list[str]:
        try:
            ledger = json.loads(self._projection_path.read_text())
        except (OSError, ValueError):
            ledger = {}
        return [
            f"{row['kind']}:{row['scope']}@{row['version']}"
            for row in snapshot.get("projections", [])
            if int(ledger.get(str(row.get("path", "")), {}).get("version", 0))
            < int(row["version"])
        ]

    def prepare(self, *, mutation: bool = False) -> Any:
        from .multiplayer_client import MultiplayerUnavailable

        view = self.refresh(allow_stale=not mutation)
        if not view.stale:
            self.synchronize(view.snapshot)
        if mutation and self.projection_lag(view.snapshot):
            raise MultiplayerUnavailable("local projection has not reached required authority")
        return view

    def authenticate_local_session(self) -> Any:
        from .members import Principal

        state = self.refresh(allow_stale=False).snapshot
        member = state["members"].get(self.member_id) or {}
        if state["installations"].get(self.installation_id) != self.member_id or not member.get(
            "active"
        ):
            return None
        return Principal(
            self.store.garden_id,
            self.member_id,
            self.installation_id,
            member.get("role", "member"),
            member.get("visibility", "assigned"),
            frozenset(member.get("projects", [])),
        )

    def reconcile_inherited_task_owners(
        self, *, project: str, phase: str, owner: str, tasks: list[str], generation: int,
    ) -> dict[str, Any]:
        task_set = hashlib.sha256("\0".join(sorted(tasks)).encode()).hexdigest()[:12]
        accepted = self.store.reconcile_inherited_task_owners(
            f"phase-owner:{project}:{phase}:{generation}:{task_set}", actor=self.member_id,
            installation=self.installation_id, project=project, phase=phase,
            owner=owner, tasks=tasks,
        )
        return accepted.result

    def begin_handoff(self, *, kind: str, scope: str, pending_owner: str,
                      expected_version: int, projection: dict[str, Any] | None = None,
                      assignment: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record one assignment intent; draining and acknowledgement remain automatic."""
        operation = (
            f"handoff:{self.installation_id}:{kind}:{scope}:"
            f"{expected_version}:{pending_owner or 'unassigned'}"
        )
        accepted = self.store.begin_handoff(
            operation, actor=self.member_id, installation=self.installation_id,
            entity_key=f"{kind}:{scope}", expected_version=expected_version,
            pending_owner=pending_owner, projection=projection, assignment=assignment,
        )
        if accepted.result["status"] == "ready":
            completed = self.store.complete_handoff(
                f"handoff-complete:{kind}:{scope}:{expected_version}",
                actor=self.member_id, installation=self.installation_id,
                entity_key=f"{kind}:{scope}",
            )
            return completed.result
        return accepted.result

    def acknowledge_cancellations(
        self, snapshot: dict[str, Any], cancel: Callable[[str, str], bool]
    ) -> list[str]:
        """Stop this installation's claimed work, then finish its safe handoffs."""
        completed: list[str] = []
        for key, handoff in snapshot.get("handoffs", {}).items():
            if handoff.get("from_installation") != self.installation_id:
                continue
            kind, separator, scope = key.partition(":")
            if not separator or not cancel(kind, scope):
                continue
            self.store.acknowledge_stop(
                f"handoff-stop:{key}:{handoff['from_generation']}",
                actor=self.member_id, installation=self.installation_id, entity_key=key,
            )
            self.store.complete_handoff(
                f"handoff-complete:{key}:{handoff['from_generation']}",
                actor=self.member_id, installation=self.installation_id, entity_key=key,
            )
            completed.append(key)
        return completed

    def claim(
        self,
        *,
        kind: str,
        scope: str,
        owner_id: str,
        authority_generation: int,
        expected_version: int,
    ) -> dict[str, Any]:
        key = f"{kind}:{scope}"
        operation = f"claim:{self.installation_id}:{key}:{authority_generation}:{expected_version}"
        row = {
            "kind": kind,
            "scope": scope,
            "owner_id": owner_id,
            "authority_generation": authority_generation,
            "operation_id": operation,
        }
        self.store.apply(
            operation,
            actor=self.member_id,
            installation=self.installation_id,
            expected_versions={key: expected_version},
            changes={"claims": {key: row}},
        )
        return row

    @contextmanager
    def effect(
        self,
        *,
        kind: str,
        scope: str,
        owner_id: str,
        authority_generation: int,
        expected_version: int,
        effect_key: str,
        provider: str = "scheduler",
    ) -> Iterator[dict[str, Any]]:
        claim = self.claim(
            kind=kind,
            scope=scope,
            owner_id=owner_id,
            authority_generation=authority_generation,
            expected_version=expected_version,
        )
        operation = f"effect:{self.installation_id}:{effect_key}:{authority_generation}"
        effect = {
            "claim": claim["operation_id"],
            "provider": provider,
            "scope": kind + ":" + scope,
            "effect_key": effect_key,
            "outcome": "pending",
        }
        accepted = self.store.apply(
            operation,
            actor=self.member_id,
            installation=self.installation_id,
            expected_versions={kind + ":" + scope: expected_version},
            changes={
                "permits": {operation: {"claim": claim["operation_id"]}},
                "effects": {operation: effect},
            },
        )
        if accepted.replayed:
            _, state = self.store.read()
            outcome = (state["effects"].get(operation) or {}).get("outcome", "pending")
            raise GitCoordinationError(
                f"effect {effect_key} was already admitted with outcome {outcome}; "
                "reconcile it instead of executing again"
            )
        try:
            yield claim
        except BaseException:
            outcome = "unknown"
            raise
        else:
            outcome = "succeeded"
        finally:
            finish = f"finish:{operation}:{outcome}"
            self.store.apply(
                finish,
                actor=self.member_id,
                installation=self.installation_id,
                expected_versions={},
                changes={"effects": {operation: {**effect, "outcome": outcome}}},
            )
