from __future__ import annotations

import pytest

from garden import store as store_module
from garden.store import Store


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
