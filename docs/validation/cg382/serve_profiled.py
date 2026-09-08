"""Serve CG-382's disposable fixture and write one filesystem profile per HTTP request."""

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

from garden import store as store_module
from garden.model import Task
from garden.store import Store
from garden.web.app import create_app
from garden.web.common import Hub

LOG = Path(os.environ["CG382_PROFILE_LOG"])
_lock = threading.Lock()
_active: dict[str, dict[str, int]] = {}


def _count(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _lock:
            for counts in _active.values():
                counts[name] += 1
        return original(*args, **kwargs)
    return wrapped


Store._scan = _count("store_scans", Store._scan)  # type: ignore[method-assign]
Task.parse = classmethod(_count("yaml_parses", Task.parse.__func__))  # type: ignore[method-assign]
store_module.os.stat = _count("filesystem_stats", store_module.os.stat)  # type: ignore[assignment]

if os.environ.get("CG382_LEGACY_SNAPSHOT") == "1":
    Hub.begin_request = lambda self: None  # type: ignore[method-assign]
    Hub.end_request = lambda self, token: None  # type: ignore[method-assign]

app = create_app(Store(Path(os.environ["CG382_GARDEN"])), watch=False, host="127.0.0.1")


@app.middleware("http")
async def profile_request(request: Request, call_next: Callable[..., Any]):
    request_id = f"{time.time_ns()}-{threading.get_ident()}"
    with _lock:
        _active[request_id] = defaultdict(int)
    started = time.perf_counter()
    response = await call_next(request)
    with _lock:
        counts = dict(_active.pop(request_id))
        with LOG.open("a") as stream:
            stream.write(json.dumps({"path": request.url.path, "status": response.status_code,
                                     "elapsed_s": time.perf_counter() - started, "counts": counts},
                                    sort_keys=True) + "\n")
    return response
