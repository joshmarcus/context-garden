"""Provider-neutral lifecycle for declaratively managed remote hosts."""

from .command import CommandProvider, CommandResult, CommandTransport
from .config import pool_from_dict
from .core import EnvironmentStop, HostLifecycle, JsonStateStore
from .models import (
    CONTRACT_VERSION,
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
    "ScaleOperation",
    "ScaleStatus",
    "WorkerDrainStore",
    "DirectoryEnrollmentResolver",
    "durable_worker_readiness",
    "pool_from_dict",
    "status_dict",
]
