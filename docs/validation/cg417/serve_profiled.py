"""Serve CG-417's disposable fixture and count Store scans per request."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Request

from garden.store import Store
from garden.web.app import create_app

LOG = Path(os.environ["CG417_PROFILE_LOG"])
_lock = threading.Lock()
_active: dict[str, int] = {}


def _count(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _lock:
            for request_id in _active:
                _active[request_id] += 1
        return original(*args, **kwargs)
    return wrapped


Store._scan = _count(Store._scan)  # type: ignore[method-assign]
app = create_app(Store(Path(os.environ["CG417_GARDEN"])), watch=False, host="127.0.0.1")


@app.middleware("http")
async def profile_request(request: Request, call_next: Callable[..., Any]):
    request_id = f"{time.time_ns()}-{threading.get_ident()}"
    with _lock:
        _active[request_id] = 0
    started = time.perf_counter()
    response = await call_next(request)
    with _lock:
        scans = _active.pop(request_id)
    with LOG.open("a") as stream:
        stream.write(json.dumps({"path": request.url.path, "status": response.status_code,
                                 "elapsed_s": time.perf_counter() - started,
                                 "store_scans": scans}, sort_keys=True) + "\n")
    return response
