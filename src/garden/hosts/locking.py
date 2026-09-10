"""Small process and thread lock for durable host-operation files."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_threads: dict[str, threading.Lock] = {}
_guard = threading.Lock()


@contextmanager
def file_lock(path: Path, *, timeout: float = 60):
    """Serialize controllers on POSIX (including macOS/WSL) and native Windows.

    Lock acquisition is bounded; an interrupted owner releases its OS lock. Keeping the
    lock file avoids replacing the inode while another controller is waiting on it.
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _guard:
        thread_lock = _threads.setdefault(str(path), threading.Lock())
    if not thread_lock.acquire(timeout=timeout):
        raise TimeoutError("another controller is updating this host operation")
    try:
        with path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()

                def acquire():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

                def release():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def acquire():
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

                def release():
                    fcntl.flock(handle, fcntl.LOCK_UN)

            deadline = time.monotonic() + timeout
            while True:
                try:
                    acquire()
                    break
                except (BlockingIOError, PermissionError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("another controller is updating this host operation") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                release()
    finally:
        thread_lock.release()
