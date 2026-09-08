"""Versioned, portable host lifecycle data contracts.

These types intentionally contain no garden task or scheduler concepts.  A worker pool and
a workplace development-host service can therefore use the same lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

CONTRACT_VERSION = "garden.hosts/v1"


class HostState(StrEnum):
    PROVISIONING = "provisioning"
    BOOTSTRAPPING = "bootstrapping"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    STOPPED = "stopped"
    TERMINATED = "terminated"


@dataclass(frozen=True)
class ProviderCapabilities:
    stop_start: bool = False
    persistent_disks: bool = False
    spot: bool = False


@dataclass(frozen=True)
class EnvironmentProfile:
    name: str
    version: str
    image: str
    bootstrap_version: str
    cpu: int
    memory_mib: int
    disk_gib: int
    endpoint: str = ""
    enrollment_secret_ref: str = ""
    persistent_workspace: bool = False
    health_path: str = "/health"
    provider_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PoolDeclaration:
    name: str
    owner: str
    purpose: str
    provider: str
    profile: EnvironmentProfile
    enabled: bool = False
    minimum: int = 0
    maximum: int = 1
    desired: int = 0
    idle_timeout_minutes: int = 10
    maximum_age_minutes: int = 24 * 60
    estimated_runtime_hours: float = 1.0
    spend_limit_usd: float = 10.0
    provider_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HostDeclaration:
    host_id: str
    operation_id: str
    pool: PoolDeclaration


@dataclass(frozen=True)
class HostFacts:
    host_id: str
    provider_id: str
    operation_id: str
    state: HostState
    image: str
    bootstrap_version: str
    retained_resources: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class HostReadiness:
    """Read-only evidence required before a host may run a process."""

    workspace: bool
    revision: bool
    provisioned: bool
    harness_login: bool
    smoke_probe: bool
    detail: str = ""

    @property
    def ready(self) -> bool:
        return all(
            (self.workspace, self.revision, self.provisioned, self.harness_login, self.smoke_probe)
        )


@dataclass(frozen=True)
class HostPlan:
    pool: str
    enabled: bool
    current: int
    desired: int
    create: int
    retire: tuple[str, ...]
    estimated_hourly_usd: float
    estimated_accrued_usd: float
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class HostEvent:
    kind: str
    host_id: str
    state: HostState
    detail: str = ""
