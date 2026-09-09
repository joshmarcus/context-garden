"""Instrumented disposable server for the CG-385 retained-history validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import uvicorn

from garden.store import Store
from garden.web.app import create_app

root, counter_path, port = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
original_tasks = Store.tasks


def counted_tasks(self: Store):
    try:
        counts = json.loads(counter_path.read_text())
    except (OSError, ValueError):
        counts = {"reads": 0, "scans": 0}
    counts["reads"] += 1
    counts["scans"] += 1
    counter_path.write_text(json.dumps(counts))
    return original_tasks(self)


Store.tasks = counted_tasks
uvicorn.run(create_app(Store(root), watch=False, host="127.0.0.1", port=port),
            host="127.0.0.1", port=port, log_level="error")
