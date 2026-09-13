"""Deterministic, operator-updated compatibility lock for enabled plugins."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from .loading import LoadedPlugins, PluginConfigurationError, load_configured_plugins

LOCK_VERSION = "garden.plugin-lock/v1"
LOCK_NAME = "garden.lock"


@dataclass(frozen=True)
class PluginLockStatus:
    valid: bool
    diagnostics: tuple[str, ...]
    identity: dict[str, Any]

    @property
    def hold_message(self) -> str:
        return "plugin compatibility hold: " + "; ".join(self.diagnostics)


def locked_identity(loaded: LoadedPlugins) -> dict[str, Any]:
    plugins = []
    for name in loaded.plugin_names:
        plugin = loaded.plugin(name)
        manifest = plugin.manifest
        plugins.append({
            "name": manifest.name,
            "distribution": manifest.distribution,
            "distribution_version": manifest.distribution_version,
            "api_version": manifest.api_version,
            "distribution_fingerprint": plugin.distribution_fingerprint,
            "configuration_digest": plugin.configuration_digest,
            "resources": [
                {"name": resource.name, "version": resource.version, "digest": resource.digest,
                 "package_version": getattr(resource, "package_version", "")}
                for resource in sorted(manifest.resources, key=lambda item: item.name)
            ],
        })
    return {"lock_version": LOCK_VERSION, "core_version": __version__, "plugins": plugins}


def read_lock(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        raise PluginConfigurationError(f"{LOCK_NAME} is missing; run `garden plugins lock`") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginConfigurationError(f"cannot read {LOCK_NAME}: {exc}") from exc
    if not isinstance(value, dict) or value.get("lock_version") != LOCK_VERSION:
        raise PluginConfigurationError(
            f"{LOCK_NAME} has an unsupported lock_version; run `garden plugins lock`"
        )
    if not isinstance(value.get("plugins"), list) or not isinstance(value.get("core_version"), str):
        raise PluginConfigurationError(f"{LOCK_NAME} is malformed; run `garden plugins lock`")
    return value


def inspect_lock(root: Path, configured: Any) -> tuple[LoadedPlugins, PluginLockStatus]:
    """Load the observed set and diagnose drift without changing the lock."""
    if not configured:
        empty = load_configured_plugins(None)
        return empty, PluginLockStatus(True, (), locked_identity(empty))
    try:
        loaded = load_configured_plugins(configured)
    except PluginConfigurationError as exc:
        empty = load_configured_plugins(None)
        return empty, PluginLockStatus(False, (str(exc),), {})
    observed = locked_identity(loaded)
    try:
        expected = read_lock(root / LOCK_NAME)
    except PluginConfigurationError as exc:
        status = PluginLockStatus(False, (str(exc),), observed)
        loaded.hold(status.hold_message)
        return loaded, status
    diagnostics = tuple(_drift(expected, observed))
    status = PluginLockStatus(
        not diagnostics, diagnostics, expected if not diagnostics else observed
    )
    if not status.valid:
        loaded.hold(status.hold_message)
    return loaded, status


def _drift(expected: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    if expected.get("core_version") != observed.get("core_version"):
        diagnostics.append(
            f"core version expected {expected.get('core_version')!r}, observed {observed.get('core_version')!r}"
        )
    expected_plugins = {item.get("name"): item for item in expected["plugins"] if isinstance(item, dict)}
    observed_plugins = {item.get("name"): item for item in observed["plugins"]}
    for name in sorted(expected_plugins.keys() - observed_plugins.keys()):
        diagnostics.append(f"plugin {name!r} is missing; expected {expected_plugins[name].get('distribution')}=={expected_plugins[name].get('distribution_version')}")
    for name in sorted(observed_plugins.keys() - expected_plugins.keys()):
        diagnostics.append(f"plugin {name!r} is additional; observed {observed_plugins[name].get('distribution')}=={observed_plugins[name].get('distribution_version')}")
    for name in sorted(expected_plugins.keys() & observed_plugins.keys()):
        expected_plugin, observed_plugin = expected_plugins[name], observed_plugins[name]
        for field in ("distribution", "distribution_version", "api_version", "distribution_fingerprint", "configuration_digest", "resources"):
            if expected_plugin.get(field) != observed_plugin.get(field):
                diagnostics.append(
                    f"plugin {name!r} {field} expected {expected_plugin.get(field)!r}, observed {observed_plugin.get(field)!r}"
                )
    return diagnostics


def write_lock(root: Path, loaded: LoadedPlugins) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path = root / LOCK_NAME
    previous = None
    if path.exists():
        previous = read_lock(path)
    current = locked_identity(loaded)
    payload = json.dumps(current, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{LOCK_NAME}.", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return previous, current
