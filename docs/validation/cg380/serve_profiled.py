"""Serve the CG-380 disposable garden with request-local attribution spans."""

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

LOG = Path(os.environ["CG380_PROFILE_LOG"])
LOCK = threading.Lock()
ACTIVE: dict[str, dict[str, float]] = {}
COUNTS: dict[str, dict[str, int]] = {}


def timed(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            with LOCK:
                for spans in ACTIVE.values():
                    spans[name] += time.perf_counter() - started
                for counts in COUNTS.values():
                    counts[name] += 1

    return wrapped


EventLog.read = timed("event_read_parse_s", EventLog.read)  # type: ignore[method-assign]
RunStore._ensure_index = timed("run_index_s", RunStore._ensure_index)  # type: ignore[method-assign]
Store._scan = timed("task_product_scan_s", Store._scan)  # type: ignore[method-assign]
Scheduler.resource_status = timed("process_resource_inspection_s", Scheduler.resource_status)  # type: ignore[method-assign]
Jinja2Templates.TemplateResponse = timed("render_s", Jinja2Templates.TemplateResponse)  # type: ignore[method-assign]

app = create_app(Store(Path(os.environ["CG380_GARDEN"])), watch=False, host="127.0.0.1")


@app.middleware("http")
async def profile_request(request: Request, call_next: Callable[..., Any]):
    request_id = f"{time.time_ns()}-{threading.get_ident()}"
    with LOCK:
        ACTIVE[request_id] = defaultdict(float)
        COUNTS[request_id] = defaultdict(int)
    wall = time.perf_counter()
    cpu = time.process_time()
    response = await call_next(request)
    row: dict[str, Any] = {
        "path": request.url.path,
        "wall_s": time.perf_counter() - wall,
        "process_cpu_s": time.process_time() - cpu,
    }
    with LOCK:
        row["spans"] = dict(ACTIVE.pop(request_id))
        row["counts"] = dict(COUNTS.pop(request_id))
        with LOG.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    return response
