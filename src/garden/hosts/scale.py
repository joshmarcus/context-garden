"""Durable, resumable scaling of a managed worker pool.

The operation coordinates credentials and readiness around :class:`HostLifecycle`; cloud
mutation remains in the provider adapter.  Secret values never enter this state file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Protocol

from .config import pool_from_dict
from .core import HostLifecycle
from .locking import file_lock
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
    phase: str


class ScaleOperation:
    """One durable request which can be safely continued after any interruption."""

    def __init__(self, lifecycle: HostLifecycle, state_path: Path,
                 enrollments: EnrollmentResolver, *, now=lambda: dt.datetime.now(dt.UTC),
                 enrollment_config_path: str = "", execution_context: dict | None = None):
        self.lifecycle = lifecycle
        self.state_path = state_path
        self.enrollments = enrollments
        self.now = now
        self.enrollment_config_path = enrollment_config_path
        self.execution_context = dict(execution_context or {})

    @staticmethod
    def _identity(pool: PoolDeclaration) -> str:
        profile = pool.profile
        stable = {"owner": pool.owner, "pool": pool.name, "provider": pool.provider,
                  "image": profile.image, "profile_version": profile.version,
                  "bootstrap_version": profile.bootstrap_version}
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:32]

    def request(self, pool: PoolDeclaration, *, deadline: dt.datetime,
                aggregate_spend_limit_usd: float | None = None) -> ScaleStatus:
        if deadline.tzinfo is None:
            raise ValueError("termination deadline must be a future absolute timestamp")
        deadline_text = deadline.astimezone(dt.UTC).isoformat()
        aggregate_limit = (pool.spend_limit_usd if aggregate_spend_limit_usd is None
                           else aggregate_spend_limit_usd)
        if (isinstance(aggregate_limit, bool) or not math.isfinite(aggregate_limit)
                or aggregate_limit <= 0):
            raise ValueError("aggregate spend limit must be finite and positive")
        declaration = {"contract_version": CONTRACT_VERSION, **asdict(pool)}
        with self._locked():
            current = self._read()
            identity = self._identity(pool)
            if current:
                if (current.get("admitted_declaration") != declaration
                        or current.get("deadline") != deadline_text
                        or current.get("aggregate_spend_limit_usd") != aggregate_limit):
                    raise ValueError("scale request is already admitted with different limits; "
                                     "use a new operation, never rewrite an existing admission")
                # A duplicate request, including one after expiry/cleanup, cannot reset
                # the deadline, mint credentials, resurrect capacity or release liability.
                return self.status(pool)
            if deadline <= self.now():
                raise ValueError("termination deadline must be a future absolute timestamp")
            source = pool.profile.source_head
            if source and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source):
                raise ValueError("profile.source_head must be an exact full source commit")
            if pool.provider == "ec2" and pool.profile.endpoint and not source:
                raise ValueError("production scale requests require profile.source_head")
            if pool.desired > 1 and "{host_id}" not in pool.profile.enrollment_secret_ref:
                raise ValueError("multi-host pools require a separate {host_id} enrollment reference")
            plan = self.lifecycle.plan(pool)
            duration_hours = (deadline - self.now()).total_seconds() / 3600
            admitted_cost = plan.estimated_hourly_usd * max(
                pool.estimated_runtime_hours, duration_hours)
            if not math.isfinite(admitted_cost) or admitted_cost < 0:
                raise ValueError("scale cost estimate must be finite")
            if admitted_cost > pool.spend_limit_usd:
                raise ValueError("scale request exceeds its pool spend limit")
            other_admitted = 0.0
            effective_limit = aggregate_limit
            for path in self.state_path.parent.glob("*.json"):
                if path.resolve() != self.state_path.resolve():
                    sibling = json.loads(path.read_text())
                    if not isinstance(sibling, dict) or "operation_id" not in sibling:
                        continue
                    sibling_pool = sibling.get("admitted_declaration") or {}
                    if (sibling_pool.get("owner") == pool.owner
                            and sibling_pool.get("name") == pool.name
                            and sibling.get("phase") != "cleaned"):
                        raise ValueError("this pool already has an active scale operation; continue "
                                         "that operation or finish its cleanup before a new admission")
                    charge = float(sibling.get("estimated_accrued_usd", 0))
                    if not math.isfinite(charge) or charge < 0:
                        raise ValueError("invalid saved aggregate admission charge")
                    other_admitted += charge
                    sibling_limit = float(sibling.get("aggregate_spend_limit_usd", aggregate_limit))
                    if not math.isfinite(sibling_limit) or sibling_limit <= 0:
                        raise ValueError("invalid saved aggregate admission limit")
                    effective_limit = min(effective_limit, sibling_limit)
            if other_admitted + admitted_cost > effective_limit:
                raise ValueError("scale request exceeds aggregate admitted worker budget")
            value = {
                "operation_id": hashlib.sha256(json.dumps(
                    {"declaration": declaration, "deadline": deadline_text,
                     "execution_context": self.execution_context},
                    sort_keys=True).encode()).hexdigest()[:32],
                "pool_identity": identity,
                "pool": pool.name,
                "admitted_declaration": declaration,
                "desired": pool.desired,
                "maximum": pool.maximum,
                "spend_limit_usd": pool.spend_limit_usd,
                "aggregate_spend_limit_usd": aggregate_limit,
                "estimated_accrued_usd": admitted_cost,
                "deadline": deadline_text,
                "exact_version": f"{pool.profile.image}/{pool.profile.version}/{pool.profile.bootstrap_version}",
                "source_head": source,
                "phase": "admitted",
                "requested_at": self.now().isoformat(),
                "enrollment_config_path": self.enrollment_config_path,
                "execution_context": self.execution_context,
                "enrollment_dir": str(self.enrollments.root.resolve())
                    if hasattr(self.enrollments, "root") else "",
                "retained_resources": [],
                "ephemeral_credentials_pending_revocation": [],
            }
            self._write(value)
        return self.status(pool)

    def continue_(self, pool: PoolDeclaration) -> ScaleStatus:
        with self._locked():
            return self._continue_locked(pool)

    def _continue_locked(self, pool: PoolDeclaration) -> ScaleStatus:
        operation = self._require(pool)
        admitted = self._admitted(operation)
        if asdict(pool) != asdict(admitted):
            raise ValueError("pool declaration changed; submit a new admitted scale request")
        pool = admitted
        if self.enrollment_config_path and not operation.get("enrollment_config_path"):
            operation["enrollment_config_path"] = self.enrollment_config_path
        if not operation.get("execution_context"):
            operation["execution_context"] = self.execution_context
        deadline = dt.datetime.fromisoformat(operation["deadline"])
        if self.now() >= deadline or operation.get("phase") in {"cleaning", "cleaned"}:
            return self._cleanup(replace(pool, desired=0, enabled=True), operation, safe=True)
        # Persist intent before any credential or infrastructure operation. The resolver
        # and provider both reconcile their own stable step identities on retry.
        operation["phase"] = "converging"
        self._write(operation)
        missing = self._missing(pool, ensure=True)
        if missing:
            # Never scale down a healthy sibling because another slot lost enrollment.
            eligible = {slot for slot in range(pool.desired)
                        if f"{pool.name}-{slot}" not in missing}
            hosts = self.lifecycle.reconcile(pool, eligible_slots=eligible)
            operation["phase"] = "awaiting_enrollment"
            self._write(operation)
            return self.status(pool, hosts=hosts)
        hosts = self.lifecycle.reconcile(pool)
        retained = sorted({resource for host in hosts for resource in host.retained_resources})
        operation["phase"] = "running"
        operation["retained_resources"] = retained
        self._write(operation)
        return self.status(pool, hosts=hosts)

    def cleanup(self, pool: PoolDeclaration) -> ScaleStatus:
        """Drain active work before retiring the capacity admitted by this operation."""
        with self._locked():
            operation = self._require(pool)
            admitted = self._admitted(operation)
            return self._cleanup(replace(admitted, desired=0, enabled=True), operation, safe=True)

    def emergency_stop(self, pool: PoolDeclaration) -> ScaleStatus:
        """Immediately retire capacity, explicitly bypassing the normal work drain."""
        with self._locked():
            operation = self._require(pool)
            admitted = self._admitted(operation)
            return self._cleanup(replace(admitted, desired=0, enabled=True), operation, safe=False)

    def _cleanup(self, pool: PoolDeclaration, operation: dict, *, safe: bool) -> ScaleStatus:
        enrolled_slots = self._admitted(operation).desired
        operation["phase"] = "cleaning"
        operation["desired"] = 0
        self._write(operation)
        if safe:
            hosts = self.lifecycle.drain(
                pool, deadline=str(operation["deadline"]), detail="operator-requested pool drain"
            )
        else:
            hosts = self.lifecycle.force_retire(
                pool, detail="operator-requested emergency pool stop"
            )
        active_ids = {host.host_id for host in hosts if host.state != HostState.TERMINATED}
        pending = set()
        for slot in range(enrolled_slots):
            host_id = f"{pool.name}-{slot}"
            if host_id in active_ids:
                pending.add(f"{host_id}: waiting for host termination before revocation")
            else:
                pending.update(self.enrollments.revoke(host_id))
        operation["ephemeral_credentials_pending_revocation"] = sorted(pending)
        retained = sorted({
            resource for host in hosts for resource in host.retained_resources
        })
        if not active_ids:
            retained = sorted(set(retained) | set(self.lifecycle.orphaned_resources(pool)))
        operation["retained_resources"] = retained
        if not active_ids and not retained and not pending:
            operation["phase"] = "cleaned"
            # Keep the original conservative aggregate charge. Provider billing is
            # delayed; termination is not evidence that this operation cost zero.
        else:
            operation["phase"] = "cleaning"
        self._write(operation)
        return self.status(pool, hosts=hosts)

    def status(self, pool: PoolDeclaration, *, hosts: list[HostFacts] | None = None) -> ScaleStatus:
        operation = self._require(pool)
        admitted = self._admitted(operation)
        pool = replace(admitted, desired=int(operation["desired"]))
        hosts = hosts if hosts is not None else self.lifecycle.inspect(pool)
        if self.lifecycle.health_check is not None:
            checked = []
            for host in hosts:
                if host.state in {HostState.BOOTSTRAPPING, HostState.READY}:
                    ready, detail = self.lifecycle.health_check(host, admitted)
                    state = (HostState.READY if ready is True else HostState.FAILED
                             if ready is False else HostState.BOOTSTRAPPING)
                    host = replace(host, state=state, detail=detail)
                checked.append(host)
            hosts = checked
        active = [host for host in hosts if host.state != HostState.TERMINATED]
        healthy = sum(host.state in {HostState.READY, HostState.BUSY} for host in active)
        pending = sum(host.state in {HostState.PROVISIONING, HostState.BOOTSTRAPPING,
                                    HostState.DRAINING} for host in active)
        failed = sum(host.state in {HostState.FAILED, HostState.INTERRUPTED} for host in active)
        return ScaleStatus(
            operation["operation_id"], operation["desired"], healthy, pending, failed,
            operation["exact_version"], operation["deadline"], operation["estimated_accrued_usd"],
            operation["spend_limit_usd"], operation["aggregate_spend_limit_usd"], pool.maximum,
            pool.profile.cpu, pool.profile.memory_mib, pool.profile.disk_gib,
            tuple(hosts), self._missing(pool),
            tuple(operation.get("retained_resources", [])),
            tuple(operation.get("ephemeral_credentials_pending_revocation", [])),
            "provider billing can arrive after teardown; retained resources may continue to cost",
            str(operation.get("phase") or "unknown"),
        )

    def _missing(self, pool: PoolDeclaration, *, ensure: bool = False) -> dict[str, tuple[str, ...]]:
        active = [host for host in self.lifecycle.inspect(pool) if host.state != HostState.TERMINATED]
        active_operations = {host.operation_id for host in active}
        active_hosts = {host.host_id for host in active}
        result = {}
        provider = self.lifecycle._provider(pool)
        needs_deadline_setup = (pool.provider == "ec2" and self.lifecycle.absolute_deadline
                                and getattr(provider, "deadline_enforcer", None) is None)
        for slot in range(pool.desired):
            declaration = self.lifecycle._declaration(pool, slot)
            if declaration.operation_id in active_operations or declaration.host_id in active_hosts:
                continue
            setup_probe = getattr(self.enrollments, "setup_missing", None)
            setup_missing = tuple(setup_probe(declaration.host_id)) if setup_probe else ()
            enrollment = (self.enrollments.ensure(
                declaration.host_id, declaration.pool.profile.enrollment_secret_ref
            ) if ensure and not needs_deadline_setup and not setup_missing
                else self.enrollments.resolve(declaration.host_id))
            missing = enrollment.missing(self.now())
            if setup_missing:
                missing = tuple(dict.fromkeys((*missing, *setup_missing)))
            if needs_deadline_setup:
                missing = (*missing, "controller-independent termination scheduler")
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
        if value.get("pool_identity", value["operation_id"]) != self._identity(pool):
            raise ValueError("pool/version does not match the durable scale operation")
        if value.get("execution_context") and value["execution_context"] != self.execution_context:
            raise ValueError("provider enrollment context changed; use the admitted account, "
                             "region and repository")
        self.lifecycle.absolute_deadline = value["deadline"]
        self.lifecycle.operation_seed = value["operation_id"] if value.get("pool_identity") else ""
        return value

    @staticmethod
    def _admitted(operation: dict) -> PoolDeclaration:
        declaration = operation.get("admitted_declaration")
        if not isinstance(declaration, dict):
            raise ValueError("legacy scale operation must be requested again for durable admission")
        return pool_from_dict(declaration)

    def _locked(self):
        # Admission is aggregate across sibling operations, so they share one lock.
        return file_lock(self.state_path.parent / ".scale-admission.lock")

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
        expected_source = pool.profile.source_head
        if not expected_source:
            return None, "awaiting an exact admitted source commit for durable worker readiness"
        options = {**pool.provider_options, **pool.profile.provider_options}
        expected_bootstrap = str(options.get("bootstrap_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_bootstrap):
            return None, "awaiting an admitted bootstrap artifact digest for durable worker readiness"
        for facts_path in garden_dir.glob("runs/*/*/host_facts.json"):
            run_path = facts_path.with_name("run.json")
            result_path = facts_path.with_name("remote_result.json")
            exit_path = facts_path.with_name("exit_code")
            if not run_path.exists() or not result_path.exists() or not exit_path.exists():
                continue
            try:
                facts = json.loads(facts_path.read_text())
                run = json.loads(run_path.read_text())
                returned = json.loads(result_path.read_text())
                exit_code = exit_path.read_text().strip()
            except (OSError, ValueError):
                continue
            if not all(isinstance(item, dict) for item in (facts, run, returned)):
                continue
            attestations = facts.get("readiness_attestations", {})
            source_bootstrap = facts.get("source_bootstrap", {})
            if not isinstance(attestations, dict) or not isinstance(source_bootstrap, dict):
                continue
            manifest = attestations.get("bootstrap_manifest", {})
            registration = attestations.get("authenticated_registration", {})
            repository = attestations.get("repository_access", {})
            ci_read = attestations.get("ci_provider_read", {})
            if not all(isinstance(item, dict)
                       for item in (manifest, registration, repository, ci_read)):
                continue
            result = returned.get("result", {})
            if not isinstance(result, dict):
                continue
            bootstrap_digest = source_bootstrap.get("bootstrap_sha256")
            if (facts.get("provider_id") == host.provider_id
                    and facts.get("operation_id") == host.operation_id
                    and facts.get("profile_version") == pool.profile.version
                    and facts.get("bootstrap_version") == pool.profile.bootstrap_version
                    and facts.get("source_head") == expected_source
                    and facts.get("schema_version") == 1
                    and source_bootstrap.get("source_head") == expected_source
                    and source_bootstrap.get("operation_id") == host.operation_id
                    and bootstrap_digest == expected_bootstrap
                    and manifest.get("ok") is True
                    and manifest.get("source_head") == expected_source
                    and manifest.get("profile_version") == pool.profile.version
                    and manifest.get("bootstrap_version") == pool.profile.bootstrap_version
                    and manifest.get("bootstrap_sha256") == bootstrap_digest
                    and manifest.get("direct_url_commit") == expected_source
                    and manifest.get("installed_distribution") == "context-garden"
                    and registration.get("ok") is True
                    and registration.get("method") == "scoped-worker-token"
                    and repository.get("ok") is True
                    and ci_read.get("ok") is True
                    and ci_read.get("source_head") == expected_source
                    and run.get("host") == host.host_id
                    and run.get("mode") in {"work", "revise"}
                    and run.get("status") == "done"
                    and run.get("finished_at")
                    and run.get("final_received_at")
                    and exit_code == "0"
                    and result.get("status") == "done"):
                return True, ("bootstrap manifest, authenticated registration, repository/CI "
                              f"doctor and identity-bound durable task result "
                              f"{run.get('run_id', facts_path.parent.name)} verified")
        return None, ("awaiting bootstrap manifest, authenticated registration, repository/CI "
                      "doctor and a durable real-task result")

    return check
