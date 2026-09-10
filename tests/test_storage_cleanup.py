from __future__ import annotations

import json
import os
import time

import pytest

from garden import gitops
from garden.model import Status
from garden.storage_cleanup import cleanup_home_caches, remove_owned_tree, space_status, tree_bytes


def test_orphan_worker_home_preview_and_bounded_idempotent_cleanup(sched):
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    sched.store.save(task)
    home = sched.cfg.worktrees_dir / ".garden-home-DM-001"
    cache = home / ".cache" / "pip"
    cache.mkdir(parents=True)
    (cache / "wheel").write_bytes(b"x" * 4096)
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".codex" / "sessions" / "required.json").write_text("evidence")
    old = time.time() - 3 * 86400
    os.utime(home, (old, old))

    preview = sched.sweep_storage(type("Report", (), {"transitions": []})(), apply=False, limit=1)
    row = next(row for row in preview["inventory"]["items"] if row["path"] == str(home))
    assert row["eligible"] and row["bytes"] >= 4096
    assert cache.exists()

    applied = sched.sweep_storage(type("Report", (), {"transitions": []})(), apply=True, limit=1)
    assert applied["bytes_reclaimed"] > 0
    assert not cache.exists()
    assert (home / ".codex" / "sessions" / "required.json").read_text() == "evidence"
    assert sched.sweep_storage(type("Report", (), {"transitions": []})(), apply=True, limit=1)[
        "bytes_reclaimed"
    ] == 0
    assert json.loads(open(applied["audit_path"]).read())["bytes_reclaimed"] > 0
    for _ in range(25):
        sched.sweep_storage(type("Report", (), {"transitions": []})(), apply=False, limit=1)
    assert len(list((sched.cfg.garden_dir / "storage-cleanup").glob("*.json"))) == 20


def test_active_dirty_unique_and_foreign_storage_are_retained(sched):
    task = sched.store.task("DM-001")
    sched.tick()
    sched.tick(dispatch=False)
    worktree = sched.worktree_for(task)
    (worktree / "dirty").write_text("unique")
    task.status = Status.DONE
    sched.store.save(task)
    old = time.time() - 3 * 86400
    os.utime(worktree, (old, old))
    active = sched.runs.new_run(task.id, "local")
    foreign = sched.cfg.worktrees_dir / "foreign"
    foreign.mkdir()
    escape = sched.cfg.worktrees_dir / ".garden-home-escape"
    escape.symlink_to(foreign, target_is_directory=True)

    rows = sched.storage_inventory()["items"]
    assert "active" in next(row for row in rows if row["path"] == str(worktree))["reason"]
    assert "provenance" in next(row for row in rows if row["path"] == str(foreign))["reason"]
    assert "symlink" in next(row for row in rows if row["path"] == str(escape))["reason"]

    active.status = "done"
    active.save()
    row = next(row for row in sched.storage_inventory()["items"] if row["path"] == str(worktree))
    assert not row["eligible"] and row["reason"] == "dirty worktree"


def test_deletion_failure_is_recorded_and_links_never_followed(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        remove_owned_tree(root, link)
    assert (outside / "keep").exists()

    cache = root / ".garden-home-X" / ".cache" / "pip"
    cache.mkdir(parents=True)
    results = cleanup_home_caches(cache.parents[1], root, limit=1,
                                  remove=lambda _root, _path: (_ for _ in ()).throw(OSError("busy")))
    assert results[0]["outcome"] == "failed" and results[0]["bytes_reclaimed"] == 0


def test_storage_measurement_uses_allocated_bytes_and_reports_capabilities(tmp_path):
    path = tmp_path / "owned"
    path.mkdir()
    (path / "data").write_bytes(b"x" * 8192)
    assert tree_bytes(path) >= 8192
    status = space_status(path)
    assert status["guest_free_bytes"] is not None
    assert "host_reason" in status


def test_completed_worktree_is_removed_before_its_branch(sched):
    task = sched.store.task("DM-001")
    sched.tick()
    sched.tick(dispatch=False)
    task = sched.store.task("DM-001")
    worktree = sched.worktree_for(task)
    gitops.git("reset", "--hard", gitops.base_ref(worktree, "main"), cwd=worktree)
    gitops.git("push", "--force", "origin", f"HEAD:{task.branch}", cwd=worktree)
    task.status = Status.DONE
    sched.store.save(task)
    state = sched.state.get(task.id)
    state.clear()
    state.update({"pr_state": "MERGED", "head_sha": gitops.head_sha(worktree)})
    if task.pr:
        sched.github.close_pr("test/demo", int(task.pr.rsplit("/", 1)[1]))
    sched.state.get("__open_prs__").clear()
    for run in sched.runs.active():
        run.status = "done"
        run.save()
    old = time.time() - 3 * 86400
    os.utime(worktree, (old, old))
    report = type("Report", (), {"transitions": []})()

    storage = sched.sweep_storage(report, apply=True, limit=20)
    assert not worktree.exists()
    assert storage["bytes_reclaimed"] > 0
    row = next(row for row in sched.branch_cleanup_inventory() if row.branch == task.branch)
    assert row.classification == "removable", row.reason
    results = sched.sweep_worker_branches(report, limit=20)
    assert results[0]["outcome"] == "removed"
