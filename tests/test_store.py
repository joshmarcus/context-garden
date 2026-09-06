from __future__ import annotations

import os
import textwrap

import pytest
import yaml

from garden import store as store_module
from garden.store import Store


def _task_file(garden, tid: str, name: str) -> None:
    """Write a task file into demo/p1 with the given id (used to force id collisions)."""
    path = garden / "demo" / "p1" / "tasks" / f"{tid}-{name}.md"
    path.write_text(textwrap.dedent(f"""
        ---
        id: {tid}
        title: {name}
        status: ready
        depends_on: []
        priority: 3
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        {name}
        """).lstrip())


def _rewrite_config(garden, **changes):
    """Change garden.yaml on disk and bump its mtime, so the mtime comparison in
    Store.reload_config_if_changed sees the edit regardless of filesystem granularity."""
    p = garden / "garden.yaml"
    data = yaml.safe_load(p.read_text())
    data.update(changes)
    p.write_text(yaml.safe_dump(data))
    future = os.stat(p).st_mtime + 10
    os.utime(p, (future, future))


def test_config_reloaded_when_garden_yaml_changes(garden):
    store = Store(garden)
    assert store.config.get("max_parallel") == 2  # the fixture value
    _rewrite_config(garden, max_parallel=9)
    store.invalidate()
    assert store.config.get("max_parallel") == 9
    assert store.last_config_change.get("max_parallel") == (2, 9)


def test_config_not_reloaded_when_unchanged(garden):
    store = Store(garden)
    cfg = store.config
    store.invalidate()
    assert store.config is cfg  # no needless reload
    assert store.last_config_change == {}


def test_save_writes_task_content(garden):
    store = Store(garden)
    t = store.task("DM-001")
    t.title = "Updated title"
    store.save(t)

    assert "Updated title" in t.path.read_text()


def test_save_leaves_no_temp_file_behind(garden):
    store = Store(garden)
    t = store.task("DM-001")
    store.save(t)

    leftovers = [p for p in t.path.parent.iterdir() if p.name.startswith(f".{t.path.name}.")]
    assert leftovers == []


def test_save_does_not_truncate_on_write_failure(garden, monkeypatch):
    store = Store(garden)
    t = store.task("DM-001")
    original = t.path.read_text()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store_module.os, "replace", boom)
    with pytest.raises(OSError):
        store.save(t)

    # the original file must still be intact: no truncate-before-write window
    assert t.path.read_text() == original
    leftovers = [p for p in t.path.parent.iterdir() if p.name.startswith(f".{t.path.name}.")]
    assert leftovers == []


# ---- durable id reservations ---------------------------------------------------------------
def test_reserved_ids_are_skipped_by_next_id_and_create_task(garden):
    store = Store(garden)
    assert store.next_id("demo") == "DM-003"  # past the fixture's DM-001, DM-002
    ids = store.reserve_ids("demo", 2, owner="retro:demo/p1")
    assert ids == ["DM-003", "DM-004"]
    # both next_id and a live create_task now step past the reserved block
    assert store.next_id("demo") == "DM-005"
    t = store.create_task("demo", "p1", "Filed live", "## Goal\n\nx\n")
    assert t.id == "DM-005"
    assert store.reserve_ids("demo", 1, owner="retro:demo/p1") == ["DM-006"]


def test_reservations_survive_a_fresh_store(garden):
    Store(garden).reserve_ids("demo", 1, owner="retro:demo/p1")
    # a fresh Store (as after a restart) reads the ledger off disk and still skips the id
    assert Store(garden).next_id("demo") == "DM-004"


def test_release_reservation_reclaims_the_batch(garden):
    store = Store(garden)
    store.reserve_ids("demo", 2, owner="retro:demo/p1")
    assert store.next_id("demo") == "DM-005"
    assert sorted(store.release_reservation("retro:demo/p1")) == ["DM-003", "DM-004"]
    assert store.next_id("demo") == "DM-003"
    assert store.reserved_ids() == {}


def test_prune_reservations_drops_ids_that_became_task_files(garden):
    store = Store(garden)
    store.reserve_ids("demo", 2, owner="retro:demo/p1")  # DM-003, DM-004
    store.create_task("demo", "p1", "Landed", "## Goal\n\nx\n", task_id="DM-003")  # as if the draft merged
    assert store.prune_reservations() == ["DM-003"]
    assert set(store.reserved_ids()) == {"DM-004"}


# ---- duplicate task ids ---------------------------------------------------------------------
def test_duplicate_task_id_is_quarantined_not_fatal(garden):
    _task_file(garden, "DM-003", "clash-a")
    _task_file(garden, "DM-003", "clash-b")
    store = Store(garden)
    tasks = store.tasks()  # does not raise
    assert "DM-003" not in tasks  # ambiguous: kept out of the map so it cannot dispatch
    assert "DM-001" in tasks and "DM-002" in tasks  # unrelated tasks stay available
    dups = store.duplicate_ids()
    assert set(dups) == {"DM-003"}
    assert len(dups["DM-003"]) == 2
    # the next free id still steps past the quarantined id
    assert store.next_id("demo") == "DM-004"
