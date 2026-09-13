"""The plugin platform's public data model: manifests, an index, and inert discovery.

Nothing in this package imports, instantiates or runs plugin code.  ``docs/plugin-contract.md``
is the contract these types express.
"""

from __future__ import annotations

from .discovery import ENTRY_POINT_GROUP, DiscoveredEntryPoint, installed_entry_points
from .loading import (
    ActionProvenance,
    InvocationResult,
    LoadedPlugin,
    LoadedPlugins,
    PluginConfigurationError,
    PluginRedactor,
    load_configured_plugins,
)
from .lock import LOCK_NAME, PluginLockStatus, inspect_lock, locked_identity, write_lock
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
from .resources import IncludedResource, PluginResources, apply_profile, profile_files

__all__ = [
    "API_VERSION",
    "ActionProvenance",
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
    "IncludedResource",
    "InvocationResult",
    "LOCK_NAME",
    "LoadedPlugin",
    "LoadedPlugins",
    "PluginError",
    "PluginConfigurationError",
    "PluginManifest",
    "PluginLockStatus",
    "PluginRegistry",
    "PluginResources",
    "PluginRedactor",
    "ResourceDeclaration",
    "UndeclaredCapability",
    "UnknownPlugin",
    "installed_entry_points",
    "inspect_lock",
    "load_configured_plugins",
    "locked_identity",
    "manifest_from_dict",
    "release",
    "split_capability_name",
    "write_lock",
    "apply_profile",
    "profile_files",
]
