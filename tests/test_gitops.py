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


# ---- CG-220: sync_to_origin_head and a lease-protected push ------------------
def test_sync_to_origin_head_backs_up_local_only_commits(origin_repo: tuple[Path, Path]) -> None:
    """A worktree with commits origin never saw (a killed prior run's progress) is reset onto
    origin's head, and those commits are kept on a named backup ref, not dropped."""
    repo, remote = origin_repo
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "pushed work", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature", cwd=repo)
    origin_sha = _sha(repo)

    _git("commit", "--allow-empty", "-q", "-m", "stray local commit", cwd=repo)
    local_sha = _sha(repo)

    subjects = gitops.sync_to_origin_head(repo, "feature", "backup/run-1")

    assert len(subjects) == 1 and subjects[0].endswith("stray local commit")
    assert _sha(repo) == origin_sha  # reset onto origin's head
    assert _sha(repo, "backup/run-1") == local_sha  # nothing lost


def test_sync_to_origin_head_noop_when_already_synced(origin_repo: tuple[Path, Path]) -> None:
    repo, _ = origin_repo
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "work", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature", cwd=repo)
    head = _sha(repo)

    subjects = gitops.sync_to_origin_head(repo, "feature", "backup/run-1")

    assert subjects == []
    assert _sha(repo) == head
    assert not gitops.branch_exists(repo, "backup/run-1")


def test_sync_to_origin_head_noop_without_an_origin_branch(origin_repo: tuple[Path, Path]) -> None:
    """A branch never pushed to origin has nothing to sync to: left untouched."""
    repo, _ = origin_repo
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "work", cwd=repo)
    head = _sha(repo)

    subjects = gitops.sync_to_origin_head(repo, "feature", "backup/run-1")

    assert subjects == [] and _sha(repo) == head


def test_push_with_matching_lease_succeeds(origin_repo: tuple[Path, Path]) -> None:
    repo, remote = origin_repo
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "first", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature", cwd=repo)
    start_head = _sha(repo)
    _git("commit", "--allow-empty", "-q", "-m", "second", cwd=repo)

    gitops.push(repo, "feature", lease=start_head)

    assert _sha(remote, "refs/heads/feature") == _sha(repo)


def test_push_with_stale_lease_raises_naming_both_heads(origin_repo: tuple[Path, Path]) -> None:
    """Someone else pushed to the same branch after this run started: the lease push is
    rejected with both the head we started from and the head origin actually holds now."""
    repo, remote = origin_repo
    _git("checkout", "-b", "feature", cwd=repo)
    _git("commit", "--allow-empty", "-q", "-m", "first", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature", cwd=repo)
    start_head = _sha(repo)

    other = repo.parent / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    _git("checkout", "feature", cwd=other)
    _git("commit", "--allow-empty", "-q", "-m", "interloper", cwd=other)
    _git("push", "-q", "origin", "feature", cwd=other)
    interloper_sha = _sha(other)

    _git("commit", "--allow-empty", "-q", "-m", "this run's work", cwd=repo)

    with pytest.raises(gitops.LeaseRejected) as exc:
        gitops.push(repo, "feature", lease=start_head)

    assert exc.value.expected == start_head
    assert exc.value.actual == interloper_sha
    assert _sha(remote, "refs/heads/feature") == interloper_sha  # origin untouched by the rejected push


# ---- CG-210: patch id is blind to context/line-number churn, not to real content changes ----
def _write_lines(path: Path, n: int) -> None:
    path.write_text("".join(f"line{i}\n" for i in range(1, n + 1)))


def test_patch_id_unchanged_when_base_only_shifts_context(origin_repo: tuple[Path, Path]) -> None:
    """A base commit that inserts lines next to (not on) the branch's own changed line moves the
    diff's hunk-header line numbers and its surrounding context, but not patch id: `diff_hash`
    (a plain hash of the diff text) is affected by exactly this churn; patch_id must not be."""
    repo, _ = origin_repo
    _write_lines(repo / "shared.txt", 10)
    _git("add", "shared.txt", cwd=repo)
    _git("commit", "-q", "-m", "shared.txt", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    _git("checkout", "-b", "feature", cwd=repo)
    lines = (repo / "shared.txt").read_text().splitlines()
    lines[5] = "line6-changed"
    (repo / "shared.txt").write_text("\n".join(lines) + "\n")
    _git("commit", "-aq", "-m", "feature changes line6", cwd=repo)
    before = gitops.patch_id(repo, "main")
    assert before

    _git("checkout", "main", cwd=repo)
    text = (repo / "shared.txt").read_text().splitlines()
    text = text[:2] + ["inserted-a", "inserted-b"] + text[2:]
    (repo / "shared.txt").write_text("\n".join(text) + "\n")
    _git("commit", "-aq", "-m", "main inserts lines near the hunk", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    _git("checkout", "feature", cwd=repo)
    _git("rebase", "main", cwd=repo)
    after = gitops.patch_id(repo, "main")

    assert after == before


def test_patch_id_changes_when_the_branchs_own_content_changes(origin_repo: tuple[Path, Path]) -> None:
    """When the base independently carries the identical edit, a clean rebase folds the branch's
    commit away as already-applied: the resulting diff is empty, so patch id must differ."""
    repo, _ = origin_repo
    _write_lines(repo / "shared.txt", 5)
    _git("add", "shared.txt", cwd=repo)
    _git("commit", "-q", "-m", "shared.txt", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    _git("checkout", "-b", "feature", cwd=repo)
    lines = (repo / "shared.txt").read_text().splitlines()
    lines[2] = "line3-changed"
    (repo / "shared.txt").write_text("\n".join(lines) + "\n")
    _git("commit", "-aq", "-m", "feature changes line3", cwd=repo)
    before = gitops.patch_id(repo, "main")
    assert before

    _git("checkout", "main", cwd=repo)
    text = (repo / "shared.txt").read_text().splitlines()
    text[2] = "line3-changed"
    (repo / "shared.txt").write_text("\n".join(text) + "\n")
    _git("commit", "-aq", "-m", "main makes the identical change", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    _git("checkout", "feature", cwd=repo)
    _git("rebase", "main", cwd=repo)
    after = gitops.patch_id(repo, "main")

    assert after != before
    assert after == ""  # the branch's commit contributed nothing once rebased; the diff is empty
