"""Serve one disposable garden while recording request-local profiling spans."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from garden.events import EventLog
from garden.runs import RunStore
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app

PROFILE_LOG = Path(os.environ["CG367_PROFILE_LOG"])
_lock = threading.Lock()
_active: dict[str, dict[str, float]] = {}
_counts: dict[str, dict[str, int]] = {}


def _timed(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started
            with _lock:
                for values in _active.values():
                    values[name] += elapsed
                for counts in _counts.values():
                    counts[name] += 1

    return wrapped


EventLog.read = _timed("event_read_parse_s", EventLog.read)  # type: ignore[method-assign]
RunStore._ensure_index = _timed("run_index_s", RunStore._ensure_index)  # type: ignore[method-assign]
Store._scan = _timed("task_product_scan_s", Store._scan)  # type: ignore[method-assign]
Scheduler.resource_status = _timed("resource_observation_s", Scheduler.resource_status)  # type: ignore[method-assign]
Jinja2Templates.TemplateResponse = _timed(  # type: ignore[method-assign]
    "template_s", Jinja2Templates.TemplateResponse
)

app = create_app(Store(Path(os.environ["CG367_GARDEN"])), watch=False, host="127.0.0.1")


@app.middleware("http")
async def profile_request(request: Request, call_next: Callable[..., Any]):
    request_id = f"{time.time_ns()}-{threading.get_ident()}"
    with _lock:
        _active[request_id] = defaultdict(float)
        _counts[request_id] = defaultdict(int)
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    response = await call_next(request)
    row = {
        "path": request.url.path,
        "wall_s": time.perf_counter() - wall_started,
        "process_cpu_s": time.process_time() - cpu_started,
    }
    with _lock:
        row["spans"] = dict(_active.pop(request_id))
        row["counts"] = dict(_counts.pop(request_id))
        with PROFILE_LOG.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    return response
