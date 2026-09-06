"""Own every descendant of one local run, including children that call ``setsid``."""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _execution_slot(run_dir: Path, should_stop: object) -> object:
    """Take one host-wide execution lease, recoverable by kernel lock release."""
    limit = int(os.environ.get("GARDEN_HEAVY_TEST_PARALLEL", "1"))
    if limit <= 0:
        (run_dir / "execution.json").write_text(json.dumps({"state": "disabled", "limit": 0}))
        return None
    lock_root = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    while True:
        if should_stop():
            raise InterruptedError
        for slot in range(limit):
            handle = (lock_root / f"garden-heavy-test-{os.getuid()}-{slot}.lock").open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            (run_dir / "execution.json").write_text(json.dumps(
                {"state": "running", "slot": slot, "limit": limit, "pid": os.getpid()}
            ))
            return handle
        (run_dir / "execution.json").write_text(json.dumps(
            {"state": "waiting", "reason": f"heavy-test budget full (limit {limit})", "limit": limit}
        ))
        time.sleep(0.1)


def _enter_execution_cgroup(run_dir: Path) -> None:
    configured = os.environ.get("GARDEN_EXECUTION_CGROUP", "")
    status = {"configured": bool(configured), "enforced": False}
    if configured:
        try:
            target = Path(configured)
            (target / "cgroup.procs").write_text(str(os.getpid()))
            status.update({"enforced": True, "path": str(target)})
        except OSError as exc:
            status["reason"] = f"execution cgroup unavailable: {exc}"
    else:
        status["reason"] = "execution cgroup is not configured"
    (run_dir / "isolation.json").write_text(json.dumps(status))


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


def _run_setup(run_dir: Path) -> bool:
    payload = run_dir / "setup_input.json"
    if not payload.exists():
        return True
    from garden.runner.base import RunnerError, run_setup

    try:
        run_setup(Path.cwd(), json.loads(payload.read_text()), log_path=run_dir / "setup.log",
                  env=dict(os.environ))
    except (OSError, ValueError, RunnerError) as exc:
        (run_dir / "stderr.log").write_text(f"{exc}\n")
        (run_dir / "exit_code").write_text("1")
        return False
    return True


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    run_dir, script = Path(sys.argv[1]), sys.argv[2]
    _become_subreaper()
    _enter_execution_cgroup(run_dir)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        _signal_descendants(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    try:
        slot = _execution_slot(run_dir, lambda: stopping)
    except InterruptedError:
        (run_dir / "exit_code").write_text("143")
        return 143
    if not _run_setup(run_dir):
        return 1
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
    del slot  # keep the flock alive until every adopted descendant has exited
    return code


if __name__ == "__main__":
    raise SystemExit(main())
