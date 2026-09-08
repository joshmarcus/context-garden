"""Vendor-neutral controller-side command host adapter.

The configured wrapper receives an action as one additional argv item and one JSON object on
stdin.  It prints exactly one JSON object on stdout.  Commands run only on the controller;
the wrapper may use any approved transport, so no host-to-host SSH topology is implied.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from .models import (
    CONTRACT_VERSION,
    HostDeclaration,
    HostFacts,
    HostReadiness,
    HostState,
    ProviderCapabilities,
)
from .provider import ProviderError, ProvisioningUncertain, TransientProviderError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    stdin: bytes
    stdout: bytes
    stderr: bytes
    exit_code: int


class CommandTransport:
    """Bounded byte-faithful subprocess transport."""

    def run(self, argv: list[str], stdin: bytes, *, timeout_seconds: float) -> CommandResult:
        try:
            completed = subprocess.run(
                argv, input=stdin, capture_output=True, timeout=timeout_seconds, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientProviderError(
                f"command timed out after {timeout_seconds:g}s: {argv[0]}"
            ) from exc
        except OSError as exc:
            raise ProviderError(f"cannot execute command {argv[0]!r}: {exc}") from exc
        return CommandResult(
            tuple(argv), stdin, completed.stdout, completed.stderr, completed.returncode
        )


class CommandProvider:
    name = "command"
    contract_version = CONTRACT_VERSION
    capabilities = ProviderCapabilities(stop_start=True, persistent_disks=True)
    ALLOWED_OPTIONS = {"command", "timeout_seconds", "hourly_usd"}

    def __init__(self, transport: CommandTransport | None = None):
        self.transport = transport or CommandTransport()

    def validate_options(self, options: dict[str, Any]) -> None:
        unknown = set(options) - self.ALLOWED_OPTIONS
        if unknown:
            raise ValueError(f"unsupported command options: {sorted(unknown)}")

    @staticmethod
    def _options(declaration: HostDeclaration) -> dict[str, Any]:
        return {**declaration.pool.provider_options, **declaration.pool.profile.provider_options}

    def estimate_hourly_usd(self, declaration: HostDeclaration) -> float:
        return float(self._options(declaration).get("hourly_usd", 0))

    def _invoke(self, action: str, payload: dict[str, Any], options: dict[str, Any]) -> Any:
        command = options.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ValueError("command provider requires command as a nonempty argv list")
        timeout = float(options.get("timeout_seconds", 60))
        if not 0 < timeout <= 3600:
            raise ValueError("command timeout_seconds must be in (0, 3600]")
        stdin = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        result = self.transport.run([*command, action], stdin, timeout_seconds=timeout)
        if result.exit_code != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise ProviderError(
                f"command {action} exited {result.exit_code}" + (f": {detail}" if detail else "")
            )
        try:
            return json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"command {action} returned invalid JSON") from exc

    @staticmethod
    def _facts(value: dict[str, Any]) -> HostFacts:
        try:
            return HostFacts(
                host_id=str(value["host_id"]),
                provider_id=str(value["provider_id"]),
                operation_id=str(value["operation_id"]),
                state=HostState(value["state"]),
                image=str(value["image"]),
                bootstrap_version=str(value["bootstrap_version"]),
                retained_resources=tuple(str(x) for x in value.get("retained_resources", [])),
                detail=str(value.get("detail", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"command returned invalid host facts: {exc}") from exc

    def _declaration_payload(self, declaration: HostDeclaration) -> dict[str, Any]:
        pool = declaration.pool
        return {
            "contract_version": CONTRACT_VERSION,
            "host_id": declaration.host_id,
            "operation_id": declaration.operation_id,
            "owner": pool.owner,
            "pool": pool.name,
            "profile": pool.profile.name,
            "profile_version": pool.profile.version,
            "image": pool.profile.image,
            "bootstrap_version": pool.profile.bootstrap_version,
        }

    def discover(self, owner: str, pool: str) -> list[HostFacts]:
        # Discovery options are provider configuration, supplied once by the embedding app.
        raise ProviderError("command discovery requires a bound pool declaration")

    def bind(self, declaration: HostDeclaration) -> CommandProvider:
        return _BoundCommandProvider(self, declaration)

    def provision(self, declaration: HostDeclaration) -> HostFacts:
        value = self._invoke("acquire", self._declaration_payload(declaration), self._options(declaration))
        if value.get("uncertain"):
            raise ProvisioningUncertain(str(value.get("detail", "acquisition outcome uncertain")))
        facts = self._facts(value)
        if (facts.host_id, facts.operation_id) != (
            declaration.host_id,
            declaration.operation_id,
        ):
            raise ProviderError("command acquire changed the logical host or operation identity")
        return facts

    def inspect(self, provider_id: str) -> HostFacts:
        raise ProviderError("command inspection requires a bound pool declaration")

    def stop(self, provider_id: str) -> HostFacts:
        raise ProviderError("command stop requires a bound pool declaration")

    def start(self, provider_id: str) -> HostFacts:
        raise ProviderError("command start requires a bound pool declaration")

    def destroy(self, provider_id: str, *, delete_storage: bool) -> HostFacts:
        raise ProviderError("command retirement requires a bound pool declaration")


class _BoundCommandProvider(CommandProvider):
    """Pool-bound view satisfying the existing provider contract without global state."""

    def __init__(self, parent: CommandProvider, declaration: HostDeclaration):
        super().__init__(parent.transport)
        self.declaration = declaration
        self.options = parent._options(declaration)

    def discover(self, owner: str, pool: str) -> list[HostFacts]:
        rows = self._invoke(
            "inspect", {"contract_version": CONTRACT_VERSION, "owner": owner, "pool": pool}, self.options
        )
        return [self._facts(row) for row in rows]

    def inspect(self, provider_id: str) -> HostFacts:
        return self._facts(self._invoke("inspect-one", {"provider_id": provider_id}, self.options))

    def _change(self, action: str, provider_id: str, **extra: Any) -> HostFacts:
        return self._facts(self._invoke(action, {"provider_id": provider_id, **extra}, self.options))

    def stop(self, provider_id: str) -> HostFacts:
        return self._change("release", provider_id)

    def start(self, provider_id: str) -> HostFacts:
        return self._change("start", provider_id)

    def destroy(self, provider_id: str, *, delete_storage: bool) -> HostFacts:
        return self._change("retire", provider_id, delete_storage=delete_storage)

    def readiness(
        self, provider_id: str, *, workspace: str, revision: str, harness: str
    ) -> HostReadiness:
        value = self._invoke(
            "ready",
            {
                "provider_id": provider_id,
                "workspace": workspace,
                "revision": revision,
                "harness": harness,
                "read_only": True,
            },
            self.options,
        )
        if not isinstance(value, dict):
            raise ProviderError("command ready returned invalid readiness evidence")
        return HostReadiness(
            workspace=value.get("workspace") is True,
            revision=value.get("revision") is True,
            provisioned=value.get("provisioned") is True,
            harness_login=value.get("harness_login") is True,
            smoke_probe=value.get("smoke_probe") is True,
            detail=str(value.get("detail", "")),
        )
