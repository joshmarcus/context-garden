"""Run a worker-issued validation inside its owning execution budget."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: python -m garden.validation -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    outer = os.environ.get("GARDEN_EXECUTION_RUN_DIR")
    if not outer or os.environ.get("GARDEN_EXECUTION_LEASED") != "1":
        print("validation must run inside a supervised local garden run", file=sys.stderr)
        return 2
    status_dir = Path(outer) / "validations" / str(os.getpid())
    status_dir.mkdir(parents=True, exist_ok=True)
    # Replace this process with the ordinary supervisor: nested validation therefore gets
    # the same signal forwarding, subreaper ownership and adopted-descendant drain as an
    # outer run, while _execution_slot selects its owner-scoped rather than host lock.
    os.execv(sys.executable, [sys.executable, "-m", "garden.run_supervisor",
                              str(status_dir), shlex.join(argv)])
    return 2  # pragma: no cover - execv either replaces us or raises


if __name__ == "__main__":
    raise SystemExit(main())
