"""The worktree fence (CG-111): a worker's writes are confined to its own worktree by the
runner. The harness denies edits outside the worktree; finalize reverts anything that got
through to the live garden or the product clone and fails the run with a card for the Inbox."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from garden.gitops import head_sha
from garden.harness import Harness
from garden.inbox import build_inbox

from .conftest import wait_for_runs


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    """Turn the live garden root into a git repo (it is not one by default in the fixture)."""
    (path / ".gitignore").write_text(".garden/\nno-live-garden/\n")
    _git("init", "-q", "-b", "main", cwd=path)
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "garden", cwd=path)


def _attention_card(sched, task_id: str) -> str:
    items = build_inbox(sched.store, sched)
    for it in items:
        if it["group"] == "attention" and it["task"] == task_id:
            return str(it["why"])
    return ""


# ---- belt and braces: finalize reverts and fails --------------------------

def test_worker_writing_to_product_clone_is_reverted_and_fails(sched, monkeypatch, tmp_path):
    clone = tmp_path / "repo"  # the product's clone; the fixture's product repo
    before_head = head_sha(clone)
    before_readme = (clone / "README.md").read_text()

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "escape")
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_DIR", str(clone))
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_FILE", "README.md")

    sched.tick()            # dispatch DM-001
    wait_for_runs(sched)
    sched.tick()            # reap -> fence check

    task = sched.store.task("DM-001")
    assert task.status.value == "failed"
    # the write is undone: no runaway commit, file content restored
    assert head_sha(clone) == before_head
    assert (clone / "README.md").read_text() == before_readme
    # the Inbox says what it touched
    card = _attention_card(sched, "DM-001")
    assert "product clone" in card and "README.md" in card
    # the run itself is marked failed, not pushed
    assert not sched.github.created


def test_worker_writing_to_live_garden_is_reverted_and_fails(sched, garden, monkeypatch):
    _init_repo(garden)
    before_head = head_sha(garden)
    before_cfg = (garden / "garden.yaml").read_text()

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "escape")
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_DIR", str(garden))
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_FILE", "garden.yaml")

    sched.tick()
    wait_for_runs(sched)
    sched.tick()

    task = sched.store.task("DM-001")
    assert task.status.value == "failed"
    assert head_sha(garden) == before_head
    assert (garden / "garden.yaml").read_text() == before_cfg
    card = _attention_card(sched, "DM-001")
    assert "live garden" in card and "garden.yaml" in card


def test_uncommitted_escape_is_reverted(sched, monkeypatch, tmp_path):
    """A worker that edits outside its worktree without committing is still caught and undone."""
    clone = tmp_path / "repo"
    before_readme = (clone / "README.md").read_text()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "escape")
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_DIR", str(clone))
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_FILE", "README.md")
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_COMMIT", "0")  # write but do not commit

    sched.tick()
    wait_for_runs(sched)
    sched.tick()

    assert sched.store.task("DM-001").status.value == "failed"
    assert (clone / "README.md").read_text() == before_readme


def test_scheduler_task_file_edits_do_not_trip_the_fence(sched, garden, monkeypatch):
    """The scheduler edits task files in the live garden during a run; that is its own and
    must not be mistaken for a worker escape."""
    _init_repo(garden)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")  # a well-behaved worker

    sched.tick()
    wait_for_runs(sched)
    sched.tick()

    task = sched.store.task("DM-001")
    assert task.status.value not in ("failed",)  # reached review, not fenced
    assert not _attention_card(sched, "DM-001")


def test_fence_ignores_scheduler_owned_commits(sched, tmp_path):
    """A commit touching only task files or .garden/ (e.g. `garden sync`) is the scheduler's
    own and must not be reverted; a commit touching other files is an escape."""
    clone = tmp_path / "repo"
    task = sched.store.task("DM-001")

    sched._fence_snapshot(task)
    owned = clone / "demo" / "p1" / "tasks" / "x.md"
    owned.parent.mkdir(parents=True, exist_ok=True)
    owned.write_text("owned\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "garden: update task state", cwd=clone)
    head_after_sync = head_sha(clone)
    assert sched._fence_check(task) == []       # ignored, not reverted
    assert head_sha(clone) == head_after_sync    # the sync commit survives

    sched._fence_snapshot(task)
    (clone / "code.py").write_text("x = 1\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "rogue", cwd=clone)
    violations = sched._fence_check(task)
    assert violations and "code.py" in violations[0]["files"]
    assert head_sha(clone) == head_after_sync    # the rogue commit is dropped


# ---- first line of defence: the harness deny rules ------------------------

def test_command_fences_writes_to_deny_paths():
    h = Harness("claude", {"bin": "/x/claude"})
    cmd = h.command("opus", deny_paths=["/live/garden", "/clones/demo"])
    assert "--settings" in cmd
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    deny = settings["permissions"]["deny"]
    assert "Edit(//live/garden/**)" in deny
    assert "Write(//clones/demo/**)" in deny
    assert any(d.startswith("Bash(cd /live/garden") for d in deny)
    # edits inside the worktree still auto-accept: acceptEdits mode is untouched
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_command_without_deny_paths_has_no_settings():
    assert "--settings" not in Harness("claude", {"bin": "/x/claude"}).command("opus")


def test_bypass_mode_skips_the_fence():
    cmd = Harness("claude", {"permission_mode": "bypass"}).command("o", deny_paths=["/x"])
    assert "--settings" not in cmd


def test_sandbox_is_opt_in():
    plain = Harness("claude", {"bin": "/x/claude"}).command("o", deny_paths=["/g"], worktree="/wt")
    assert "sandbox" not in json.loads(plain[plain.index("--settings") + 1])
    boxed = Harness("claude", {"bin": "/x/claude", "sandbox": True}).command("o", deny_paths=["/g"], worktree="/wt")
    s = json.loads(boxed[boxed.index("--settings") + 1])
    assert s["sandbox"]["filesystem"]["allowWrite"][0] == "/wt"
    assert "/x" not in s["sandbox"]["filesystem"]["allowWrite"]
