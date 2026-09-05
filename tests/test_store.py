from __future__ import annotations

import os

import pytest
import yaml

from garden import store as store_module
from garden.store import Store


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
