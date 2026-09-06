"""The worktree fence (CG-111): a worker's writes are confined to its own worktree by the
runner. The harness denies edits outside the worktree; finalize reverts anything that got
through to the live garden or the product clone and fails the run with a card for the Inbox."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from garden import gitops
from garden.gitops import head_sha
from garden.harness import Harness
from garden.inbox import build_inbox


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
    sched.tick()

    assert sched.store.task("DM-001").status.value == "failed"
    assert (clone / "README.md").read_text() == before_readme


def test_scheduler_task_file_edits_do_not_trip_the_fence(sched, garden, monkeypatch):
    """The scheduler edits task files in the live garden during a run; that is its own and
    must not be mistaken for a worker escape."""
    _init_repo(garden)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")  # a well-behaved worker

    sched.tick()
    sched.tick()

    task = sched.store.task("DM-001")
    assert task.status.value not in ("failed",)  # reached review, not fenced
    assert not _attention_card(sched, "DM-001")


# ---- the config/state hash-check (CG-194) ---------------------------------

def test_fence_guard_targets_include_harness_config_files(sched):
    targets = {rel for rel, _, _ in sched._fence_guard_targets()}
    assert {"settings.json", "settings.local.json", "CLAUDE.md", "config.toml"} <= targets


def test_worker_writing_garden_yaml_is_caught_by_hash_check_without_git(sched, garden, monkeypatch):
    """The live garden is not a git repo here, so the git-based fence guards nothing; the
    hash-check still catches (and reverts) a worker write to garden.yaml."""
    before_cfg = (garden / "garden.yaml").read_text()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "escape")
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_DIR", str(garden))
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_FILE", "garden.yaml")
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_COMMIT", "0")  # no repo to commit into

    sched.tick()
    sched.tick()

    assert sched.store.task("DM-001").status.value == "failed"
    assert (garden / "garden.yaml").read_text() == before_cfg  # reverted from the snapshot
    card = _attention_card(sched, "DM-001")
    assert "live garden" in card and "garden.yaml" in card
    assert not sched.github.created


def test_worker_writing_state_json_is_caught_and_fails(sched, garden, monkeypatch):
    """A worker state.json write is detected, attributed and restored at reap."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "escape")
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_DIR", str(garden))
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_FILE", ".garden/state.json")
    monkeypatch.setenv("FAKE_CLAUDE_ESCAPE_COMMIT", "0")

    sched.tick()
    sched.tick()

    assert sched.store.task("DM-001").status.value == "failed"
    assert "state.json" in _attention_card(sched, "DM-001")
    full = sched.state.get("DM-001")["needs_human"]
    assert "state.json" in full and "reverted" in full.lower()
    assert not sched.github.created


def test_state_fence_restores_foreign_task_keys_from_dispatch_snapshot(sched):
    task = sched.store.task("DM-001")
    sched.state.get("DM-002")["foreign"] = "before"
    sched.state.save()
    run = _run_naming(sched, "DM-001", str(sched.state.path))
    sched._fence_snapshot(task, run)
    state = json.loads(sched.state.path.read_text())
    state["DM-002"]["foreign"] = "worker changed this"
    sched.state.path.write_text(json.dumps(state))

    violations = sched._fence_guard_check(task, run)

    assert violations and ".garden/state.json" in violations[0]["files"]
    assert json.loads(sched.state.path.read_text())["DM-002"]["foreign"] == "before"


def test_state_fence_preserves_foreign_scheduler_updates_after_dispatch(sched):
    task = sched.store.task("DM-001")
    sched.state.get("DM-002")["foreign"] = "before"
    sched.state.save()
    run = _run_naming(sched, "DM-001", str(sched.state.path))
    sched._fence_snapshot(task, run)
    # A scheduler update after dispatch is legitimate and must survive repairing the
    # worker's write to a different key in this task's state entry.
    sched.state.get("DM-002")["scheduler"] = "live"
    sched.state.save()
    state = json.loads(sched.state.path.read_text())
    state["DM-002"]["foreign"] = "worker changed this"
    sched.state.path.write_text(json.dumps(state))

    sched._fence_guard_check(task, run)

    restored = json.loads(sched.state.path.read_text())["DM-002"]
    assert restored == {"foreign": "before", "scheduler": "live"}


