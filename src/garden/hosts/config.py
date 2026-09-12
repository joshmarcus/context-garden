"""Strict parsing for the portable declarative host-pool schema."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, TypeVar

from .models import (
    CONTRACT_VERSION,
    WORKER_CONFIGURATION_CONTRACT_VERSION,
    CapabilityGrant,
    EnvironmentProfile,
    PoolDeclaration,
    ProviderLifecycleCapabilities,
    ResourceCeilings,
    WorkerConfiguration,
    WorkerIdentityBinding,
    WorkerInstance,
    WorkerObservation,
)

T = TypeVar("T")


def _construct(cls: type[T], values: dict[str, Any], *, where: str) -> T:
    allowed = {item.name for item in fields(cls)}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"unsupported {where} fields: {unknown}")
    return cls(**values)


def pool_from_dict(value: dict[str, Any]) -> PoolDeclaration:
    """Parse one versioned pool mapping, rejecting misspelled or unnamespaced fields."""
    data = dict(value)
    version = data.pop("contract_version", "")
    if version != CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {CONTRACT_VERSION!r}")
    raw_profile = data.get("profile")
    if not isinstance(raw_profile, dict):
        raise ValueError("profile must be a mapping")
    data["profile"] = _construct(EnvironmentProfile, raw_profile, where="profile")
    return _construct(PoolDeclaration, data, where="pool")


def _tuple_of_strings(value: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{where} must be a list of non-empty strings")
    return tuple(value)


def worker_configuration_from_dict(name: str, value: dict[str, Any]) -> WorkerConfiguration:
    """Parse one trusted template, rejecting embedded private credential values."""
    if not isinstance(value, dict):
        raise ValueError(f"worker_configurations.{name} must be a mapping")
    data = dict(value)
    data.setdefault("name", name)
    if data["name"] != name:
        raise ValueError(f"worker_configurations.{name}.name must match its mapping key")
    if data.get("contract_version") != WORKER_CONFIGURATION_CONTRACT_VERSION:
        raise ValueError(
            f"worker_configurations.{name}.contract_version must be "
            f"{WORKER_CONFIGURATION_CONTRACT_VERSION!r}"
        )
    for key in ("activities", "projects", "identity_references"):
        data[key] = _tuple_of_strings(data.get(key, []), where=f"worker_configurations.{name}.{key}")
    for reference in data["identity_references"]:
        if "=" in reference or reference.startswith(("http://", "https://")):
            raise ValueError(
                f"worker_configurations.{name}.identity_references must contain opaque references, "
                "not credential values"
            )
    ceilings = data.get("resource_ceilings", {})
    lifecycle = data.get("provider_capabilities", {})
    grants = data.get("grants", [])
    if not isinstance(ceilings, dict) or not isinstance(lifecycle, dict) or not isinstance(grants, list):
        raise ValueError(f"worker_configurations.{name} nested values have invalid types")
    data["resource_ceilings"] = _construct(ResourceCeilings, ceilings, where="resource ceilings")
    data["provider_capabilities"] = _construct(
        ProviderLifecycleCapabilities, lifecycle, where="provider capabilities"
    )
    data["grants"] = tuple(
        _construct(CapabilityGrant, item, where="capability grant")
        for item in grants if isinstance(item, dict)
    )
    if len(data["grants"]) != len(grants):
        raise ValueError(f"worker_configurations.{name}.grants must contain mappings")
    result = _construct(WorkerConfiguration, data, where="worker configuration")
    if not result.version or result.generation < 1:
        raise ValueError(f"worker_configurations.{name} requires version and a positive generation")
    if any(value < 0 for value in result.resource_ceilings.__dict__.values()):
        raise ValueError(f"worker_configurations.{name}.resource_ceilings cannot be negative")
    return result


def worker_instance_from_dict(value: dict[str, Any]) -> WorkerInstance:
    """Parse one private authenticated installation binding."""
    if not isinstance(value, dict):
        raise ValueError("worker instance must be a mapping")
    data = dict(value)
    observations = data.get("observations", [])
    bindings = data.get("identity_bindings", [])
    if not isinstance(observations, list) or not isinstance(bindings, list):
        raise ValueError("worker instance observations and identity_bindings must be lists")
    data["observations"] = tuple(
        _construct(WorkerObservation, item, where="worker observation")
        for item in observations if isinstance(item, dict)
    )
    if len(data["observations"]) != len(observations):
        raise ValueError("worker instance observations must contain mappings")
    data["identity_bindings"] = tuple(
        _construct(WorkerIdentityBinding, item, where="worker identity binding")
        for item in bindings if isinstance(item, dict)
    )
    if len(data["identity_bindings"]) != len(bindings):
        raise ValueError("worker instance identity_bindings must contain mappings")
    return _construct(WorkerInstance, data, where="worker instance")
