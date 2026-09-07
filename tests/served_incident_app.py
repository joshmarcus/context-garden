"""Disposable ASGI app with file-controlled overload/setup gates for CG-358."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import uvicorn

from garden.store import Store
from garden.web.app import create_app

root = Path(sys.argv[1])
port = int(sys.argv[2])
gate_dir = Path(sys.argv[3])
real_tasks = Store.tasks


def overloaded_tasks(self: Store):
    if (gate_dir / "slow").exists() and threading.current_thread().name == "AnyIO worker thread":
        (gate_dir / "read-entered").touch()
        while not (gate_dir / "read-release").exists():
            time.sleep(0.01)
    return real_tasks(self)


Store.tasks = overloaded_tasks
uvicorn.run(create_app(Store(root), watch=False, host="127.0.0.1", port=port), host="127.0.0.1", port=port, log_level="error")
