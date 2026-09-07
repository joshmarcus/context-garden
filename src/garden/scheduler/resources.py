"""Host-wide admission and pressure sensing for local execution.

Workers, reviews and product checks are different scheduler queues, but on the operator
machine they compete for the same memory, CPU and temporary filesystem.  This module gives
every local launch path one shared gate.  Reaping is never gated, so an overloaded machine
can drain and recover without losing run records or restarting the controller.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..model import now_iso
from ..run_supervisor import _authoritative_limit, _finite_cgroup_limits

_ADMISSION_LOCKS: dict[str, threading.Lock] = {}
_ADMISSION_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ResourceStatus:
    active: int
    limit: int
    memory_available_mb: int | None
    memory_min_mb: int
    temp_free_mb: int | None
    temp_min_mb: int
    cgroup_available_mb: int | None
    cgroup_boundary: str
    cgroup_events: tuple[tuple[str, int], ...]
    isolation: str
    requested_heavy_limit: int
    heavy_limit: int
    heavy_conflict: str | None
    heavy_running: int
    heavy_waiting: int
    pressure_reasons: tuple[str, ...]

    @property
    def capacity_full(self) -> bool:
        """Whether ordinary local concurrency, not host pressure, is full."""
        return self.active >= self.limit

    @property
    def capacity_reason(self) -> str | None:
        if self.capacity_full:
            return f"local execution capacity is full ({self.active}/{self.limit} busy)"
        return None

    @property
    def reasons(self) -> tuple[str, ...]:
        """All admission blockers, with capacity kept distinct from pressure."""
        return ((self.capacity_reason,) if self.capacity_reason else ()) + self.pressure_reasons

    @property
    def pressured(self) -> bool:
        """True only for a real host resource gate, never normal occupancy."""
        return bool(self.pressure_reasons)

    @property
    def admission_blocked(self) -> bool:
        return self.capacity_full or self.pressured


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


def _cgroup_path_for_process(root: Path = Path("/sys/fs/cgroup")) -> Path | None:
    try:
        relative = next(line.split("::", 1)[1] for line in Path("/proc/self/cgroup").read_text().splitlines()
                        if line.startswith("0::"))
        return root / relative.lstrip("/")
    except (OSError, StopIteration):
        return None


def _cgroup_memory_status(group: Path) -> tuple[int | None, dict[str, int]]:
    """Return finite memory headroom and pressure counters for one cgroup."""
    try:
        current = int((group / "memory.current").read_text().strip())
        ceilings = []
        for name in ("memory.high", "memory.max"):
            raw = (group / name).read_text().strip()
            if raw != "max":
                ceilings.append(int(raw))
        events = dict(line.split(maxsplit=1) for line in (group / "memory.events").read_text().splitlines())
        return (max(0, min(ceilings) - current) // (1024 * 1024) if ceilings else None,
                {key: int(events.get(key, "0")) for key in ("high", "max", "oom", "oom_kill")})
    except (OSError, ValueError):
        return None, {}


def _cgroup_memory_available_mb(root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """Compatibility helper for the control process's cgroup headroom."""
    group = _cgroup_path_for_process(root)
    return _cgroup_memory_status(group)[0] if group else None


