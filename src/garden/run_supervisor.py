"""Own every descendant of one local run, including children that call ``setsid``."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import IO


def _finite_cgroup_limits(target: Path) -> tuple[bool, dict[str, str], str]:
    """Return whether *target* has finite aggregate CPU and memory controls."""
    try:
        cpu_max = (target / "cpu.max").read_text().strip()
        memory_high = (target / "memory.high").read_text().strip()
        memory_max = (target / "memory.max").read_text().strip()
    except OSError as exc:
        return False, {}, f"cannot read execution cgroup limits: {exc}"
    limits = {"cpu.max": cpu_max, "memory.high": memory_high, "memory.max": memory_max}
    try:
        cpu_finite = int(cpu_max.split()[0]) > 0
    except (ValueError, IndexError):
        cpu_finite = False
    memory_finite = False
    for value in (memory_high, memory_max):
        try:
            memory_finite |= int(value) > 0
        except ValueError:
            pass
    if not cpu_finite or not memory_finite:
        missing = " and ".join(name for name, finite in (
            ("finite cpu.max", cpu_finite), ("finite memory.high or memory.max", memory_finite)
        ) if not finite)
        return False, limits, f"execution cgroup is unbounded: missing {missing}"
    return True, limits, ""


def _process_cgroup_path(pid: int | str = "self", root: Path = Path("/sys/fs/cgroup")) -> Path | None:
    try:
        relative = next(line.split("::", 1)[1] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                        if line.startswith("0::"))
    except (OSError, StopIteration):
        return None
    return (root / relative.lstrip("/")).resolve()


def _private_runtime_dir() -> Path:
    """Return the per-user 0700 directory used for shared execution leases.

    ``/tmp`` itself is deliberately never a lock root: another user can replace a
    predictable entry there between ordinary path operations.  The fallback is a
    user-owned private child, while an explicitly supplied XDG directory must itself
    be private and owned by this uid.
    """
    uid = os.getuid()
    raw = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(raw) if raw else Path("/tmp")
    try:
        base_stat = base.lstat()
    except OSError as exc:
        raise RuntimeError(f"runtime directory is unavailable: {exc}") from exc
    if not base.is_dir() or base.is_symlink():
        raise RuntimeError("runtime directory is not a real directory")
    if raw:
        if base_stat.st_uid != uid:
            raise RuntimeError("XDG_RUNTIME_DIR is not a user-owned directory")
        if base_stat.st_mode & 0o077:
            raise RuntimeError("XDG_RUNTIME_DIR is not private (requires mode 0700)")
    elif base_stat.st_uid != 0 or not base_stat.st_mode & stat.S_ISVTX:
        raise RuntimeError("/tmp fallback is not a root-owned sticky directory")
    root = base / f"garden-{uid}"
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        root_stat = root.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot create private runtime directory: {exc}") from exc
    if root.is_symlink() or not root.is_dir() or root_stat.st_uid != uid or root_stat.st_mode & 0o077:
        raise RuntimeError("private runtime directory must be user-owned, non-symlink, and mode 0700")
    return root


def _safe_runtime_file(root: Path, name: str) -> IO[str]:
    """Open a regular user-owned 0600 runtime file without following a symlink."""
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(root, directory_flags)
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise RuntimeError(f"unsafe runtime file {name}: {exc}") from exc
    finally:
        if "directory_fd" in locals():
            os.close(directory_fd)
    file_stat = os.fstat(fd)
    if file_stat.st_uid != os.getuid() or not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise RuntimeError(f"unsafe runtime file {name}: not a user-owned regular file")
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "r+", encoding="utf-8")


def _read_limit(metadata: IO[str]) -> int | None:
    metadata.seek(0)
    try:
        payload = json.load(metadata)
        limit = int(payload["limit"])
        return limit if limit >= 0 and int(payload["uid"]) == os.getuid() else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _write_limit(metadata: IO[str], requested: int) -> None:
    metadata.seek(0)
    metadata.truncate()
    json.dump({"limit": requested, "uid": os.getuid()}, metadata)
    metadata.flush()
    os.fsync(metadata.fileno())


def _authoritative_limit(requested: int) -> tuple[int, str | None]:
    """Resolve one capacity for every garden using the verified runtime directory."""
    root = _private_runtime_dir()
    metadata_name = f"garden-heavy-test-{os.getuid()}-capacity.json"
    guard_name = f"garden-heavy-test-{os.getuid()}-capacity.lock"
    with _safe_runtime_file(root, guard_name) as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        with _safe_runtime_file(root, metadata_name) as metadata:
            existing = _read_limit(metadata)
            if existing is None:
                _write_limit(metadata, requested)
                return requested, None
            if existing == requested:
                return existing, None
            return existing, f"configured limit {requested} conflicts with authoritative limit {existing}"


def _set_execution_state(run_dir: Path, state: str) -> None:
    path = run_dir / "execution.json"
    try:
        status = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    status["state"] = state
    path.write_text(json.dumps(status))


def _execution_slot(run_dir: Path, should_stop: object, *, owner_scoped: bool = False) -> object:
    """Take one authoritative host-wide heavy-work lease, released by the kernel."""
    limit = int(os.environ.get("GARDEN_HEAVY_TEST_PARALLEL", "1"))
    owner_handle = None
    if owner_scoped:
        owner = os.environ.get("GARDEN_EXECUTION_OWNER", "")
        if not owner:
            (run_dir / "execution.json").write_text(json.dumps({
                "state": "unsupported", "limit": 1, "inherited": True, "pid": os.getpid(),
                "reason": "inherited execution lease has no owner identity",
            }))
            raise RuntimeError("inherited execution lease has no owner identity")
        lock_root = _private_runtime_dir()
        owner_key = hashlib.sha256(owner.encode()).hexdigest()[:20]
        while True:
            if should_stop():
                raise InterruptedError
            owner_handle = _safe_runtime_file(lock_root, f"garden-heavy-test-{os.getuid()}-owner-{owner_key}.lock")
            try:
                fcntl.flock(owner_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owner_handle.close()
                (run_dir / "execution.json").write_text(json.dumps({
                    "state": "waiting", "reason": "another validation in this run is active",
                    "limit": 1, "inherited": True, "owner": owner,
                }))
                time.sleep(0.1)
                continue
            break
    if limit <= 0:
        (run_dir / "execution.json").write_text(json.dumps({"state": "disabled", "limit": 0}))
        return None
    try:
        lock_root = _private_runtime_dir()
    except RuntimeError as exc:
        (run_dir / "execution.json").write_text(json.dumps({
            "state": "unsupported", "limit": 0, "requested_limit": limit, "reason": str(exc),
        }))
        raise
    while True:
        if should_stop():
            raise InterruptedError
        authoritative, conflict = _authoritative_limit(limit)
        for slot in range(authoritative):
            handle = _safe_runtime_file(lock_root, f"garden-heavy-test-{os.getuid()}-{slot}.lock")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            (run_dir / "execution.json").write_text(json.dumps(
                {"state": "running", "slot": slot, "limit": authoritative, "pid": os.getpid(),
                 "requested_limit": limit, "conflict": conflict, "owner_scoped": owner_scoped}
            ))
            return handle, owner_handle
        (run_dir / "execution.json").write_text(json.dumps(
            {"state": "waiting", "reason": conflict or f"heavy-test budget full (limit {authoritative})",
             "limit": authoritative, "requested_limit": limit, "conflict": conflict}
        ))
        time.sleep(0.1)


def _enter_execution_cgroup(run_dir: Path) -> None:
    configured = os.environ.get("GARDEN_EXECUTION_CGROUP", "")
    status = {"configured": bool(configured), "enforced": False}
    if configured:
        try:
            target = Path(configured).resolve()
            bounded, limits, reason = _finite_cgroup_limits(target)
            status.update({"path": str(target), "limits": limits})
            if not bounded:
                status["reason"] = reason
                (run_dir / "isolation.json").write_text(json.dumps(status))
                return
            (target / "cgroup.procs").write_text(str(os.getpid()))
            actual = _process_cgroup_path()
            if actual != target:
                status["reason"] = f"execution cgroup migration failed: process remains in {actual or 'unknown'}"
            else:
                status.update({"enforced": True, "actual_path": str(actual)})
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
    os.environ.setdefault("GARDEN_EXECUTION_OWNER", f"{os.getpid()}:{run_dir.resolve()}")
    os.environ.setdefault("GARDEN_EXECUTION_RUN_DIR", str(run_dir.resolve()))
    os.environ.setdefault("GARDEN_VALIDATION_RUNNER", sys.executable)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        _signal_descendants(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    try:
        slot = (_execution_slot(run_dir, lambda: stopping,
                                owner_scoped=os.environ.get("GARDEN_OWNER_SCOPED") == "1")
                if os.environ.get("GARDEN_HEAVY_EXECUTION") == "1" else None)
    except InterruptedError:
        (run_dir / "exit_code").write_text("143")
        return 143
    except RuntimeError as exc:
        (run_dir / "stderr.log").write_text(f"{exc}\n")
        (run_dir / "exit_code").write_text("1")
        return 1
    if (run_dir / "setup_input.json").exists() and slot is None:
        try:
            setup_slot = _execution_slot(run_dir, lambda: stopping)
        except InterruptedError:
            (run_dir / "exit_code").write_text("143")
            return 143
        except RuntimeError as exc:
            (run_dir / "stderr.log").write_text(f"{exc}\n")
            (run_dir / "exit_code").write_text("1")
            return 1
        setup_ok = _run_setup(run_dir)
        _set_execution_state(run_dir, "idle")
        del setup_slot
        if not setup_ok:
            return 1
    elif not _run_setup(run_dir):
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
    if slot is not None:
        _set_execution_state(run_dir, "finished")
    del slot  # keep the flock alive until every adopted descendant has exited
    return code


if __name__ == "__main__":
    raise SystemExit(main())
