"""A deliberately small plugin using only the documented public API."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from garden.plugins import (
    DoctorCheckResult,
    JsonLinesCommand,
    ProviderCheckResult,
    manifest_from_dict,
)

VERSION = "0.1.0"
PACKAGE = Path(__file__).parent


def _digest(name: str) -> str:
    return "sha256:" + hashlib.sha256((PACKAGE / "resources" / name).read_bytes()).hexdigest()


def python_check(
    *, revision: str, context: dict[str, Any], plugin_config: dict[str, Any]
) -> ProviderCheckResult:
    """Return deterministic evidence through the trusted Python capability contract."""
    return ProviderCheckResult(
        status="pass",
        observed_revision=revision,
        evidence={"mode": "python", "label": plugin_config["label"]},
    )


def command_check(
    *, revision: str, context: dict[str, Any], plugin_config: dict[str, Any]
) -> ProviderCheckResult:
    """Delegate a check over the public, versioned JSON Lines command boundary."""
    command = JsonLinesCommand(
        [sys.executable, "-m", "context_garden_example.command"],
        capability="check_provider",
        redact=lambda text: text.replace(plugin_config["credentials"]["token"], "[REDACTED]"),
    )
    reply = command.invoke("check", {"revision": revision, "label": context.get("label")})
    return ProviderCheckResult(**reply.result)


def doctor(*, plugin_config: dict[str, Any]) -> DoctorCheckResult:
    return DoctorCheckResult(
        severity="info",
        message="Example tools are ready.",
        remediation="No action is required.",
    )


MANIFEST = manifest_from_dict({
    "name": "example-tools",
    "distribution": "context-garden-example-plugin",
    "distribution_version": VERSION,
    "api_version": "garden.plugins/v1",
    "core_range": {"minimum": "0.4", "below": "0.5"},
    "summary": "Generic checks and safe starter context for plugin authors.",
    "capabilities": [
        {"name": "example-tools/python-check", "kind": "check_provider",
         "entry_point": "context_garden_example:python_check"},
        {"name": "example-tools/command-check", "kind": "check_provider",
         "entry_point": "context_garden_example:command_check",
         "contract_version": "1"},
        {"name": "example-tools/doctor", "kind": "doctor_check",
         "entry_point": "context_garden_example:doctor"},
        {"name": "example-tools/starter-context", "kind": "context_pack",
         "entry_point": "context_garden_example:MANIFEST"},
        {"name": "example-tools/starter-profile", "kind": "init_profile",
         "entry_point": "context_garden_example:MANIFEST"},
    ],
    "config_schema": {
        "label": {"type": "string", "required": True},
        "credentials": {"type": "object", "properties": {
            "token": {"type": "string", "required": True},
        }},
    },
    "redacted_config_keys": ["credentials.token"],
    "resources": [
        {"name": "starter-context", "kind": "context_pack", "version": VERSION,
         "digest": _digest("starter.md"), "products": ["sample-product"],
         "path": "context_garden_example/resources/starter.md", "audience": ["worker"],
         "media_type": "text/markdown", "size_limit": 4096, "public_safe": True,
         "package_version": VERSION},
        {"name": "starter-profile", "kind": "init_profile", "version": VERSION,
         "digest": _digest("profile.json"), "products": [],
         "path": "context_garden_example/resources/profile.json", "audience": ["garden-init"],
         "media_type": "application/json", "size_limit": 4096, "public_safe": True,
         "package_version": VERSION},
    ],
})
