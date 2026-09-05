"""CG-147: a fresh product clone gets a git identity, so a commit made inside it by the
scheduler or a worker never fails with "Author identity unknown"."""

from __future__ import annotations

import subprocess
from pathlib import Path

from garden import gitops
from garden.scheduler import Scheduler


def _isolate_ambient_git_config(monkeypatch, tmp_path: Path) -> None:
    """No test may depend on whatever git identity happens to be configured on the machine
    running it — that ambient identity papering over a clone with none of its own is exactly
    the bug CG-147 fixes."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-such-gitconfig"))


def _clone_via_url(sched: Scheduler, tmp_path: Path) -> Path:
    """Point the `demo` product at the fixture's bare remote by URL, so `repo_for` actually
    clones it under repos_dir instead of returning the fixture's local path repo as-is."""
    remote = tmp_path / "remote.git"
    sched.cfg.data["products"]["demo"]["repo"] = f"file://{remote}"
    task = sched.store.task("DM-001")
    return sched.repo_for(task)


def test_fresh_clone_gets_the_configured_git_identity(sched, tmp_path, monkeypatch):
    _isolate_ambient_git_config(monkeypatch, tmp_path)
    sched.cfg.data["git"] = {"user_name": "Garden Bot", "user_email": "bot@example.com"}

    clone = _clone_via_url(sched, tmp_path)

    assert gitops.identity(clone) == ("Garden Bot", "bot@example.com")


def test_fresh_clone_falls_back_to_the_garden_checkouts_own_git_config(sched, tmp_path, monkeypatch):
    _isolate_ambient_git_config(monkeypatch, tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=sched.store.root, check=True)
    subprocess.run(["git", "config", "user.name", "Checkout Owner"], cwd=sched.store.root, check=True)
    subprocess.run(["git", "config", "user.email", "owner@example.com"], cwd=sched.store.root, check=True)

    clone = _clone_via_url(sched, tmp_path)

    assert gitops.identity(clone) == ("Checkout Owner", "owner@example.com")


def test_fresh_clone_falls_back_to_the_gh_login(sched, tmp_path, monkeypatch):
    """No `git.user_name`/`git.user_email` in garden.yaml and no identity on the garden
    checkout: the authenticated `gh` login (the `fake_github` stand-in's `.me()`, "garden-bot")
    becomes the clone's identity, with a GitHub noreply email."""
    _isolate_ambient_git_config(monkeypatch, tmp_path)

    clone = _clone_via_url(sched, tmp_path)

    assert gitops.identity(clone) == ("garden-bot", "garden-bot@users.noreply.github.com")


def test_an_existing_clone_is_left_alone(sched, tmp_path, monkeypatch):
    """Only a fresh clone is stamped with an identity: re-resolving an already-cloned repo, or
    changing garden.yaml's git identity afterwards, never rewrites it."""
    _isolate_ambient_git_config(monkeypatch, tmp_path)
    sched.cfg.data["git"] = {"user_name": "First Bot", "user_email": "first@example.com"}
    clone = _clone_via_url(sched, tmp_path)
    assert gitops.identity(clone) == ("First Bot", "first@example.com")

    sched.cfg.data["git"] = {"user_name": "Second Bot", "user_email": "second@example.com"}
    same_clone = _clone_via_url(sched, tmp_path)

    assert same_clone == clone
    assert gitops.identity(clone) == ("First Bot", "first@example.com")
