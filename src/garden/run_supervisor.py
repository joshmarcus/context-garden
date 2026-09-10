"""Own every descendant of one local run, including children that call ``setsid``."""

from __future__ import annotations

import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import math
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
    _write_execution_state(run_dir, status)


def _write_execution_state(run_dir: Path, status: dict[str, object]) -> None:
    """Publish execution state as one complete JSON document.

    The scheduler and web surfaces read this small status file while a supervisor is
    running.  Replacing a sibling temporary file prevents them from observing the
    empty interval created by an in-place truncate-and-write.
    """
    path = run_dir / "execution.json"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(status))
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _recorded_waiting_since(run_dir: Path) -> str | None:
    try:
        previous = json.loads((run_dir / "execution.json").read_text())
    except (OSError, json.JSONDecodeError):
        previous = {}
    recorded = previous.get("waiting_since") if previous.get("state") == "waiting" else None
    if isinstance(recorded, str) and recorded:
        return recorded
    return None


def _waiting_since(run_dir: Path) -> str:
    """Keep one admission-clock origin while a supervisor remains waiting."""
    return _recorded_waiting_since(run_dir) or dt.datetime.now(dt.UTC).isoformat()


def _execution_slot(run_dir: Path, should_stop: object, *, owner_scoped: bool = False) -> object:
    """Take one authoritative host-wide heavy-work lease, released by the kernel."""
    limit = int(os.environ.get("GARDEN_HEAVY_TEST_PARALLEL", "1"))
    owner_handle = None
    if owner_scoped:
        owner = os.environ.get("GARDEN_EXECUTION_OWNER", "")
        if not owner:
            _write_execution_state(run_dir, {
                "state": "unsupported", "limit": 1, "inherited": True, "pid": os.getpid(),
                "reason": "inherited execution lease has no owner identity",
            })
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
                _write_execution_state(run_dir, {
                    "state": "waiting", "reason": "another validation in this run is active",
                    "limit": 1, "inherited": True, "owner": owner,
                    "waiting_since": _waiting_since(run_dir),
                })
                time.sleep(0.1)
                continue
            break
    if limit <= 0:
        _write_execution_state(run_dir, {"state": "disabled", "limit": 0})
        return None
    try:
        lock_root = _private_runtime_dir()
    except RuntimeError as exc:
        _write_execution_state(run_dir, {
            "state": "unsupported", "limit": 0, "requested_limit": limit, "reason": str(exc),
        })
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
            waiting_since = _recorded_waiting_since(run_dir)
            running = {
                "state": "running", "slot": slot, "limit": authoritative, "pid": os.getpid(),
                "requested_limit": limit, "conflict": conflict, "owner_scoped": owner_scoped,
            }
            if waiting_since:
                running.update({
                    "admission_wait_started_at": waiting_since,
                    "admitted_at": dt.datetime.now(dt.UTC).isoformat(),
                })
            _write_execution_state(run_dir, running)
            return handle, owner_handle
        _write_execution_state(run_dir, {
            "state": "waiting", "reason": conflict or f"heavy-test budget full (limit {authoritative})",
            "limit": authoritative, "requested_limit": limit, "conflict": conflict,
            "waiting_since": _waiting_since(run_dir),
        })
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


