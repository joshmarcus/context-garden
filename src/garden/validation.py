"""Run a worker-issued validation inside its owning execution budget."""

from __future__ import annotations

import json
import math
import os
import shlex
import sys
from pathlib import Path

HARD_VALIDATION_TIMEOUT_SECONDS = 900


def bounded_validation_timeout_seconds(raw: object | None = None) -> float:
    """Return a usable validation budget without allowing the hard ceiling to rise."""
    if raw is None:
        raw = os.environ.get("GARDEN_VALIDATION_TIMEOUT_SECONDS", HARD_VALIDATION_TIMEOUT_SECONDS)
    try:
        configured = float(raw)
    except (TypeError, ValueError):
        configured = HARD_VALIDATION_TIMEOUT_SECONDS
    if not math.isfinite(configured) or configured <= 0:
        configured = HARD_VALIDATION_TIMEOUT_SECONDS
    return min(configured, HARD_VALIDATION_TIMEOUT_SECONDS)


def validation_timeout_result(run_dir: Path, exit_code: int | None) -> dict[str, str] | None:
    """Translate a supervisor timeout receipt into one stable check result."""
    if exit_code != 124:
        return None
    try:
        receipt = json.loads((run_dir / "validation_timeout.json").read_text())
        if (receipt.get("kind") != "validation_execution_timeout"
                or int(receipt.get("exit_code")) != 124):
            return None
        reason = str(receipt.get("reason") or "").strip()
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not reason:
        return None
    return {
        "name": "checks", "status": "error", "summary": "check execution timed out",
        "details": reason[:2000],
    }


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: python -m garden.validation -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    outer = os.environ.get("GARDEN_EXECUTION_RUN_DIR")
    if not outer or not os.environ.get("GARDEN_EXECUTION_OWNER"):
        print("validation must run inside a supervised local garden run", file=sys.stderr)
        return 2
    status_dir = Path(outer) / "validations" / str(os.getpid())
    status_dir.mkdir(parents=True, exist_ok=True)
    os.environ["GARDEN_HEAVY_EXECUTION"] = "1"
    os.environ["GARDEN_OWNER_SCOPED"] = "1"
    os.environ["GARDEN_EXECUTION_TIMEOUT_SECONDS"] = f"{bounded_validation_timeout_seconds():g}"
    # Replace this process with the ordinary supervisor: nested validation therefore gets
    # the same signal forwarding, subreaper ownership and adopted-descendant drain as an
    # outer run, plus both the authoritative host slot and its owner's serialization lock.
    os.execv(sys.executable, [sys.executable, "-m", "garden.run_supervisor",
                              str(status_dir), shlex.join(argv)])
    return 2  # pragma: no cover - execv either replaces us or raises


if __name__ == "__main__":
    raise SystemExit(main())
