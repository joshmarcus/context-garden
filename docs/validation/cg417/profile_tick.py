"""Run one no-dispatch tick and record its Store scan count and wall time."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from garden.scheduler import Scheduler
from garden.store import Store


def _count(original: Callable[..., Any], counts: list[int]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        counts[0] += 1
        return original(*args, **kwargs)
    return wrapped


def main() -> None:
    counts = [0]
    original_scan = Store._scan
    Store._scan = _count(original_scan, counts)  # type: ignore[method-assign]
    scheduler = Scheduler(Store(Path(os.environ["CG417_GARDEN"])), read_only=True)
    started = time.perf_counter()
    scheduler._tick_locked(dispatch=False)
    print(json.dumps({"elapsed_s": time.perf_counter() - started, "store_scans": counts[0]}))


if __name__ == "__main__":
    main()
