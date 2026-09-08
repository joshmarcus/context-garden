"""Strict parsing for the portable declarative host-pool schema."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, TypeVar

from .models import CONTRACT_VERSION, EnvironmentProfile, PoolDeclaration

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
