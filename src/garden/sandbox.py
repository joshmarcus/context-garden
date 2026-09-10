"""Capability-checked execution sandbox policy.

The policy is deliberately separate from prompts and environment scrubbing: callers must
ask for a concrete enforcement mechanism before starting untrusted code.  Native agent
sandboxes protect harness runs; command checks require an operator-supplied OS wrapper.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxError(RuntimeError):
    """The configured isolation policy cannot be enforced on this host or executor."""


_DESTINATION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?$")


@dataclass(frozen=True)
class SandboxPolicy:
    required: bool
    network_destinations: tuple[str, ...]
    command: tuple[str, ...]

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> SandboxPolicy:
        raw = ((config or {}).get("sandbox") or {})
        if not isinstance(raw, dict):
            raise SandboxError("sandbox must be a mapping")
        destinations = tuple(str(item).strip() for item in (raw.get("network_destinations") or []))
        invalid = [item for item in destinations if not _DESTINATION.fullmatch(item)]
        if invalid:
            raise SandboxError("sandbox.network_destinations must contain host names with optional ports")
        command = raw.get("command") or []
        if isinstance(command, str):
            command = shlex.split(command)
        if not isinstance(command, list):
            raise SandboxError("sandbox.command must be an argv list or shell-like string")
        return cls(bool(raw.get("required", False)), destinations, tuple(str(item) for item in command))

    def native_harness(self, harness: str, permission_mode: str) -> str:
        """Return the native mechanism name or fail before a model process is launched."""
        if not self.required:
            return ""
        if permission_mode in {"bypass", "bypassPermissions", "dangerously-skip-permissions",
                               "dangerously-bypass-approvals-and-sandbox"}:
            raise SandboxError("sandbox.required is incompatible with a bypass permission mode")
        if harness == "claude":
            return "claude-native"
        if harness == "codex":
            if self.network_destinations:
                raise SandboxError(
                    "Codex native sandbox cannot enforce named network destinations; "
                    "configure no destinations or use a supported executor"
                )
            return "codex-native"
        raise SandboxError(f"harness {harness!r} does not declare an enforceable sandbox capability")

    def command_argv(self, shell_command: str, writable_root: Path) -> tuple[list[str], str]:
        """Wrap an approved command with the configured OS sandbox executable."""
        if not self.required:
            return ["sh", "-c", shell_command], ""
        if not self.command:
            raise SandboxError(
                "sandbox.required command execution needs sandbox.command; "
                "configure a platform sandbox wrapper (run garden in WSL on Windows)"
            )
        binary = self.command[0]
        if not (Path(binary).is_file() if os.path.isabs(binary) else shutil.which(binary)):
            raise SandboxError(f"sandbox command {binary!r} is not available on this host")
        values = {
            "writable_root": str(writable_root.resolve()),
            "network_destinations": ",".join(self.network_destinations),
        }
        argv = [part.format(**values) for part in self.command]
        return [*argv, "--", "sh", "-c", shell_command], "configured-os-wrapper"

    @staticmethod
    def report_env(mechanism: str) -> dict[str, str]:
        """Auditable, host-detail-free report inherited by every child process."""
        if not mechanism:
            return {}
        return {
            "GARDEN_SANDBOX_MECHANISM": mechanism,
            "GARDEN_SANDBOX_ENFORCED": "1",
        }

    def claude_settings(self, writable_root: Path | str) -> dict[str, Any]:
        sandbox: dict[str, Any] = {
            "enabled": True,
            "filesystem": {"allowWrite": [str(writable_root), "$TMPDIR"], "denyWrite": ["//"]},
            "network": {"allowedDomains": list(self.network_destinations)},
        }
        return sandbox

    def summary(self, mechanism: str) -> str:
        return json.dumps({"mechanism": mechanism, "network_destinations": len(self.network_destinations)},
                          separators=(",", ":"))
