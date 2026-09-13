"""Continuously maintain the configured healthy worker count.

The durable scale operation already models admission, provisioning, readiness, replacement,
spend and deadlines.  This module is only the recurring driver: it resumes that operation on
a bounded cadence, keeps the dynamic worker registry's dispatch fence in step with observed
host state, and records one local projection the read-only surfaces can show.

Nothing here widens an admission.  A configured count larger than the admitted maximum, a
changed declaration, or a pool that was never admitted stops with one concrete operator
action instead of provisioning anything.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .configuration import worker_pool_settings
from .hosts import HostState, ScaleStatus, pool_from_dict
from .hosts.drain import WorkerDrainStore
from .hosts.factory import operation_path_for, scale_operation
from .hosts.models import PoolDeclaration

# Hosts that must not receive new work.  A pending host is deliberately absent: readiness
# depends on a completed run, so fencing a bootstrapping host would prevent it ever
# becoming healthy.
UNDISPATCHABLE = {HostState.DRAINING, HostState.INTERRUPTED, HostState.FAILED,
                  HostState.STOPPED, HostState.TERMINATED}
CLOSING_PHASES = {"cleaning", "cleaned"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class FleetSettings:
    """The versioned `workers.pool` contract, resolved against one garden directory."""

    declaration_path: Path
    state_path: Path | None
    desired: int
    enrollment_dir: Path | None
    enrollment_config: Path | None
    interval_seconds: int
    backoff_seconds: int
    backoff_ceiling_seconds: int
    failure_threshold: int
    garden_dir: Path

    def declaration(self) -> PoolDeclaration:
        """Parse the referenced admitted pool declaration strictly."""
        return pool_from_dict(json.loads(self.declaration_path.read_text()))

    def operation_path(self, pool: PoolDeclaration) -> Path:
        """The durable operation file this pool's admission lives in.

        The default is exactly the file `garden hosts scale` writes, which is named for the
        pool, not for the declaration file: a declaration kept in `fleet-pool.json` still
        maintains the `workers` pool's own operation.  `workers.pool.state` names a
        different file, for an admission deliberately kept outside the convention.
        """
        return self.state_path or operation_path_for(pool.name, self.garden_dir)


def fleet_settings(config: Any) -> FleetSettings | None:
    """Resolve the optional recurring pool contract, or ``None`` for a static garden."""
    declared = config.get("workers.pool")
    if declared is None:
        return None
    block = worker_pool_settings({"workers": {"pool": declared}})
    assert block is not None
    garden_dir = Path(config.garden_dir)
    root = Path(getattr(config, "root", garden_dir.parent))
    declaration = (root / block["declaration"]).resolve()
    state = block["state"]
    return FleetSettings(
        declaration_path=declaration,
        state_path=(root / state).resolve() if state else None,
        desired=int(block["desired"]),
        enrollment_dir=(root / block["enrollment_dir"]).resolve()
        if block["enrollment_dir"] else None,
        enrollment_config=(root / block["enrollment_config"]).resolve()
        if block["enrollment_config"] else None,
        interval_seconds=int(block["interval_seconds"]),
        backoff_seconds=int(block["backoff_seconds"]),
        backoff_ceiling_seconds=int(block["backoff_ceiling_seconds"]),
        failure_threshold=int(block["failure_threshold"]),
        garden_dir=garden_dir,
    )


class FleetState:
    """The controller's durable reconciliation record.

    Backoff, the breaker and the attempt count belong to one admitted generation, so a
    scheduler restart or a transport loss resumes them rather than resetting them.
    """

    def __init__(self, garden_dir: Path):
        self.path = garden_dir / "fleet.json"

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)


class FleetController:
    """Resume one admitted pool toward its configured healthy count."""

    def __init__(self, config: Any, *, providers: dict[str, Any] | None = None,
                 enrollments: Any = None, health_check: Any = None,
                 interruption_drain: Any = None, now=_now):
        self.config = config
        self.providers = providers
        self.enrollments = enrollments
        self.health_check = health_check
        self.interruption_drain = interruption_drain
        self.now = now
        self.settings = fleet_settings(config)
        self.state = FleetState(Path(config.garden_dir))

    # -- convergence ------------------------------------------------------------------

    def converge(self, *, force: bool = False) -> dict[str, Any]:
        """Take at most one reconciliation step and return the durable projection.

        ``force`` runs the step even when the cadence or the backoff timer says to wait; a
        tripped breaker still refuses, because only an operator clears it.
        """
        if self.settings is None:
            return {"configured": False}
        record = self._record()
        now = self.now()
        if record.get("breaker") and not record.get("action_required"):
            record["action_required"] = record.get("breaker_detail") or "clear the fleet breaker"
        if record.get("breaker"):
            self.state.write(record)
            return record
        if not force and record.get("next_attempt_at") \
                and _parse(record["next_attempt_at"]) and _parse(record["next_attempt_at"]) > now:
            return record
        record["last_attempt_at"] = now.isoformat()
        try:
            status = self._step(record)
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            # Provider text can be long, but it is the operator's only clue; the durable
            # operation keeps its own progress, so the failure is safe to retry.
            self._failed(record, f"{type(exc).__name__}: {exc}", now)
        except Exception as exc:
            # A provider exception may carry credential-bearing request state; keep the
            # type only.
            self._failed(record, f"provider call failed ({type(exc).__name__})", now)
        else:
            if status is not None:
                self._observed(record, status, now)
        self.state.write(record)
        return record

    def resume(self) -> dict[str, Any]:
        """Clear a tripped breaker and its required action at an operator's request."""
        record = self._record()
        record.update(breaker=False, breaker_detail="", attempts=0, next_attempt_at="",
                      action_required="")
        self.state.write(record)
        return record

    def _step(self, record: dict[str, Any]) -> ScaleStatus | None:
        """Resume the admitted operation once, or record the operator action that blocks it."""
        settings = self.settings
        assert settings is not None
        if not settings.declaration_path.exists():
            record["action_required"] = (
                f"add the admitted pool declaration at {settings.declaration_path.name}")
            record["last_outcome"] = "declaration missing"
            return None
        pool = settings.declaration()
        record["pool"] = pool.name
        operation_path = settings.operation_path(pool)
        if not operation_path.exists():
            record["action_required"] = (
                f"admit this pool first: garden hosts scale {settings.declaration_path.name} "
                "--deadline <absolute ISO-8601 UTC>")
            record["last_outcome"] = "not admitted"
            return None
        operation = scale_operation(
            pool, operation_path, settings.enrollment_dir, settings.enrollment_config,
            garden_dir=settings.garden_dir, providers=self.providers,
            enrollments=self.enrollments, health_check=self.health_check,
            interruption_drain=self.interruption_drain, now=self.now,
        )
        durable = _admitted_declaration(operation_path)
        self._reset_on_new_generation(record, str(durable.get("operation_id") or ""))
        admitted = pool_from_dict(durable["admitted_declaration"])
        # Reconcile against the admitted declaration, never the configured file: an edited
        # profile, image or identity is a new admission, and the hosts already running
        # belong to the old one.  Existing healthy capacity stays available either way.
        desired, action = _bounded_desired(settings.desired, admitted,
                                          settings.declaration_path.name)
        if asdict(replace(pool, desired=admitted.desired)) != asdict(admitted):
            action = (f"{settings.declaration_path.name} no longer matches the admitted "
                      "declaration; maintaining the admitted one. Admit the change: "
                      f"garden hosts scale {settings.declaration_path.name} "
                      "--deadline <absolute ISO-8601 UTC>")
        record["action_required"] = action
        status = operation.converge(admitted, desired=desired)
        record["last_outcome"] = (
            f"{status.healthy} healthy, {status.pending} pending, {status.failed} failed "
            f"of {status.desired} desired")
        if status.missing_setup:
            record["action_required"] = record["action_required"] or (
                "complete the private enrollment for " + "; ".join(
                    f"{host} ({', '.join(reasons)})"
                    for host, reasons in sorted(status.missing_setup.items())))
        if desired > 0 and str(status.phase) in CLOSING_PHASES:
            # The operation reached its absolute deadline. Reconciliation cannot extend an
            # admission, so the count stays unmet until an operator admits a new one.
            record["action_required"] = record["action_required"] or (
                "this pool's admission has ended; admit a new scale operation to run workers "
                f"again: garden hosts scale {settings.declaration_path.name} "
                "--deadline <absolute ISO-8601 UTC>")
        self._fence(status, settings)
        return status

    def _reset_on_new_generation(self, record: dict[str, Any], generation: str) -> None:
        if generation and record.get("generation") != generation:
            record.update(generation=generation, attempts=0, next_attempt_at="",
                          breaker=False, breaker_detail="")

    def _failed(self, record: dict[str, Any], detail: str, now: dt.datetime) -> None:
        """Apply bounded exponential backoff, tripping the breaker at the threshold."""
        settings = self.settings
        assert settings is not None
        attempts = int(record.get("attempts") or 0) + 1
        record["attempts"] = attempts
        record["last_outcome"] = detail
        if attempts >= settings.failure_threshold:
            record["breaker"] = True
            record["breaker_detail"] = (
                f"{attempts} consecutive convergence failures ({detail}); fix the image, "
                "credentials, health probe or provider access, then run "
                "`garden hosts fleet --resume`")
            record["action_required"] = record["breaker_detail"]
            record["next_attempt_at"] = ""
            return
        delay = min(settings.backoff_seconds * 2 ** (attempts - 1),
                    settings.backoff_ceiling_seconds)
        record["next_attempt_at"] = (now + dt.timedelta(seconds=delay)).isoformat()

    def _observed(self, record: dict[str, Any], status: ScaleStatus, now: dt.datetime) -> None:
        """Record the reading and either back off or schedule the next ordinary pass.

        Capacity that is still coming up counts toward the target, so an ordinarily slow
        bootstrap is not a setback.  A pass that ends short with nothing pending is: that is
        the shape a broken image, credential, health probe or provider takes, and it is what
        the backoff and the breaker exist to bound.
        """
        settings = self.settings
        assert settings is not None
        record["observed"] = projection_of(status)
        shortfall = status.desired - (status.healthy + status.pending)
        if status.failed or (shortfall > 0 and str(status.phase) not in CLOSING_PHASES):
            self._failed(record, f"{status.failed} host(s) failed or interrupted" if status.failed
                         else f"{shortfall} of {status.desired} host(s) did not become healthy", now)
            return
        record["attempts"] = 0
        record["next_attempt_at"] = (now + dt.timedelta(seconds=settings.interval_seconds)).isoformat()

    def _fence(self, status: ScaleStatus, settings: FleetSettings) -> None:
        """Keep the claim endpoint's drain fence in step with observed host state.

        A newly healthy host becomes dispatchable by having its fence cleared; a retired,
        draining, expired or failed host stops receiving work by keeping it.  Both take
        effect on the next claim, without a controller restart.
        """
        drains = self.interruption_drain
        if drains is None:
            drains = WorkerDrainStore(settings.garden_dir)
        if not hasattr(drains, "request") or not hasattr(drains, "clear"):
            return
        closing = str(status.phase) in CLOSING_PHASES
        for host in status.hosts:
            if closing or host.state in UNDISPATCHABLE:
                drains.request(host, deadline=status.deadline,
                               detail=f"host is {host.state}; not dispatchable")
            elif host.state in {HostState.READY, HostState.BUSY}:
                drains.clear(host.operation_id)

    def _record(self) -> dict[str, Any]:
        record = self.state.read()
        record.setdefault("attempts", 0)
        record.setdefault("breaker", False)
        record.setdefault("action_required", "")
        return record


