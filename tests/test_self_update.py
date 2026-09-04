"""Tests for self-update: garden fast-forwards its own checkout after a repo:. PR merges."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from garden.inbox import build_inbox
from garden.model import Status
from garden.scheduler import Scheduler, TickReport
from garden.store import Store

GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _sha(cwd: Path, ref: str = "HEAD") -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd, capture_output=True, text=True).stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


@pytest.fixture
def self_garden(tmp_path: Path):
    """Garden whose root IS the product repo (repo: .), backed by a bare remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)

    root = tmp_path / "garden"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("remote", "add", "origin", str(remote), cwd=root)

    # garden.yaml lives in the garden root (which is also the product repo)
    (root / "garden.yaml").write_text(yaml.safe_dump({
        "name": "test-self",
        "max_attempts": 2,
        "max_revisions": 2,
        "max_parallel": 2,
        "timeout_minutes": 1,
        "review": {"enabled": False},
        "github": {"draft_pr": False},
        "harnesses": {},
        "products": {
            "context-garden": {
                "repo": ".",
                "base_branch": "main",
                "id_prefix": "CG",
                "github": "test/context-garden",
            }
        },
    }))
    write(root / "principles" / "00-index.md", "# Digest\n\n- be good\n")
    write(root / "context-garden" / "product.md", "# context-garden\n\nThe tool.\n")
    write(root / "context-garden" / "p1" / "goals.md", "# p1\n\nShip it.\n")
    write(root / "context-garden" / "p1" / "tasks" / "CG-001.md", """
        ---
        id: CG-001
        title: First task
        status: in_review
        depends_on: []
        priority: 1
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        branch: garden/cg-001-first-task
        pr: https://github.com/test/context-garden/pull/1
        ---

        ## Goal

        Do the first thing.
        """)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    _git("push", "-q", "-u", "origin", "main", cwd=root)
    return root


@pytest.fixture
def self_sched(self_garden):
    from garden.github import PRInfo
    from tests.conftest import FakeGitHub

    gh = FakeGitHub()
    gh.prs["garden/cg-001-first-task"] = PRInfo(
        number=1,
        url="https://github.com/test/context-garden/pull/1",
        state="OPEN",
        title="CG-001: First task",
        head="garden/cg-001-first-task",
        base="main",
        updated_at="t1",
        is_draft=False,
    )
    store = Store(self_garden)
    return Scheduler(store, github=gh, log=print)


def _advance_origin(root: Path, message: str = "new commit on origin") -> str:
    """Simulate a new commit landing on origin/main without touching the local checkout.

    Uses git commit-tree on the bare remote so no intermediate clone is needed.
    Returns the new sha of origin/main.
    """
    remote = root.parent / "remote.git"
    parent = _sha(remote, "refs/heads/main")
    tree = subprocess.run(
        ["git", "rev-parse", f"{parent}^{{tree}}"],
        cwd=remote, capture_output=True, text=True, check=True,
    ).stdout.strip()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    new_commit = subprocess.run(
        ["git", "commit-tree", tree, "-p", parent, "-m", message],
        cwd=remote, capture_output=True, text=True, env=env,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/heads/main", new_commit],
        cwd=remote, check=True, capture_output=True,
    )
    return new_commit


# ---- fast-forward on clean tree -------------------------------------------

def test_clean_tree_fast_forwards(self_sched, self_garden):
    """After a merge into repo: ., the garden checkout is fast-forwarded to origin/main."""
    new_sha = _advance_origin(self_garden, "fix: the scheduler bug")
    task = self_sched.store.task("CG-001")
    rep = TickReport()

    self_sched._self_update(task, rep)

    # The local main branch was advanced to the new sha
    assert _sha(self_garden, "refs/heads/main") == new_sha
    assert f"self-updated to {new_sha[:8]}" in rep.transitions[0]

    # State reflects the update
    meta = self_sched.state.get("_self")
    assert meta["needs_restart"] is True
    assert meta["updated_sha"] == new_sha
    assert "updated_at" in meta


def test_already_up_to_date_is_a_noop(self_sched, self_garden):
    """If origin/main is already the local HEAD, no state change happens."""
    task = self_sched.store.task("CG-001")
    rep = TickReport()

    self_sched._self_update(task, rep)

    assert rep.transitions == []
    meta = self_sched.state.get("_self")
    assert not meta.get("needs_restart")


# ---- dirty tree ------------------------------------------------------------