def test_fence_attributes_a_worker_edit_to_a_live_task_file(sched, garden):
    _init_repo(garden)
    task = sched.store.task("DM-001")
    task_path = garden / "demo" / "p1" / "tasks" / "DM-001-first.md"
    before = task_path.read_text()
    run = _run_naming(sched, "DM-001", str(task_path))
    sched._fence_snapshot(task, run)
    task_path.write_text("worker edit\n")

    violations = sched._fence_check(task, run)

    assert violations and "demo/p1/tasks/DM-001-first.md" in violations[0]["files"]
    assert task_path.read_text() == before


def test_fence_restores_attributed_sibling_run_output_and_audit_manifest(sched):
    task = sched.store.task("DM-001")
    sibling = sched.runs.new_run("DM-002", "local")
    output = sibling.path / "stdout.json"
    output.write_text("sibling before\n")
    run = _run_naming(sched, "DM-001", str(output))
    sched._fence_snapshot(task, run)
    output.write_text("worker redirect\n")

    violations = sched._fence_guard_check(task, run)

    assert violations and str(output.relative_to(sched.store.root)) in violations[0]["files"]
    assert output.read_text() == "sibling before\n"

    manifest = run.path / "fence_guard.json"
    before = manifest.read_text()
    (run.path / "stdout.json").write_text(json.dumps({"result": f"redirect {manifest}"}))
    manifest.write_text("worker redirect\n")
    violations = sched._fence_guard_check(task, run)
    assert violations and str(manifest.relative_to(sched.store.root)) in violations[0]["files"]
    assert manifest.read_text() == before


def test_fence_reuses_sibling_output_backup_across_dispatches(sched):
    task = sched.store.task("DM-001")
    sibling = sched.runs.new_run("DM-002", "local")
    output = sibling.path / "stdout.json"
    output.write_text("sibling output\n")
    first = _run_naming(sched, "DM-001", str(output))
    sched._fence_snapshot(task, first)
    first_entry = next(entry for entry in json.loads((first.path / "fence_guard.json").read_text())
                       if entry["abs"] == str(output))
    cache_file = sched.cfg.garden_dir / "fence-guard-cache" / first_entry["snap"].removeprefix("cache:")
    before = cache_file.stat()

    second = _run_naming(sched, "DM-001", str(output))
    sched._fence_snapshot(task, second)

    second_entry = next(entry for entry in json.loads((second.path / "fence_guard.json").read_text())
                        if entry["abs"] == str(output))
    assert second_entry["snap"] == first_entry["snap"]
    assert cache_file.stat().st_mtime_ns == before.st_mtime_ns


def test_reading_config_without_changing_it_does_not_trip_the_hash_check(sched, garden, monkeypatch):
    """A well-behaved worker leaves garden.yaml and state.json alone: no false positive."""
    _init_repo(garden)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "done")
    sched.tick()
    sched.tick()
    assert sched.store.task("DM-001").status.value != "failed"
    assert not _attention_card(sched, "DM-001")


def _run_naming(sched, task_id: str, *paths: str):
    """A fake run whose stdout.json names the given paths, as a real worker's transcript
    (Edit/Write file_path, Bash commands, final message) would."""
    run = sched.runs.new_run(task_id, "local")
    result = {"type": "result", "result": "I edited " + " and ".join(paths) + "."}
    (run.path / "stdout.json").write_text(json.dumps(result))
    return run


def test_fence_ignores_scheduler_owned_commits(sched, tmp_path):
    """A commit touching only task files or .garden/ (e.g. `garden sync`) is the scheduler's
    own and must not be reverted; a commit touching other files the worker named is an escape."""
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
    run = _run_naming(sched, "DM-001", str(clone / "code.py"))
    violations = sched._fence_check(task, run)
    assert violations and "code.py" in violations[0]["files"]
    assert head_sha(clone) == head_after_sync    # the rogue commit is dropped