def _bounded_desired(configured: int, admitted: PoolDeclaration,
                     declaration: str) -> tuple[int, str]:
    """Clamp a configured count to the admission and name the action that lifts the limit."""
    if configured <= admitted.desired:
        return configured, ""
    return admitted.desired, (
        f"configured desired {configured} exceeds the {admitted.desired} host(s) this pool "
        f"admitted; maintaining {admitted.desired}. Admit more capacity: garden hosts scale "
        f"{declaration} --deadline <absolute ISO-8601 UTC>")


def _admitted_declaration(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get("admitted_declaration"), dict):
        raise ValueError("durable scale operation is missing its admitted declaration")
    return value


def projection_of(status: ScaleStatus) -> dict[str, Any]:
    """One stable read-only reading of scale state, free of secrets and provider detail."""
    hosts = [host for host in status.hosts if host.state != HostState.TERMINATED]
    return {
        "operation_id": status.operation_id,
        "phase": status.phase,
        "desired": status.desired,
        "healthy": status.healthy,
        "dispatchable": sum(host.state in {HostState.READY, HostState.BUSY} for host in hosts)
        if str(status.phase) not in CLOSING_PHASES else 0,
        "pending": status.pending,
        "draining": sum(host.state == HostState.DRAINING for host in hosts),
        "failed": status.failed,
        "exact_version": status.exact_version,
        "deadline": status.deadline,
        "estimated_accrued_usd": status.estimated_accrued_usd,
        "spend_limit_usd": status.spend_limit_usd,
        "maximum_hosts": status.maximum_hosts,
        "missing_setup": {host: list(reasons) for host, reasons in status.missing_setup.items()},
        "pending_credential_revocations": list(status.pending_credential_revocations),
        "hosts": [{"host_id": host.host_id, "state": str(host.state), "detail": host.detail}
                  for host in hosts],
    }