def _reap_exited_children(*, excluding: int | None = None) -> None:
    """Reap exited children owned by this supervisor, except the run leader.

    Targeting current direct children individually avoids consuming the leader's exit
    status between ``Popen.poll`` calls.  Orphaned descendants become direct children
    of this subreaper, so this reaps only processes this run owns.
    """
    for pid in _children(os.getpid()):
        if pid == excluding:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _execution_timeout_seconds() -> float | None:
    """Return this supervisor's hard execution budget, when explicitly requested.

    Ordinary worker supervisors retain their task-level timeout. ``garden.validation`` and
    detached check launchers translate the configured validation budget into this private
    supervisor input; model-session launchers do not.
    """
    raw = os.environ.get("GARDEN_EXECUTION_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise RuntimeError("execution timeout must be a positive number of seconds") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise RuntimeError("execution timeout must be a positive number of seconds")
    return seconds


def _preserved_child_fds() -> tuple[int, ...]:
    """Return worker-owned lock descriptors that must follow the author process tree."""
    descriptors = []
    for raw in os.environ.get("GARDEN_PRESERVE_FDS", "").split(","):
        if not raw.strip():
            continue
        try:
            descriptor = int(raw)
            os.fstat(descriptor)
        except (ValueError, OSError):
            continue
        descriptors.append(descriptor)
    return tuple(dict.fromkeys(descriptors))


def _mark_execution_started(run_dir: Path, timeout_seconds: float | None) -> tuple[float, str]:
    """Publish one fixed execution-clock origin after admission has completed."""
    started_monotonic = time.monotonic()
    started_at = dt.datetime.now(dt.UTC).isoformat()
    if timeout_seconds is None:
        return started_monotonic, started_at
    try:
        status = json.loads((run_dir / "execution.json").read_text())
    except (OSError, json.JSONDecodeError):
        status = {"state": "running", "pid": os.getpid()}
    status.update({
        "execution_started_at": started_at,
        "timeout_seconds": timeout_seconds,
        "deadline_at": (
            dt.datetime.fromisoformat(started_at) + dt.timedelta(seconds=timeout_seconds)
        ).isoformat(),
        "owner": os.environ["GARDEN_EXECUTION_OWNER"],
    })
    if os.environ.get("GARDEN_VALIDATION_INHERITS_LEASE") == "1":
        status["inherited_lease"] = True
    _write_execution_state(run_dir, status)
    return started_monotonic, started_at


def _record_execution_timeout(
    run_dir: Path, timeout_seconds: float, started_at: str, *, exit_code: int = 124
) -> None:
    """Persist a timeout result before signalling this supervisor's descendants."""
    timed_out_at = dt.datetime.now(dt.UTC).isoformat()
    result = {
        "kind": "validation_execution_timeout",
        "timeout_seconds": timeout_seconds,
        "execution_started_at": started_at,
        "timed_out_at": timed_out_at,
        "pid": os.getpid(),
        "exit_code": exit_code,
        "reason": f"validation execution exceeded {timeout_seconds:g} seconds",
    }
    (run_dir / "validation_timeout.json").write_text(json.dumps(result, indent=2))
    try:
        status = json.loads((run_dir / "execution.json").read_text())
    except (OSError, json.JSONDecodeError):
        status = {}
    status.update({
        "state": "timeout",
        "execution_started_at": started_at,
        "timeout_seconds": timeout_seconds,
        "timed_out_at": timed_out_at,
        "reason": result["reason"],
        "exit_code": exit_code,
    })
    _write_execution_state(run_dir, status)
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
    try:
        timeout_seconds = _execution_timeout_seconds()
    except RuntimeError as exc:
        (run_dir / "stderr.log").write_text(f"{exc}\n")
        (run_dir / "exit_code").write_text("2")
        del slot
        return 2
    execution_started, execution_started_at = _mark_execution_started(run_dir, timeout_seconds)
    execution_deadline = execution_started + timeout_seconds if timeout_seconds is not None else None
    child = subprocess.Popen(["sh", "-c", script], pass_fds=_preserved_child_fds())
    kill_deadline = None
    timed_out = False

    def enforce_deadline() -> None:
        nonlocal kill_deadline, timed_out
        now = time.monotonic()
        if not timed_out and execution_deadline is not None and now >= execution_deadline:
            timed_out = True
            assert timeout_seconds is not None
            try:
                _record_execution_timeout(run_dir, timeout_seconds, execution_started_at)
            except OSError as exc:
                # A full or disappearing result filesystem must not leave the owned workload
                # running beyond its budget. Preserve the metadata failure when possible.
                try:
                    with (run_dir / "stderr.log").open("a") as error_log:
                        error_log.write(f"could not record validation timeout: {exc}\n")
                except OSError:
                    pass
            finally:
                _signal_descendants(signal.SIGTERM)
            kill_deadline = now + 5.0
        elif timed_out and kill_deadline is not None and now >= kill_deadline:
            _signal_descendants(signal.SIGKILL)

    while (code := child.poll()) is None:
        _reap_exited_children(excluding=child.pid)
        if stopping:
            kill_deadline = kill_deadline or time.monotonic() + 5.0
            if time.monotonic() >= kill_deadline:
                _signal_descendants(signal.SIGKILL)
        enforce_deadline()
        time.sleep(0.05)
    deadline = time.monotonic() + 5.0 if stopping else None
    while True:
        try:
            waited, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if waited == 0:
            if deadline is not None and time.monotonic() >= deadline:
                _signal_descendants(signal.SIGKILL)
            enforce_deadline()
            time.sleep(0.05)
    if timed_out:
        code = 124
    (run_dir / "exit_code").write_text(str(code))
    if slot is not None and not timed_out:
        _set_execution_state(run_dir, "finished")
    del slot  # keep the flock alive until every adopted descendant has exited
    return code


if __name__ == "__main__":
    raise SystemExit(main())
