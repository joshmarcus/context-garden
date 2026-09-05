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


def _parent_branch(repo: Path) -> str:
    """Push a parent branch (main + one commit) to origin; return its sha. Leaves repo on main."""
    _git("checkout", "-b", "garden/parent", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "parent work", cwd=repo)
    _git("push", "-q", "-u", "origin", "garden/parent", cwd=repo)
    sha = _sha(repo)
    _git("checkout", "main", cwd=repo)
    return sha


def test_prepare_worktree_fast_forwards_stacked_child_onto_parent(origin_repo, tmp_path) -> None:
    """A stacked child whose branch has no commits of its own must be checked out on the
    parent branch, not left at main (the recurring stacked-provisioning bug, CG-126)."""
    repo, _ = origin_repo
    parent_sha = _parent_branch(repo)
    # A child branch left over on disk pointing at main (an earlier dispatch before the parent
    # had a PR), with no commits of its own.
    _git("branch", "garden/child", "main", cwd=repo)

    wt = tmp_path / "wt-child"
    gitops.prepare_worktree(repo, wt, "garden/child", base="garden/parent")

    assert _sha(wt) == parent_sha  # the child now sits on the parent branch


def test_prepare_worktree_reuse_fast_forwards_onto_parent(origin_repo, tmp_path) -> None:
    """Re-provisioning an existing worktree (reuse path) also moves a commit-less branch from a
    stale base onto the parent it now stacks on."""
    repo, _ = origin_repo
    parent_sha = _parent_branch(repo)

    wt = tmp_path / "wt-child"
    gitops.prepare_worktree(repo, wt, "garden/child", base="main")  # early dispatch, based on main
    assert _sha(wt) == _sha(repo, "main")

    gitops.prepare_worktree(repo, wt, "garden/child", base="garden/parent")  # now stacks on parent
    assert _sha(wt) == parent_sha


def test_prepare_worktree_keeps_child_commits_on_stale_base(origin_repo, tmp_path) -> None:
    """A branch that carries its own commits on a different base is left untouched: a reset
    would discard the work, and rebasing is the restack path's job, not provisioning's."""
    repo, _ = origin_repo
    _parent_branch(repo)
    # A child with its own commit, built on main rather than the parent.
    _git("checkout", "-b", "garden/child", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "child work", cwd=repo)
    child_sha = _sha(repo)
    _git("checkout", "main", cwd=repo)

    wt = tmp_path / "wt-child"
    gitops.prepare_worktree(repo, wt, "garden/child", base="garden/parent")

    assert _sha(wt) == child_sha  # own work preserved
    # the parent tip was NOT silently merged into the child
    not_merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "origin/garden/parent", "HEAD"], cwd=wt
    )
    assert not_merged.returncode != 0


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
