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
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..model import now_iso
from ..run_supervisor import _authoritative_limit, _finite_cgroup_limits
from ..storage import StorageVolume, measure_storage
from ..system_resources import memory_bytes

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
    reclaim: str = ""
    host_memory_available_mb: int | None = None
    storage_volumes: tuple[StorageVolume, ...] = ()
    disk_reserve_bytes: int = 0
    disk_required_bytes: int = 0
    disk_operation: str = "local operation"

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
    available, _total = memory_bytes(path)
    return available // (1024 * 1024) if available is not None else None


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


def _memory_stat(group: Path) -> dict[str, int] | None:
    try:
        values = dict(line.split(maxsplit=1) for line in (group / "memory.stat").read_text().splitlines())
        return {name: int(values.get(name, "0")) for name in ("file", "shmem", "inactive_file")}
    except (OSError, ValueError):
        return None


def _reclaim_pid_alive(pid: int, token: str) -> bool:
    try:
        os.kill(pid, 0)
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return b"garden.resource_reclaim" in command and token.encode() in command
    except (OSError, ValueError):
        return False


class ResourceMixin:
    def _reclaim_paths(self) -> tuple[Path, Path]:
        return (self.cfg.garden_dir / "resource-reclaim.json",
                self.cfg.garden_dir / "resource-reclaim-report.json")

    def _read_reclaim_state(self) -> dict[str, Any]:
        """Observe published state without reconciling or writing from UI/read paths."""
        state_path, report_path = self._reclaim_paths()
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        try:
            report = json.loads(report_path.read_text())
        except (OSError, ValueError):
            report = None
        if state.get("running") and report and report.get("token") == state.get("token"):
            return {**state, "running": False, "result": report,
                    "finished_at": report.get("finished_at")}
        return state

    def _reconcile_reclaim_state(self) -> dict[str, Any]:
        """Publish helper completion while the caller holds the admission lock."""
        state_path, report_path = self._reclaim_paths()
        state = self._read_reclaim_state()
        try:
            report = json.loads(report_path.read_text())
        except (OSError, ValueError):
            report = None
        if state.get("running") is False and report and report.get("token") == state.get("token"):
            state.update({"running": False, "result": report, "finished_at": report.get("finished_at")})
            self._write_reclaim_state(state_path, state)
        elif state.get("running"):
            pid = int(state.get("pid") or 0)
            token = str(state.get("token") or "")
            timeout = float(self.effective("resources.reclaim_timeout_seconds", 5) or 5)
            if time.time() - float(state.get("started_at") or 0) > timeout + 1:
                if _reclaim_pid_alive(pid, token):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                state.update({"running": False, "finished_at": time.time(),
                              "result": {"status": "error", "error": "reclaim helper timed out"}})
                self._write_reclaim_state(state_path, state)
            elif not _reclaim_pid_alive(pid, token):
                state.update({"running": False, "finished_at": time.time(),
                              "result": {"status": "error", "error": "reclaim helper exited without a report"}})
                self._write_reclaim_state(state_path, state)
        return state

    @staticmethod
    def _write_reclaim_state(path: Path, state: dict[str, Any]) -> None:
        temporary = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
        os.replace(temporary, path)

    def _reclaim_description(self) -> str:
        state = self._read_reclaim_state()
        if state.get("running"):
            return f"bounded cache reclaim running for {state.get('boundary', 'limiting cgroup')}"
        result = state.get("result") or {}
        if result:
            before = result.get("headroom_before_bytes")
            after = result.get("headroom_after_bytes")
            headroom = (f" ({int(before) // (1024 * 1024)}→{int(after) // (1024 * 1024)} MiB actual headroom"
                        if isinstance(before, int) and isinstance(after, int) else "")
            error = f": {result['error']}" if result.get("error") else ""
            return f"last bounded cache reclaim {result.get('status', 'unknown')}{headroom}{')' if headroom else ''}{error}"
        return ""

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
        return [r for r in self.active_runs() if r.is_local_execution]

    def resource_weight(self, task_id: str) -> int:
        task = self.store.tasks().get(task_id)
        return self.cfg.product_resource_weight(task.product) if task is not None else 1

    def run_resource_weight(self, run: Any) -> int:
        """Read the dispatch-time reservation; legacy runs consume one unit."""
        value = (run.env_snapshot or {}).get("resource_weight", 1)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1

    def _storage_status(self, *, fresh: bool) -> tuple[StorageVolume, ...]:
        """Keep passive status rendering cheap while admission always requests fresh data."""
        cached = getattr(self, "_storage_status_cache", None)
        if not fresh and cached and time.monotonic() - cached[0] < 5:
            return cached[1]
        volumes = measure_storage(
            (self.cfg.garden_dir, self.cfg.work_dir, self.cfg.work_dir / "tmp"),
            windows_backing_path=str(self.effective("resources.windows_backing_path", "") or ""),
        )
        self._storage_status_cache = (time.monotonic(), volumes)
        return volumes

    def resource_status(self, *, include_next_disk: bool = True,
                        fresh_storage: bool = False) -> ResourceStatus:
        active = sum(self.run_resource_weight(run) for run in self.local_runs_active())
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
        events = execution_events if cgroup_boundary == "execution cgroup" else controller_events
        values = [v for v in (host_memory, cgroup_memory) if v is not None]
        memory = min(values) if values else None
        temp = _free_mb(self.cfg.work_dir / "tmp")
        disk_reserve = max(0, int(self.effective("resources.disk_reserve_bytes", 0) or 0))
        disk_required = sum(max(0, int((run.env_snapshot or {}).get("disk_required_bytes", 0) or 0))
                            for run in self.local_runs_active())
        next_required = (max(0, int(self.effective("resources.operation_required_bytes", 0) or 0))
                         if include_next_disk else 0)
        volumes = self._storage_status(fresh=fresh_storage)
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
        for volume in volumes:
            if not disk_reserve:
                continue
            if volume.free_bytes is None:
                pressure_reasons.append(f"{volume.label} {volume.error}")
            elif volume.free_bytes - disk_required - next_required < disk_reserve:
                pressure_reasons.append(
                    f"{volume.label} has {volume.free_bytes} free bytes; reserve {disk_reserve} bytes "
                    f"plus {disk_required + next_required} reserved bytes is required"
                )
        return ResourceStatus(active, limit, memory, memory_min, temp, temp_min, cgroup_memory,
                              cgroup_boundary, tuple(sorted(events.items())), isolation,
                              requested_heavy_limit, heavy_limit, heavy_conflict,
                              heavy_running, heavy_waiting, tuple(pressure_reasons), self._reclaim_description(), host_memory,
                              volumes, disk_reserve, disk_required,
                              str((self.control().get("resource_pressure") or {}).get("operation") or "local operation"))

    def _start_reclaim_if_eligible(self, status: ResourceStatus) -> bool:
        """Start one helper only when cgroup headroom is the sole remaining gate."""
        if len(status.reasons) != 1 or "available memory" not in status.reasons[0]:
            return False
        if status.cgroup_boundary not in ("controller cgroup", "execution cgroup"):
            return False
        if status.memory_available_mb is None or status.cgroup_available_mb != status.memory_available_mb:
            return False
        if status.host_memory_available_mb is None or status.host_memory_available_mb < status.memory_min_mb:
            return False
        if status.memory_available_mb >= status.memory_min_mb:
            return False
        group = (_cgroup_path_for_process() if status.cgroup_boundary == "controller cgroup" else
                 Path(str(self.effective("resources.execution_cgroup", "") or "")))
        maximum_mb = max(0, int(self.effective("resources.reclaim_max_mb", 512) or 0))
        stats = _memory_stat(group) if group else None
        if not group or not maximum_mb or not stats or stats["inactive_file"] <= 0:
            return False
        state = self._reconcile_reclaim_state()
        if state.get("running"):
            return False
        cooldown = max(0, float(self.effective("resources.reclaim_cooldown_seconds", 300) or 0))
        if time.time() - float(state.get("finished_at") or 0) < cooldown:
            return False
        # inactive_file is an eligibility signal and request bound, never admission credit.
        request = min(maximum_mb * 1024 * 1024, stats["inactive_file"])
        state_path, report_path = self._reclaim_paths()
        proc: subprocess.Popen[bytes] | None = None
        try:
            identity = group.stat()
            report_path.unlink(missing_ok=True)
            started = time.time()
            token = uuid.uuid4().hex
            timeout = max(0.1, float(self.effective("resources.reclaim_timeout_seconds", 5) or 5))
            proc = subprocess.Popen(
                [sys.executable, "-m", "garden.resource_reclaim", "--cgroup", str(group),
                 "--bytes", str(request), "--timeout", str(timeout), "--report", str(report_path),
                 "--token", token, "--device", str(identity.st_dev), "--inode", str(identity.st_ino)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            state = {"running": True, "pid": proc.pid, "started_at": started, "token": token,
                     "boundary": status.cgroup_boundary, "requested_bytes": request,
                     "memory_stat": stats}
            self._write_reclaim_state(state_path, state)
        except OSError as exc:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            self._write_reclaim_state(state_path, {"running": False, "finished_at": time.time(),
                                                    "result": {"status": "error", "error": str(exc)}})
            return False
        self.events.emit("resource_reclaim", "", status="started", boundary=status.cgroup_boundary,
                         requested_bytes=request, headroom_before_mb=status.cgroup_available_mb)
        self.log(f"resource pressure: started bounded cache reclaim in {status.cgroup_boundary}; "
                 f"requested {request // (1024 * 1024)} MiB, admission remains deferred pending a fresh check")
        return True

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
        status = self.resource_status(fresh_storage=True)
        self._record_resource_status(status)
        return status

    def local_slots_free(self, task_id: str = "") -> int:
        status = self.resource_status()
        if status.admission_blocked:
            return 0
        free = max(0, status.limit - status.active)
        return free if not task_id or free >= self.resource_weight(task_id) else 0

    def _try_reclaim_for_pending_local_launch(self) -> bool:
        """Give queued local work one serialized reclaim attempt without admitting it."""
        with self._local_admission_lock():
            status = self.refresh_resource_pressure()
            return status.pressured and self._start_reclaim_if_eligible(status)

    def _admit_local_launch(self, kind: str, weight: int = 1) -> None:
        status = self.refresh_resource_pressure()
        if status.pressured or status.active + weight > status.limit:
            if not status.pressured:
                raise ResourcePressureError(
                    f"{kind} needs {weight} capacity unit(s) and waits for a local execution slot "
                    f"({status.active}/{status.limit} in use); "
                    "eligible work dispatches automatically when one finishes"
                )
            if status.pressured:
                self._start_reclaim_if_eligible(status)
            pressure = self.control().setdefault("resource_pressure", {"at": now_iso()})
            pressure["operation"] = kind
            self.state.save()
            raise ResourcePressureError(
                f"{kind} deferred by resource pressure: {'; '.join(status.reasons)}; "
                "pause dispatch or wait for active runs to drain, then retry"
            )

    def _recheck_local_materialization(self, run: Any, operation: str) -> None:
        """Freshly recheck an admitted reservation immediately before disk materialization."""
        with self._local_admission_lock():
            status = self.resource_status(include_next_disk=False, fresh_storage=True)
            self._record_resource_status(status)
            if status.pressured:
                pressure = self.control().setdefault("resource_pressure", {"at": now_iso()})
                pressure["operation"] = operation
                self.state.save()
                raise ResourcePressureError(
                    f"{operation} deferred by resource pressure: {'; '.join(status.pressure_reasons)}"
                )

    @contextmanager
    def _local_staging_admission(self, operation: str):
        """Serialize unreserved controller staging with a fresh physical-space check."""
        with self._local_admission_lock():
            status = self.resource_status(fresh_storage=True)
            self._record_resource_status(status)
            if status.pressured:
                pressure = self.control().setdefault("resource_pressure", {"at": now_iso()})
                pressure["operation"] = operation
                self.state.save()
                raise ResourcePressureError(
                    f"{operation} deferred by resource pressure: {'; '.join(status.pressure_reasons)}"
                )
            yield

    def _new_local_run(self, task_id: str, mode: str, kind: str, *, run_id: str = "",
                       runner_name: str = "local", resource_weight: int | None = None) -> Any:
        """Atomically admit and publish a running local run across all launchers."""
        with self._local_admission_lock():
            weight = self.resource_weight(task_id) if resource_weight is None else resource_weight
            self._admit_local_launch(kind, weight)
            run = self.runs.new_run(task_id, runner_name, mode=mode, run_id=run_id)
            run.execution_remote = False
            # A local run is published before worktree and brief preparation complete.
            # Record the process that owns that short pre-worker window so another
            # scheduler can avoid reaping an in-progress launch, while a restart can
            # still reclaim the reservation once this process is gone.
            run.preparer_pid = os.getpid()
            run.env_snapshot["resource_weight"] = weight
            run.env_snapshot["disk_required_bytes"] = max(
                0, int(self.effective("resources.operation_required_bytes", 0) or 0)
            )
            run.save()
            # Setup and runtime scratch are created by a detached supervisor after this
            # lock is released.  Preserve the aggregate that this serialized admission
            # accepted so their fresh physical-space checks include sibling reservations.
            # A later contender includes this run in its own aggregate, so it cannot race
            # an older supervisor for the same estimated headroom.
            run.env_snapshot["disk_recheck_required_bytes"] = sum(
                max(0, int((active.env_snapshot or {}).get("disk_required_bytes", 0) or 0))
                for active in self.local_runs_active()
            )
            run.save()
            return run
