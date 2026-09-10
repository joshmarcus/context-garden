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
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxError(RuntimeError):
    """The configured isolation policy cannot be enforced on this host or executor."""


_DESTINATION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?$")
_CAPABILITY_FLAG = "--garden-sandbox-capabilities"
_POLICY_FLAG = "--garden-sandbox-policy"
_REQUIRED_CAPABILITIES = frozenset({
    "filesystem.readable-roots", "filesystem.writable-roots", "filesystem.protected-roots",
    "filesystem.resolve-symlinks", "network.destination-allowlist", "process.descendants",
})


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
            native = "claude-native"
        if harness == "codex":
            native = "codex-native"
        if harness not in {"claude", "codex"}:
            raise SandboxError(f"harness {harness!r} does not declare an enforceable sandbox capability")
        # The native sandbox is defence in depth. The OS wrapper is the common contract that
        # also confines reads, descendants, symlink resolution, and destination-level network.
        _, mechanism = self._command_prefix()
        return f"{mechanism}+{native}"

    def _command_prefix(self) -> tuple[list[str], str]:
        if not self.command:
            raise SandboxError(
                "sandbox.required execution needs sandbox.command; configure a platform "
                "sandbox wrapper (run garden in WSL on Windows)"
            )
        binary = self.command[0]
        if not (Path(binary).is_file() if os.path.isabs(binary) else shutil.which(binary)):
            raise SandboxError(f"sandbox command {binary!r} is not available on this host")
        prefix = list(self.command)
        try:
            probe = subprocess.run(
                [*prefix, _CAPABILITY_FLAG], capture_output=True, text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError(f"sandbox capability probe failed: {exc}") from exc
        try:
            report = json.loads(probe.stdout)
        except json.JSONDecodeError as exc:
            raise SandboxError("sandbox command did not return a valid capability report") from exc
        reported_capabilities = report.get("capabilities") if isinstance(report, dict) else None
        capabilities = ({item for item in reported_capabilities if isinstance(item, str)}
                        if isinstance(reported_capabilities, list) else set())
        mechanism = report.get("mechanism") if isinstance(report, dict) else None
        version = report.get("contract_version") if isinstance(report, dict) else None
        if (probe.returncode != 0 or version != 1
                or not isinstance(mechanism, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", mechanism)
                or not _REQUIRED_CAPABILITIES <= capabilities):
            missing = sorted(_REQUIRED_CAPABILITIES - capabilities)
            detail = f"; missing {', '.join(missing)}" if missing else ""
            raise SandboxError(f"sandbox command does not attest to contract version 1{detail}")
        self._verify_enforcement(prefix)
        return prefix, mechanism

    def _verify_enforcement(self, prefix: list[str]) -> None:
        """Challenge the wrapper's claimed boundary with real hostile operations.

        The challenge is deliberately independent of command text and runs before untrusted
        input. A wrapper must permit an authorized read/write while denying protected access,
        arbitrary outside-allowlist access, a symlink escape, descendant access, and an
        unapproved connection.
        """
        with tempfile.TemporaryDirectory(prefix="garden-sandbox-probe-") as raw:
            root = Path(raw)
            writable = root / "writable"
            readable = root / "readable"
            protected = root / "protected"
            outside = root / "outside"
            for path in (writable, readable, protected, outside):
                path.mkdir()
            (readable / "context").write_text("context")
            (protected / "secret").write_text("secret")
            (outside / "secret").write_text("outside")
            (writable / "escape").symlink_to(protected, target_is_directory=True)
            allowed_listener = socket.socket()
            allowed_listener.bind(("127.0.0.1", 0))
            allowed_listener.listen(1)
            allowed_port = allowed_listener.getsockname()[1]
            denied_listener = socket.socket()
            denied_listener.bind(("127.0.0.1", 0))
            denied_listener.listen(1)
            denied_port = denied_listener.getsockname()[1]
            token = os.urandom(16).hex()
            script = """
import pathlib, socket, subprocess, sys
w, r, p, outside, allowed_port, denied_port, token = (pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]), sys.argv[7])
def denied(fn):
    try: fn()
    except (OSError, PermissionError): return True
    return False
ok = (r / 'context').read_text() == 'context'
(w / 'normal').write_text('ok')
with socket.create_connection(('127.0.0.1', allowed_port), timeout=.25): pass
checks = [
    denied(lambda: (p / 'secret').read_text()),
    denied(lambda: (p / 'changed').write_text('bad')),
    denied(lambda: (outside / 'secret').read_text()),
    denied(lambda: (outside / 'changed').write_text('bad')),
    denied(lambda: (w / 'escape' / 'secret').read_text()),
    subprocess.run([sys.executable, '-c', "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('bad')", str(p / 'child')], capture_output=True).returncode != 0,
    subprocess.run([sys.executable, '-c', "import pathlib,sys; pathlib.Path(sys.argv[1]).read_text()", str(outside / 'secret')], capture_output=True).returncode != 0,
    subprocess.run([sys.executable, '-c', "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('bad')", str(outside / 'child')], capture_output=True).returncode != 0,
    denied(lambda: socket.create_connection(('127.0.0.1', denied_port), timeout=.25)),
]
if ok and all(checks): print(token)
else: raise SystemExit(97)
"""
            policy = {
                "contract_version": 1,
                "writable_roots": [str(writable)],
                "readable_roots": [str(writable), str(readable)],
                "protected_roots": [str(protected)],
                "network_destinations": [f"127.0.0.1:{allowed_port}"],
                "inherit_to_descendants": True,
                "resolve_symlinks": True,
            }
            argv = [*prefix, _POLICY_FLAG, json.dumps(policy, separators=(",", ":")), "--",
                    sys.executable, "-c", script, str(writable), str(readable), str(protected), str(outside),
                    str(allowed_port), str(denied_port), token]
            try:
                result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=5)
            except (OSError, subprocess.SubprocessError) as exc:
                raise SandboxError(f"sandbox enforcement challenge failed: {exc}") from exc
            finally:
                allowed_listener.close()
                denied_listener.close()
            if result.returncode != 0 or result.stdout.strip() != token:
                raise SandboxError(
                    "sandbox command failed the allowlist, protected-root, descendant, symlink, or network "
                    "enforcement challenge"
                )

    def command_argv(self, shell_command: str, writable_root: Path, *,
                     additional_writable_roots: list[Path] | None = None,
                     readable_roots: list[Path] | None = None,
                     protected_roots: list[Path] | None = None) -> tuple[list[str], str]:
        """Wrap an approved command with the configured OS sandbox executable."""
        if not self.required:
            return ["sh", "-c", shell_command], ""
        prefix, mechanism = self._command_prefix()
        writable = str(writable_root.resolve())
        writable_roots = [writable, *[
            str(path.resolve()) for path in (additional_writable_roots or [])
            if str(path.resolve()) != writable
        ]]
        policy = {
            "contract_version": 1,
            "writable_roots": writable_roots,
            "readable_roots": [str(path.resolve()) for path in (readable_roots or [writable_root])],
            "protected_roots": [str(path.resolve()) for path in (protected_roots or [])],
            "network_destinations": list(self.network_destinations),
            "inherit_to_descendants": True,
            "resolve_symlinks": True,
        }
        values = {
            "writable_root": writable,
            "network_destinations": ",".join(self.network_destinations),
        }
        argv = [part.format(**values) for part in prefix]
        return [*argv, _POLICY_FLAG, json.dumps(policy, separators=(",", ":")),
                "--", "sh", "-c", shell_command], mechanism

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
