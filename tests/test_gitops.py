"""Tests for gitops.push rebase-detection logic."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from garden import gitops

GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _sha(cwd: Path, ref: str = "HEAD") -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin_repo(tmp_path: Path):
    """Bare remote + local clone with one commit on main, both pointing at the same sha."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    return repo, remote


def test_fast_forward_push_unchanged(origin_repo: tuple[Path, Path]) -> None:
    """origin/branch is ancestor of HEAD: plain push, no rebase note."""
    repo, remote = origin_repo

    # Create and push a feature branch
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "first", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature", cwd=repo)

    # Add another commit on top (fast-forward from origin/feature)
    _git("commit", "--allow-empty", "-q", "-m", "second", cwd=repo)

    note = gitops.push(repo, "feature", base="main")

    assert note == ""
    # Origin received the new commit
    assert _sha(remote, "refs/heads/feature") == _sha(repo)


def test_rebased_branch_pushes_with_lease(origin_repo: tuple[Path, Path]) -> None:
    """origin/branch not ancestor of HEAD, but origin/base is: force-with-lease, log note."""
    repo, remote = origin_repo

    # Create and push feature branch on current main
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "feature work", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature", cwd=repo)

    # Advance main on origin (simulates another commit landing on main)
    _git("checkout", "main", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "main advance", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    # Worker fetches and rebases feature onto new origin/main
    _git("checkout", "feature", cwd=repo)
    _git("fetch", "origin", cwd=repo)
    _git("rebase", "origin/main", cwd=repo)

    # Conditions: origin/feature != ancestor of HEAD; origin/main == ancestor of HEAD
    note = gitops.push(repo, "feature", base="main")

    assert note == "rebased branch force-pushed"
    assert _sha(remote, "refs/heads/feature") == _sha(repo)


def test_diverged_not_rebased_fails(origin_repo: tuple[Path, Path]) -> None:
    """origin/branch not ancestor of HEAD, origin/base not ancestor either: plain push fails."""
    repo, remote = origin_repo

    # Create and push feature branch
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "feature work", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature", cwd=repo)

    # Build an orphan commit so HEAD's ancestry does NOT include origin/main
    _git("checkout", "--orphan", "_scratch", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "orphan root", cwd=repo)
    orphan_sha = _sha(repo)

    # Reset feature to the orphan commit (history is now unrelated to main)
    _git("checkout", "feature", cwd=repo)
    _git("reset", "--hard", orphan_sha, cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "diverged work", cwd=repo)
    _git("branch", "-D", "_scratch", cwd=repo)

    # Neither origin/feature nor origin/main is an ancestor of HEAD
    with pytest.raises(gitops.GitError):
        gitops.push(repo, "feature", base="main")

    # Origin was NOT updated
    assert _sha(remote, "refs/heads/feature") != _sha(repo)


def test_sync_remote_branch_folds_in_remote_only_commits(origin_repo: tuple[Path, Path]) -> None:
    """A commit that exists only on origin/<branch> is rebased into HEAD before a rebase round."""
    repo, remote = origin_repo

    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "feature work", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature", cwd=repo)
    local_before = _sha(repo)

    # a second checkout pushes an extra commit straight to origin/feature
    other = repo.parent / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    _git("checkout", "feature", cwd=other)
    _git("commit", "--allow-empty", "-q", "-m", "remote-only", cwd=other)
    _git("push", "-q", "origin", "feature", cwd=other)
    remote_sha = _sha(other)

    ok, files = gitops.sync_remote_branch(repo, "feature")

    assert ok and files == []
    # HEAD now includes the remote-only commit (it was behind, so it fast-forwarded)
    assert _sha(repo) == remote_sha
    assert local_before != remote_sha


def test_sync_remote_branch_noop_without_remote_commits(origin_repo: tuple[Path, Path]) -> None:
    """When the remote branch holds nothing extra (or does not exist), HEAD is untouched."""
    repo, _ = origin_repo
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "feature work", cwd=repo)
    head = _sha(repo)

    ok, files = gitops.sync_remote_branch(repo, "feature")  # no origin/feature yet

    assert ok and files == [] and _sha(repo) == head
