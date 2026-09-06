"""Own every descendant of one local run, including children that call ``setsid``."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _become_subreaper() -> None:
    if sys.platform.startswith("linux"):
        # Orphaned grandchildren are reparented here rather than to init. This keeps a
        # daemonized test process owned by the run until it exits or the run is stopped.
        ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER


def _children(pid: int) -> list[int]:
    try:
        return [int(value) for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]
    except (OSError, ValueError):
        return []


def _descendants(pid: int) -> list[int]:
    found: list[int] = []
    pending = _children(pid)
    while pending:
        child = pending.pop()
        found.append(child)
        pending.extend(_children(child))
    return found


def _signal_descendants(sig: int) -> None:
    for pid in reversed(_descendants(os.getpid())):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    run_dir, script = Path(sys.argv[1]), sys.argv[2]
    _become_subreaper()
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        _signal_descendants(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    child = subprocess.Popen(["sh", "-c", script])
    code = child.wait()
    deadline = time.monotonic() + 5.0 if stopping else None
    while True:
        try:
            waited, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if waited == 0:
            if deadline is not None and time.monotonic() >= deadline:
                _signal_descendants(signal.SIGKILL)
            time.sleep(0.05)
    (run_dir / "exit_code").write_text(str(code))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
