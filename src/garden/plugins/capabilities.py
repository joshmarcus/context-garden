"""Runtime contracts and resolution for registered plugin capabilities.

Resolvers in this module deliberately return core-owned facades and normalized values.
Plugin implementations perform system-specific work; they do not receive scheduler policy or
gain methods for the human-only actions which remain outside the plugin contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..hosts.provider import HostProvider
from ..runner.base import Runner
from ..source_control import UnsupportedOperation
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


CHECK_STATUSES = frozenset({"pass", "fail", "pending", "error", "unavailable"})
DOCTOR_SEVERITIES = frozenset({"info", "warning", "error"})
DOCTOR_DISPATCH_IMPACTS = frozenset({"none", "warning", "block"})


@dataclass(frozen=True)
class ProviderCheckResult:
    """Provider evidence before core applies an exact-revision validation gate."""

    status: str
    observed_revision: str
    evidence: Mapping[str, Any]
    unavailable_reason: str = ""
    failure_reason: str = ""
    provenance: ActionProvenance | None = None

    def __post_init__(self) -> None:
        if self.status not in CHECK_STATUSES:
            raise ValueError(f"unsupported check status {self.status!r}")
        if not isinstance(self.observed_revision, str):
            raise TypeError("observed_revision must be a string")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("check evidence must be a mapping")
        if self.status == "unavailable" and not self.unavailable_reason:
            raise ValueError("unavailable check result requires unavailable_reason")
        if self.status in {"fail", "error"} and not self.failure_reason:
            raise ValueError(f"{self.status} check result requires failure_reason")


@dataclass(frozen=True)
class DoctorCheckResult:
    """One diagnostic observation; only core aggregates its dispatch impact."""

    severity: str
    message: str
    remediation: str = ""
    dispatch_impact: str = "none"
    provenance: ActionProvenance | None = None

    def __post_init__(self) -> None:
        if self.severity not in DOCTOR_SEVERITIES:
            raise ValueError(f"unsupported doctor severity {self.severity!r}")
        if not self.message:
            raise ValueError("doctor result requires a message")
        if self.dispatch_impact not in DOCTOR_DISPATCH_IMPACTS:
            raise ValueError(f"unsupported doctor dispatch impact {self.dispatch_impact!r}")


@dataclass(frozen=True)
class DoctorReport:
    results: tuple[DoctorCheckResult, ...]

    @property
    def admission_allowed(self) -> bool:
        return not any(result.dispatch_impact == "block" for result in self.results)


class PluginSourceControl:
    """Least-privilege facade over a plugin source-control implementation.

    The facade intentionally has no approve, merge, deploy, access-management, or review-
    policy method. Core may create and update change requests after making its own state and
    policy decisions, but a capability cannot be used for a human-only merge.
    """

    def __init__(self, provider: Any, provenance: ActionProvenance, loaded: LoadedPlugins,
                 audit_path: Path | None = None):
        self._provider = provider
        self.provenance = provenance
        self._loaded = loaded
        self._audit_path = audit_path

    def _record(self, operation: str, status: str, error: str = "") -> None:
        if self._audit_path is None:
            return
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        row: dict[str, Any] = {
            "capability": provenance_dict(self.provenance),
            "operation": operation,
            "status": status,
        }
        if error:
            row["error"] = error
        with self._audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")

    @property
    def available(self) -> bool:
        return bool(self._loaded.invoke_callable(
            self.provenance.capability_name, lambda: self._provider.available
        ))

    def describe(self) -> str:
        return str(self._loaded.invoke_callable(
            self.provenance.capability_name, lambda: self._provider.describe()
        ))

    def me(self) -> str:
        return str(self._loaded.invoke_callable(
            self.provenance.capability_name, lambda: self._provider.me()
        ))

    def is_authenticated(self) -> bool:
        return bool(self._loaded.invoke_callable(
            self.provenance.capability_name, lambda: self._provider.is_authenticated()
        ))


def _source_operation(name: str):
    def operation(self: PluginSourceControl, *args: Any, **kwargs: Any) -> Any:
        unsupported = object()

        def invoke() -> Any:
            target = getattr(self._provider, name, None)
            if not callable(target):
                return unsupported
            return target(*args, **kwargs)

        try:
            value = self._loaded.invoke_callable(
                self.provenance.capability_name, invoke
            )
        except Exception as exc:
            # The durable record carries no arguments or provider exception text: either may
            # contain repository secrets. The caller receives the original typed failure.
            self._record(name, "error", type(exc).__name__)
            raise
        if value is unsupported:
            self._record(name, "unsupported")
            raise UnsupportedOperation(f"source-control provider does not support {name}")
        self._record(name, "ok")
        return value

    operation.__name__ = name
    return operation


# These are repository/change-request operations authorized by core. Privileged operations
# such as merge_pr and delete_branch are conspicuously absent from the public facade.
for _operation_name in (
    "repository_from_remote", "change_request_number", "is_safe_change_request_url",
    "find_pr", "find_open_pr", "find_open_pr_by_base", "list_open_prs", "get_pr",
    "create_pr", "feedback_since", "incremental_feedback_since", "complete_feedback",
    "update_pr", "mark_ready", "close_pr", "reopen_pr", "branch_exists",
    "base_ref_deleted", "issue_comments", "comment",
):
    setattr(PluginSourceControl, _operation_name, _source_operation(_operation_name))


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


def resolve_source_control_provider(
    loaded: LoadedPlugins, reference: str, route: Mapping[str, str],
    *, audit_path: Path | None = None,
) -> tuple[PluginSourceControl, ActionProvenance]:
    declaration = loaded.registry.capability(reference)
    if declaration.kind != "source_control_provider":
        raise PluginError(
            f"capability {reference!r} is {declaration.kind!r}, not 'source_control_provider'"
        )
    result = loaded.invoke(reference, route=dict(route))
    required = (
        "available", "describe", "me", "is_authenticated", "repository_from_remote",
        "get_pr", "create_pr", "update_pr", "feedback_since", "comment",
    )
    implements_contract = loaded.invoke_callable(
        reference, lambda: all(hasattr(result.value, name) for name in required)
    )
    if not implements_contract:
        raise PluginError(f"capability {reference!r} does not implement source-control contract")
    provider = PluginSourceControl(result.value, result.provenance, loaded, audit_path)
    return provider, result.provenance


def run_check_provider(
    loaded: LoadedPlugins, reference: str, *, revision: str,
    context: Mapping[str, Any] | None = None,
) -> ProviderCheckResult:
    declaration = loaded.registry.capability(reference)
    if declaration.kind != "check_provider":
        raise PluginError(f"capability {reference!r} is {declaration.kind!r}, not 'check_provider'")
    invoked = loaded.invoke(reference, revision=revision, context=dict(context or {}))
    value = invoked.value
    if isinstance(value, Mapping):
        try:
            value = ProviderCheckResult(**value)
        except (TypeError, ValueError) as exc:
            raise PluginError(f"capability {reference!r} returned malformed check evidence: {exc}") from exc
    if not isinstance(value, ProviderCheckResult) or value.provenance is not None:
        if not isinstance(value, ProviderCheckResult):
            raise PluginError(f"capability {reference!r} returned malformed check evidence")
        raise PluginError(f"capability {reference!r} may not supply its own provenance")
    return replace(value, evidence=loaded.redact_data(dict(value.evidence)),
                   provenance=invoked.provenance)


def run_doctor_checks(loaded: LoadedPlugins) -> DoctorReport:
    results: list[DoctorCheckResult] = []
    for declaration in loaded.registry.capabilities("doctor_check"):
        invoked = loaded.invoke(declaration.name)
        values = invoked.value if isinstance(invoked.value, (list, tuple)) else [invoked.value]
        for value in values:
            if isinstance(value, Mapping):
                try:
                    value = DoctorCheckResult(**value)
                except (TypeError, ValueError) as exc:
                    raise PluginError(
                        f"capability {declaration.name!r} returned malformed doctor result: {exc}"
                    ) from exc
            if not isinstance(value, DoctorCheckResult) or value.provenance is not None:
                if not isinstance(value, DoctorCheckResult):
                    raise PluginError(
                        f"capability {declaration.name!r} returned malformed doctor result"
                    )
                raise PluginError(
                    f"capability {declaration.name!r} may not supply its own provenance"
                )
            results.append(replace(
                value,
                message=loaded.redact_text(value.message),
                remediation=loaded.redact_text(value.remediation),
                provenance=invoked.provenance,
            ))
    return DoctorReport(tuple(results))


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
