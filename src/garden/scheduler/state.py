"""`.garden/state.json`: the per-task side-store, with dirty-key merging on save."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any


class _TaskState(dict):
    """dict subclass that records which keys have been written since creation.

    Tracks dirty keys so State.save() can merge only changed keys back to disk,
    letting two concurrent writers update different keys of the same task without
    losing each other's changes.
    """

    def __init__(self, data: dict) -> None:
        super().__init__(data)
        # Store _dirty in the object's __dict__, not in the dict key-value store.
        object.__setattr__(self, "_dirty", set())

    @property
    def dirty(self) -> set:
        return object.__getattribute__(self, "_dirty")

    def __getitem__(self, key: str) -> Any:
        val = super().__getitem__(key)
        # Mark mutable values (dict/list) as dirty immediately: the caller is
        # likely to mutate the nested object in-place, and we have no way to
        # intercept those mutations.  Scalar reads are harmless to leave clean.
        if isinstance(val, (dict, list)):
            self.dirty.add(key)
        return val

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self.dirty.add(key)

    def pop(self, key: str, *args: Any) -> Any:  # type: ignore[override]
        result = super().pop(key, *args)
        self.dirty.add(key)
        return result

    def setdefault(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key not in self:
            self[key] = default  # goes through __setitem__ → marks dirty
        else:
            # Key already present; caller may mutate the value in place (e.g. list.append).
            # Mark it dirty so save() picks up in-place mutations.
            self.dirty.add(key)
        return self[key]


class State:
    """Small JSON side-store for things that don't belong in task frontmatter.

    Concurrency guarantee: save() acquires an exclusive flock on a companion
    lock file, re-reads the on-disk state, and merges only the keys that this
    process actually wrote on top of what is currently on disk.  Two concurrent
    writers that touch different keys of the same task will both survive. The
    new content is written to a temp file and moved into place with os.replace(),
    so a concurrent reader (e.g. __init__ from another process, which does not
    take the lock) always sees either the old or the new file in full, never a
    truncated one.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, _TaskState] = {}
        if path.exists():
            try:
                raw: dict[str, Any] = json.loads(path.read_text())
                self.data = {
                    k: _TaskState(v) if isinstance(v, dict) else v
                    for k, v in raw.items()
                }
            except json.JSONDecodeError:
                self.data = {}

    def get(self, task_id: str) -> _TaskState:
        existing = self.data.get(task_id)
        if existing is None:
            ts = _TaskState({})
            self.data[task_id] = ts
            return ts
        if not isinstance(existing, _TaskState):
            ts = _TaskState(existing)
            self.data[task_id] = ts
            return ts
        return existing

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        dirty_by_tid: dict[str, set] = {
            tid: ts.dirty
            for tid, ts in self.data.items()
            if isinstance(ts, _TaskState) and ts.dirty
        }
        if not dirty_by_tid:
            return
        lock_path = self.path.parent / (self.path.name + ".lock")
        with open(lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            disk: dict[str, Any] = {}
            if self.path.exists():
                try:
                    disk = json.loads(self.path.read_text())
                except json.JSONDecodeError:
                    disk = {}
            for tid, dirty_keys in dirty_by_tid.items():
                task_disk = disk.setdefault(tid, {})
                task_mem = self.data[tid]
                for key in dirty_keys:
                    if key in task_mem:
                        task_disk[key] = task_mem[key]
                    else:
                        task_disk.pop(key, None)
            tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(disk, indent=2, sort_keys=True))
            os.replace(tmp_path, self.path)
            # Clear dirty keys now that they are safely on disk, so a later save() only
            # re-writes keys touched since this write and can't clobber a concurrent
            # writer's newer update to a key we already flushed.
            for dirty_keys in dirty_by_tid.values():
                dirty_keys.clear()
