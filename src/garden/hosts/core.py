"""Idempotent reconciliation for bounded, declarative host pools."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from .models import (
    CONTRACT_VERSION,
    HostDeclaration,
    HostEvent,
    HostFacts,
    HostPlan,
    HostState,
    PoolDeclaration,
)
from .provider import (
    AllowPolicy,
    HostProvider,
    PolicyResolver,
    ProvisioningUncertain,
    TransientProviderError,
)


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


class HostLifecycle:
    def __init__(
        self,
        providers: dict[str, HostProvider],
        state: JsonStateStore,
        policy: PolicyResolver | None = None,
        health_check: Callable[[HostFacts, PoolDeclaration], tuple[bool, str]] | None = None,
        retry_attempts: int = 3,
        retry_delay: Callable[[float], None] = time.sleep,
    ):
        self.providers = providers
        self.state = state
        self.policy = policy or AllowPolicy()
        self.health_check = health_check
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay

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
                f"hard pool spend limit ${pool.spend_limit_usd:.2f}",
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
        for slot in range(len(active), pool.desired):
            declaration = self._declaration(pool, slot)
            self.policy.authorize("provision", declaration)
            # Discover by the stable operation tag immediately before launch. This closes
            # restart and delayed-response races even when the local state write never ran.
            existing = next(
                (
                    h
                    for h in provider.discover(pool.owner, pool.name)
                    if h.operation_id == declaration.operation_id
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
            events.append(
                HostEvent(
                    "retired", retired.host_id, retired.state, ", ".join(retired.retained_resources)
                )
            )
        refreshed = sorted(provider.discover(pool.owner, pool.name), key=lambda h: h.host_id)
        discovered_operations = {host.operation_id for host in refreshed}
        failures = [host for host in failures if host.operation_id not in discovered_operations]
        checked: list[HostFacts] = list(failures)
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
                if h.operation_id == declaration.operation_id
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
        data = self.state.read()
        pools = data.setdefault("pools", {})
        assert isinstance(pools, dict)
        pools[pool.name] = {"plan": asdict(plan), "hosts": [asdict(h) for h in hosts]}
        event_rows = data.setdefault("events", [])
        assert isinstance(event_rows, list)
        event_rows.extend(asdict(e) for e in events)
        self.state.write(data)
