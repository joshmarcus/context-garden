"""Run a worker-issued validation inside its owning execution budget."""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
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
    # Run the ordinary supervisor: nested validation therefore gets
    # the same signal forwarding, subreaper ownership and adopted-descendant drain as an
    # outer run, plus both the authoritative host slot and its owner's serialization lock.
    command = shlex.join(argv)
    completed = subprocess.run(
        [sys.executable, "-m", "garden.run_supervisor", str(status_dir), command],
        check=False,
    )
    try:
        source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        source_sha = ""
    receipt = {
        "version": 1,
        "source_sha": source_sha,
        "command": command,
        "selection": argv,
        "exit_code": completed.returncode,
        "log_location": str(status_dir),
    }
    (status_dir / "result.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
