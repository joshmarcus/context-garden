"""CGS-023: local-run activity checks must bound their filesystem work independent of
worktree size, and fail open (never report idle) when that bound is reached."""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pytest

from garden import runs as runs_module
from garden.runs import RunStore


class _CountingScandirResult:
    """Wraps a real os.scandir() iterator, counting how many entries are actually pulled from
    it, so a test can assert the walk stopped early instead of reading a directory in full."""

    def __init__(self, real_iterator, counter: dict):
        self._it = real_iterator
        self._counter = counter

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self._it)
        self._counter["n"] += 1
        return entry

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._it.close()
        return False


def _count_scandir_entries(monkeypatch) -> dict:
    """Patch os.scandir, as seen by garden.runs, to count every entry pulled from any
    directory listing it performs. Returns the live counter dict."""
    counter = {"n": 0}
    real_scandir = os.scandir

    def counting_scandir(path="."):
        return _CountingScandirResult(real_scandir(path), counter)

    monkeypatch.setattr(runs_module.os, "scandir", counting_scandir)
    return counter


def _make_flat_tree(root: Path, count: int, mtime: float | None = None) -> None:
    """A single directory with `count` empty files: the case a per-directory listing cap must
    still bound, since the whole tree is one directory wider than any budget."""
    root.mkdir(parents=True, exist_ok=True)
    for n in range(count):
        p = root / f"f{n}"
        p.touch()
        if mtime is not None:
            os.utime(p, (mtime, mtime))


def test_newest_mtime_bounds_its_work_on_a_tree_larger_than_the_budget(tmp_path, monkeypatch):
    budget = 200
    _make_flat_tree(tmp_path, budget * 5)
    counter = _count_scandir_entries(monkeypatch)

    start = time.monotonic()
    newest, complete = runs_module._newest_mtime(tmp_path, budget=budget)
    elapsed = time.monotonic() - start

    assert complete is False
    # Bounded by the budget, not by how many files actually exist under root: proves the walk
    # was abandoned mid-listing rather than reading the directory in full first.
    assert counter["n"] <= budget + 1
    # No timing-dependent sleep is used anywhere above; this just shows that bounding the work
    # also bounds the wall clock, with a generous non-flaky margin.
    assert elapsed < 5.0


def test_newest_mtime_completes_and_finds_recent_activity_on_a_small_tree(tmp_path):
    old = dt.datetime.now(dt.UTC).timestamp() - 3600
    _make_flat_tree(tmp_path, 10, mtime=old)
    recent = tmp_path / "just-written.txt"
    recent.write_text("x")

    newest, complete = runs_module._newest_mtime(tmp_path)

    assert complete is True
    assert newest == pytest.approx(recent.stat().st_mtime)


def test_newest_mtime_skips_the_git_directory_but_not_a_gitlink_file(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("churn")  # would look newest if not skipped
    old = dt.datetime.now(dt.UTC).timestamp() - 3600
    stable = tmp_path / "code.py"
    stable.write_text("x")
    os.utime(stable, (old, old))

    newest, complete = runs_module._newest_mtime(tmp_path)

    assert complete is True
    assert newest == pytest.approx(stable.stat().st_mtime)

    # A linked worktree's .git is a gitlink *file*, not a directory, and is read like any other
    # file rather than being specially skipped.
    linked = tmp_path.parent / "linked"
    linked.mkdir()
    (linked / "code.py").write_text("x")
    os.utime(linked / "code.py", (old, old))
    gitlink_time = dt.datetime.now(dt.UTC).timestamp()
    (linked / ".git").write_text("gitdir: /elsewhere\n")
    os.utime(linked / ".git", (gitlink_time, gitlink_time))

    newest, complete = runs_module._newest_mtime(linked)
    assert complete is True
    assert newest == pytest.approx(gitlink_time)


def test_repeated_idle_checks_on_a_huge_tree_return_promptly_without_sleeping(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_module, "_ACTIVITY_SCAN_BUDGET", 100)
    _make_flat_tree(tmp_path, 100 * 20)

    for _ in range(5):
        newest, complete = runs_module._newest_mtime(tmp_path)
        assert complete is False


def test_last_activity_at_fails_open_when_the_worktree_scan_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_module, "_ACTIVITY_SCAN_BUDGET", 50)
    rs = RunStore(tmp_path / "garden")
    run = rs.new_run("DM-001", "local", run_id="20260101T000000Z-work")
    worktree = tmp_path / "worktree"
    old = dt.datetime.now(dt.UTC).timestamp() - 3600
    _make_flat_tree(worktree, 500, mtime=old)
    run.worktree = str(worktree)
    run.started_at = dt.datetime.fromtimestamp(old, dt.UTC).isoformat()
    run.save()

    before = dt.datetime.now(dt.UTC)
    activity = run.last_activity_at()
    after = dt.datetime.now(dt.UTC)

    assert activity is not None
    assert before <= activity <= after  # fails open: reported as "now", never as idle
    assert run.idle_minutes() == pytest.approx(0.0, abs=0.5)


def test_last_activity_at_still_finds_ordinary_signals_on_a_small_worktree(tmp_path):
    rs = RunStore(tmp_path / "garden")
    run = rs.new_run("DM-001", "local", run_id="20260101T000000Z-work")
    old = dt.datetime.now(dt.UTC).timestamp() - 3600

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    stale = worktree / "old.py"
    stale.write_text("x")
    os.utime(stale, (old, old))
    run.worktree = str(worktree)
    run.started_at = dt.datetime.fromtimestamp(old, dt.UTC).isoformat()
    run.save()

    # A small, quiet worktree with no recent output is correctly idle.
    assert run.idle_minutes() >= 59

    # A file changing in the worktree counts as activity.
    fresh = worktree / "new.py"
    fresh.write_text("x")
    assert run.idle_minutes() == pytest.approx(0.0, abs=0.5)

    # Backdate it again and instead grow stderr.log: output growth counts as activity too.
    os.utime(fresh, (old, old))
    (run.path / "stderr.log").write_text("still working\n")
    assert run.idle_minutes() == pytest.approx(0.0, abs=0.5)
