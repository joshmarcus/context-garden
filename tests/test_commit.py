"""Tests for `garden commit` and the uncommitted-task-files warning in `garden status`."""
from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from garden.cli import app
from garden.gitops import commit_task_files, uncommitted_task_files

runner = CliRunner()


def _run(garden: Path, *args):
    import os
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)


def _git_init(path: Path) -> None:
    """Init path as a git repo with local user config and commit all existing files."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "--local", "user.email", "t@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "--local", "user.name", "t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, capture_output=True)


def _first_task(garden: Path) -> Path:
    return next(garden.glob("**/tasks/*.md"))


# ---------------------------------------------------------------------------
# uncommitted_task_files / commit_task_files


def test_uncommitted_task_files_clean(garden: Path):
    _git_init(garden)
    assert uncommitted_task_files(garden) == []


def test_uncommitted_task_files_modified(garden: Path):
    _git_init(garden)
    task = _first_task(garden)
    task.write_text(task.read_text() + "\n<!-- touched -->")
    dirty = uncommitted_task_files(garden)
    assert any(task.name in f for f in dirty)


def test_uncommitted_task_files_not_a_repo(garden: Path):
    # garden root is not a git repo; should return empty rather than raising
    assert uncommitted_task_files(garden) == []


def test_commit_task_files_commits(garden: Path):
    _git_init(garden)
    task = _first_task(garden)
    task.write_text(task.read_text() + "\n<!-- touched -->")
    committed = commit_task_files(garden, "garden: update task state")
    assert len(committed) == 1
    assert task.name in committed[0]
    assert uncommitted_task_files(garden) == []
    log = subprocess.check_output(["git", "log", "--oneline", "-1"], cwd=garden, text=True)
    assert "garden: update task state" in log


def test_commit_task_files_no_changes(garden: Path):
    _git_init(garden)
    log_before = subprocess.check_output(["git", "log", "--oneline"], cwd=garden, text=True)
    committed = commit_task_files(garden, "garden: update task state")
    assert committed == []
    log_after = subprocess.check_output(["git", "log", "--oneline"], cwd=garden, text=True)
    assert log_before == log_after


# ---------------------------------------------------------------------------
# garden commit CLI command


def test_commit_command_nothing_to_commit(garden: Path):
    _git_init(garden)
    r = _run(garden, "commit")
    assert r.exit_code == 0, r.output
    assert "nothing to commit" in r.output


def test_commit_command_commits_dirty_files(garden: Path):
    _git_init(garden)
    task = _first_task(garden)
    task.write_text(task.read_text() + "\n<!-- touched -->")
    r = _run(garden, "commit")
    assert r.exit_code == 0, r.output
    assert task.name in r.output
    assert uncommitted_task_files(garden) == []


def test_commit_command_not_a_repo(garden: Path):
    r = _run(garden, "commit")
    assert r.exit_code == 1
    assert "not a git repository" in r.output


# ---------------------------------------------------------------------------
# garden status warning


def test_status_warns_when_dirty(garden: Path):
    _git_init(garden)
    task = _first_task(garden)
    task.write_text(task.read_text() + "\n<!-- touched -->")
    r = _run(garden, "status")
    assert r.exit_code == 0, r.output
    assert "uncommitted" in r.output
    assert "garden commit" in r.output


def test_status_no_warning_when_clean(garden: Path):
    _git_init(garden)
    r = _run(garden, "status")
    assert r.exit_code == 0, r.output
    assert "uncommitted" not in r.output
