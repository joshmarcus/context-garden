"""Service-free multiplayer coordination over a dedicated Garden Git ref."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL = "context-garden/git-coordination"
VERSION = 1


class GitCoordinationError(RuntimeError):
    """The shared state is unavailable, invalid, or rejected."""


class GitContention(GitCoordinationError):
    """Another installation accepted a transaction first."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _run(cwd: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
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
        "entities": {},
        "claims": {},
        "operations": {},
        "permits": {},
        "effects": {},
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
        "entities",
        "claims",
        "operations",
        "permits",
        "effects",
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
    ):
        if not state_ref.startswith("refs/heads/") or any(c.isspace() for c in state_ref):
            raise ValueError("multiplayer.git.state_ref must be a full branch ref")
        self.repo = repo.resolve()
        self.garden_id, self.remote, self.state_ref = garden_id, remote, state_ref
        self.retries = retries
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

    def _remote_oid(self) -> str:
        output = _run(self.repo, "ls-remote", "--refs", self.remote, self.state_ref)
        return output.split()[0] if output else ""

    def _fetch(self) -> str:
        observed = ""
        for _ in range(3):
            before = self._remote_oid()
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
            )
            after = self._remote_oid()
            observed = _run(self.repo, "rev-parse", "refs/garden/state-observed")
            if before == after == observed:
                break
        else:
            raise GitContention("coordination ref kept changing during fetch")
        try:
            prior = self.observed_path.read_text().strip()
        except OSError:
            prior = ""
        if (
            prior
            and subprocess.run(
                ["git", "merge-base", "--is-ancestor", prior, observed], cwd=self.repo
            ).returncode
        ):
            self._preserve("unexpected-ref-rewrite", {"previous": prior, "observed": observed})
            raise GitCoordinationError("coordination ref was unexpectedly rewritten")
        self._validate_history(observed)
        self.observed_path.parent.mkdir(parents=True, exist_ok=True)
        self.observed_path.write_text(observed + "\n")
        return observed

    def _validate_history(self, head: str) -> None:
        commits = _run(self.repo, "rev-list", "--reverse", head).splitlines()
        previous = ""
        sequence = -1
        for commit in commits:
            parents = _run(self.repo, "show", "-s", "--format=%P", commit).split()
            if previous and parents != [previous]:
                self._preserve("non-linear-history", {"commit": commit, "parents": parents})
                raise GitCoordinationError("coordination history is not a single-parent chain")
            if not previous and parents:
                self._preserve("rewritten-root", {"commit": commit, "parents": parents})
                raise GitCoordinationError("coordination history has an unexpected root")
            state = self._read(commit)
            validate_state(state, self.garden_id)
            if state["sequence"] != sequence + 1:
                raise GitCoordinationError("coordination history sequence is corrupt")
            sequence = state["sequence"]
            previous = commit

    def _read(self, commit: str) -> dict[str, Any]:
        try:
            names = _run(self.repo, "ls-tree", "--name-only", commit).splitlines()
            if names != ["state.json"]:
                raise GitCoordinationError("coordination commit must contain only state.json")
            return json.loads(_run(self.repo, "show", f"{commit}:state.json"))
        except (ValueError, TypeError) as exc:
            raise GitCoordinationError("coordination state is not valid JSON") from exc

    def read(self) -> tuple[str, dict[str, Any]]:
        head = self._fetch()
        return head, self._read(head)

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
        result = subprocess.run(
            ["git", "push", "--porcelain", lease, self.remote, f"{commit}:{self.state_ref}"],
            cwd=self.repo,
            text=True,
            capture_output=True,
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
                return AcceptedTransaction(
                    operation_id, head, deepcopy(old.get("result", {})), True
                )
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
            try:
                self._push(commit, head)
                return AcceptedTransaction(operation_id, commit, result)
            except GitContention:
                if attempt + 1 == self.retries:
                    raise
                time.sleep(min(0.01 * (2**attempt), 0.1))
            except GitCoordinationError:
                # A transport can lose the acknowledgement after the server accepted it.
                # Resolve the stable ID against accepted history before allowing a retry.
                resolved_head, resolved = self.read()
                accepted = resolved["operations"].get(operation_id)
                if accepted and accepted.get("fingerprint") == fingerprint:
                    return AcceptedTransaction(
                        operation_id, resolved_head, deepcopy(accepted.get("result", {})), True
                    )
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
            binding = state["installations"].get(installation)
            if binding != actor or actor not in state["members"]:
                raise PermissionError("installation is not bound to an active member")
            if not state["members"][actor].get("active", False):
                raise PermissionError("member is disabled")
            for entity, version in expected_versions.items():
                if int(state["entities"].get(entity, {}).get("version", 0)) != version:
                    raise GitContention(f"stale entity version for {entity}")
            for entity, patch in changes.get("entities", {}).items():
                current = state["entities"].setdefault(entity, {"version": 0})
                current.update(deepcopy(patch))
                current["version"] += 1
            released_claims: dict[str, str] = {}
            for table in ("claims", "permits", "effects", "reservations"):
                for key, value in changes.get(table, {}).items():
                    current = state[table].get(key)
                    if value is None:
                        if current and (
                            current.get("actor"), current.get("installation")
                        ) != (actor, installation):
                            raise PermissionError(f"cannot release another member's {table[:-1]}")
                        if table == "claims" and current:
                            released_claims[key] = current.get("operation_id")
                        state[table].pop(key, None)
                    else:
                        row = deepcopy(value)
                        row.setdefault("actor", actor)
                        row.setdefault("installation", installation)
                        if table == "claims":
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
                        if table in {"permits", "effects"}:
                            claim_ids = {
                                claim.get("operation_id") for claim in state["claims"].values()
                            }
                            if row.get("claim") not in claim_ids:
                                raise GitCoordinationError(
                                    f"{table[:-1]} does not reference an active claim"
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
                        if current and (current.get("actor"), current.get("installation")) != (
                            actor,
                            installation,
                        ):
                            raise GitContention(f"conflicting {table[:-1]} {key}")
                        state[table][key] = row
            for claim_key, claim_operation in released_claims.items():
                blocked = [
                    key
                    for key, permit in state["permits"].items()
                    if permit.get("claim") == claim_operation
                    and (state["effects"].get(key) or {}).get("outcome", "pending")
                    in {"pending", "unknown"}
                ]
                if blocked:
                    raise GitCoordinationError(
                        f"claim release blocked by unresolved permits or effects for "
                        f"{claim_key}: {', '.join(sorted(blocked))}"
                    )
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


class GitMultiplayerClient:
    """Production adapter selected by ``multiplayer.git`` configuration."""

    def __init__(self, store: GitStateStore, member_id: str, installation_id: str):
        self.store, self.member_id, self.installation_id = store, member_id, installation_id

    @classmethod
    def from_config(cls, config: Any) -> GitMultiplayerClient | None:
        if not config.get("multiplayer.enabled", False):
            return None
        remote = str(config.get("multiplayer.git.remote", "origin"))
        ref = str(config.get("multiplayer.git.state_ref", "refs/heads/garden-state"))
        garden_id = str(config.get("multiplayer.garden_id", ""))
        member = str(config.get("multiplayer.member_id", ""))
        installation = str(config.get("multiplayer.installation_id", ""))
        if not all((remote, ref, garden_id, member, installation)):
            raise GitCoordinationError("multiplayer Git enrollment is incomplete")
        return cls(
            GitStateStore(config.root, garden_id=garden_id, remote=remote, state_ref=ref),
            member,
            installation,
        )

    def refresh(self, *, allow_stale: bool = True) -> Any:
        from .multiplayer_client import AuthoritativeView

        try:
            _, state = self.store.read()
            snapshot = deepcopy(state)
            snapshot.update({"member_id": self.member_id, "installation_id": self.installation_id})
            snapshot["role"] = (state["members"].get(self.member_id) or {}).get("role", "member")
            snapshot["projects"] = (state["members"].get(self.member_id) or {}).get("projects", [])
            snapshot["assignment"] = (state["members"].get(self.member_id) or {}).get("assignment")
            snapshot["authority"] = list(state["entities"].values())
            return AuthoritativeView(snapshot, False)
        except GitCoordinationError as exc:
            from .multiplayer_client import MultiplayerUnavailable

            raise MultiplayerUnavailable(f"authoritative Git state unavailable: {exc}") from exc

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
