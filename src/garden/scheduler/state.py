"""`.garden/state.json`: the per-task side-store, with dirty-key merging on save."""

from __future__ import annotations

import copy
import fcntl
import json
import os
from pathlib import Path
from typing import Any


class _TaskState(dict):
    """dict subclass that records which keys have actually changed since load.

    Tracks changed keys so State.save() can merge only those keys back to disk,
    letting two concurrent writers update different keys of the same task without
    losing each other's changes.

    Two mechanisms feed the change set:

    - explicit writes (`__setitem__`, `pop`, a `setdefault` that inserts) name the
      key directly; and
    - a mutable value (dict/list) handed out by `__getitem__` is snapshotted, so
      an in-place mutation of the nested object — which we cannot intercept — is
      caught by comparing the live value against its snapshot at save() time.

    Reading a key does *not* by itself mark it dirty: a read that leaves the value
    unchanged must never clobber a concurrent writer's update to that same key.
    """

    def __init__(self, data: dict) -> None:
        super().__init__(data)
        # Kept in the object's __dict__, not in the dict key-value store.
        # _written: keys named by an explicit write/pop/inserting setdefault.
        # _snapshots: key -> deep copy of a mutable value handed out by __getitem__,
        #   used to detect in-place mutation at save() time.
        object.__setattr__(self, "_written", set())
        object.__setattr__(self, "_snapshots", {})

    @property
    def _written_keys(self) -> set:
        return object.__getattribute__(self, "_written")

    @property
    def _snaps(self) -> dict:
        return object.__getattribute__(self, "_snapshots")

    @property
    def dirty(self) -> set:
        """Keys that save() would write: explicit writes plus any snapshotted
        mutable whose live value now differs from the snapshot taken on read."""
        changed = set(self._written_keys)
        snaps = self._snaps
        for key, snap in snaps.items():
            if key in self and dict.__getitem__(self, key) != snap:
                changed.add(key)
        return changed

    def __missing__(self, key: str) -> Any:
        """An unset key reads as None (a template's `state.foo` must see a real falsy
        value, not raise, under a strict Jinja environment)."""
        return None

    def __getitem__(self, key: str) -> Any:
        val = super().__getitem__(key)
        # Snapshot mutable values so save() can tell whether the caller mutated the
        # nested object in place: reading alone leaves the snapshot equal to the live
        # value, so a read no longer marks the key dirty and can't clobber a
        # concurrent writer's update to it.  Scalars need no snapshot.
        if isinstance(val, (dict, list)):
            snaps = self._snaps
            if key not in snaps:
                snaps[key] = copy.deepcopy(val)
        return val

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        """Like dict.get, but snapshots a mutable value the same way __getitem__ does, so an
        in-place mutation of a value read through st.get(key) is caught at save() time.
        dict.get bypasses __getitem__ (it reads the value at the C level), which would leave
        such a mutation invisible to save(); routing a present key back through __getitem__
        fixes that. A missing key returns `default` and is never snapshotted."""
        if key in self:
            return self[key]  # __getitem__ snapshots a mutable value
        return default

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self._written_keys.add(key)
        self._snaps.pop(key, None)

    def pop(self, key: str, *args: Any) -> Any:  # type: ignore[override]
        result = super().pop(key, *args)
        self._written_keys.add(key)
        self._snaps.pop(key, None)
        return result

    def setdefault(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key not in self:
            self[key] = default  # goes through __setitem__ → marks written
        return self[key]  # goes through __getitem__ → snapshots a mutable default

    def flushed(self, keys: set) -> None:
        """Reset change tracking for `keys` that save() has just written to disk, so
        a later save() re-writes only keys touched since now (and can't clobber a
        concurrent writer's newer update to a key we already flushed)."""
        written = self._written_keys
        snaps = self._snaps
        for key in keys:
            written.discard(key)
            if key in self:
                val = dict.__getitem__(self, key)
                if isinstance(val, (dict, list)):
                    snaps[key] = copy.deepcopy(val)
                else:
                    snaps.pop(key, None)
            else:
                snaps.pop(key, None)


class State:
    """Small JSON side-store for things that don't belong in task frontmatter.

    Concurrency guarantee: save() acquires an exclusive flock on a companion
    lock file, re-reads the on-disk state, and merges only the keys that this
    process actually changed on top of what is currently on disk.  Two concurrent
    writers that touch different keys of the same task will both survive, and a
    key that was only read — never mutated — is left alone so it can't clobber a
    concurrent writer's update to it. The new content is written to a temp file
    and moved into place with os.replace(), so a concurrent reader (e.g. __init__
    from another process, which does not take the lock) always sees either the
    old or the new file in full, never a truncated one.
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
            tid: dirty
            for tid, ts in self.data.items()
            if isinstance(ts, _TaskState) and (dirty := ts.dirty)
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
            # Reset change tracking now that these keys are safely on disk, so a later
            # save() only re-writes keys touched since this write and can't clobber a
            # concurrent writer's newer update to a key we already flushed.
            for tid, dirty_keys in dirty_by_tid.items():
                self.data[tid].flushed(dirty_keys)
