"""Interfaces injected into the provider-neutral host lifecycle."""

from __future__ import annotations

from typing import Any, Protocol

from .models import (
    HostAdmission,
    HostDeclaration,
    HostFacts,
    HostReadiness,
    HostRequirements,
    ProviderCapabilities,
)


class ProviderError(RuntimeError):
    """A provider operation failed with a known outcome."""


class ProvisioningUncertain(ProviderError):
    """The request may have succeeded; reconcile discovery before retrying it."""


class TransientProviderError(ProviderError):
    """A known-safe-to-retry provider failure."""


class HostProvider(Protocol):
    name: str
    contract_version: str
    capabilities: ProviderCapabilities

    def validate_options(self, options: dict[str, Any]) -> None: ...
    def estimate_hourly_usd(self, declaration: HostDeclaration) -> float: ...
    def discover(self, owner: str, pool: str) -> list[HostFacts]: ...
    def provision(self, declaration: HostDeclaration) -> HostFacts: ...
    def inspect(self, provider_id: str) -> HostFacts: ...
    def stop(self, provider_id: str) -> HostFacts: ...
    def start(self, provider_id: str) -> HostFacts: ...
    def destroy(self, provider_id: str, *, delete_storage: bool) -> HostFacts: ...


class AcquisitionProvider(HostProvider, Protocol):
    """Optional extension used by providers that can verify reusable workspaces."""

    def readiness(
        self, provider_id: str, *, workspace: str, revision: str, harness: str
    ) -> HostReadiness: ...

    def admit(
        self, provider_id: str, *, requirements: HostRequirements, acquisition_id: str
    ) -> HostAdmission: ...

    def renew_admission(self, provider_id: str, *, lease_id: str) -> HostAdmission: ...

    def release_admission(self, provider_id: str, *, lease_id: str) -> None: ...


class PolicyResolver(Protocol):
    def authorize(self, action: str, declaration: HostDeclaration) -> None: ...


class AllowPolicy:
    def authorize(self, action: str, declaration: HostDeclaration) -> None:
        return None
