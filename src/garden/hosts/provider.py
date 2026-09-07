"""Interfaces injected into the provider-neutral host lifecycle."""

from __future__ import annotations

from typing import Any, Protocol

from .models import HostDeclaration, HostFacts, ProviderCapabilities


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


class PolicyResolver(Protocol):
    def authorize(self, action: str, declaration: HostDeclaration) -> None: ...


class AllowPolicy:
    def authorize(self, action: str, declaration: HostDeclaration) -> None:
        return None