def test_fence_leaves_changes_the_worker_did_not_make(sched, tmp_path):
    """A person edits the live garden (or a `git fetch` advances a clone) while a run is live.
    The worker's transcript never names those paths, so the fence must not revert them or fail
    the run — only a path the worker's transcript names is reverted, and a moved HEAD alone is
    not an escape."""
    clone = tmp_path / "repo"
    task = sched.store.task("DM-001")

    sched._fence_snapshot(task)
    # a human edits and commits a config file by hand during the run
    (clone / "config.yaml").write_text("changed by a person\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "human edit", cwd=clone)
    head_after_human = head_sha(clone)

    run = _run_naming(sched, "DM-001", str(clone / "unrelated.py"))  # names something else
    assert sched._fence_check(task, run) == []     # not attributed to the worker: left alone
    assert head_sha(clone) == head_after_human      # the human's commit survives
    assert (clone / "config.yaml").read_text() == "changed by a person\n"


def test_fence_reports_foreign_changes_alongside_the_reverted_ones(sched, tmp_path):
    """When the worker did escape, an interleaved human/other change in the same repo is
    reported on the card but left in place, not swept away with the worker's revert."""
    clone = tmp_path / "repo"
    task = sched.store.task("DM-001")

    sched._fence_snapshot(task)
    (clone / "worker.py").write_text("escaped\n")       # the worker's own write
    (clone / "person.txt").write_text("a person's edit\n")  # not the worker's
    run = _run_naming(sched, "DM-001", str(clone / "worker.py"))
    violations = sched._fence_check(task, run)
    assert violations
    assert violations[0]["files"] == ["worker.py"]       # reverted
    assert violations[0]["foreign"] == ["person.txt"]    # reported, left in place
    assert not (clone / "worker.py").exists()             # the escape is undone
    assert (clone / "person.txt").read_text() == "a person's edit\n"  # left alone


def test_fence_attributes_paths_named_relative_to_the_worktree(sched, tmp_path):
    """A worker that names a fenced path relative to its worktree (its cwd) rather than by an
    absolute path is still attributed and reverted; matching is not limited to absolute forms."""
    clone = tmp_path / "repo"
    task = sched.store.task("DM-001")
    wt = sched.worktree_for(task)

    sched._fence_snapshot(task)
    (clone / "rogue.py").write_text("x = 1\n")
    rel = os.path.relpath(str(clone / "rogue.py"), str(wt))  # e.g. ../../../repo/rogue.py
    assert not os.path.isabs(rel)
    run = _run_naming(sched, "DM-001", rel)
    violations = sched._fence_check(task, run)
    assert violations and "rogue.py" in violations[0]["files"]
    assert not (clone / "rogue.py").exists()  # the escape is undone


# ---- the git-internals guard (CG-239) --------------------------------------

def test_worktree_config_write_is_attributed_and_blocks_git_at_reap(sched, fake_github):
    """A `git config` run from inside a worker's own worktree can rewrite the *shared* clone's
    `.git/config` (a worktree shares its clone's config by default) — e.g. pointing
    core.hooksPath somewhere a later scheduler-side `git` call in that clone would run it with
    the operator's own credentials. The fence must catch this at reap, attribute it on the
    task, and refuse every further git command in that clone."""
    sched.cfg.data["stack"] = False
    task = sched.store.task("DM-001")
    clone = sched.repo_for(task)
    wt = sched.worktree_for(task)

    sched.tick()  # dispatch DM-001 (the in-process worker runs synchronously and commits)
    cfg_path = clone / ".git" / "config"
    cfg_path.write_text(cfg_path.read_text() + "\n[core]\n\thooksPath = /tmp/garden-test-evil-hooks\n")

    sched.tick()  # reap: the git guard runs before the ordinary fence and git-based checks

    task = sched.store.task("DM-001")
    assert task.status.value == "failed"
    card = _attention_card(sched, "DM-001")
    assert "git internals" in card and "clone .git/config" in card
    assert not fake_github.created  # never reached the PR step
    with pytest.raises(gitops.GitError):
        gitops.git("status", cwd=clone)
    with pytest.raises(gitops.GitError):
        gitops.git("status", cwd=wt)


def test_worktree_commits_alone_do_not_trip_the_git_guard(sched, fake_github):
    """A well-behaved run commits into its own worktree — moving HEAD, the index and the
    admin directory's logs — and dispatches a sibling task against the same shared clone,
    which adds that branch's tracking entry to the clone's `.git/config`. Neither is tampering
    and neither must trip the git guard."""
    sched.tick()  # dispatches DM-001 and (with the default stack setting) may touch DM-002 too
    sched.tick()  # reap

    task = sched.store.task("DM-001")
    assert task.status.value != "failed"
    assert not _attention_card(sched, "DM-001")


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
