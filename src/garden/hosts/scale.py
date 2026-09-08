"""Durable, resumable scaling of a managed worker pool.

The operation coordinates credentials and readiness around :class:`HostLifecycle`; cloud
mutation remains in the provider adapter.  Secret values never enter this state file.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Protocol

from .config import pool_from_dict
from .core import HostLifecycle
from .models import CONTRACT_VERSION, HostFacts, HostState, PoolDeclaration


@dataclass(frozen=True)
class Enrollment:
    """References and checks for one host; all values are non-secret metadata."""

    secret_ref: str = ""
    model_identity: str = ""
    repository_identity: str = ""
    tailnet_identity: str = ""
    controller_identity: str = ""
    model_expires_at: str = ""

    def missing(self, now: dt.datetime) -> tuple[str, ...]:
        missing = []
        for field, label in (
            (self.secret_ref, "scoped bootstrap secret"),
            (self.model_identity, "dedicated model identity"),
            (self.repository_identity, "repository installation/key identity"),
            (self.tailnet_identity, "tag-limited tailnet enrollment"),
            (self.controller_identity, "scoped controller enrollment"),
        ):
            if not field:
                missing.append(label)
        if self.model_expires_at:
            try:
                expiry = dt.datetime.fromisoformat(self.model_expires_at.replace("Z", "+00:00"))
            except ValueError:
                missing.append("valid model identity expiry")
            else:
                if expiry.tzinfo is None:
                    missing.append("valid model identity expiry")
                    return tuple(missing)
                if expiry <= now:
                    missing.append("renew expired model identity")
        return tuple(missing)


class EnrollmentResolver(Protocol):
    def ensure(self, host_id: str, secret_ref: str) -> Enrollment: ...

    def resolve(self, host_id: str) -> Enrollment: ...

    def revoke(self, host_id: str) -> tuple[str, ...]: ...


class DirectoryEnrollmentResolver:
    """Read controller-owned, per-host enrollment metadata from a directory.

    Each ``HOST.json`` file contains references/identity labels only.  Provisioning systems
    can create the referenced secret with Roles Anywhere or another scoped AWS identity,
    a Tailscale OAuth client, and renewable repository installation credentials.
    """

    def __init__(self, root: Path):
        self.root = root

    def resolve(self, host_id: str) -> Enrollment:
        path = self.root / f"{host_id}.json"
        if not path.exists():
            return Enrollment()
        value = json.loads(path.read_text())
        allowed = {field.name for field in fields(Enrollment)}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unsupported enrollment metadata for {host_id}: {unknown}")
        return Enrollment(**value)

    def ensure(self, host_id: str, secret_ref: str) -> Enrollment:
        """Return provisioner-produced identities, or an actionable missing state.

        Deployments replace this resolver with an integration which mints renewable
        repository, tailnet, controller, bootstrap and eligible model identities.  The
        directory implementation is the owner-handoff boundary and never fabricates or
        copies an administrator credential.
        """
        return self.resolve(host_id)

    def revoke(self, host_id: str) -> tuple[str, ...]:
        # Revocation is performed by the credential integration.  Keeping the reference
        # visible prevents a local file deletion from being mistaken for cloud revocation.
        enrollment = self.resolve(host_id)
        return tuple(filter(None, (enrollment.secret_ref, enrollment.repository_identity,
                                   enrollment.tailnet_identity, enrollment.controller_identity)))


@dataclass(frozen=True)
class ScaleStatus:
    operation_id: str
    desired: int
    healthy: int
    pending: int
    failed: int
    exact_version: str
    deadline: str
    estimated_accrued_usd: float
    spend_limit_usd: float
    aggregate_spend_limit_usd: float
    maximum_hosts: int
    per_host_cpu: int
    per_host_memory_mib: int
    per_host_disk_gib: int
    hosts: tuple[HostFacts, ...]
    missing_setup: dict[str, tuple[str, ...]]
    retained_resources: tuple[str, ...]
    pending_credential_revocations: tuple[str, ...]
    delayed_cost_notice: str


class ScaleOperation:
    """One durable request which can be safely continued after any interruption."""

    def __init__(self, lifecycle: HostLifecycle, state_path: Path,
                 enrollments: EnrollmentResolver, *, now=lambda: dt.datetime.now(dt.UTC)):
        self.lifecycle = lifecycle
        self.state_path = state_path
        self.enrollments = enrollments
        self.now = now

    @staticmethod
    def _identity(pool: PoolDeclaration) -> str:
        profile = pool.profile
        stable = {"owner": pool.owner, "pool": pool.name, "provider": pool.provider,
                  "image": profile.image, "profile_version": profile.version,
                  "bootstrap_version": profile.bootstrap_version}
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:32]

    def request(self, pool: PoolDeclaration, *, deadline: dt.datetime,
                aggregate_spend_limit_usd: float | None = None) -> ScaleStatus:
        if deadline.tzinfo is None or deadline <= self.now():
            raise ValueError("termination deadline must be a future absolute timestamp")
        with self._locked():
            current = self._read()
            identity = self._identity(pool)
            if current and current["operation_id"] != identity:
                raise ValueError("scale operation already belongs to a different pool/version")
            if pool.desired > 1 and "{host_id}" not in pool.profile.enrollment_secret_ref:
                raise ValueError("multi-host pools require a separate {host_id} enrollment reference")
            plan = self.lifecycle.plan(pool)
            aggregate_limit = aggregate_spend_limit_usd or pool.spend_limit_usd
            if aggregate_limit <= 0:
                raise ValueError("aggregate spend limit must be positive")
            other_admitted = 0.0
            for path in self.state_path.parent.glob("*-scale.json"):
                if path != self.state_path:
                    other_admitted += float(json.loads(path.read_text()).get("estimated_accrued_usd", 0))
            if other_admitted + plan.estimated_accrued_usd > aggregate_limit:
                raise ValueError("scale request exceeds aggregate admitted worker budget")
            value = {
                **current,
                "operation_id": identity,
                "pool": pool.name,
                "admitted_declaration": {"contract_version": CONTRACT_VERSION, **asdict(pool)},
                "desired": pool.desired,
                "maximum": pool.maximum,
                "spend_limit_usd": pool.spend_limit_usd,
                "aggregate_spend_limit_usd": aggregate_limit,
                "estimated_accrued_usd": plan.estimated_accrued_usd,
                "deadline": deadline.astimezone(dt.UTC).isoformat(),
                "exact_version": f"{pool.profile.image}/{pool.profile.version}/{pool.profile.bootstrap_version}",
                "retained_resources": current.get("retained_resources", []) if current else [],
                "ephemeral_credentials_pending_revocation": [],
            }
            self._write(value)
        return self.status(pool)

    def continue_(self, pool: PoolDeclaration) -> ScaleStatus:
        operation = self._require(pool)
        admitted = self._admitted(operation)
        if asdict(pool) != asdict(admitted):
            raise ValueError("pool declaration changed; submit a new admitted scale request")
        pool = admitted
        enrolled_slots = int(operation["desired"])
        deadline = dt.datetime.fromisoformat(operation["deadline"])
        if self.now() >= deadline:
            pool = replace(pool, desired=0, enabled=True)
        else:
            missing = self._missing(pool)
            if missing:
                # Slots are stable and filled in order. Converge the ready prefix so a
                # missing later enrollment does not discard useful partial progress.
                first_blocked = min(int(host_id.rsplit("-", 1)[1]) for host_id in missing)
                hosts = self.lifecycle.reconcile(replace(pool, desired=first_blocked))
                return self.status(pool, hosts=hosts)
        hosts = self.lifecycle.reconcile(pool)
        retained = sorted({resource for host in hosts for resource in host.retained_resources})
        if pool.desired == 0:
            revoked = sorted({ref for slot in range(enrolled_slots)
                              for ref in self.enrollments.revoke(f"{pool.name}-{slot}")})
            operation["ephemeral_credentials_pending_revocation"] = revoked
        if pool.desired == 0:
            operation["desired"] = 0
            operation["estimated_accrued_usd"] = 0.0
        operation["retained_resources"] = retained
        self._write(operation)
        return self.status(pool, hosts=hosts)

    def cleanup(self, pool: PoolDeclaration) -> ScaleStatus:
        """Retire the capacity admitted by this operation without trusting new inputs."""
        operation = self._require(pool)
        admitted = self._admitted(operation)
        return self._cleanup(replace(admitted, desired=0, enabled=True), operation)

    def _cleanup(self, pool: PoolDeclaration, operation: dict) -> ScaleStatus:
        enrolled_slots = int(operation["desired"])
        hosts = self.lifecycle.reconcile(pool)
        operation["ephemeral_credentials_pending_revocation"] = sorted({
            ref for slot in range(enrolled_slots)
            for ref in self.enrollments.revoke(f"{pool.name}-{slot}")
        })
        operation["desired"] = 0
        operation["estimated_accrued_usd"] = 0.0
        operation["retained_resources"] = sorted({
            resource for host in hosts for resource in host.retained_resources
        })
        self._write(operation)
        return self.status(pool, hosts=hosts)

    def status(self, pool: PoolDeclaration, *, hosts: list[HostFacts] | None = None) -> ScaleStatus:
        operation = self._require(pool)
        admitted = self._admitted(operation)
        pool = replace(admitted, desired=int(operation["desired"]))
        hosts = hosts if hosts is not None else self.lifecycle.inspect(pool)
        active = [host for host in hosts if host.state != HostState.TERMINATED]
        healthy = sum(host.state in {HostState.READY, HostState.BUSY} for host in active)
        pending = sum(host.state in {HostState.PROVISIONING, HostState.BOOTSTRAPPING,
                                    HostState.DRAINING} for host in active)
        failed = sum(host.state in {HostState.FAILED, HostState.INTERRUPTED} for host in active)
        plan = self.lifecycle.plan(pool)
        return ScaleStatus(
            operation["operation_id"], operation["desired"], healthy, pending, failed,
            operation["exact_version"], operation["deadline"], plan.estimated_accrued_usd,
            operation["spend_limit_usd"], operation["aggregate_spend_limit_usd"], pool.maximum,
            pool.profile.cpu, pool.profile.memory_mib, pool.profile.disk_gib,
            tuple(hosts), self._missing(pool),
            tuple(operation.get("retained_resources", [])),
            tuple(operation.get("ephemeral_credentials_pending_revocation", [])),
            "provider billing can arrive after teardown; retained resources may continue to cost",
        )

    def _missing(self, pool: PoolDeclaration) -> dict[str, tuple[str, ...]]:
        active_operations = {host.operation_id for host in self.lifecycle.inspect(pool)
                             if host.state != HostState.TERMINATED}
        result = {}
        for slot in range(pool.desired):
            declaration = self.lifecycle._declaration(pool, slot)
            if declaration.operation_id in active_operations:
                continue
            enrollment = self.enrollments.ensure(
                declaration.host_id, declaration.pool.profile.enrollment_secret_ref
            )
            missing = enrollment.missing(self.now())
            if (enrollment.secret_ref
                    and enrollment.secret_ref != declaration.pool.profile.enrollment_secret_ref):
                missing = (*missing, "matching per-host bootstrap secret reference")
            if missing:
                result[declaration.host_id] = missing
        return result

    def _require(self, pool: PoolDeclaration) -> dict:
        value = self._read()
        if not value:
            raise ValueError("scale operation has not been requested")
        if value["operation_id"] != self._identity(pool):
            raise ValueError("pool/version does not match the durable scale operation")
        return value

    @staticmethod
    def _admitted(operation: dict) -> PoolDeclaration:
        declaration = operation.get("admitted_declaration")
        if not isinstance(declaration, dict):
            raise ValueError("legacy scale operation must be requested again for durable admission")
        return pool_from_dict(declaration)

    def _locked(self):
        class Lock:
            def __init__(inner, path: Path):
                inner.path = path
                inner.file = None

            def __enter__(inner):
                inner.path.parent.mkdir(parents=True, exist_ok=True)
                inner.file = inner.path.open("a")
                fcntl.flock(inner.file, fcntl.LOCK_EX)

            def __exit__(inner, *_args):
                assert inner.file is not None
                inner.file.close()

        # Admission is aggregate across sibling operations, so they share one lock.
        return Lock(self.state_path.parent / ".scale-admission.lock")

    def _read(self) -> dict:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def _write(self, value: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.state_path)


def status_dict(status: ScaleStatus) -> dict:
    return asdict(status)


def durable_worker_readiness(garden_dir: Path):
    """Build a readiness gate from authenticated, durably returned run artifacts.

    A claim alone is insufficient.  A matching host must have completed a portable-protocol
    run at the requested source/profile/bootstrap versions and returned its result to the
    controller's run store.
    """

    def check(host: HostFacts, pool: PoolDeclaration) -> tuple[bool | None, str]:
        for facts_path in garden_dir.glob("runs/*/*/host_facts.json"):
            run_path = facts_path.with_name("run.json")
            result_path = facts_path.with_name("remote_result.json")
            if not run_path.exists() or not result_path.exists():
                continue
            try:
                facts = json.loads(facts_path.read_text())
                run = json.loads(run_path.read_text())
                returned = json.loads(result_path.read_text())
            except (OSError, ValueError):
                continue
            attestations = facts.get("readiness_attestations", {})
            gates = ("bootstrap_manifest", "authenticated_registration", "repository_ci")
            if (facts.get("provider_id") == host.provider_id
                    and facts.get("profile_version") == pool.profile.version
                    and facts.get("bootstrap_version") == pool.profile.bootstrap_version
                    and facts.get("source_head") == pool.profile.version
                    and all(attestations.get(gate) is True for gate in gates)
                    and run.get("status") == "done"
                    and run.get("finished_at")
                    and returned.get("result", {}).get("status") == "done"):
                return True, ("bootstrap manifest, authenticated registration, repository/CI "
                              f"doctor and durable task result {run.get('run_id', facts_path.parent.name)} verified")
        return None, ("awaiting bootstrap manifest, authenticated registration, repository/CI "
                      "doctor and a durable real-task result")

    return check
