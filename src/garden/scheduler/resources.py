"""Host-wide admission and pressure sensing for local execution.

Workers, reviews and product checks are different scheduler queues, but on the operator
machine they compete for the same memory, CPU and temporary filesystem.  This module gives
every local launch path one shared gate.  Reaping is never gated, so an overloaded machine
can drain and recover without losing run records or restarting the controller.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..model import now_iso


@dataclass(frozen=True)
class ResourceStatus:
    active: int
    limit: int
    memory_available_mb: int | None
    memory_min_mb: int
    temp_free_mb: int | None
    temp_min_mb: int
    reasons: tuple[str, ...]

    @property
    def pressured(self) -> bool:
        return bool(self.reasons)


class ResourcePressureError(RuntimeError):
    """A launch was deferred because the host, rather than the branch, is constrained."""


def _memory_available_mb(path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _free_mb(path: Path) -> int | None:
    try:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        stat = os.statvfs(probe)
        return stat.f_bavail * stat.f_frsize // (1024 * 1024)
    except OSError:
        return None


class ResourceMixin:
    def resource_parallel_limit(self) -> int:
        configured = self.effective("resources.max_parallel")
        if configured not in (None, ""):
            return max(1, int(configured))
        # Preserve the historic independent worker/review pools unless an operator opts in
        # to a host-wide bound.  The documented local profile sets this explicitly.
        return self.effective_max_parallel() + self.review_parallel_limit()

    def local_runs_active(self) -> list[Any]:
        """Every process launched on this host, regardless of scheduler queue or CLI path."""
        return [r for r in self.active_runs() if r.runner == "local"]

    def resource_status(self) -> ResourceStatus:
        active = len(self.local_runs_active())
        limit = self.resource_parallel_limit()
        memory_min = int(self.effective("resources.min_memory_available_mb", 0) or 0)
        temp_min = int(self.effective("resources.min_temp_free_mb", 0) or 0)
        memory = _memory_available_mb()
        temp = _free_mb(self.cfg.work_dir / "tmp")
        reasons: list[str] = []
        if active >= limit:
            reasons.append(f"local execution limit reached ({active}/{limit})")
        if memory_min and memory is not None and memory < memory_min:
            reasons.append(f"available memory {memory} MiB is below {memory_min} MiB")
        if temp_min and temp is not None and temp < temp_min:
            reasons.append(f"temporary storage {temp} MiB free is below {temp_min} MiB")
        return ResourceStatus(active, limit, memory, memory_min, temp, temp_min, tuple(reasons))

    def _record_resource_status(self, status: ResourceStatus) -> None:
        ctrl = self.control()
        old = ctrl.get("resource_pressure")
        if status.pressured:
            reason = "; ".join(status.reasons)
            if not old or old.get("reason") != reason:
                ctrl["resource_pressure"] = {"reason": reason, "at": now_iso()}
                self.events.emit("resource_pressure", "", reason=reason, active=status.active, limit=status.limit)
                self.log(f"resource pressure: {reason}; new local launches deferred while active work drains")
                self.state.save()
        elif old:
            ctrl.pop("resource_pressure", None)
            self.events.emit("resource_recovered", "", active=status.active, limit=status.limit)
            self.log("resource pressure cleared; local launches may resume")
            self.state.save()

    def refresh_resource_pressure(self) -> ResourceStatus:
        status = self.resource_status()
        self._record_resource_status(status)
        return status

    def local_slots_free(self) -> int:
        status = self.resource_status()
        if status.pressured:
            return 0
        return max(0, status.limit - status.active)

    def _admit_local_launch(self, kind: str) -> None:
        status = self.refresh_resource_pressure()
        if status.pressured:
            raise ResourcePressureError(
                f"{kind} deferred by resource pressure: {'; '.join(status.reasons)}; "
                "pause dispatch or wait for active runs to drain, then retry"
            )
