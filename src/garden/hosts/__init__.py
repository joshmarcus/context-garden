"""Provider-neutral lifecycle for declaratively managed remote hosts."""

from .config import pool_from_dict
from .core import HostLifecycle, JsonStateStore
from .models import (
    CONTRACT_VERSION,
    EnvironmentProfile,
    HostDeclaration,
    HostEvent,
    HostFacts,
    HostPlan,
    HostState,
    PoolDeclaration,
    ProviderCapabilities,
)
from .scale import (
    DirectoryEnrollmentResolver,
    Enrollment,
    ScaleOperation,
    ScaleStatus,
    durable_worker_readiness,
    status_dict,
)

__all__ = [
    "CONTRACT_VERSION",
    "EnvironmentProfile",
    "Enrollment",
    "HostDeclaration",
    "HostEvent",
    "HostFacts",
    "HostLifecycle",
    "HostPlan",
    "HostState",
    "JsonStateStore",
    "PoolDeclaration",
    "ProviderCapabilities",
    "ScaleOperation",
    "ScaleStatus",
    "DirectoryEnrollmentResolver",
    "durable_worker_readiness",
    "pool_from_dict",
    "status_dict",
]
