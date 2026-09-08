"""Deterministic provider for consumers, examples and contract tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .models import CONTRACT_VERSION, HostDeclaration, HostFacts, HostState, ProviderCapabilities
from .provider import ProvisioningUncertain


class FakeProvider:
    name = "fake"
    contract_version = CONTRACT_VERSION
    capabilities = ProviderCapabilities(stop_start=True, persistent_disks=True)

    def __init__(self, *, hourly_usd: float = 0.10):
        self.hourly_usd = hourly_usd
        self.hosts: dict[str, HostFacts] = {}
        self.ownership: dict[str, tuple[str, str]] = {}
        self.provision_calls = 0
        self.destroy_calls: list[tuple[str, bool]] = []
        self.delay_next_response = False

    def validate_options(self, options: dict[str, Any]) -> None:
        unknown = set(options) - {"fixture_label"}
        if unknown:
            raise ValueError(f"fake provider options not supported: {sorted(unknown)}")

    def estimate_hourly_usd(self, declaration: HostDeclaration) -> float:
        return self.hourly_usd

    def discover(self, owner: str, pool: str) -> list[HostFacts]:
        return [
            host
            for provider_id, host in self.hosts.items()
            if self.ownership.get(provider_id) == (owner, pool)
        ]

    def provision(self, declaration: HostDeclaration) -> HostFacts:
        existing = next(
            (
                h
                for h in self.hosts.values()
                if h.operation_id == declaration.operation_id and h.state != HostState.TERMINATED
            ),
            None,
        )
        if existing:
            return existing
        self.provision_calls += 1
        host = HostFacts(
            declaration.host_id,
            f"fake-{self.provision_calls}",
            declaration.operation_id,
            HostState.READY,
            declaration.pool.profile.image,
            declaration.pool.profile.bootstrap_version,
        )
        self.hosts[host.provider_id] = host
        self.ownership[host.provider_id] = (declaration.pool.owner, declaration.pool.name)
        if self.delay_next_response:
            self.delay_next_response = False
            raise ProvisioningUncertain("provider response timed out after accepting request")
        return host

    def inspect(self, provider_id: str) -> HostFacts:
        return self.hosts[provider_id]

    def stop(self, provider_id: str) -> HostFacts:
        self.hosts[provider_id] = replace(self.hosts[provider_id], state=HostState.STOPPED)
        return self.hosts[provider_id]

    def start(self, provider_id: str) -> HostFacts:
        self.hosts[provider_id] = replace(self.hosts[provider_id], state=HostState.READY)
        return self.hosts[provider_id]

    def destroy(self, provider_id: str, *, delete_storage: bool) -> HostFacts:
        host = self.hosts[provider_id]
        retained = () if delete_storage else (f"disk:{provider_id}",)
        host = replace(host, state=HostState.TERMINATED, retained_resources=retained)
        self.hosts[provider_id] = host
        self.destroy_calls.append((provider_id, delete_storage))
        return host