def fleet_projection(config: Any) -> dict[str, Any]:
    """Read the controller's last durable reading; never contacts a provider."""
    settings = fleet_settings(config)
    record = FleetState(Path(config.garden_dir)).read()
    if settings is None:
        return {"configured": False}
    observed = dict(record.get("observed") or {})
    return {
        "configured": True,
        "configured_desired": settings.desired,
        "interval_seconds": settings.interval_seconds,
        "generation": str(record.get("generation") or ""),
        "attempts": int(record.get("attempts") or 0),
        "next_retry_at": str(record.get("next_attempt_at") or ""),
        "breaker": bool(record.get("breaker")),
        "action_required": str(record.get("action_required") or ""),
        "last_attempt_at": str(record.get("last_attempt_at") or ""),
        "last_outcome": str(record.get("last_outcome") or ""),
        **observed,
    }


def fleet_summary(config: Any) -> str:
    """One compact line for `garden observe`, or ``""`` for a static garden."""
    reading = fleet_projection(config)
    if not reading.get("configured"):
        return ""
    bits = (f"fleet {reading.get('healthy', 0)}/{reading['configured_desired']} healthy",
            f"{reading.get('dispatchable', 0)} dispatchable",
            f"{reading.get('pending', 0)} pending",
            f"{reading.get('draining', 0)} draining",
            f"{reading.get('failed', 0)} failed")
    line = " · ".join(bits)
    if reading.get("action_required"):
        line += " — action required"
    return line


