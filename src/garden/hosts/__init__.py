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

__all__ = [
    "CONTRACT_VERSION",
    "EnvironmentProfile",
    "EnvironmentStop",
    "CommandProvider",
    "CommandResult",
    "CommandTransport",
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
    "pool_from_dict",
]
