"""Versioned, portable host lifecycle data contracts.

These types intentionally contain no garden task or scheduler concepts.  A worker pool and
a workplace development-host service can therefore use the same lifecycle.
"""

from __future__ import annotations

import hashlib
import time
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


WORKER_CONFIGURATION_CONTRACT_VERSION = "garden.worker-configuration/v1"
WORKER_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class ResourceCeilings:
    """Maximum resources one instance may reserve; zero means unavailable."""

    memory_mib: int = 0
    vcpu: int = 0
    gpu_count: int = 0
    gpu_device_memory_mib: int = 0


@dataclass(frozen=True)
class ProviderLifecycleCapabilities:
    """Host-management features, deliberately separate from execution authority."""

    stop_start: bool = False
    replace: bool = False
    drain: bool = False


@dataclass(frozen=True)
class CapabilityGrant:
    """An operator-issued capability grant bound to one profile generation."""

    capability: str
    approved_by: str
    approved_at: float
    profile_generation: int
    revoked_at: float = 0

    def active(self, *, generation: int, now: float) -> bool:
        return (
            bool(self.approved_by)
            and self.approved_at <= now
            and self.profile_generation == generation
            and (not self.revoked_at or self.revoked_at > now)
        )


@dataclass(frozen=True)
class WorkerObservation:
    """A worker-reported fact. It can aid matching but never confers authority."""

    capability: str
    observed_at: float
    expires_at: float

    def fresh(self, now: float) -> bool:
        return self.observed_at <= now < self.expires_at


@dataclass(frozen=True)
class WorkerIdentityBinding:
    """Private credential enrollment owned by one user and installation."""

    identity_reference: str
    credential_reference: str
    operating_user: str
    installation_id: str
    enrolled_at: float
    revoked_at: float = 0

    def active(self, now: float) -> bool:
        return self.enrolled_at <= now and (not self.revoked_at or self.revoked_at > now)


@dataclass(frozen=True)
class WorkerConfiguration:
    """Reusable logical worker template; contains references, never credential values."""

    name: str
    version: str
    generation: int
    activities: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    resource_ceilings: ResourceCeilings = field(default_factory=ResourceCeilings)
    identity_references: tuple[str, ...] = ()
    grants: tuple[CapabilityGrant, ...] = ()
    provider_capabilities: ProviderLifecycleCapabilities = field(
        default_factory=ProviderLifecycleCapabilities
    )
    contract_version: str = WORKER_CONFIGURATION_CONTRACT_VERSION

    def granted_capabilities(self, now: float | None = None) -> tuple[str, ...]:
        checked_at = time.time() if now is None else now
        return tuple(sorted({
            grant.capability for grant in self.grants
            if grant.active(generation=self.generation, now=checked_at)
        }))

    def public_dict(self) -> dict[str, Any]:
        """Return shareable metadata with private identity bindings redacted."""
        return {
            "contract_version": self.contract_version,
            "name": self.name,
            "version": self.version,
            "generation": self.generation,
            "activities": list(self.activities),
            "projects": list(self.projects),
            "resource_ceilings": self.resource_ceilings.__dict__,
            "identity_references": ["<redacted>"] * len(self.identity_references),
            "granted_capabilities": list(self.granted_capabilities()),
            "provider_capabilities": self.provider_capabilities.__dict__,
        }


@dataclass(frozen=True)
class WorkerInstance:
    """One authenticated, user-owned installation of a reusable configuration."""

    instance_id: str
    configuration: str
    configuration_version: str
    profile_generation: int
    operating_user: str
    installation_id: str
    authenticated_at: float
    readiness_checked_at: float
    readiness_expires_at: float
    observations: tuple[WorkerObservation, ...] = ()
    identity_bindings: tuple[WorkerIdentityBinding, ...] = ()
    protocol_version: int = WORKER_PROTOCOL_VERSION
    revoked_at: float = 0


@dataclass(frozen=True)
class WorkerConfigurationAdmission:
    eligible: bool
    authoritative_capabilities: tuple[str, ...] = ()
    observed_capabilities: tuple[str, ...] = ()
    detail: str = ""


