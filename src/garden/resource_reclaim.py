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


def _identity(path: Path, stat: os.stat_result) -> dict[str, object]:
    return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino}


def _read_at(directory_fd: int, name: str) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        return os.read(fd, 128).decode().strip()
    finally:
        os.close(fd)


def _headroom(directory_fd: int) -> int | None:
    current = int(_read_at(directory_fd, "memory.current"))
    limits = []
    for name in ("memory.high", "memory.max"):
        value = _read_at(directory_fd, name)
        if value != "max":
            limits.append(int(value))
    return max(0, min(limits) - current) if limits else None


def _write(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--bytes", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--inode", type=int, required=True)
    args = parser.parse_args()
    started = time.time()
    result: dict[str, object] = {"token": args.token, "started_at": started, "requested_bytes": args.bytes}

    def timed_out(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"memory.reclaim exceeded {args.timeout:g}s")

    try:
        directory_fd = os.open(args.cgroup, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_stat = os.fstat(directory_fd)
        before_identity = _identity(args.cgroup, directory_stat)
        result["cgroup_before"] = before_identity
        if (directory_stat.st_dev, directory_stat.st_ino) != (args.device, args.inode):
            raise RuntimeError("limiting cgroup identity changed before reclaim")
        result["headroom_before_bytes"] = _headroom(directory_fd)
        signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, args.timeout)
        reclaim_fd = os.open("memory.reclaim", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            os.write(reclaim_fd, str(args.bytes).encode())
        finally:
            os.close(reclaim_fd)
        signal.setitimer(signal.ITIMER_REAL, 0)
        after_stat = os.fstat(directory_fd)
        after_identity = _identity(args.cgroup, after_stat)
        result.update({"cgroup_after": after_identity, "headroom_after_bytes": _headroom(directory_fd)})
        result["status"] = "complete"
    except Exception as exc:  # noqa: BLE001 - the report must retain every kernel/read failure
        result.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if "directory_fd" in locals():
            os.close(directory_fd)
        result["finished_at"] = time.time()
        _write(args.report, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
