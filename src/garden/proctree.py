"""Observe and signal a local run's process tree, on Linux and on systems without ``/proc``.

Every platform difference in how the scheduler answers "is this run's work still running?"
and "which processes does this run own?" lives here, so its callers stay platform-neutral.

Two properties of the BSD process model drive the branches below:

* **An exited process that nobody has reaped still answers a kill probe.** ``kill(pid, 0)``
  succeeds for a zombie, and on Darwin ``killpg(pgid, 0)`` reports ``EPERM`` for a group
  whose only remaining member is a zombie. A kill probe alone therefore reports a finished
  run as permanently live, which stalls reaping and never lets a worktree be reused. Linux
  reads the state character from ``/proc``; elsewhere ``ps`` supplies the same answer.
* **There is no ``PR_SET_CHILD_SUBREAPER``.** A descendant that calls ``setsid`` and outlives
  its parent is reparented to init rather than to the run's supervisor, so no supervisor
  outside Linux can own it. What a supervisor can always do is find and signal every
  descendant still reachable through live parentage, which is what ``ps`` reports.

Linux keeps the repeated poll path on procfs. Systems without procfs use the process table
only for the lifecycle decisions that need it.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_PROC = Path("/proc")


@dataclass(frozen=True)
class ProcessInfo:
    """The process fields Garden needs, normalized across procfs and BSD/procps ``ps``."""

    pid: int
    ppid: int
    pgid: int
    state: str

    @property
    def exited(self) -> bool:
        return self.state.startswith("Z")


def _proc_root() -> Path | None:
    """The mounted ``/proc``, or None when this system does not publish one."""
    return _PROC if _PROC.is_dir() else None


def _ps_lines(*args: str) -> list[str] | None:
    """Non-empty lines of ``ps`` output, or None when ``ps`` could not answer at all.

    A selector matching no process exits non-zero with empty output; that is an answer
    ("no such process"), not a failure. A genuine failure also writes to stderr.
    """
    binary = next((path for path in ("/bin/ps", "/usr/bin/ps") if os.path.exists(path)), None)
    if binary is None:
        return None
    try:
        done = subprocess.run([binary, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 and done.stderr.strip():
        return None
    return [line for line in done.stdout.splitlines() if line.strip()]


def _ps_pid_live(pid: int) -> bool | None:
    """Whether ``ps`` sees *pid* as a process that has not exited; None when it cannot answer."""
    lines = _ps_lines("-o", "stat=", "-p", str(pid))
    if lines is None:
        return None
    return any(not line.strip().startswith("Z") for line in lines)


def _proc_processes(proc: Path) -> list[ProcessInfo] | None:
    """One procfs process-table snapshot, or None when procfs cannot be inspected."""
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    processes: list[ProcessInfo] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            # ``comm`` is parenthesized and may itself contain a closing parenthesis.
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            processes.append(ProcessInfo(
                pid=int(entry.name), state=fields[0], ppid=int(fields[1]), pgid=int(fields[2]),
            ))
        except (OSError, ValueError, IndexError):
            continue
    return processes


def _ps_processes() -> list[ProcessInfo] | None:
    """One process-table snapshot using options shared by BSD and procps ``ps``."""
    lines = _ps_lines("-A", "-o", "pid=,ppid=,pgid=,stat=")
    if lines is None:
        return None
    processes: list[ProcessInfo] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            processes.append(ProcessInfo(
                pid=int(fields[0]), ppid=int(fields[1]), pgid=int(fields[2]), state=fields[3],
            ))
        except ValueError:
            continue
    return processes


def process_snapshot() -> list[ProcessInfo]:
    """Processes visible to this user, with procfs preferred and ``ps`` as the fallback."""
    proc = _proc_root()
    processes = _proc_processes(proc) if proc is not None else None
    if processes is None:
        processes = _ps_processes()
    return processes or []


def _ps_group_live(pgid: int) -> bool | None:
    """Whether ``ps`` sees a non-exited member of process group *pgid*; None when it cannot.

    Every process is listed and filtered here rather than selected with ``ps -g``, whose
    meaning is not the same on BSD (process group) as it is with procps (session).
    """
    processes = _ps_processes()
    if processes is None:
        return None
    return any(process.pgid == pgid and not process.exited for process in processes)


def pid_alive(pid: int) -> bool:
    """Whether *pid* is a process that has not yet exited.

    An unknown answer counts as alive: the callers use this to decide whether a worktree is
    still in use, where guessing "gone" is the harmful direction.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Another user's process, which cannot be this run's own zombie.
    proc = _proc_root()
    if proc is not None:
        try:
            state = (proc / str(pid) / "stat").read_text().split(")")[-1].split()[0]
            return state != "Z"
        except OSError:
            return True
    live = _ps_pid_live(pid)
    return True if live is None else live


def process_group_alive(pgid: int) -> bool:
    """Whether any process that has not yet exited remains in process group *pgid*."""
    proc = _proc_root()
    if proc is not None:
        processes = _proc_processes(proc)
        if processes is not None:
            return any(process.pgid == pgid and not process.exited for process in processes)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False  # No such group: every member has exited and been reaped.
    except PermissionError:
        pass
    # BSD kernels may report either success or EPERM for a group whose only members are
    # unreaped zombies. In both cases only the process-state snapshot settles liveness.
    live = _ps_group_live(pgid)
    return True if live is None else live


def _proc_children(pid: int, proc: Path) -> list[int]:
    """Direct children of *pid* as Linux publishes them."""
    try:
        text = (proc / str(pid) / "task" / str(pid) / "children").read_text()
        return [int(value) for value in text.split()]
    except (OSError, ValueError):
        return []


def _ps_children() -> dict[int, list[int]]:
    """A parent-pid to direct-children map covering every process ``ps`` can see."""
    tree: dict[int, list[int]] = {}
    for process in _ps_processes() or []:
        tree.setdefault(process.ppid, []).append(process.pid)
    return tree


def direct_children(pid: int) -> list[int]:
    """Direct children of *pid* from procfs or a portable process-table snapshot."""
    proc = _proc_root()
    if proc is not None:
        return _proc_children(pid, proc)
    return _ps_children().get(pid, [])


def descendants(pid: int) -> list[int]:
    """Every process below *pid* in the live parentage tree, shallowest first.

    Read once per call: the tree is a snapshot either way, and a caller that signals the
    result walks it in reverse so a parent is never signalled before its own children.
    """
    proc = _proc_root()
    if proc is None:
        snapshot = _ps_children()

        def children(parent: int) -> list[int]:
            return snapshot.get(parent, [])
    else:
        def children(parent: int) -> list[int]:
            return _proc_children(parent, proc)

    return _walk(pid, children)


def _walk(pid: int, children: Callable[[int], list[int]]) -> list[int]:
    found: list[int] = []
    seen = {pid}
    pending = list(children(pid))
    while pending:
        child = pending.pop(0)
        if child in seen:
            continue  # A reparented or recycled pid must not turn the walk into a loop.
        seen.add(child)
        found.append(child)
        pending.extend(children(child))
    return found
