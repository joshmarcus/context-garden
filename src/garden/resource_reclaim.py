"""Bounded cgroup-v2 reclaim helper used by local admission.

The scheduler starts this as a detached process so a kernel ``memory.reclaim`` write can
never hold a tick or web action.  This process records observations only; admission always
re-reads the normal resource gates after it exits.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path


def _identity(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {"path": str(path.resolve()), "device": stat.st_dev, "inode": stat.st_ino}


def _headroom(path: Path) -> int | None:
    current = int((path / "memory.current").read_text().strip())
    limits = []
    for name in ("memory.high", "memory.max"):
        value = (path / name).read_text().strip()
        if value != "max":
            limits.append(int(value))
    return max(0, min(limits) - current) if limits else None


def _write(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--bytes", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    started = time.time()
    result: dict[str, object] = {"token": args.token, "started_at": started, "requested_bytes": args.bytes}

    def timed_out(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"memory.reclaim exceeded {args.timeout:g}s")

    try:
        before_identity = _identity(args.cgroup)
        result.update({"cgroup_before": before_identity, "headroom_before_bytes": _headroom(args.cgroup)})
        signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, args.timeout)
        (args.cgroup / "memory.reclaim").write_text(str(args.bytes))
        signal.setitimer(signal.ITIMER_REAL, 0)
        after_identity = _identity(args.cgroup)
        result.update({"cgroup_after": after_identity, "headroom_after_bytes": _headroom(args.cgroup)})
        if after_identity != before_identity:
            raise RuntimeError("limiting cgroup identity changed during reclaim")
        result["status"] = "complete"
    except Exception as exc:  # noqa: BLE001 - the report must retain every kernel/read failure
        result.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        result["finished_at"] = time.time()
        _write(args.report, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