def verify_worker_configuration(
    configuration: WorkerConfiguration,
    instance: WorkerInstance,
    *,
    activity: str,
    project: str,
    required_capabilities: tuple[str, ...] = (),
    required_memory_mib: int = 0,
    required_vcpu: int = 0,
    required_gpu_count: int = 0,
    required_gpu_device_memory_mib: int = 0,
    required_protocol_version: int = 0,
    now: float | None = None,
) -> WorkerConfigurationAdmission:
    """Validate cached readiness and authority without contacting a provider or data source.

    Version-zero workers remain eligible for unconstrained legacy tasks. Any constraint they
    cannot represent fails closed.
    """
    checked_at = time.time() if now is None else now
    constrained = bool(
        required_capabilities or required_memory_mib or required_vcpu or required_gpu_count
        or required_gpu_device_memory_mib or configuration.activities or configuration.projects
    )
    if configuration.contract_version != WORKER_CONFIGURATION_CONTRACT_VERSION:
        return WorkerConfigurationAdmission(False, detail="unsupported configuration contract")
    if instance.protocol_version < required_protocol_version:
        return WorkerConfigurationAdmission(False, detail="worker protocol does not support required constraints")
    if instance.protocol_version == 0 and constrained:
        return WorkerConfigurationAdmission(False, detail="legacy worker cannot verify required constraints")
    if instance.revoked_at and instance.revoked_at <= checked_at:
        return WorkerConfigurationAdmission(False, detail="worker instance is revoked")
    if not instance.operating_user or not instance.installation_id:
        return WorkerConfigurationAdmission(False, detail="worker instance is not authenticated to a user and installation")
    if instance.authenticated_at > checked_at:
        return WorkerConfigurationAdmission(False, detail="worker authentication is not yet valid")
    if (instance.configuration != configuration.name
            or instance.configuration_version != configuration.version
            or instance.profile_generation != configuration.generation):
        return WorkerConfigurationAdmission(False, detail="worker profile generation does not match")
    if not (instance.readiness_checked_at <= checked_at < instance.readiness_expires_at):
        return WorkerConfigurationAdmission(False, detail="worker readiness is stale")
    active_bindings = {
        binding.identity_reference for binding in instance.identity_bindings
        if binding.operating_user == instance.operating_user
        and binding.installation_id == instance.installation_id
        and binding.credential_reference
        and binding.active(checked_at)
    }
    missing_bindings = sorted(set(configuration.identity_references) - active_bindings)
    if missing_bindings:
        return WorkerConfigurationAdmission(
            False, detail="worker identity binding is missing, revoked, or owned by another installation"
        )
    if configuration.activities and activity not in configuration.activities:
        return WorkerConfigurationAdmission(False, detail="activity is outside worker scope")
    if configuration.projects and project not in configuration.projects:
        return WorkerConfigurationAdmission(False, detail="project is outside worker scope")
    ceilings = configuration.resource_ceilings
    if (required_memory_mib > ceilings.memory_mib or required_vcpu > ceilings.vcpu
            or required_gpu_count > ceilings.gpu_count
            or required_gpu_device_memory_mib > ceilings.gpu_device_memory_mib):
        return WorkerConfigurationAdmission(False, detail="resource requirement exceeds worker ceiling")
    granted = configuration.granted_capabilities(checked_at)
    missing = sorted(set(required_capabilities) - set(granted))
    if missing:
        return WorkerConfigurationAdmission(
            False, granted, detail=f"capabilities lack active operator grants: {', '.join(missing)}"
        )
    observed = tuple(sorted({
        item.capability for item in instance.observations if item.fresh(checked_at)
    }))
    return WorkerConfigurationAdmission(True, granted, observed)


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
    source_head: str = ""


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
    purchase_policy: str = "on_demand"
    on_demand_fallback: bool = False
    recoverable_workspace: bool = False
    provider_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HostDeclaration:
    host_id: str
    operation_id: str
    pool: PoolDeclaration
    deadline_utc: str = ""


def host_operation_id(pool: PoolDeclaration, slot: int, scale_operation_id: str = "") -> str:
    """Bind a slot to its admitted generation; keep the generic legacy identity stable."""
    raw = f"{CONTRACT_VERSION}\0{pool.owner}\0{pool.name}\0{slot}"
    if scale_operation_id:
        raw += "\0" + scale_operation_id
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


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
class HostRequirements:
    """Portable requirements for one host-local admission decision."""

    activity: str
    host_class: str
    environment: str
    capabilities: tuple[str, ...] = ()
    memory_mib: int = 0
    vcpu: int = 0
    gpu_count: int = 0
    gpu_vendor: str = ""
    gpu_device_memory_mib: int = 0
    gpu_features: tuple[str, ...] = ()
    disk_gib: int = 0
    heavy: bool = False
    effective_requirement_digest: str = ""
    profile_revision: str = ""
    worker_id: str = ""
    run_id: str = ""
    operating_user: str = ""
    installation_id: str = ""
    lease_generation: int = 0
    probe_max_age_seconds: int = 60
    lease_seconds: int = 120


@dataclass(frozen=True)
class HostAdmission:
    """Measured readiness and an authoritative lease issued by the host service."""

    eligible: bool
    measured_at: float
    host_class: str
    environment: str
    capabilities: tuple[str, ...]
    memory_available_mib: int
    disk_free_gib: int
    effective_requirement_digest: str = ""
    profile_revision: str = ""
    worker_id: str = ""
    run_id: str = ""
    activity: str = ""
    operating_user: str = ""
    installation_id: str = ""
    lease_generation: int = 0
    memory_limit_mib: int = 0
    vcpu_limit: int = 0
    gpu_devices: tuple[str, ...] = ()
    gpu_device_memory_mib: tuple[int, ...] = ()
    gpu_device_vendors: tuple[str, ...] = ()
    gpu_device_features: tuple[tuple[str, ...], ...] = ()
    resources_enforced: bool = False
    lease_id: str = ""
    lease_expires_at: float = 0
    detail: str = ""


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
