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

__all__ = [
    "CONTRACT_VERSION",
    "EnvironmentProfile",
    "HostDeclaration",
    "HostEvent",
    "HostFacts",
    "HostLifecycle",
    "HostPlan",
    "HostState",
    "JsonStateStore",
    "PoolDeclaration",
    "ProviderCapabilities",
    "pool_from_dict",
]
