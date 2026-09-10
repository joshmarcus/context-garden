"""Tests for State concurrent-write safety (CG-053)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from garden.scheduler import State, StateCorruptionError, _TaskState

# ── _TaskState unit tests ──────────────────────────────────────────────────────

def test_task_state_tracks_setitem():
    ts = _TaskState({"a": 1})
    assert not ts.dirty
    ts["b"] = 2
    assert ts.dirty == {"b"}


def test_task_state_tracks_dict_mutators():
    ts = _TaskState({"a": 1, "b": 2, "c": 3})

    ts.update({"a": 10}, d=4)
    ts |= {"b": 20}
    ts.clear()

    assert ts == {}
    assert ts.dirty == {"a", "b", "c", "d"}


def test_task_state_tracks_deletion_mutators():
    ts = _TaskState({"a": 1, "b": 2})

    del ts["a"]
    ts.popitem()

    assert ts == {}
    assert ts.dirty == {"a", "b"}


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


def test_update_persists_through_save(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)

    state.get("CG-001").update({"pr_number": 42, "pending_feedback": "fix this"})
    state.save()

    final = State(path)
    assert final.get("CG-001") == {"pr_number": 42, "pending_feedback": "fix this"}


def test_ior_persists_through_save(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)

    task_state = state.get("CG-001")
    task_state |= {"pr_number": 42, "pending_feedback": "fix this"}
    state.save()

    final = State(path)
    assert final.get("CG-001") == {"pr_number": 42, "pending_feedback": "fix this"}


def test_clear_deletions_persist_through_save(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    state.get("CG-001").update({"pr_number": 42, "pending_feedback": "fix this"})
    state.save()

    state.get("CG-001").clear()
    state.save()

    final = State(path)
    assert final.get("CG-001") == {}


def test_task_state_setdefault_existing_key_not_dirty_until_mutated():
    # setdefault on a present key hands back the live value and snapshots it, but does
    # not mark it dirty on its own: only an actual in-place mutation makes it dirty.
    ts = _TaskState({"runs": [1]})
    val = ts.setdefault("runs", [])
    assert val == [1]
    assert "runs" not in ts.dirty
    val.append(2)
    assert "runs" in ts.dirty


def test_task_state_read_mutable_not_dirty_without_mutation():
    # Reading a dict/list value does not mark it dirty (the dirty-on-read clobber fix).
    ts = _TaskState({"cfg": {"a": 1}})
    _ = ts["cfg"]
    assert "cfg" not in ts.dirty


def test_task_state_in_place_mutation_marks_dirty():
    ts = _TaskState({"cfg": {"a": 1}})
    ts["cfg"]["a"] = 2
    assert "cfg" in ts.dirty


def test_task_state_list_append_survives(tmp_path):
    """In-place list mutation via setdefault is captured on save."""
    path = tmp_path / "state.json"
    s = State(path)
    runs = s.get("_aux").setdefault("runs", [])
    runs.append({"run_id": "r1"})
    s.save()

    final = State(path)
    assert final.get("_aux")["runs"] == [{"run_id": "r1"}]


def test_initial_corruption_is_reported_and_preserved(tmp_path):
    path = tmp_path / "state.json"
    corrupt = b'{"_control":{"dispatch":"paused"}'
    path.write_bytes(corrupt)

    with pytest.raises(StateCorruptionError, match=r"state is corrupt.*line 1 column") as exc:
        State(path)

    assert path.read_bytes() == corrupt
    assert "Repair it in place or move it aside" in str(exc.value)


def test_save_time_corruption_preserves_disk_and_pending_changes(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"_control":{"dispatch":"paused"}}')
    state = State(path)
    pending_key = state.get("CG-001")
    pending_key["continuation"] = {"stage": "review"}
    corrupt = b"not json\xff"
    path.write_bytes(corrupt)

    with pytest.raises(StateCorruptionError, match="state is corrupt"):
        state.save()

    assert path.read_bytes() == corrupt
    assert pending_key["continuation"] == {"stage": "review"}
    assert pending_key.dirty == {"continuation"}


def test_fence_recovery_refuses_to_replace_corrupt_state(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    corrupt = b"{broken"
    path.write_bytes(corrupt)

    with pytest.raises(StateCorruptionError, match="state is corrupt"):
        state.restore_other_task_keys({"CG-002": {"lease": "old"}}, "CG-001", {"CG-001", "CG-002"})

    assert path.read_bytes() == corrupt


def test_completed_state_history_is_compressed_exact_and_restorable(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    feedback = [{"body": "same detailed finding " * 200, "round": n} for n in range(20)]
    state.get("CG-001")["review_feedback_history"] = feedback
    state.get("CG-001")["head_sha"] = "abc123"
    state.save()
    before = path.stat().st_size

    report = state.archive_completed({"CG-001"}, limit=1)
    compact = State(path)

    assert report["tasks"] == 1
    assert report["stored_bytes"] < report["logical_bytes"]
    assert path.stat().st_size < before / 10
    assert compact.get("CG-001")["head_sha"] == "abc123"
    assert "review_feedback_history" not in compact.get("CG-001")
    assert compact.historical("CG-001")["review_feedback_history"] == feedback
    assert compact.restore_operational({"CG-001"}) == 1
    compact.save()
    assert State(path).get("CG-001")["review_feedback_history"] == feedback


def test_state_history_crash_before_compact_state_commit_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = State(path)
    payload = [{"body": "important failure"}]
    state.get("CG-001")["review_feedback_history"] = payload
    state.save()
    original_save = state.save

    def interrupted_save():
        raise OSError("simulated state commit failure")

    monkeypatch.setattr(state, "save", interrupted_save)
    with pytest.raises(OSError, match="state commit failure"):
        state.archive_completed({"CG-001"}, limit=1)
    monkeypatch.setattr(state, "save", original_save)

    assert State(path).get("CG-001")["review_feedback_history"] == payload


def test_state_history_rejects_corrupt_existing_blob_without_compacting(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    payload = [{"body": "important failure"}]
    state.get("CG-001")["review_feedback_history"] = payload
    state.save()
    raw = json.dumps(
        {"review_feedback_history": payload}, sort_keys=True, separators=(",", ":")
    ).encode()
    sha = hashlib.sha256(raw).hexdigest()
    blob = state.history_dir / "blobs" / sha[:2] / f"{sha}.json.gz"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"corrupt")

    with pytest.raises(StateCorruptionError, match="verification failed"):
        state.archive_completed({"CG-001"}, limit=1)

    fresh = State(path)
    assert fresh.get("CG-001")["review_feedback_history"] == payload
    assert "_history_ref" not in fresh.get("CG-001")


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


def test_dict_mutators_persist_and_retain_concurrent_disjoint_updates(tmp_path):
    path = tmp_path / "state.json"

    init = State(path)
    init.get("CG-001").update({"old": 1, "remove": True})
    init.save()

    state_a = State(path)
    state_b = State(path)
    task_a = state_a.get("CG-001")
    task_a.update({"from_a": 3})
    state_b.get("CG-001")["from_b"] = 4

    state_a.save()
    state_b.save()

    final = State(path)
    assert final.get("CG-001") == {
        "old": 1,
        "remove": True,
        "from_a": 3,
        "from_b": 4,
    }


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
    _ = s.get("CG-001")["pr_number"]      # __getitem__ of a scalar
    s.save()

    # File should be untouched (no dirty keys → save is a no-op)
    final = State(path)
    assert final.get("CG-001")["pr_number"] == 7


def test_reading_mutable_does_not_clobber_concurrent_write(tmp_path):
    """The dirty-on-read clobber: a writer that only *reads* a task's mutable value
    (and writes some other key) must not overwrite a concurrent writer's change to the
    key it merely read."""
    path = tmp_path / "state.json"

    init = State(path)
    init.get("CG-001")["cfg"] = {"a": 1}
    init.save()

    # A reads cfg but does not mutate it, and writes an unrelated key.
    state_a = State(path)
    _ = state_a.get("CG-001")["cfg"]["a"]            # read only
    state_a.get("CG-001")["pr_number"] = 5

    # B mutates cfg in place and saves first.
    state_b = State(path)
    state_b.get("CG-001")["cfg"]["a"] = 2
    state_b.save()

    # A saves after B; because A only read cfg, it must not clobber B's cfg update.
    state_a.save()

    final = State(path)
    assert final.get("CG-001")["cfg"] == {"a": 2}
    assert final.get("CG-001")["pr_number"] == 5


def test_in_place_mutation_of_read_value_survives(tmp_path):
    """A mutable value read via __getitem__ and then mutated in place is persisted."""
    path = tmp_path / "state.json"

    init = State(path)
    init.get("CG-001")["items"] = [1]
    init.save()

    s = State(path)
    s.get("CG-001")["items"].append(2)
    s.save()

    final = State(path)
    assert final.get("CG-001")["items"] == [1, 2]


def test_task_state_get_snapshots_mutable_like_getitem():
    # st.get(key) must snapshot a mutable value the same way st[key] does, so an in-place
    # mutation of the value read through .get is caught by dirty (dict.get bypasses __getitem__).
    ts = _TaskState({"cfg": {"a": 1}})
    val = ts.get("cfg")
    assert "cfg" not in ts.dirty  # a bare read is not dirty
    val["a"] = 2
    assert "cfg" in ts.dirty
    # a missing key returns the default and is never snapshotted
    assert ts.get("missing", "d") == "d"
    assert "missing" not in ts.dirty


def test_in_place_mutation_of_value_read_via_get_survives(tmp_path):
    """A mutable value read through st.get(key) and then mutated in place is persisted:
    dict.get bypasses __getitem__, so without the get override the mutation is lost."""
    path = tmp_path / "state.json"

    init = State(path)
    init.get("CG-001")["items"] = [1]
    init.save()

    s = State(path)
    s.get("CG-001").get("items").append(2)  # read through .get, then mutate in place
    s.save()

    final = State(path)
    assert final.get("CG-001")["items"] == [1, 2]


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
