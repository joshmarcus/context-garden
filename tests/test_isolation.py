"""Worktree isolation: find_root() refuses to escape .garden/, GARDEN_ROOT blocks workers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from garden.config import find_root


def _make_garden(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "garden.yaml").write_text(yaml.safe_dump({"name": "test"}))
    return path


def test_find_root_normal(tmp_path):
    root = _make_garden(tmp_path / "g")
    sub = root / "some" / "sub"
    sub.mkdir(parents=True)
    assert find_root(sub) == root


def test_find_root_refuses_worktree(tmp_path):
    """find_root() from inside .garden/worktrees/<id> must not return the enclosing garden."""
    root = _make_garden(tmp_path / "g")
    wt = root / ".garden" / "worktrees" / "CG-001" / "src"
    wt.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match=r"inside its \.garden/"):
        find_root(wt)


def test_find_root_refuses_any_dotgarden_subpath(tmp_path):
    root = _make_garden(tmp_path / "g")
    inner = root / ".garden" / "runs" / "CG-001"
    inner.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match=r"inside its \.garden/"):
        find_root(inner)


def test_find_root_worktree_nested_garden_is_fine(tmp_path):
    """A garden.yaml inside the worktree itself (not the enclosing one) is safe to return."""
    outer = _make_garden(tmp_path / "g")
    wt = outer / ".garden" / "worktrees" / "CG-001"
    _make_garden(wt)  # the worktree has its own garden.yaml (e.g. self-hosted)
    sub = wt / "src"
    sub.mkdir(parents=True)
    # find_root from `sub` should find the garden.yaml in `wt`, not the outer one
    assert find_root(sub) == wt


def test_garden_root_env_valid_is_ignored(tmp_path, monkeypatch):
    """GARDEN_ROOT pointing to a real garden is ignored; cwd walk finds the right root.

    check_ctx sets GARDEN_ROOT to the live garden so check commands can use
    $GARDEN_ROOT/.venv/bin/python, but find_root() must not use it as a redirect —
    otherwise tests running inside a pre-PR check subprocess land on the live garden.
    """
    root = _make_garden(tmp_path / "g")
    other = _make_garden(tmp_path / "other")
    monkeypatch.setenv("GARDEN_ROOT", str(other))
    # GARDEN_ROOT=other is ignored; find_root(root) still finds root via the cwd walk
    assert find_root(root) == root


def test_garden_root_env_invalid(tmp_path, monkeypatch):
    """GARDEN_ROOT set to a non-existent path must fail with a clear message."""
    monkeypatch.setenv("GARDEN_ROOT", str(tmp_path / "nonexistent"))
    with pytest.raises(FileNotFoundError, match="workers must not run garden"):
        find_root()


def test_local_runner_sets_garden_root(sched, monkeypatch):
    """The local runner must pass GARDEN_ROOT to workers pointing at a non-existent path."""
    import garden.runner.local as local_mod

    captured_envs: list[dict] = []
    orig_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        env = kwargs.get("env")
        if env is not None:
            captured_envs.append(dict(env))
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(local_mod.subprocess, "Popen", spy_popen)
    sched.tick()

    # Find the worker launch: it has GARDEN_TASK_ID set (git calls do not pass env at all)
    worker_envs = [e for e in captured_envs if "GARDEN_TASK_ID" in e]
    assert worker_envs, "no worker subprocess.Popen call was intercepted (with GARDEN_TASK_ID)"
    env = worker_envs[0]
    assert "GARDEN_ROOT" in env, "GARDEN_ROOT must be set in worker environment"
    # The sentinel path must not be a valid garden (no garden.yaml there)
    assert not (Path(env["GARDEN_ROOT"]) / "garden.yaml").exists(), (
        f"worker GARDEN_ROOT={env['GARDEN_ROOT']!r} must not point at a real garden"
    )
