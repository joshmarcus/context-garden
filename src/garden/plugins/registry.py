"""Index plugin manifests and resolve capability references without instantiating anything.

The registry is the last place a plugin is only data.  It answers "which plugins are
present, which capabilities do they declare, and is this reference one of them" so that
compatibility and namespace mistakes are diagnosed before a later slice loads code.  It
holds no factories and calls nothing a plugin ships.
"""

from __future__ import annotations

from collections.abc import Iterable

from .. import __version__
from .manifest import (
    SUPPORTED_API_VERSIONS,
    CapabilityDeclaration,
    PluginManifest,
    release,
    split_capability_name,
)


class PluginError(Exception):
    """A plugin diagnostic written for the operator who has to fix it."""


class DuplicatePlugin(PluginError):
    """Two manifests claim the same plugin name."""


class DuplicateCapability(PluginError):
    """Two manifests claim the same namespaced capability."""


class UnknownPlugin(PluginError):
    """A reference names a plugin the registry does not hold."""


class UndeclaredCapability(PluginError):
    """A reference names a capability its plugin's manifest does not declare."""


class IncompatiblePlugin(PluginError):
    """A manifest targets a plugin API version or core range this core cannot satisfy."""


class PluginRegistry:
    """The read-only index of the manifests this core accepts."""

    def __init__(
        self,
        manifests: Iterable[PluginManifest] = (),
        *,
        core_version: str = __version__,
        api_versions: frozenset[str] = SUPPORTED_API_VERSIONS,
    ):
        release(core_version)  # a comparable core version, or say so before indexing
        self.core_version = core_version
        self.api_versions = api_versions
        plugins: dict[str, PluginManifest] = {}
        capabilities: dict[str, CapabilityDeclaration] = {}
        for manifest in manifests:
            self._check_compatible(manifest)
            if manifest.name in plugins:
                raise DuplicatePlugin(
                    f"plugin {manifest.name!r} is provided by both "
                    f"{plugins[manifest.name].identity} and {manifest.identity}; "
                    "uninstall one distribution or rename its plugin"
                )
            plugins[manifest.name] = manifest
            for capability in manifest.capabilities:
                if capability.name in capabilities:
                    owner = capabilities[capability.name].plugin
                    raise DuplicateCapability(
                        f"capability {capability.name!r} is declared by both {owner!r} and "
                        f"{manifest.name!r}"
                    )
                capabilities[capability.name] = capability
        self._plugins = plugins
        self._capabilities = capabilities

    def _check_compatible(self, manifest: PluginManifest) -> None:
        if manifest.api_version not in self.api_versions:
            raise IncompatiblePlugin(
                f"plugin {manifest.name!r} ({manifest.identity}) targets plugin API "
                f"{manifest.api_version!r}; this core supports "
                f"{sorted(self.api_versions)}"
            )
        if not manifest.core_range.contains(self.core_version):
            raise IncompatiblePlugin(
                f"plugin {manifest.name!r} ({manifest.identity}) supports core "
                f"{manifest.core_range}; this core is {self.core_version}"
            )

    @property
    def plugin_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))

    @property
    def capability_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._capabilities))

    def __len__(self) -> int:
        return len(self._plugins)

    def manifest(self, name: str) -> PluginManifest:
        """The manifest for one plugin, or an error naming what is installed."""
        found = self._plugins.get(name)
        if found is None:
            raise UnknownPlugin(
                f"no plugin named {name!r} is registered; "
                f"registered plugins: {list(self.plugin_names)}"
            )
        return found

    def capability(self, reference: str) -> CapabilityDeclaration:
        """Resolve one '<plugin>/<capability>' reference to its declaration."""
        try:
            plugin, _ = split_capability_name(reference)
        except ValueError as exc:
            raise PluginError(str(exc)) from exc
        manifest = self.manifest(plugin)
        found = self._capabilities.get(reference)
        if found is None:
            raise UndeclaredCapability(
                f"plugin {plugin!r} ({manifest.identity}) does not declare capability "
                f"{reference!r}; it declares: "
                f"{[item.name for item in manifest.capabilities]}"
            )
        return found

    def capabilities(self, kind: str | None = None) -> tuple[CapabilityDeclaration, ...]:
        """Every declared capability, optionally narrowed to one kind, in name order."""
        return tuple(
            self._capabilities[name]
            for name in self.capability_names
            if kind is None or self._capabilities[name].kind == kind
        )

    def resolve(self, references: Iterable[str]) -> tuple[CapabilityDeclaration, ...]:
        """Resolve every reference, failing on the first one that cannot be satisfied."""
        return tuple(self.capability(reference) for reference in references)
