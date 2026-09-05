"""A product may point at the garden's own repo (`self: true`): its tasks change the
garden's own files (a friction document, the next phase's goals, `garden.yaml`) and land
as PRs to the garden repo, in a worktree of that repo, with the same fence and checks as
any product task. The live garden is never edited by a worker (see docs/architecture.md).
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from garden.cli import app
from garden.config import find_root
from garden.model import Status
from garden.scheduler import Scheduler
from garden.store import Store
from tests.conftest import FAKE_CLAUDE

runner = CliRunner()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


def _garden_repo(tmp_path: Path) -> str:
    """A stand-in for the garden's own repo: a working checkout with a bare origin, whose
    files include a garden.yaml and a friction document a worker would edit. Returns the
    path (relative-friendly absolute) to the working checkout."""
    repo = tmp_path / "garden-repo"
    remote = tmp_path / "garden-remote.git"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _write(repo / "garden.yaml", "name: real-garden\n")
    _write(repo / "docs" / "friction.md", "# Friction\n\nnothing yet\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    return str(repo)


def _live_garden(tmp_path: Path, *, repo: str, work_dir: str | None) -> Path:
    """A live garden with a single self product `gdn` pointing at `repo`."""
    root = tmp_path / "live"
    root.mkdir()
    cfg: dict = {
        "name": "live",
        "max_parallel": 2,
        "timeout_minutes": 1,
        "review": {"enabled": False},
        "github": {"draft_pr": False},
        "harnesses": {"claude": {"bin": str(FAKE_CLAUDE), "max_turns": {"easy": 40, "medium": 5, "hard": 80}}},
        "products": {"gdn": {"repo": repo, "base_branch": "main", "id_prefix": "GD",
                             "self": True, "github": "test/garden"}},
    }
    if work_dir is not None:
        cfg["work_dir"] = work_dir
    (root / "garden.yaml").write_text(yaml.safe_dump(cfg))
    _write(root / "principles" / "00-index.md", "# Digest\n\n- be good\n")
    _write(root / "gdn" / "product.md", "# the garden\n\nThe garden's own files.\n")
    _write(root / "gdn" / "p1" / "goals.md", "# p1\n\nClose the phase.\n")
    _write(root / "gdn" / "p1" / "tasks" / "GD-001-friction.md", """
        ---
        id: GD-001
        title: Write the friction document
        status: ready
        depends_on: []
        priority: 1
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Fill in docs/friction.md for the phase.
        """)
    return root


def _run_cli(cwd: Path, *args: str):
    import os

    old = os.getcwd()
    os.chdir(cwd)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(old)


def test_self_product_runs_in_worktree_of_the_garden_repo_and_opens_pr(tmp_path, fake_github, monkeypatch):
    """A task in a self product is dispatched to a worktree of the garden repo; the worker
    commits there, the scheduler pushes and opens a PR to the garden repo."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    work = str(tmp_path / "work")  # work_dir OUTSIDE the live garden
    root = _live_garden(tmp_path, repo=repo, work_dir=work)

    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    sched.tick()  # dispatch GD-001
    sched.tick()  # reap -> push -> PR

    task = store.task("GD-001")
    assert task.status == Status.IN_REVIEW, task.status
    # a PR was opened to the garden repo (its slug), based on main
    assert fake_github.created, "no PR opened"
    assert fake_github.created[0]["base"] == "main"
    # the worktree is a checkout of the garden repo itself: it has the garden's own files
    wt = sched.worktree_for(task)
    assert (wt / "garden.yaml").exists(), "worktree is not a checkout of the garden repo"
    assert (wt / "docs" / "friction.md").exists()
    # and it sits under work_dir, outside the live garden
    assert Path(work).resolve() in wt.resolve().parents
    assert root.resolve() not in wt.resolve().parents


def test_fence_allows_self_worktree_denies_live_garden(tmp_path):
    """The fence resolves a worker's garden worktree to that worktree's own garden.yaml,
    and still refuses to act on the enclosing live garden from inside its .garden/."""
    live = tmp_path / "live"
    live.mkdir()
    (live / "garden.yaml").write_text("name: live\n")
    # denies the live garden: find_root from inside its .garden/ refuses
    inner = live / ".garden" / "worktrees" / "GD-001"
    inner.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match=r"inside its \.garden/"):
        find_root(inner)
    # allows the worker's garden worktree: a checkout of the garden repo, outside the live
    # garden, with its own garden.yaml — find_root resolves to it, not the live garden
    wt = tmp_path / "work" / "worktrees" / "GD-001"
    wt.mkdir(parents=True)
    (wt / "garden.yaml").write_text("name: worktree\n")
    sub = wt / "docs"
    sub.mkdir()
    assert find_root(sub) == wt


def test_doctor_shows_self_product_and_refuses_work_dir_inside_live_garden(tmp_path):
    """`garden doctor` shows a self product and refuses a work_dir inside the live garden
    (default work_dir is `.garden`, inside), so the garden's clone and worktrees stay out
    of the live checkout."""
    # a URL repo means doctor doesn't need an on-disk git repo for this product
    root = _live_garden(tmp_path, repo="https://example.com/garden.git", work_dir=None)
    r = _run_cli(root, "doctor")
    assert r.exit_code == 1, r.output
    out = " ".join(r.output.split())
    assert "product gdn" in out and "self: the garden's own repo" in out
    assert "inside the live garden" in out


def test_doctor_allows_self_product_with_work_dir_outside(tmp_path):
    """With work_dir set outside the live garden, the self-product work_dir check passes
    (other doctor checks may still fail in this environment, but not this one)."""
    root = _live_garden(tmp_path, repo="https://example.com/garden.git",
                        work_dir=str(tmp_path / "work"))
    r = _run_cli(root, "doctor")
    out = " ".join(r.output.split())
    assert "inside the live garden" not in out


def test_doctor_refuses_self_repo_pointing_at_the_live_garden(tmp_path):
    """A self product whose repo resolves to the live garden root is refused: a worker must
    edit a fresh clone, never the live checkout."""
    root = _live_garden(tmp_path, repo=".", work_dir=str(tmp_path / "work"))
    r = _run_cli(root, "doctor")
    assert r.exit_code == 1, r.output
    out = " ".join(r.output.split())
    assert "points at the live garden itself" in out
