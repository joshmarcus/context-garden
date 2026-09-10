"""`.garden/state.json`: the per-task side-store, with dirty-key merging on save."""

from __future__ import annotations

import copy
import fcntl
import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StateCorruptionError(RuntimeError):
    """The durable scheduler side-store cannot be safely interpreted."""


def _read_state(path: Path) -> dict[str, Any]:
    """Read an existing state file, refusing to reinterpret corrupt bytes as no state."""
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        location = ""
        if isinstance(exc, json.JSONDecodeError):
            location = f" at line {exc.lineno} column {exc.colno}"
        raise StateCorruptionError(
            f"scheduler state is corrupt: {path}{location}; the file was preserved. "
            "Repair it in place or move it aside after inspecting/recovering its controls, "
            "then retry the operation."
        ) from None
    if not isinstance(raw, dict):
        raise StateCorruptionError(
            f"scheduler state is corrupt: {path} must contain a JSON object; the file was "
            "preserved. Repair it in place or move it aside after inspecting/recovering its "
            "controls, then retry the operation."
        )
    return raw


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

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._written_keys.add(key)
        self._snaps.pop(key, None)

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        """Update keys through ``__setitem__`` so every changed key is tracked."""
        values = dict(*args, **kwargs)
        for key, value in values.items():
            self[key] = value

    def __ior__(self, other: Any) -> _TaskState:
        self.update(other)
        return self

    def clear(self) -> None:
        """Mark existing keys deleted before clearing the mapping."""
        self._written_keys.update(self)
        self._snaps.clear()
        super().clear()

    def pop(self, key: str, *args: Any) -> Any:  # type: ignore[override]
        result = super().pop(key, *args)
        self._written_keys.add(key)
        self._snaps.pop(key, None)
        return result

    def popitem(self) -> tuple[Any, Any]:
        key, value = super().popitem()
        self._written_keys.add(key)
        self._snaps.pop(key, None)
        return key, value

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
            raw = _read_state(path)
            self.data = {
                k: _TaskState(v) if isinstance(v, dict) else v
                for k, v in raw.items()
            }

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

    # These values grow with review history but are not control-plane inputs once a task is
    # terminal.  They remain losslessly available through ``historical`` and are restored
    # before a task is allowed to become operational again.
    HISTORICAL_KEYS = frozenset({
        "feedback_ignored", "persona_reviews", "rebase_artifacts", "review_feedback_history",
        "review_heads", "stashes", "verification_advisories",
    })

    @property
    def history_dir(self) -> Path:
        return self.path.parent / "state-history"

    def _history_index(self) -> dict[str, Any]:
        path = self.history_dir / "index.json"
        if not path.exists():
            return {"version": 1, "tasks": {}}
        value = json.loads(path.read_text())
        if value.get("version") != 1 or not isinstance(value.get("tasks"), dict):
            raise StateCorruptionError(f"scheduler state history index is corrupt: {path}")
        return value

    def historical(self, task_id: str) -> _TaskState:
        """Return one task's complete state without scanning other historical payloads."""
        current = dict(self.get(task_id))
        reference = current.get("_history_ref")
        if not isinstance(reference, dict):
            return _TaskState(current)
        sha = str(reference.get("sha256") or "")
        blob = self.history_dir / "blobs" / sha[:2] / f"{sha}.json.gz"
        try:
            raw = gzip.decompress(blob.read_bytes())
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise StateCorruptionError(f"scheduler state history blob is unavailable: {sha}") from exc
        if hashlib.sha256(raw).hexdigest() != sha:
            raise StateCorruptionError(f"scheduler state history checksum mismatch: {sha}")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise StateCorruptionError(f"scheduler state history payload is corrupt: {sha}")
        return _TaskState({**payload, **current})

    def restore_operational(self, task_ids: set[str]) -> int:
        """Rehydrate archived fields for tasks that may participate in scheduling again."""
        restored = 0
        for task_id in task_ids:
            current = self.get(task_id)
            if "_history_ref" not in current:
                continue
            complete = self.historical(task_id)
            current.pop("_history_ref", None)
            current.update({key: value for key, value in complete.items() if key in self.HISTORICAL_KEYS})
            restored += 1
        return restored

    def archive_completed(self, task_ids: set[str], *, limit: int) -> dict[str, int]:
        """Move bounded terminal-task payloads to a durable, indexed compressed CAS."""
        report = {"tasks": 0, "logical_bytes": 0, "stored_bytes": 0}
        counted_blobs: set[str] = set()
        if limit <= 0:
            return report
        lock_path = self.path.parent / "state-history.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            index = self._history_index()
            for task_id in sorted(task_ids):
                if report["tasks"] >= limit:
                    break
                current = self.get(task_id)
                payload = {key: current[key] for key in self.HISTORICAL_KEYS if key in current}
                if not payload:
                    continue
                raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                sha = hashlib.sha256(raw).hexdigest()
                blob = self.history_dir / "blobs" / sha[:2] / f"{sha}.json.gz"
                blob.parent.mkdir(parents=True, exist_ok=True)
                if not blob.exists():
                    compressed = gzip.compress(raw, mtime=0)
                    self._durable_bytes(blob, compressed)
                # A content-addressed path may predate this transaction. Verify both new
                # and deduplicated blobs before publishing a reference and retiring the
                # hot-state fields that remain the usable original on failure.
                try:
                    reconstructed = gzip.decompress(blob.read_bytes())
                except (OSError, EOFError, gzip.BadGzipFile) as exc:
                    raise StateCorruptionError(
                        f"scheduler state history verification failed: {sha}"
                    ) from exc
                if reconstructed != raw or hashlib.sha256(reconstructed).hexdigest() != sha:
                    raise StateCorruptionError(f"scheduler state history verification failed: {sha}")
                index["tasks"][task_id] = {"sha256": sha, "keys": sorted(payload)}
                self._durable_bytes(
                    self.history_dir / "index.json",
                    (json.dumps(index, indent=2, sort_keys=True) + "\n").encode(),
                )
                current["_history_ref"] = index["tasks"][task_id]
                for key in payload:
                    current.pop(key, None)
                report["tasks"] += 1
                report["logical_bytes"] += len(raw)
                if sha not in counted_blobs:
                    report["stored_bytes"] += blob.stat().st_size
                    counted_blobs.add(sha)
            # The archive and its index are durable before compact references replace data.
            self.save()
        return report

    @staticmethod
    def _durable_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            staged = Path(stream.name)
        os.replace(staged, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

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
                disk = _read_state(self.path)
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

    def restore_other_task_keys(self, snapshot: dict[str, Any], task_id: str, task_ids: set[str]) -> None:
        """Restore worker-tainted keys in other tasks from a dispatch snapshot.

        The active task's entry belongs to reap, so it is deliberately preserved.  This uses
        the same lock and atomic replacement as ``save`` because a fence violation can race a
        UI or another scheduler process.
        """
        lock_path = self.path.parent / (self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            disk = _read_state(self.path) if self.path.exists() else {}
            for other_id in task_ids - {task_id}:
                before = snapshot.get(other_id, {})
                current = disk.setdefault(other_id, {})
                live = self.data.get(other_id, {})
                # ``live`` is this scheduler's view, including work it did while the
                # worker ran. Restore the dispatch value only when the scheduler did
                # not change that key; otherwise retain the scheduler's newer value.
                for key in set(before) | set(current) | set(live):
                    if key in live and live.get(key) != before.get(key):
                        current[key] = live[key]
                    elif key in before:
                        current[key] = before[key]
                    else:
                        current.pop(key, None)
                if not current:
                    disk.pop(other_id, None)
            tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(disk, indent=2, sort_keys=True))
            os.replace(tmp_path, self.path)
