"""Provider-neutral lifecycle for declaratively managed remote hosts."""

from .command import CommandProvider, CommandResult, CommandTransport
from .config import pool_from_dict, worker_configuration_from_dict, worker_instance_from_dict
from .core import EnvironmentStop, HostLifecycle, JsonStateStore
from .matching import MatchReason, WorkerMatch, match_worker
from .models import (
    CONTRACT_VERSION,
    WORKER_CONFIGURATION_CONTRACT_VERSION,
    WORKER_PROTOCOL_VERSION,
    CapabilityGrant,
    EnvironmentProfile,
    HostAdmission,
    HostDeclaration,
    HostEvent,
    HostFacts,
    HostPlan,
    HostReadiness,
    HostRequirements,
    HostState,
    PoolDeclaration,
    ProviderCapabilities,
    ProviderLifecycleCapabilities,
    ResourceCeilings,
    WorkerConfiguration,
    WorkerConfigurationAdmission,
    WorkerIdentityBinding,
    WorkerInstance,
    WorkerObservation,
    verify_worker_configuration,
)
from .scale import (
    DirectoryEnrollmentResolver,
    Enrollment,
    ScaleOperation,
    ScaleStatus,
    durable_worker_readiness,
    status_dict,
)


def __getattr__(name: str):
    """Load the run-backed drain bridge only when a caller requests it.

    ``garden.runs`` uses ``hosts.locking``.  Importing the bridge while this package is
    initializing would therefore ask for ``RunStore`` before that module has finished
    defining it.
    """
    if name == "WorkerDrainStore":
        from .drain import WorkerDrainStore
        return WorkerDrainStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "CONTRACT_VERSION",
    "EnvironmentProfile",
    "EnvironmentStop",
    "CommandProvider",
    "CommandResult",
    "CommandTransport",
    "Enrollment",
    "HostDeclaration",
    "HostEvent",
    "HostFacts",
    "HostAdmission",
    "HostLifecycle",
    "HostPlan",
    "HostReadiness",
    "HostRequirements",
    "HostState",
    "JsonStateStore",
    "PoolDeclaration",
    "ProviderCapabilities",
    "CapabilityGrant",
    "ProviderLifecycleCapabilities",
    "ResourceCeilings",
    "WorkerConfiguration",
    "WorkerConfigurationAdmission",
    "WorkerInstance",
    "WorkerIdentityBinding",
    "WorkerObservation",
    "WORKER_CONFIGURATION_CONTRACT_VERSION",
    "WORKER_PROTOCOL_VERSION",
    "verify_worker_configuration",
    "MatchReason",
    "WorkerMatch",
    "match_worker",
    "ScaleOperation",
    "ScaleStatus",
    "WorkerDrainStore",
    "DirectoryEnrollmentResolver",
    "durable_worker_readiness",
    "pool_from_dict",
    "worker_configuration_from_dict",
    "worker_instance_from_dict",
    "status_dict",
]