class ResourceMixin:
    @contextmanager
    def _local_admission_lock(self):
        """Serialize the capacity decision with publishing its running record.

        The thread lock is needed because ``flock`` locks are process-associated on some
        platforms; the file lock covers independent CLI, web and service processes.
        """
        path = self.cfg.garden_dir / "resource-admission.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        key = str(path.resolve())
        with _ADMISSION_LOCKS_GUARD:
            thread_lock = _ADMISSION_LOCKS.setdefault(key, threading.Lock())
        with thread_lock, path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                # A different process may have published a run while this process's run
                # index was warm. Force the filesystem view used by resource_status.
                self.runs.invalidate()
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
        host_memory = _memory_available_mb()
        controller_group = _cgroup_path_for_process()
        controller_memory_actual, controller_events = (_cgroup_memory_status(controller_group)
                                                       if controller_group else (None, {}))
        # Keep this seam for callers and focused tests that provide the controller reading.
        controller_memory = _cgroup_memory_available_mb()
        execution_cgroup = str(self.effective("resources.execution_cgroup", "") or "")
        execution_group = Path(execution_cgroup) if execution_cgroup else None
        execution_memory, execution_events = (_cgroup_memory_status(execution_group)
                                               if execution_group else (None, {}))
        cgroup_values = [("controller cgroup", controller_memory),
                         ("execution cgroup", execution_memory)]
        known_cgroup_values = [(name, value) for name, value in cgroup_values if value is not None]
        cgroup_memory = min((value for _, value in known_cgroup_values), default=None)
        cgroup_boundary = min(known_cgroup_values, key=lambda item: item[1])[0] if known_cgroup_values else "none"
        events = execution_events if execution_group else controller_events
        values = [v for v in (host_memory, cgroup_memory) if v is not None]
        memory = min(values) if values else None
        temp = _free_mb(self.cfg.work_dir / "tmp")
        isolation = "not configured"
        if execution_cgroup:
            target = Path(execution_cgroup)
            bounded, _, reason = _finite_cgroup_limits(target)
            procs = target / "cgroup.procs"
            isolation = "available" if bounded and procs.exists() and os.access(procs, os.W_OK) else reason or "unavailable"
        requested_heavy_limit = max(0, int(self.effective("resources.heavy_test_parallel", 1) or 0))
        if requested_heavy_limit == 0:
            heavy_limit, heavy_conflict = 0, None
        else:
            try:
                heavy_limit, heavy_conflict = _authoritative_limit(requested_heavy_limit)
            except RuntimeError as exc:
                heavy_limit, heavy_conflict = 0, str(exc)
        heavy_running = heavy_waiting = 0
        for run in self.local_runs_active():
            try:
                if execution_cgroup:
                    actual = json.loads((run.path / "isolation.json").read_text())
                    if actual.get("enforced"):
                        isolation = "enforced"
                    else:
                        isolation = str(actual.get("reason") or "unavailable")
            except (OSError, ValueError):
                pass
            status_paths = [run.path / "execution.json", *run.path.glob("validations/*/execution.json")]
            for status_path in status_paths:
                try:
                    state = json.loads(status_path.read_text()).get("state")
                except (OSError, ValueError):
                    continue
                heavy_running += state == "running"
                heavy_waiting += state == "waiting"
        pressure_reasons: list[str] = []
        if memory_min and memory is not None and memory < memory_min:
            if cgroup_memory is not None and cgroup_memory <= (host_memory or memory):
                prefix = "" if cgroup_boundary == "controller cgroup" else f"{cgroup_boundary} "
                pressure_reasons.append(f"{prefix}available memory {memory} MiB is below {memory_min} MiB")
            else:
                pressure_reasons.append(f"available memory {memory} MiB is below {memory_min} MiB")
        if any(events.get(name, 0) for name in ("oom", "oom_kill")):
            pressure_reasons.append("execution cgroup memory events report oom pressure")
        if temp_min and temp is not None and temp < temp_min:
            pressure_reasons.append(f"temporary storage {temp} MiB free is below {temp_min} MiB")
        return ResourceStatus(active, limit, memory, memory_min, temp, temp_min, cgroup_memory,
                              cgroup_boundary, tuple(sorted(events.items())), isolation,
                              requested_heavy_limit, heavy_limit, heavy_conflict,
                              heavy_running, heavy_waiting, tuple(pressure_reasons))

    def _record_resource_status(self, status: ResourceStatus) -> None:
        ctrl = self.control()
        old = ctrl.get("resource_pressure")
        if status.pressured:
            reason = "; ".join(status.pressure_reasons)
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
        if status.admission_blocked:
            return 0
        return max(0, status.limit - status.active)

    def _admit_local_launch(self, kind: str) -> None:
        status = self.refresh_resource_pressure()
        if status.admission_blocked:
            if status.capacity_full and not status.pressured:
                raise ResourcePressureError(
                    f"{kind} waits for a local execution slot ({status.active}/{status.limit} busy); "
                    "eligible work dispatches automatically when one finishes"
                )
            raise ResourcePressureError(
                f"{kind} deferred by resource pressure: {'; '.join(status.reasons)}; "
                "pause dispatch or wait for active runs to drain, then retry"
            )

    def _new_local_run(self, task_id: str, mode: str, kind: str, *, run_id: str = "") -> Any:
        """Atomically admit and publish a running local run across all launchers."""
        with self._local_admission_lock():
            self._admit_local_launch(kind)
            return self.runs.new_run(task_id, "local", mode=mode, run_id=run_id)
