"""Emit one disposable scheduler tick with phase-local wall, CPU, and read counters."""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from garden.events import EventLog
from garden.runs import RunStore
from garden.scheduler import Scheduler
from garden.scheduler.report import TickReport
from garden.store import Store

ACTIVE_PHASE = "controller_other"
COUNTERS: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))


def counted(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            COUNTERS[ACTIVE_PHASE][f"{name}_calls"] += 1
            COUNTERS[ACTIVE_PHASE][f"{name}_wall_s"] += time.perf_counter() - started

    return wrapped


Store._scan = counted("task_product_scan", Store._scan)  # type: ignore[method-assign]
EventLog.read = counted("event_read_parse", EventLog.read)  # type: ignore[method-assign]
RunStore._ensure_index = counted("run_index", RunStore._ensure_index)  # type: ignore[method-assign]
Scheduler.resource_status = counted(  # type: ignore[method-assign]
    "process_resource_inspection", Scheduler.resource_status
)

PHASE_CPU: dict[str, float] = defaultdict(float)
original_step = Scheduler._step


@contextmanager
def profiled_step(self: Scheduler, rep: TickReport, name: str) -> Iterator[None]:
    global ACTIVE_PHASE
    previous = ACTIVE_PHASE
    ACTIVE_PHASE = name
    cpu = time.process_time()
    try:
        with original_step(self, rep, name):
            yield
    finally:
        PHASE_CPU[name] += time.process_time() - cpu
        ACTIVE_PHASE = previous


Scheduler._step = profiled_step  # type: ignore[method-assign]


def main() -> None:
    garden = Path(os.environ["CG380_GARDEN"])
    scheduler = Scheduler(Store(garden), read_only=True)
    lock_path = scheduler.cfg.garden_dir / "tick.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_started = time.perf_counter()
    lock_cpu = time.process_time()
    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        lock_wall = time.perf_counter() - lock_started
        lock_process_cpu = time.process_time() - lock_cpu
        total_wall_started = time.perf_counter()
        total_cpu_started = time.process_time()
        report = scheduler._tick_locked(dispatch=False)
        total_wall = time.perf_counter() - total_wall_started
        total_cpu = time.process_time() - total_cpu_started
    named_wall = sum(report.steps.values())
    named_cpu = sum(PHASE_CPU.values())
    print(
        json.dumps(
            {
                "pid": os.getpid(),
                "cgroup": Path("/proc/self/cgroup").read_text().strip(),
                "lock_wait_wall_s": lock_wall,
                "lock_wait_process_cpu_s": lock_process_cpu,
                "total_wall_s": total_wall,
                "total_process_cpu_s": total_cpu,
                "controller_other_wall_s": max(0.0, total_wall - named_wall),
                "controller_other_process_cpu_s": max(0.0, total_cpu - named_cpu),
                "phases": {
                    name: {
                        "wall_s": wall,
                        "process_cpu_s": PHASE_CPU.get(name, 0.0),
                        "counters": dict(COUNTERS.get(name, {})),
                    }
                    for name, wall in report.steps.items()
                },
                "controller_other_counters": dict(COUNTERS.get("controller_other", {})),
                "unsupported_waits": [
                    "GitHub/network waits (fixture tasks have no PRs)",
                    "worker completion and vendor model waits (replay fixtures only)",
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
