"""Tests for State concurrent-write safety (CG-053)."""

from __future__ import annotations

import json
from pathlib import Path

from garden.scheduler import State, _TaskState

# ── _TaskState unit tests ──────────────────────────────────────────────────────

def test_task_state_tracks_setitem():
    ts = _TaskState({"a": 1})
    assert not ts.dirty
    ts["b"] = 2
    assert ts.dirty == {"b"}


def test_task_state_tracks_pop():
    ts = _TaskState({"a": 1})
    ts.pop("a", None)
    assert "a" in ts.dirty
    assert "a" not in ts


def test_task_state_pop_missing_key_still_dirty():
    ts = _TaskState({})
    ts.pop("gone", None)
    assert "gone" in ts.dirty


def test_task_state_setdefault_new_key_dirty():
    ts = _TaskState({})
    ts.setdefault("runs", [])
    assert "runs" in ts.dirty


def test_task_state_setdefault_existing_key_dirty():
    # Already-present keys are also marked dirty so in-place mutations (list.append) are captured.
    ts = _TaskState({"runs": [1]})
    val = ts.setdefault("runs", [])
    assert "runs" in ts.dirty
    assert val == [1]


def test_task_state_list_append_survives(tmp_path):
    """In-place list mutation via setdefault is captured on save."""
    path = tmp_path / "state.json"
    s = State(path)
    runs = s.get("_aux").setdefault("runs", [])
    runs.append({"run_id": "r1"})
    s.save()

    final = State(path)
    assert final.get("_aux")["runs"] == [{"run_id": "r1"}]


# ── concurrent-write tests ─────────────────────────────────────────────────────

def test_concurrent_writes_different_keys_same_task(tmp_path):
    """Two State objects each writing a different key of the same task both survive on disk."""
    path = tmp_path / "state.json"

    # Both load before either saves — the classic lost-update scenario.
    state_a = State(path)
    state_b = State(path)

    state_a.get("CG-001")["pr_number"] = 42
    state_b.get("CG-001")["pending_feedback"] = "fix this"

    state_a.save()
    state_b.save()

    final = State(path)
    assert final.get("CG-001")["pr_number"] == 42
    assert final.get("CG-001")["pending_feedback"] == "fix this"


def test_concurrent_writes_different_tasks(tmp_path):
    """Two State objects each touching a different task_id don't interfere."""
    path = tmp_path / "state.json"

    state_a = State(path)
    state_b = State(path)

    state_a.get("CG-001")["pr_number"] = 1
    state_b.get("CG-002")["pr_number"] = 2

    state_a.save()
    state_b.save()

    final = State(path)
    assert final.get("CG-001")["pr_number"] == 1
    assert final.get("CG-002")["pr_number"] == 2


def test_pop_removes_key_from_disk_even_when_another_writer_saves_later(tmp_path):
    """A popped key is absent on disk even if another State object saves after."""
    path = tmp_path / "state.json"

    # Establish initial state with two keys.
    init = State(path)
    init.get("CG-001")["force_push"] = True
    init.get("CG-001")["pr_number"] = 99
    init.save()

    # Both readers load the state.
    state_a = State(path)
    state_b = State(path)

    state_a.get("CG-001").pop("force_push", False)
    state_b.get("CG-001")["pr_number"] = 100

    state_a.save()
    state_b.save()

    final = State(path)
    assert "force_push" not in final.get("CG-001")
    assert final.get("CG-001")["pr_number"] == 100


def test_no_dirty_keys_skips_write(tmp_path):
    """save() with nothing dirty does not create or modify the file."""
    path = tmp_path / "state.json"
    s = State(path)
    s.save()  # nothing was accessed/modified
    assert not path.exists()


def test_save_clears_dirty_keys(tmp_path):
    """After a successful save(), previously-dirty keys are no longer dirty."""
    path = tmp_path / "state.json"
    s = State(path)
    s.get("CG-001")["pr_number"] = 42
    s.save()
    assert not s.get("CG-001").dirty


def test_second_save_does_not_reclobber_concurrent_update(tmp_path):
    """A second save() from the same State must not re-write a key that a concurrent
    writer changed in between, since it should no longer be considered dirty."""
    path = tmp_path / "state.json"

    state_a = State(path)
    state_a.get("CG-001")["pr_number"] = 1
    state_a.save()  # pr_number's dirty flag must clear here

    state_b = State(path)
    state_b.get("CG-001")["pr_number"] = 2
    state_b.save()

    # state_a saves again for an unrelated reason; it must not re-write the stale
    # in-memory pr_number=1 over state_b's pr_number=2.
    state_a.get("CG-001")["pending_feedback"] = "fix this"
    state_a.save()

    final = State(path)
    assert final.get("CG-001")["pr_number"] == 2
    assert final.get("CG-001")["pending_feedback"] == "fix this"


def test_read_only_access_does_not_write(tmp_path):
    """Reading a key via .get() without writing doesn't mark it dirty."""
    path = tmp_path / "state.json"

    init = State(path)
    init.get("CG-001")["pr_number"] = 7
    init.save()

    s = State(path)
    _ = s.get("CG-001").get("pr_number")  # read only, no __setitem__
    # setdefault on an existing key DOES mark it dirty so we avoid it here
    s.save()

    # File should be untouched (no dirty keys → save is a no-op)
    final = State(path)
    assert final.get("CG-001")["pr_number"] == 7


# ── atomic replace ──────────────────────────────────────────────────────────

def test_save_leaves_no_temp_files_behind(tmp_path):
    """save() writes via a temp file + os.replace(); no .tmp file should remain."""
    path = tmp_path / "state.json"
    s = State(path)
    s.get("CG-001")["pr_number"] = 1
    s.save()

    assert [p.name for p in tmp_path.iterdir() if p.name != path.name and not p.name.endswith(".lock")] == []


def test_concurrent_reader_never_sees_truncated_file(tmp_path, monkeypatch):
    """A reader racing State.__init__ against save() must see the old or new file in
    full, never a partial write (CG-091). save() must write the new content to a temp
    file first and only swap it into place with os.replace(), so the destination path
    is never open for writing (and thus never truncated) at any point readers can see."""
    import os as os_module

    path = tmp_path / "state.json"

    init = State(path)
    init.get("CG-001")["pr_number"] = 1
    init.save()
    old_content = path.read_text()

    real_replace = os_module.replace
    seen: dict = {}

    def spy_replace(src, dst):
        # Immediately before the swap, the destination must still hold the complete
        # old content (never truncated) and the source the complete new content.
        seen["dst_before_replace"] = Path(dst).read_text()
        seen["src_before_replace"] = Path(src).read_text()
        real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", spy_replace)

    s = State(path)
    s.get("CG-002")["pr_number"] = 2
    s.save()

    assert seen["dst_before_replace"] == old_content
    assert json.loads(seen["src_before_replace"])["CG-002"]["pr_number"] == 2

    final = State(path)
    assert final.get("CG-001")["pr_number"] == 1
    assert final.get("CG-002")["pr_number"] == 2