def test_dirty_tree_skipped_with_warning(self_sched, self_garden):
    """If the garden checkout has changes outside tasks/ and .garden/, skip and warn."""
    new_sha = _advance_origin(self_garden, "origin advance")
    # Create a dirty file outside the safe prefixes
    (self_garden / "dirty.py").write_text("# oops\n")
    task = self_sched.store.task("CG-001")
    rep = TickReport()

    self_sched._self_update(task, rep)

    # Local main was NOT updated
    old_sha = _sha(self_garden, "refs/heads/main")
    assert old_sha != new_sha  # local is still behind

    assert "self-update skipped (dirty tree)" in rep.transitions[0]
    meta = self_sched.state.get("_self")
    assert not meta.get("needs_restart")
    assert "dirty_warning" in meta


def test_tasks_dir_changes_not_dirty(self_sched, self_garden):
    """Changes in tasks/ are considered safe and don't block the fast-forward."""
    _advance_origin(self_garden, "fix: a real fix")
    # Simulate a task file modification (the scheduler edits these).
    # The path is <product>/<phase>/tasks/<id>.md — not a root-level tasks/ dir.
    task_file = self_garden / "context-garden" / "p1" / "tasks" / "CG-001.md"
    task_file.write_text(task_file.read_text() + "<!-- scheduler edit -->\n")
    task = self_sched.store.task("CG-001")
    rep = TickReport()

    self_sched._self_update(task, rep)

    assert "self-updated" in rep.transitions[0]
    meta = self_sched.state.get("_self")
    assert meta.get("needs_restart") is True


# ---- inbox and status show the notice -------------------------------------

def test_inbox_shows_restart_notice(self_sched, self_garden):
    """After self-update, the Inbox includes a restart item."""
    _advance_origin(self_garden, "fix: something")
    task = self_sched.store.task("CG-001")
    self_sched._self_update(task, TickReport())
    self_sched.state.save()

    items = build_inbox(self_sched.store, self_sched)
    restart_items = [i for i in items if i["group"] == "restart"]
    assert restart_items, "expected a restart item in the inbox"
    assert "restart" in restart_items[0]["why"].lower() or "updated" in restart_items[0]["why"].lower()


def test_inbox_shows_dirty_warning(self_sched, self_garden):
    """After a failed self-update due to dirty tree, the Inbox shows the dirty warning."""
    _advance_origin(self_garden, "fix: something")
    (self_garden / "oops.py").write_text("# dirty\n")
    task = self_sched.store.task("CG-001")
    self_sched._self_update(task, TickReport())
    self_sched.state.save()

    items = build_inbox(self_sched.store, self_sched)
    restart_items = [i for i in items if i["group"] == "restart"]
    assert restart_items
    assert "uncommitted changes" in restart_items[0]["title"].lower() or "dirty" in restart_items[0]["why"].lower()


# ---- clear flag on restart ------------------------------------------------

def test_flag_cleared_on_new_process_with_updated_code(self_sched, self_garden):
    """When a new Scheduler starts and HEAD already matches updated_sha, the flag clears."""
    _advance_origin(self_garden, "fix: something")
    task = self_sched.store.task("CG-001")
    self_sched._self_update(task, TickReport())
    self_sched.state.save()
    assert self_sched.state.get("_self")["needs_restart"] is True

    # Simulate a process restart: a new Scheduler is created, and the local branch
    # is already at the updated sha (as if the user restarted after the FF).
    from tests.conftest import FakeGitHub
    gh2 = FakeGitHub()
    store2 = Store(self_garden)
    sched2 = Scheduler(store2, github=gh2, log=print)

    # The flag should be cleared because HEAD == updated_sha
    meta = sched2.state.get("_self")
    assert not meta.get("needs_restart")


# ---- on_merged integration -------------------------------------------------

def test_on_merged_triggers_self_update_for_self_repo(self_sched, self_garden):
    """_on_merged calls _self_update when the product's repo is the garden root."""
    _advance_origin(self_garden, "merged fix")
    task = self_sched.store.task("CG-001")
    task.status = Status.DONE
    rep = TickReport()

    self_sched._on_merged(task, rep)

    meta = self_sched.state.get("_self")
    assert meta.get("needs_restart") is True


def test_on_merged_skips_self_update_for_other_repos(tmp_path, fake_github, garden, sched):
    """For non-self repos, _on_merged does NOT set the restart flag."""
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    rep = TickReport()

    sched._on_merged(task, rep)

    meta = sched.state.get("_self")
    assert not meta.get("needs_restart")
