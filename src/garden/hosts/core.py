"""Idempotent reconciliation for bounded, declarative host pools."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .models import (
    CONTRACT_VERSION,
    HostAdmission,
    HostDeclaration,
    HostEvent,
    HostFacts,
    HostPlan,
    HostRequirements,
    HostState,
    PoolDeclaration,
)
from .provider import (
    AllowPolicy,
    HostProvider,
    PolicyResolver,
    ProviderError,
    ProvisioningUncertain,
    TransientProviderError,
)


class EnvironmentStop(RuntimeError):
    """Host preparation failed before a task attempt was dispatched."""


class JsonStateStore:
    """Small atomic store for operation identities and lifecycle events."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"contract_version": CONTRACT_VERSION, "pools": {}, "events": []}
        return json.loads(self.path.read_text())

    def write(self, value: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)

    @contextmanager
    def locked(self):
        """Serialize a read-modify-write transaction across controller processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(f"{self.path.suffix}.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class HostLifecycle:
    def __init__(
        self,
        providers: dict[str, HostProvider],
        state: JsonStateStore,
        policy: PolicyResolver | None = None,
        health_check: Callable[[HostFacts, PoolDeclaration], tuple[bool, str]] | None = None,
        retry_attempts: int = 3,
        retry_delay: Callable[[float], None] = time.sleep,
        reservation_seconds: float = 300,
    ):
        self.providers = providers
        self.state = state
        self.policy = policy or AllowPolicy()
        self.health_check = health_check
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        if reservation_seconds <= 0:
            raise ValueError("reservation_seconds must be positive")
        self.reservation_seconds = reservation_seconds

    def _provider(self, pool: PoolDeclaration) -> HostProvider:
        try:
            provider = self.providers[pool.provider]
        except KeyError:
            raise ValueError(f"unknown host provider {pool.provider!r}") from None
        if provider.contract_version != CONTRACT_VERSION:
            raise ValueError(
                f"provider {pool.provider!r} uses {provider.contract_version}; expected {CONTRACT_VERSION}"
            )
        provider.validate_options(pool.provider_options)
        provider.validate_options(pool.profile.provider_options)
        binder = getattr(provider, "bind", None)
        if binder is not None:
            provider = binder(self._declaration(pool, 0))
        return provider

    @staticmethod
    def validate(pool: PoolDeclaration) -> None:
        if not pool.name or not pool.owner or not pool.purpose:
            raise ValueError("pool name, owner and purpose are required")
        if pool.minimum < 0 or pool.maximum < 0 or not pool.minimum <= pool.desired <= pool.maximum:
            raise ValueError("capacity must satisfy 0 <= minimum <= desired <= maximum")
        if pool.spend_limit_usd <= 0:
            raise ValueError("spend_limit_usd must be positive")
        if pool.estimated_runtime_hours <= 0:
            raise ValueError("estimated_runtime_hours must be positive")
        profile = pool.profile
        if not profile.image or not profile.version or not profile.bootstrap_version:
            raise ValueError("profile image, version and bootstrap_version must be pinned")
        if profile.endpoint and not profile.endpoint.startswith("https://"):
            raise ValueError("profile endpoint must use reachable HTTPS")
        if profile.endpoint and not profile.enrollment_secret_ref:
            raise ValueError("an HTTPS enrollment endpoint requires a scoped secret reference")

    def _operation_id(self, pool: PoolDeclaration, slot: int) -> str:
        raw = f"{CONTRACT_VERSION}\0{pool.owner}\0{pool.name}\0{slot}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _declaration(self, pool: PoolDeclaration, slot: int) -> HostDeclaration:
        operation = self._operation_id(pool, slot)
        return HostDeclaration(host_id=f"{pool.name}-{slot}", operation_id=operation, pool=pool)

    def _available_slots(self, pool: PoolDeclaration, hosts: list[HostFacts]) -> list[int]:
        """Return the lowest stable slots not occupied by discovered hosts."""
        occupied_operations = {host.operation_id for host in hosts}
        available: list[int] = []
        slot = 0
        while len(available) < pool.desired:
            if self._operation_id(pool, slot) not in occupied_operations:
                available.append(slot)
            slot += 1
        return available

    def plan(self, pool: PoolDeclaration) -> HostPlan:
        self.validate(pool)
        provider = self._provider(pool)
        current = sorted(provider.discover(pool.owner, pool.name), key=lambda h: h.host_id)
        active = [h for h in current if h.state != HostState.TERMINATED]
        retire = tuple(h.host_id for h in active[pool.desired :])
        sample = self._declaration(pool, len(active))
        estimate = provider.estimate_hourly_usd(sample) * pool.desired
        return HostPlan(
            pool=pool.name,
            enabled=pool.enabled,
            current=len(active),
            desired=pool.desired,
            create=max(0, pool.desired - len(active)),
            retire=retire,
            estimated_hourly_usd=estimate,
            estimated_accrued_usd=estimate * pool.estimated_runtime_hours,
            assumptions=(
                f"compute and {pool.profile.disk_gib} GiB storage for {pool.desired} host(s)",
                "public IPv4 and transfer are provider-dependent and excluded unless adapter pricing includes them",
                f"projected admission budget ${pool.spend_limit_usd:.2f}; not a hard billing cap",
                f"estimated runtime {pool.estimated_runtime_hours:g} hour(s)",
            ),
        )

    def reconcile(self, pool: PoolDeclaration) -> list[HostFacts]:
        """Converge an enabled pool. Loading or planning a disabled pool never mutates it."""
        plan = self.plan(pool)
        if not pool.enabled:
            raise ValueError("pool is disabled; set enabled=true only after reviewing the plan")
        if plan.estimated_accrued_usd > pool.spend_limit_usd:
            raise ValueError(
                f"estimated cost ${plan.estimated_accrued_usd:.2f} exceeds "
                f"pool spend limit ${pool.spend_limit_usd:.2f}"
            )
        provider = self._provider(pool)
        hosts = sorted(provider.discover(pool.owner, pool.name), key=lambda h: h.host_id)
        active = [h for h in hosts if h.state != HostState.TERMINATED]
        events: list[HostEvent] = []
        failures: list[HostFacts] = []
        retirements: list[HostFacts] = []
        slots = self._available_slots(pool, active)[: max(0, pool.desired - len(active))]
        for slot in slots:
            declaration = self._declaration(pool, slot)
            self.policy.authorize("provision", declaration)
            # Discover by the stable operation tag immediately before launch. This closes
            # restart and delayed-response races even when the local state write never ran.
            existing = next(
                (
                    h
                    for h in provider.discover(pool.owner, pool.name)
                    if h.operation_id == declaration.operation_id
                    and h.state != HostState.TERMINATED
                ),
                None,
            )
            if existing:
                active.append(existing)
                continue
            try:
                host = self.provision(pool, slot)
            except ProvisioningUncertain as exc:
                events.append(
                    HostEvent(
                        "provisioning_uncertain",
                        declaration.host_id,
                        HostState.PROVISIONING,
                        str(exc),
                    )
                )
                failures.append(
                    HostFacts(
                        declaration.host_id,
                        "",
                        declaration.operation_id,
                        HostState.PROVISIONING,
                        pool.profile.image,
                        pool.profile.bootstrap_version,
                        detail=str(exc),
                    )
                )
                continue
            except TransientProviderError as exc:
                failed = HostFacts(
                    declaration.host_id,
                    "",
                    declaration.operation_id,
                    HostState.FAILED,
                    pool.profile.image,
                    pool.profile.bootstrap_version,
                    detail=f"provider retries exhausted: {exc}",
                )
                events.append(
                    HostEvent("provision_failed", failed.host_id, failed.state, failed.detail)
                )
                failures.append(failed)
                continue
            active.append(host)
            events.append(HostEvent("provisioned", host.host_id, host.state))
        for host in active[pool.desired :]:
            declaration = self._declaration(pool, 0)
            self.policy.authorize("destroy", declaration)
            delete_storage = not pool.profile.persistent_workspace
            retired = provider.destroy(host.provider_id, delete_storage=delete_storage)
            retirements.append(retired)
            events.append(
                HostEvent(
                    "retired", retired.host_id, retired.state, ", ".join(retired.retained_resources)
                )
            )
        refreshed = sorted(provider.discover(pool.owner, pool.name), key=lambda h: h.host_id)
        discovered_operations = {host.operation_id for host in refreshed}
        failures = [host for host in failures if host.operation_id not in discovered_operations]
        retired_ids = {host.provider_id for host in retirements}
        refreshed = [host for host in refreshed if host.provider_id not in retired_ids]
        checked: list[HostFacts] = [*failures, *retirements]
        for host in refreshed:
            if self.health_check is not None and host.state in {
                HostState.BOOTSTRAPPING,
                HostState.READY,
            }:
                healthy, detail = self.health_check(host, pool)
                if not healthy:
                    host = HostFacts(
                        **{**asdict(host), "state": HostState.FAILED, "detail": detail}
                    )
                    events.append(HostEvent("bootstrap_failed", host.host_id, host.state, detail))
                    self.policy.authorize("destroy", self._declaration(pool, 0))
                    host = provider.destroy(
                        host.provider_id, delete_storage=not pool.profile.persistent_workspace
                    )
                elif host.state == HostState.BOOTSTRAPPING:
                    host = HostFacts(**{**asdict(host), "state": HostState.READY, "detail": detail})
                    events.append(HostEvent("registered", host.host_id, host.state, detail))
            checked.append(host)
        self._record(pool, checked, events, plan)
        return checked

    def inspect(self, pool: PoolDeclaration) -> list[HostFacts]:
        self.validate(pool)
        return self._provider(pool).discover(pool.owner, pool.name)

    def provision(self, pool: PoolDeclaration, slot: int = 0) -> HostFacts:
        """Provision one stable slot after the caller has explicitly enabled the pool."""
        self.validate(pool)
        if not pool.enabled:
            raise ValueError("pool is disabled; set enabled=true only after reviewing the plan")
        declaration = self._declaration(pool, slot)
        self.policy.authorize("provision", declaration)
        provider = self._provider(pool)
        existing = next(
            (
                h
                for h in provider.discover(pool.owner, pool.name)
                if h.operation_id == declaration.operation_id and h.state != HostState.TERMINATED
            ),
            None,
        )
        if existing:
            return existing
        for attempt in range(self.retry_attempts):
            try:
                return provider.provision(declaration)
            except TransientProviderError:
                if attempt + 1 == self.retry_attempts:
                    raise
                self.retry_delay(2**attempt)
        raise AssertionError("unreachable")

    def _owned(self, pool: PoolDeclaration, provider_id: str) -> HostFacts:
        host = next((h for h in self.inspect(pool) if h.provider_id == provider_id), None)
        if host is None:
            raise ValueError(f"host {provider_id!r} is not owned by pool {pool.name!r}")
        return host

    def stop(self, pool: PoolDeclaration, provider_id: str) -> HostFacts:
        provider = self._provider(pool)
        if not provider.capabilities.stop_start:
            raise ValueError(f"provider {provider.name!r} does not support stop/start")
        self._owned(pool, provider_id)
        return provider.stop(provider_id)

    def start(self, pool: PoolDeclaration, provider_id: str) -> HostFacts:
        provider = self._provider(pool)
        if not provider.capabilities.stop_start:
            raise ValueError(f"provider {provider.name!r} does not support stop/start")
        self._owned(pool, provider_id)
        return provider.start(provider_id)

    def destroy(
        self, pool: PoolDeclaration, provider_id: str, *, delete_storage: bool
    ) -> HostFacts:
        if pool.profile.persistent_workspace and delete_storage:
            raise ValueError(
                "persistent workspace deletion requires the consumer's explicit release flow"
            )
        self._owned(pool, provider_id)
        return self._provider(pool).destroy(provider_id, delete_storage=delete_storage)

    def _record(
        self, pool: PoolDeclaration, hosts: list[HostFacts], events: list[HostEvent], plan: HostPlan
    ) -> None:
        with self.state.locked():
            data = self.state.read()
            pools = data.setdefault("pools", {})
            assert isinstance(pools, dict)
            pools[pool.name] = {"plan": asdict(plan), "hosts": [asdict(h) for h in hosts]}
            event_rows = data.setdefault("events", [])
            assert isinstance(event_rows, list)
            event_rows.extend(asdict(e) for e in events)
            self.state.write(data)

    def acquire_ready(
        self,
        pool: PoolDeclaration,
        *,
        workspace: str,
        revision: str,
        harness: str,
        process_terminal: Callable[[str], bool],
        now: Callable[[], float] = time.time,
        requirements: HostRequirements | None = None,
    ) -> HostFacts:
        """Acquire one verified host, preferring an eligible warm host.

        The durable lease is written only after every read-only readiness check succeeds.
        Callers perform this before incrementing a task attempt; ``EnvironmentStop`` is an
        infrastructure outcome, not worker failure.
        """
        if requirements is not None:
            self._validate_requirements(requirements)
        try:
            checked = self.reconcile(pool)
        except ProviderError as exc:
            self._record_environment_stop(pool, str(exc))
            raise EnvironmentStop(str(exc)) from exc
        data = self.state.read()
        leases = data.setdefault("leases", {})
        assert isinstance(leases, dict)
        provider = self._provider(pool)
        readiness = getattr(provider, "readiness", None)
        if readiness is None:
            raise EnvironmentStop(f"provider {provider.name!r} does not support readiness")
        current_time = now()
        candidates: list[HostFacts] = []
        for host in checked:
            if host.state not in {HostState.READY, HostState.STOPPED}:
                continue
            if (host.image, host.bootstrap_version) != (
                pool.profile.image,
                pool.profile.bootstrap_version,
            ):
                continue
            lease = leases.get(host.provider_id, {})
            if isinstance(lease, dict):
                acquired = float(lease.get("acquired_at", current_time))
                if current_time - acquired >= pool.maximum_age_minutes * 60:
                    self.destroy(
                        pool,
                        host.provider_id,
                        delete_storage=not pool.profile.persistent_workspace,
                    )
                    self.cancel_acquisition(host.provider_id)
                    leases.pop(host.provider_id, None)
                    continue
                previous = str(lease.get("run_id", ""))
                reserved_until = float(lease.get("reserved_until", 0))
                if not lease.get("released", False) and not previous:
                    if current_time < reserved_until:
                        continue
                    leases.pop(host.provider_id, None)
                if previous and not process_terminal(previous):
                    continue
            candidates.append(host)
        failures: list[str] = []
        for host in candidates:
            lease = leases.get(host.provider_id, {})
            assert isinstance(lease, dict)
            acquired_at = float(lease.get("acquired_at", current_time))
            try:
                if host.state == HostState.STOPPED:
                    if not provider.capabilities.stop_start:
                        failures.append(f"{host.host_id}: stopped host cannot be restarted")
                        continue
                    host = provider.start(host.provider_id)
                    if host.state != HostState.READY:
                        failures.append(
                            f"{host.host_id}: start completed in state {host.state.value}"
                        )
                        continue
                evidence = readiness(
                    host.provider_id, workspace=workspace, revision=revision, harness=harness
                )
            except ProviderError as exc:
                detail = f"{host.host_id}: {exc}"
                self._record_environment_stop(pool, detail)
                raise EnvironmentStop(detail) from exc
            if not evidence.ready:
                failures.append(f"{host.host_id}: {evidence.detail or 'readiness checks failed'}")
                continue
            admission: HostAdmission | None = None
            if requirements is not None:
                admit = getattr(provider, "admit", None)
                if admit is None:
                    failures.append(f"{host.host_id}: provider does not support host-local admission")
                    continue
                try:
                    admission = admit(
                        host.provider_id,
                        requirements=requirements,
                        acquisition_id=uuid.uuid4().hex,
                    )
                except ProviderError as exc:
                    detail = f"{host.host_id}: admission unavailable: {exc}"
                    self._record_environment_stop(pool, detail, data=data)
                    raise EnvironmentStop(detail) from exc
                reason = self._admission_rejection(admission, requirements, current_time)
                if reason:
                    if admission.lease_id:
                        try:
                            provider.release_admission(
                                host.provider_id, lease_id=admission.lease_id
                            )
                        except ProviderError as exc:
                            reason = f"{reason}; admission lease release failed: {exc}"
                    failures.append(f"{host.host_id}: {reason}")
                    continue
            # Read and claim again under the store lock. Another controller may have
            # completed the same readiness probe while this one was in the wrapper.
            with self.state.locked():
                claimed = self.state.read()
                claimed_leases = claimed.setdefault("leases", {})
                assert isinstance(claimed_leases, dict)
                existing = claimed_leases.get(host.provider_id)
                if isinstance(existing, dict):
                    existing_run = str(existing.get("run_id", ""))
                    reserved_until = float(existing.get("reserved_until", 0))
                    actively_reserved = (
                        not existing.get("released", False)
                        and not existing_run
                        and current_time < reserved_until
                    )
                    active_run = bool(existing_run and not process_terminal(existing_run))
                    if actively_reserved or active_run:
                        if admission is not None and admission.lease_id:
                            try:
                                provider.release_admission(
                                    host.provider_id, lease_id=admission.lease_id
                                )
                            except ProviderError as exc:
                                detail = f"{host.host_id}: admission lease release failed: {exc}"
                                self._record_environment_stop(pool, detail, data=claimed)
                                raise EnvironmentStop(detail) from exc
                        continue
                    acquired_at = float(existing.get("acquired_at", acquired_at))
                claimed_leases[host.provider_id] = {
                    "acquired_at": acquired_at,
                    "last_used_at": current_time,
                    "reserved_until": current_time + self.reservation_seconds,
                    "workspace": workspace,
                    "revision": revision,
                    "harness": harness,
                    "run_id": "",
                    "released": False,
                }
                if admission is not None:
                    claimed_leases[host.provider_id]["admission"] = asdict(admission)
                    claimed_leases[host.provider_id]["requirements"] = asdict(requirements)
                stops = claimed.setdefault("environment_stops", {})
                if isinstance(stops, dict):
                    stops.pop(pool.name, None)
                self.state.write(claimed)
                return host
        detail = "; ".join(failures) or "no ready host is available"
        self._record_environment_stop(pool, detail)
        raise EnvironmentStop(detail)

    @staticmethod
    def _validate_requirements(requirements: HostRequirements) -> None:
        if not requirements.activity or not requirements.host_class or not requirements.environment:
            raise ValueError("activity, host_class and environment are required for host admission")
        if requirements.memory_mib < 0 or requirements.disk_gib < 0:
            raise ValueError("host resource requirements cannot be negative")
        if requirements.probe_max_age_seconds <= 0 or requirements.lease_seconds <= 0:
            raise ValueError("probe and lease durations must be positive")

    @staticmethod
    def _admission_rejection(
        admission: HostAdmission, requirements: HostRequirements, current_time: float
    ) -> str:
        if not admission.eligible:
            return admission.detail or "host-local admission rejected"
        if not math.isfinite(admission.measured_at):
            return "resource probe timestamp is invalid"
        if admission.measured_at > current_time + 5:
            return "resource probe timestamp is in the future"
        age = current_time - admission.measured_at
        if age > requirements.probe_max_age_seconds:
            return f"resource probe is stale ({age:.0f}s old)"
        if admission.host_class != requirements.host_class:
            return f"host class {admission.host_class!r} does not match {requirements.host_class!r}"
        if admission.environment != requirements.environment:
            return f"environment {admission.environment!r} does not match {requirements.environment!r}"
        missing = sorted(set(requirements.capabilities) - set(admission.capabilities))
        if missing:
            return f"missing capabilities: {', '.join(missing)}"
        if admission.memory_available_mib < requirements.memory_mib:
            return (
                f"host memory {admission.memory_available_mib} MiB is below "
                f"{requirements.memory_mib} MiB"
            )
        if admission.disk_free_gib < requirements.disk_gib:
            return f"host disk {admission.disk_free_gib} GiB is below {requirements.disk_gib} GiB"
        if (
            not admission.lease_id
            or not math.isfinite(admission.lease_expires_at)
            or admission.lease_expires_at <= current_time
        ):
            return "host-local admission lease is missing or expired"
        return ""

    def renew_admission(
        self, pool: PoolDeclaration, provider_id: str, *, now: Callable[[], float] = time.time
    ) -> HostAdmission:
        """Renew a host-owned lease, failing closed when ownership was lost."""
        data = self.state.read()
        leases = data.get("leases", {})
        lease = leases.get(provider_id) if isinstance(leases, dict) else None
        raw = lease.get("admission") if isinstance(lease, dict) else None
        raw_requirements = lease.get("requirements") if isinstance(lease, dict) else None
        lease_id = str(raw.get("lease_id", "")) if isinstance(raw, dict) else ""
        if not lease_id or not isinstance(raw_requirements, dict):
            raise EnvironmentStop("host admission lease is not recorded")
        try:
            requirements = HostRequirements(
                **{
                    **raw_requirements,
                    "capabilities": tuple(raw_requirements.get("capabilities", ())),
                }
            )
            self._validate_requirements(requirements)
        except (TypeError, ValueError) as exc:
            raise EnvironmentStop(f"host admission requirements are invalid: {exc}") from exc
        provider = self._provider(pool)
        try:
            admission = provider.renew_admission(provider_id, lease_id=lease_id)
        except ProviderError as exc:
            detail = f"host admission lease lost: {exc}"
            self._record_environment_stop(pool, detail, data=data)
            raise EnvironmentStop(detail) from exc
        current_time = now()
        rejection = self._admission_rejection(admission, requirements, current_time)
        if admission.lease_id != lease_id:
            rejection = "host admission lease identity changed"
        if rejection:
            detail = f"host admission lease lost: {rejection}"
            self._record_environment_stop(pool, detail, data=data)
            raise EnvironmentStop(detail)
        lease["admission"] = asdict(admission)
        self.state.write(data)
        return admission

    def _record_environment_stop(
        self,
        pool: PoolDeclaration,
        detail: str,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        value = data if data is not None else self.state.read()
        stops = value.setdefault("environment_stops", {})
        assert isinstance(stops, dict)
        stops[pool.name] = {"detail": detail, "recorded_at": time.time()}
        self.state.write(value)

    def attach_run(self, provider_id: str, run_id: str) -> None:
        """Persist the controller run identity after dispatch."""
        with self.state.locked():
            data = self.state.read()
            leases = data.setdefault("leases", {})
            assert isinstance(leases, dict)
            lease = leases.get(provider_id)
            if not isinstance(lease, dict):
                raise ValueError(f"host {provider_id!r} is not acquired")
            lease["run_id"] = run_id
            self.state.write(data)

    def cancel_acquisition(
        self, provider_id: str, *, pool: PoolDeclaration | None = None
    ) -> None:
        """Cancel before dispatch, releasing a host-owned admission when present."""
        data = self.state.read()
        leases = data.setdefault("leases", {})
        assert isinstance(leases, dict)
        lease = leases.get(provider_id)
        raw = lease.get("admission") if isinstance(lease, dict) else None
        lease_id = str(raw.get("lease_id", "")) if isinstance(raw, dict) else ""
        if lease_id:
            if pool is None:
                raise ValueError("pool is required to cancel a host-local admission lease")
            try:
                self._provider(pool).release_admission(provider_id, lease_id=lease_id)
            except ProviderError as exc:
                detail = f"host admission lease release failed: {exc}"
                self._record_environment_stop(pool, detail, data=data)
                raise EnvironmentStop(detail) from exc
        leases.pop(provider_id, None)
        self.state.write(data)

    def release(self, pool: PoolDeclaration, provider_id: str) -> HostFacts:
        """Release a warm host while retaining its age and prior process identity."""
        data = self.state.read()
        leases = data.setdefault("leases", {})
        assert isinstance(leases, dict)
        lease = leases.get(provider_id)
        raw = lease.get("admission") if isinstance(lease, dict) else None
        lease_id = str(raw.get("lease_id", "")) if isinstance(raw, dict) else ""
        if lease_id:
            try:
                self._provider(pool).release_admission(provider_id, lease_id=lease_id)
            except ProviderError as exc:
                detail = f"host admission lease release failed: {exc}"
                self._record_environment_stop(pool, detail, data=data)
                raise EnvironmentStop(detail) from exc
        released = self.stop(pool, provider_id)
        if isinstance(lease, dict):
            lease["released"] = True
            lease["last_used_at"] = time.time()
            lease.pop("admission", None)
            lease.pop("requirements", None)
        self.state.write(data)
        return released

    def orphaned(self, *, process_terminal: Callable[[str], bool]) -> list[str]:
        data = self.state.read()
        leases = data.get("leases", {})
        if not isinstance(leases, dict):
            return []
        return sorted(
            provider_id
            for provider_id, lease in leases.items()
            if isinstance(lease, dict)
            and not lease.get("released", False)
            and (run_id := str(lease.get("run_id", "")))
            and process_terminal(run_id)
        )
