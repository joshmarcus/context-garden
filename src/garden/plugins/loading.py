"""Explicit plugin activation, configuration validation, redaction, and invocation."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Generic, TypeVar

from .discovery import ENTRY_POINT_GROUP
from .manifest import PluginManifest, manifest_from_dict
from .registry import PluginError, PluginRegistry

T = TypeVar("T")


class PluginConfigurationError(PluginError):
    """Configured plugin selection or plugin-owned configuration is invalid."""


@dataclass(frozen=True)
class ActionProvenance:
    plugin_name: str
    distribution_version: str
    api_version: str
    capability_name: str
    configuration_digest: str


@dataclass(frozen=True)
class InvocationResult(Generic[T]):
    value: T
    provenance: ActionProvenance


class PluginRedactor:
    """Scrub manifest-declared values from either structured or textual output."""

    def __init__(self, secrets: tuple[Any, ...]):
        self._secrets = tuple(
            value for value in secrets
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value)
        )

    def text(self, value: str) -> str:
        result = value
        for secret in sorted((str(item) for item in self._secrets), key=len, reverse=True):
            result = result.replace(secret, "<redacted>")
        return result

    def data(self, value: T) -> T:
        if isinstance(value, str):
            return self.text(value)  # type: ignore[return-value]
        if any(value == secret for secret in self._secrets):
            return "<redacted>"  # type: ignore[return-value]
        if isinstance(value, Mapping):
            return {key: self.data(item) for key, item in value.items()}  # type: ignore[return-value]
        if isinstance(value, list):
            return [self.data(item) for item in value]  # type: ignore[return-value]
        if isinstance(value, tuple):
            return tuple(self.data(item) for item in value)  # type: ignore[return-value]
        return deepcopy(value)


@dataclass(frozen=True)
class LoadedPlugin:
    manifest: PluginManifest
    _configuration_json: str
    redactor: PluginRedactor
    configuration_digest: str

    @property
    def config(self) -> Mapping[str, Any]:
        """Return a defensive copy of the configuration bound to the digest."""
        value = json.loads(self._configuration_json)
        assert isinstance(value, dict)
        return value


class LoadedPlugins:
    """Validated enabled plugins; capability code is imported only by ``invoke``."""

    def __init__(self, plugins: tuple[LoadedPlugin, ...]):
        self._plugins = {plugin.manifest.name: plugin for plugin in plugins}
        self.registry = PluginRegistry(plugin.manifest for plugin in plugins)

    @property
    def plugin_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))

    def plugin(self, name: str) -> LoadedPlugin:
        self.registry.manifest(name)
        return self._plugins[name]

    def redact_data(self, value: T) -> T:
        for plugin in self._plugins.values():
            value = plugin.redactor.data(value)
        return value

    def redact_text(self, value: str) -> str:
        for plugin in self._plugins.values():
            value = plugin.redactor.text(value)
        return value

    def invoke(self, capability_name: str, *args: Any, **kwargs: Any) -> InvocationResult[Any]:
        declaration = self.registry.capability(capability_name)
        plugin = self._plugins[declaration.plugin]
        target = _load_object(declaration.entry_point)
        if not callable(target):
            raise PluginError(f"capability {capability_name!r} entry point is not callable")
        provenance = ActionProvenance(
            plugin_name=plugin.manifest.name,
            distribution_version=plugin.manifest.distribution_version,
            api_version=plugin.manifest.api_version,
            capability_name=declaration.name,
            configuration_digest=plugin.configuration_digest,
        )
        value = target(*args, plugin_config=plugin.config, **kwargs)
        return InvocationResult(value=value, provenance=provenance)


def load_configured_plugins(configured: Any) -> LoadedPlugins:
    """Load manifests for explicitly configured installed plugins, then validate all config."""
    if configured is None:
        return LoadedPlugins(())
    if not isinstance(configured, Mapping):
        raise PluginConfigurationError("plugins must be a mapping of plugin name to configuration")

    installed = _installed_by_name()
    selected: list[tuple[PluginManifest, Mapping[str, Any]]] = []
    for name, raw in configured.items():
        path = f"plugins.{name}"
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            raise PluginConfigurationError(f"{path} must be a mapping")
        unknown = sorted(set(raw) - {"distribution", "version", "plugin_config"})
        if unknown:
            raise PluginConfigurationError(f"{path} contains unknown settings {unknown}")
        distribution = raw.get("distribution")
        version = raw.get("version")
        if not isinstance(distribution, str) or not distribution:
            raise PluginConfigurationError(f"{path}.distribution is required")
        if not isinstance(version, str) or not version:
            raise PluginConfigurationError(f"{path}.version is required")
        matches = installed.get(name, ())
        if not matches:
            raise PluginConfigurationError(
                f"{path}: plugin is not installed; install {distribution}=={version}"
            )
        match = next((entry for entry in matches if _dist_name(entry) == distribution), None)
        if match is None:
            found = sorted({_dist_name(entry) for entry in matches})
            raise PluginConfigurationError(
                f"{path}.distribution expected {distribution!r}; installed providers: {found}"
            )
        installed_version = _dist_version(match)
        if installed_version != version:
            raise PluginConfigurationError(
                f"{path}.version requires {distribution}=={version}, but {installed_version} is installed"
            )
        try:
            value = match.load()
            manifest = value if isinstance(value, PluginManifest) else manifest_from_dict(value)
        except Exception as exc:
            raise PluginConfigurationError(
                f"{path}: could not load manifest from installed {distribution}=={version}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if manifest.name != name or manifest.distribution != distribution or manifest.distribution_version != version:
            raise PluginConfigurationError(
                f"{path}: loaded manifest identifies {manifest.name!r} from {manifest.identity}, "
                f"expected {name!r} from {distribution}=={version}"
            )
        plugin_config = raw.get("plugin_config", {})
        if not isinstance(plugin_config, Mapping):
            raise PluginConfigurationError(f"{path}.plugin_config must be a mapping")
        selected.append((manifest, plugin_config))

    # Index every manifest before importing any capability implementation.
    registry = PluginRegistry(manifest for manifest, _ in selected)
    loaded: list[LoadedPlugin] = []
    for manifest, plugin_config in selected:
        prefix = f"plugins.{manifest.name}.plugin_config"
        secrets = _secret_values(plugin_config, manifest.redacted_config_keys)
        redactor = PluginRedactor(secrets)
        try:
            validated = _validate_object(plugin_config, manifest.config_schema, prefix)
        except (TypeError, ValueError) as exc:
            raise PluginConfigurationError(redactor.text(str(exc))) from exc
        configuration_json = json.dumps(
            validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        digest = "sha256:" + hashlib.sha256(configuration_json.encode()).hexdigest()
        loaded.append(LoadedPlugin(manifest, configuration_json, redactor, digest))
    assert len(registry) == len(loaded)
    return LoadedPlugins(tuple(loaded))


def _installed_by_name() -> dict[str, tuple[metadata.EntryPoint, ...]]:
    result: dict[str, list[metadata.EntryPoint]] = {}
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        result.setdefault(entry.name, []).append(entry)
    return {name: tuple(entries) for name, entries in result.items()}


def _dist_name(entry: metadata.EntryPoint) -> str:
    dist = getattr(entry, "dist", None)
    return "" if dist is None else str(dist.name or "")


def _dist_version(entry: metadata.EntryPoint) -> str:
    dist = getattr(entry, "dist", None)
    return "" if dist is None else str(dist.version or "")


def _load_object(reference: str) -> Any:
    module, _, attribute = reference.partition(":")
    value: Any = importlib.import_module(module)
    for part in attribute.split("."):
        value = getattr(value, part)
    return value


def _secret_values(config: Mapping[str, Any], paths: tuple[str, ...]) -> tuple[Any, ...]:
    values: list[Any] = []
    for path in paths:
        current: Any = config
        for segment in path.split("."):
            if not isinstance(current, Mapping) or segment not in current:
                break
            current = current[segment]
        else:
            values.append(current)
    return tuple(values)


def _validate_object(value: Mapping[str, Any], schema: Mapping[str, Any], path: str) -> dict[str, Any]:
    unknown = sorted(set(value) - set(schema))
    if unknown:
        raise ValueError(f"{path} contains unknown keys {unknown}")
    result: dict[str, Any] = {}
    for key, declaration in schema.items():
        item_path = f"{path}.{key}"
        if not isinstance(declaration, Mapping):
            raise ValueError(f"manifest schema at {item_path} must be a mapping")
        if declaration.get("required") is True and key not in value:
            raise ValueError(f"{item_path} is required")
        if key in value:
            result[key] = _validate_value(value[key], declaration, item_path)
    return result


def _validate_value(value: Any, declaration: Mapping[str, Any], path: str) -> Any:
    kind = declaration.get("type")
    expected: dict[str, type[Any] | tuple[type[Any], ...]] = {
        "string": str, "boolean": bool, "integer": int, "number": (int, float),
        "array": list, "object": Mapping,
    }
    if kind not in expected:
        raise ValueError(f"manifest schema at {path}.type is unsupported: {kind!r}")
    if isinstance(value, bool) and kind in {"integer", "number"} or not isinstance(value, expected[kind]):
        raise ValueError(f"{path} must be {kind}; got {type(value).__name__}")
    if "enum" in declaration and value not in declaration["enum"]:
        raise ValueError(f"{path} must be one of {list(declaration['enum'])!r}")
    if kind == "object":
        properties = declaration.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError(f"manifest schema at {path}.properties must be a mapping")
        return _validate_object(value, properties, path)
    if kind == "array":
        items = declaration.get("items")
        if items is None:
            return deepcopy(value)
        if not isinstance(items, Mapping):
            raise ValueError(f"manifest schema at {path}.items must be a mapping")
        return [_validate_value(item, items, f"{path}[{index}]") for index, item in enumerate(value)]
    return deepcopy(value)