def fleet_lines(config: Any) -> list[str]:
    """The operator-facing reading for `garden status` and `garden doctor`.

    A line beginning with ``fleet: action required`` is the one thing to do next; callers
    highlight it.  Every value here comes from the controller's last durable pass, so this
    reads no host and no provider.
    """
    reading = fleet_projection(config)
    if not reading.get("configured"):
        return []
    lines = [
        f"fleet: desired {reading['configured_desired']} · healthy {reading.get('healthy', 0)} · "
        f"dispatchable {reading.get('dispatchable', 0)} · pending {reading.get('pending', 0)} · "
        f"draining {reading.get('draining', 0)} · failed {reading.get('failed', 0)}",
        f"fleet: worker version {reading.get('exact_version') or 'unknown'} · "
        f"deadline {reading.get('deadline') or 'unknown'} · estimated cost "
        f"${float(reading.get('estimated_accrued_usd') or 0):.2f} of "
        f"${float(reading.get('spend_limit_usd') or 0):.2f} · "
        + (f"next retry {reading['next_retry_at']}" if reading.get("next_retry_at")
           else "no retry pending")
        + (" · replacement stopped after repeated failures" if reading.get("breaker") else ""),
    ]
    if reading.get("action_required"):
        lines.append(f"fleet: action required — {reading['action_required']}")
    return lines


def _parse(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
