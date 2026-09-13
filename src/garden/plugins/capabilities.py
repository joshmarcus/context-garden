"""Runtime contracts and resolution for lifecycle and process plugins."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..hosts.provider import HostProvider
from ..runner.base import Runner
from .loading import ActionProvenance, LoadedPlugins
from .registry import PluginError


@runtime_checkable
class RunnerTransport(Protocol):
    """The process boundary delegated by core; scheduling and fencing stay outside it."""

    name: str
    detached: bool
    remote: bool

    def start(self, run: Any, worktree: Any, brief_text: str) -> None: ...
    def collect(self, run: Any) -> dict[str, Any]: ...


def provenance_dict(value: ActionProvenance) -> dict[str, str]:
    return asdict(value)


def resolve_host_provider(loaded: LoadedPlugins, reference: str) -> tuple[HostProvider, ActionProvenance]:
    declaration = loaded.registry.capability(reference)
    if declaration.kind != "host_provider":
        raise PluginError(f"capability {reference!r} is {declaration.kind!r}, not 'host_provider'")
    result = loaded.invoke(reference)
    provider = result.value
    required = ("validate_options", "estimate_hourly_usd", "discover", "provision",
                "inspect", "stop", "start", "destroy")
    if any(not callable(getattr(provider, name, None)) for name in required):
        raise PluginError(f"capability {reference!r} does not implement HostProvider")
    return provider, result.provenance


def resolve_runner_transport(loaded: LoadedPlugins, reference: str) -> tuple[RunnerTransport, ActionProvenance]:
    declaration = loaded.registry.capability(reference)
    if declaration.kind != "runner_transport":
        raise PluginError(f"capability {reference!r} is {declaration.kind!r}, not 'runner_transport'")
    result = loaded.invoke(reference)
    transport = result.value
    if not isinstance(transport, RunnerTransport):
        raise PluginError(f"capability {reference!r} does not implement RunnerTransport")
    return transport, result.provenance


class PluginRunner(Runner):
    """Runner-compatible shell which records provenance without delegating core policy."""

    def __init__(self, transport: RunnerTransport, provenance: ActionProvenance,
                 config: dict[str, Any], harness: Any = None):
        super().__init__(config, harness)
        self.transport = transport
        self.provenance = provenance
        self.name = transport.name
        self.detached = transport.detached
        self.remote = transport.remote
        self.capabilities = {"detached": self.detached, "remote": self.remote}

    def _record(self, run: Any) -> None:
        run.env_snapshot["plugin_invocation"] = provenance_dict(self.provenance)
        run.save()

    def start(self, run: Any, worktree: Any, brief_text: str) -> None:
        assigned = Path(run.worktree or worktree).resolve()
        if Path(worktree).resolve() != assigned:
            raise PluginError("runner transport may only start in the run's assigned checkout")
        self._record(run)
        self.transport.start(run, worktree, brief_text)
        if Path(run.worktree or worktree).resolve() != assigned:
            run.worktree = str(assigned)
            run.save()
            raise PluginError("runner transport attempted to widen the assigned checkout")

    def collect(self, run: Any) -> dict[str, Any]:
        self._record(run)
        return self.transport.collect(run)
