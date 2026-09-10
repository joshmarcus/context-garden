"""Keep physical connection targets in local configuration and out of shared text."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_ -]?key|token|secret|password|credential)\b\s*[:=]\s*)([^\s,;]+)"
)
_AUTHORIZATION_ASSIGNMENT = re.compile(
    r"(?im)(\bauthorization\b\s*[:=]\s*)[^\r\n,;]+"
)
_CREDENTIAL_URL = re.compile(r"(\w+://)[^\s/@:]+:[^\s/@]+@")


def connection_aliases(config: dict[str, Any]) -> dict[str, str]:
    """Map configured SSH targets to the logical names recorded by garden.

    The target is deliberately consulted only while starting SSH. Every durable or shared
    surface uses the host entry's ``name`` instead.
    """
    ssh = config.get("ssh") if isinstance(config.get("ssh"), dict) else {}
    aliases: dict[str, str] = {}
    for entry in ssh.get("hosts") or []:
        if not isinstance(entry, dict):
            continue
        name, target = str(entry.get("name") or ""), str(entry.get("host") or "")
        if name and target and name != target:
            aliases[target] = name
    return aliases


def scrub_shared_text(value: str, config: dict[str, Any]) -> str:
    """Replace configured connection targets and credential values in shared text."""
    for target, alias in sorted(connection_aliases(config).items(), key=lambda item: len(item[0]), reverse=True):
        value = value.replace(target, alias)
    value = _CREDENTIAL_URL.sub(r"\1<redacted>@", value)
    value = _AUTHORIZATION_ASSIGNMENT.sub(r"\1<redacted>", value)
    return _SENSITIVE_ASSIGNMENT.sub(r"\1<redacted>", value)


def tracked_connection_target_fields(root: Path) -> list[str]:
    """Return tracked SSH host fields that resolve a logical name to a physical target.

    A bare logical alias (``host: build-1`` matching ``name: build-1``) is allowed. The
    local, ignored overlay is not inspected: it is the intentional boundary where a real
    DNS name, address, account prefix, or port belongs.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False
        )
    # Doctor remains useful in constrained environments (and callers may replace process
    # execution with a narrow probe); an unavailable index simply means there is no tracked
    # configuration audit to perform.
    except Exception:  # noqa: BLE001 - diagnostic audit must not stop doctor
        return []
    if result.returncode:
        return []
    fields: list[str] = []
    for raw in result.stdout.decode("utf-8", "replace").split("\0"):
        path = Path(raw)
        if not raw or not _is_config_path(path):
            continue
        try:
            data = yaml.safe_load((root / path).read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        ssh = data.get("ssh")
        hosts = ssh.get("hosts") if isinstance(ssh, dict) else []
        if not isinstance(hosts, list):
            continue
        for index, entry in enumerate(hosts):
            if not isinstance(entry, dict):
                continue
            name, target = str(entry.get("name") or ""), str(entry.get("host") or "")
            if target and _is_connection_target(target, name):
                fields.append(f"{path}: ssh.hosts[{index}].host")
    return fields


def _is_config_path(path: Path) -> bool:
    name = path.name
    return (name == "garden.yaml" or name == "garden.yml" or name.startswith("garden.")) and path.suffix in {".yaml", ".yml"}


def _is_connection_target(value: str, alias: str) -> bool:
    if value == alias and _logical_alias(value):
        return False
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        pass
    return bool(re.search(r"[@/:.]", value)) or not _logical_alias(value)


def _logical_alias(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value))
