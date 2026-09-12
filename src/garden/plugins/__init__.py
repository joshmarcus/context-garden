"""The plugin platform's public data model: manifests, an index, and inert discovery.

Nothing in this package imports, instantiates or runs plugin code.  ``docs/plugin-contract.md``
is the contract these types express.
"""

from __future__ import annotations

from .discovery import ENTRY_POINT_GROUP, DiscoveredEntryPoint, installed_entry_points
from .manifest import (
    API_VERSION,
    CAPABILITY_KINDS,
    CORE_ONLY_KINDS,
    SUPPORTED_API_VERSIONS,
    CapabilityDeclaration,
    CoreRange,
    PluginManifest,
    ResourceDeclaration,
    manifest_from_dict,
    release,
    split_capability_name,
)
from .registry import (
    DuplicateCapability,
    DuplicatePlugin,
    IncompatiblePlugin,
    PluginError,
    PluginRegistry,
    UndeclaredCapability,
    UnknownPlugin,
)

__all__ = [
    "API_VERSION",
    "CAPABILITY_KINDS",
    "CORE_ONLY_KINDS",
    "ENTRY_POINT_GROUP",
    "SUPPORTED_API_VERSIONS",
    "CapabilityDeclaration",
    "CoreRange",
    "DiscoveredEntryPoint",
    "DuplicateCapability",
    "DuplicatePlugin",
    "IncompatiblePlugin",
    "PluginError",
    "PluginManifest",
    "PluginRegistry",
    "ResourceDeclaration",
    "UndeclaredCapability",
    "UnknownPlugin",
    "installed_entry_points",
    "manifest_from_dict",
    "release",
    "split_capability_name",
]
