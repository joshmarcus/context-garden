"""Own one local run's process group and every descendant still attributable to it."""

from __future__ import annotations

import codecs
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
import threading
import time
from pathlib import Path
from typing import IO

from .proctree import descendants, direct_children, process_group_alive


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
    requested = Path(raw) if raw else Path("/tmp")
    try:
        requested_stat = requested.lstat()
    except OSError as exc:
        raise RuntimeError(f"runtime directory is unavailable: {exc}") from exc
    if raw:
        base = requested
        if not base.is_dir() or base.is_symlink():
            raise RuntimeError("runtime directory is not a real directory")
        if requested_stat.st_uid != uid:
            raise RuntimeError("XDG_RUNTIME_DIR is not a user-owned directory")
        if requested_stat.st_mode & 0o077:
            raise RuntimeError("XDG_RUNTIME_DIR is not private (requires mode 0700)")
    else:
        # Darwin exposes /tmp as a root-owned system symlink to /private/tmp. Following that
        # one trusted link preserves the same root-owned sticky-directory boundary Linux uses;
        # a symlink writable by this user remains forbidden.
        if requested.is_symlink():
            if requested_stat.st_uid != 0:
                raise RuntimeError("/tmp fallback symlink is not root-owned")
            try:
                base = requested.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(f"runtime directory is unavailable: {exc}") from exc
            try:
                base_stat = base.stat()
            except OSError as exc:
                raise RuntimeError(f"runtime directory is unavailable: {exc}") from exc
        else:
            base = requested
            base_stat = requested_stat
        if not base.is_dir() or base.is_symlink():
            raise RuntimeError("runtime directory is not a real directory")
        if base_stat.st_uid != 0 or not base_stat.st_mode & stat.S_ISVTX:
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
    def open_once() -> int:
        directory_fd = os.open(root, directory_flags)
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    # Darwin can transiently return ENOENT when two processes race to create the same
    # O_NOFOLLOW file through openat. Retry only that result; a symlink, foreign owner or
    # other unsafe condition still fails closed on the first observation.
    for attempt in range(3):
        try:
            fd = open_once()
            break
        except FileNotFoundError as exc:
            if attempt == 2:
                raise RuntimeError(f"unsafe runtime file {name}: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"unsafe runtime file {name}: {exc}") from exc
    else:  # pragma: no cover - the bounded loop either opens or raises above.
        raise RuntimeError(f"unsafe runtime file {name}: unavailable")
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


def reset_authoritative_limit(requested: int) -> tuple[int, int]:
    """Replace the shared capacity only when every lease in its namespace is idle.

    The capacity guard prevents a new supervisor from resolving or claiming a slot while
    the old slot set is inspected and the metadata is replaced.  Holding every old slot
    proves that changing the authority cannot reinterpret an active garden's lease.
    """
    if requested < 0:
        raise ValueError("heavy validation capacity must be zero or greater")
    root = _private_runtime_dir()
    metadata_name = f"garden-heavy-test-{os.getuid()}-capacity.json"
    guard_name = f"garden-heavy-test-{os.getuid()}-capacity.lock"
    with _safe_runtime_file(root, guard_name) as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        with _safe_runtime_file(root, metadata_name) as metadata:
            existing = _read_limit(metadata)
            if existing is None:
                raise RuntimeError("shared heavy-validation capacity metadata is invalid")
            if existing == requested:
                return existing, requested
            held: list[IO[str]] = []
            try:
                for slot in range(existing):
                    handle = _safe_runtime_file(
                        root, f"garden-heavy-test-{os.getuid()}-{slot}.lock"
                    )
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        handle.close()
                        raise RuntimeError(
                            "shared heavy-validation capacity is active; wait for every "
                            "garden in this user/runtime namespace to become idle"
                        ) from exc
                    held.append(handle)
                _write_limit(metadata, requested)
            finally:
                for handle in held:
                    handle.close()
    return existing, requested


def _claim_authoritative_slot(requested: int, root: Path) -> tuple[int, str | None, int | None, IO[str] | None]:
    """Resolve capacity and claim a slot under the same namespace guard."""
    metadata_name = f"garden-heavy-test-{os.getuid()}-capacity.json"
    guard_name = f"garden-heavy-test-{os.getuid()}-capacity.lock"
    with _safe_runtime_file(root, guard_name) as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        with _safe_runtime_file(root, metadata_name) as metadata:
            authoritative = _read_limit(metadata)
            if authoritative is None:
                _write_limit(metadata, requested)
                authoritative = requested
            conflict = None if authoritative == requested else (
                f"configured limit {requested} conflicts with authoritative limit {authoritative}"
            )
        for slot in range(authoritative):
            handle = _safe_runtime_file(root, f"garden-heavy-test-{os.getuid()}-{slot}.lock")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            return authoritative, conflict, slot, handle
    return authoritative, conflict, None, None


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
        authoritative, conflict, slot, handle = _claim_authoritative_slot(limit, lock_root)
        if handle is not None and slot is not None:
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
    # Elsewhere there is no equivalent: a descendant that calls setsid and outlives its
    # parent is reparented to init and leaves this run's ownership. Every descendant that
    # is still reachable through live parentage is signalled through proctree.descendants.


def _signal_descendants(sig: int) -> None:
    """Signal this supervisor's whole descendant tree, deepest first, never itself."""
    for pid in reversed(descendants(os.getpid())):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _adopted_children() -> list[int]:
    """Direct children this supervisor has adopted as a subreaper.

    Only Linux reparents orphans here, and only Linux publishes the list; on every other
    platform this supervisor's sole child is the run leader, which ``Popen`` reaps.
    """
    # Only Linux enabled subreaper ownership above. On systems without it, asking ``ps``
    # for our children would observe the short-lived ``ps`` probe itself and could make the
    # drain loop self-sustaining.
    return direct_children(os.getpid()) if sys.platform.startswith("linux") else []


def _reap_exited_children(*, excluding: int | None = None) -> None:
    """Reap exited children owned by this supervisor, except the run leader.

    Targeting current direct children individually avoids consuming the leader's exit
    status between ``Popen.poll`` calls.  Orphaned descendants become direct children
    of this subreaper, so this reaps only processes this run owns.
    """
    for pid in _adopted_children():
        if pid == excluding:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _signal_owned_processes(leader_pgid: int | None, sig: int) -> None:
    """Signal the run leader's group plus descendants that created another session."""
    # Snapshot and signal session-escaping descendants while their live parentage still
    # identifies them. Killing the leader group first would reparent them on Darwin and lose
    # the only portable ownership link before they received the signal.
    _signal_descendants(sig)
    if leader_pgid is not None:
        try:
            os.killpg(leader_pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _execution_timeout_seconds() -> float | None:
    """Return this supervisor's hard execution budget, when explicitly requested.

    Local workers, probes, detached checks and ``garden.validation`` all translate their
    configured budget into this private supervisor input, avoiding a dependency on a
    platform-specific shell utility.
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
    """Return private descriptors that must reach their intended workload process."""
    descriptors = []
    configured = os.environ.get("GARDEN_PRESERVE_FDS", "").split(",")
    # An OpenRouter credential is consumed and closed by its adapter.  Passing the
    # descriptor through the supervisor avoids ever materialising the secret in an
    # ancestor environment; the adapter closes it before starting Codex.
    configured.append(os.environ.get("GARDEN_HARNESS_API_KEY_FD", ""))
    for raw in configured:
        if not raw.strip():
            continue
        try:
            descriptor = int(raw)
            os.fstat(descriptor)
        except (ValueError, OSError):
            continue
        descriptors.append(descriptor)
    return tuple(dict.fromkeys(descriptors))


def _execution_timeout_kind() -> str:
    kind = os.environ.get("GARDEN_EXECUTION_TIMEOUT_KIND", "validation").strip().lower()
    return kind if kind in {"validation", "worker", "probe"} else "execution"


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
        "timeout_kind": _execution_timeout_kind(),
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
    timeout_kind = _execution_timeout_kind()
    result = {
        "kind": f"{timeout_kind}_execution_timeout",
        "timeout_seconds": timeout_seconds,
        "execution_started_at": started_at,
        "timed_out_at": timed_out_at,
        "pid": os.getpid(),
        "exit_code": exit_code,
        "reason": f"{timeout_kind} execution exceeded {timeout_seconds:g} seconds",
    }
    receipt = "validation_timeout.json" if timeout_kind == "validation" else "execution_timeout.json"
    (run_dir / receipt).write_text(json.dumps(result, indent=2))
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


def setup_environment() -> dict[str, str]:
    """Exclude worker-bound authority from the setup subprocess."""
    env = dict(os.environ)
    names = env.pop("GARDEN_WORKLOAD_IDENTITY_BINDINGS", "").split(",")
    for name in names:
        if name:
            env.pop(name, None)
    return env


def redact_authority_outputs(run_dir: Path) -> None:
    """Scrub worker-bound credential values before publishing completion.

    The supervisor is the last process allowed to retain the injected environment.  It
    rewrites every harness-owned run record before ``exit_code`` makes the run collectable.
    """
    names = os.environ.get("GARDEN_WORKLOAD_IDENTITY_BINDINGS", "").split(",")
    values = sorted(
        {os.environ.get(name, "") for name in names if name}, key=len, reverse=True,
    )
    values = [value for value in values if value]
    if not values:
        return
    for name in ("stdout.json", "stderr.log", "final.md"):
        path = run_dir / name
        try:
            text = path.read_text()
        except OSError:
            continue
        for value in values:
            text = text.replace(value, "<redacted>")
        path.write_text(text)


def _supervised_authority():
    """Recreate a local operation boundary inside the detached supervisor."""
    from garden.workload_identity import subprocess_authority

    raw = os.environ.pop("GARDEN_WORKLOAD_IDENTITY_CONFIG", "")
    target = os.environ.pop("GARDEN_WORKLOAD_IDENTITY_TARGET", "")
    run_identity = os.environ.pop("GARDEN_WORKLOAD_IDENTITY_RUN", "")
    if not raw:
        return subprocess_authority({}, target, run_identity, os.environ)
    return subprocess_authority(json.loads(raw), target, run_identity, os.environ)


def _pump_output(source: IO[str], destination: Path, redactor, mirror: IO[str]) -> None:
    """Persist only redacted stream output, retaining split-match tails in memory."""
    stream = redactor.stream()
    with destination.open("w") as output:
        # TextIO.read(size) may wait for all ``size`` characters on a pipe. A single
        # character keeps live views moving while the stream redactor retains only the
        # bounded suffix needed to recognize a credential split across writes.
        while chunk := source.read(1):
            safe = stream.feed(chunk)
            if safe:
                output.write(safe)
                output.flush()
                mirror.write(safe)
                mirror.flush()
        safe = stream.finish()
        if safe:
            output.write(safe)
            output.flush()
            mirror.write(safe)
            mirror.flush()


class _FinalOutput:
    """Drain a harness final path without any blocking open or reader thread."""

    def __init__(self, raw_path: Path, destination: Path, redactor) -> None:
        self.raw_path = raw_path
        self.destination = destination
        self.redactor = redactor
        self.stream = redactor.stream()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.fd = os.open(raw_path, os.O_RDONLY | os.O_NONBLOCK)
        self.fifo_stat = os.fstat(self.fd)
        self.output = destination.open("w")

    def _write(self, text: str) -> None:
        safe = self.stream.feed(text)
        if safe:
            self.output.write(safe)
            self.output.flush()
            sys.stdout.write(safe)
            sys.stdout.flush()

    def drain(self) -> None:
        """Consume all bytes currently available from the original FIFO inode."""
        while True:
            try:
                chunk = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                return
            if not chunk:
                return
            self._write(self.decoder.decode(chunk))

    def finish(self) -> None:
        """Drain the FIFO or an atomic replacement, then finish redaction once."""
        self.drain()
        try:
            current = self.raw_path.lstat()
        except OSError:
            current = None
        if current is not None and (
            current.st_dev != self.fifo_stat.st_dev or current.st_ino != self.fifo_stat.st_ino
        ):
            # A harness such as Codex may atomically replace its requested output path.
            # The workload is contained and gone now, so this regular-file read cannot
            # race a writer.  It still passes through the authority redactor before the
            # durable result is published.
            if current.st_uid != os.getuid() or not stat.S_ISREG(current.st_mode):
                raise OSError("replacement final output is not a user-owned regular file")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            replacement_fd = os.open(self.raw_path, flags)
            with os.fdopen(replacement_fd, "rb") as replacement:
                while chunk := replacement.read(64 * 1024):
                    self._write(self.decoder.decode(chunk))
        self._write(self.decoder.decode(b"", final=True))
        safe = self.stream.finish()
        if safe:
            self.output.write(safe)
            sys.stdout.write(safe)
        self.output.flush()
        sys.stdout.flush()
        self.output.close()
        os.close(self.fd)
        self.raw_path.unlink(missing_ok=True)


def _run_setup(run_dir: Path) -> bool:
    payload = run_dir / "setup_input.json"
    if not payload.exists():
        return True
    from garden.runner.base import RunnerError, run_setup

    try:
        data = json.loads(payload.read_text())
        # Older durable run records contain the setup mapping directly.
        setup = data.get("setup", data)
        config = data.get("config", {}) if "setup" in data else {}
        run_setup(Path.cwd(), setup, log_path=run_dir / "setup.log",
                  env=setup_environment(), config=config)
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
    child: subprocess.Popen[bytes] | None = None

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        _signal_owned_processes(child.pid if child is not None else None, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    gate_fd_text = os.environ.pop("GARDEN_LAUNCH_GATE_FD", "")
    if gate_fd_text:
        try:
            gate_fd = int(gate_fd_text)
            released = os.read(gate_fd, 1) == b"1"
            os.close(gate_fd)
        except (OSError, ValueError):
            released = False
        if not released:
            (run_dir / "stderr.log").write_text(
                "claim launch cancelled before durable recovery handoff\n"
            )
            (run_dir / "exit_code").write_text("1")
            return 1
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
    authority_operation = None
    resolved_authority = None
    try:
        authority_operation = _supervised_authority()
        (child_env, metadata, authority_redactor,
         resolved_authority) = authority_operation.__enter__()
        if metadata is not None:
            (run_dir / "workload_identity.json").write_text(json.dumps(metadata.__dict__))
    except Exception as exc:
        from garden.workload_identity import WorkloadIdentityError

        if not isinstance(exc, WorkloadIdentityError):
            raise
        (run_dir / "identity_error.json").write_text(json.dumps({"error": str(exc)}))
        (run_dir / "exit_code").write_text("1")
        return 1
    raw_final_value = os.environ.get("GARDEN_RAW_FINAL_PATH", "")
    final_path_value = os.environ.get("GARDEN_FINAL_PATH", "")
    raw_final = Path(raw_final_value)
    final_path = Path(final_path_value)
    final_output = None
    if raw_final_value and final_path_value:
        raw_final.unlink(missing_ok=True)
        os.mkfifo(raw_final, 0o600)

        final_output = _FinalOutput(raw_final, final_path, authority_redactor)
    # Keep the workload in a group separate from the supervisor. The supervisor can then
    # signal and observe that whole group after its shell leader exits, on both Linux and
    # Darwin, without signalling itself. Linux's subreaper additionally retains children
    # that deliberately create another session; other POSIX kernels provide no equivalent.
    # Install the final-output FIFO and its reader first: a fast harness must never create a
    # raw regular file before the redaction boundary is ready.
    child = subprocess.Popen(
        ["sh", "-c", script],
        pass_fds=_preserved_child_fds(),
        start_new_session=True,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None and child.stderr is not None
    stdout_thread = threading.Thread(
        target=_pump_output,
        args=(child.stdout, run_dir / "stdout.json", authority_redactor, sys.stdout),
    )
    stderr_thread = threading.Thread(
        target=_pump_output,
        args=(child.stderr, run_dir / "stderr.log", authority_redactor, sys.stderr),
    )
    stdout_thread.start()
    stderr_thread.start()
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
                _signal_owned_processes(child.pid, signal.SIGTERM)
            kill_deadline = now + 5.0
        elif timed_out and kill_deadline is not None and now >= kill_deadline:
            _signal_owned_processes(child.pid, signal.SIGKILL)

    while (code := child.poll()) is None:
        if final_output is not None:
            final_output.drain()
        _reap_exited_children(excluding=child.pid)
        if resolved_authority is not None:
            try:
                resolved_authority.enforce_current()
            except Exception as exc:
                from garden.workload_identity import WorkloadIdentityError

                if not isinstance(exc, WorkloadIdentityError):
                    raise
                (run_dir / "identity_error.json").write_text(json.dumps({"error": str(exc)}))
                _signal_owned_processes(child.pid, signal.SIGTERM)
                stopping = True
        if stopping:
            kill_deadline = kill_deadline or time.monotonic() + 5.0
            if time.monotonic() >= kill_deadline:
                _signal_owned_processes(child.pid, signal.SIGKILL)
        enforce_deadline()
        time.sleep(0.05)
    deadline = time.monotonic() + 5.0 if stopping else None
    while process_group_alive(child.pid) or _adopted_children():
        if final_output is not None:
            final_output.drain()
        _reap_exited_children()
        if deadline is not None and time.monotonic() >= deadline:
            _signal_owned_processes(child.pid, signal.SIGKILL)
        enforce_deadline()
        time.sleep(0.05)
    if timed_out:
        code = 124
    stdout_thread.join()
    stderr_thread.join()
    if final_output is not None:
        try:
            final_output.finish()
        except OSError as exc:
            # Keep an atomic replacement in place as evidence when collection itself
            # fails.  The real workload return code remains the run outcome.
            try:
                with (run_dir / "stderr.log").open("a") as error_log:
                    error_log.write(f"could not collect final output: {exc}\n")
            except OSError:
                pass
    if authority_operation is not None:
        authority_operation.__exit__(None, None, None)
    (run_dir / "exit_code").write_text(str(code))
    if slot is not None and not timed_out:
        _set_execution_state(run_dir, "finished")
    del slot  # keep the flock alive until every adopted descendant has exited
    return code


if __name__ == "__main__":
    raise SystemExit(main())
