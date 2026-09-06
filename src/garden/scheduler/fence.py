"""The worktree fence: snapshot the guarded repos at dispatch, revert a worker's writes outside
its worktree. Also the git-internals guard (CG-239): hash a clone's `.git/config`, its hooks
directory and a worktree's git-admin files at dispatch, and block every scheduler-side `git`
command in that clone at reap if any of them changed. And a held config reload (CG-242): hold
a live config reload that would race ahead of an in-flight run's fence manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .. import gitops
from ..config import Config, apply_executable_signature, executable_diff, executable_signature
from ..model import Status, Task, now_iso
from ..runs import Run
from .report import TickReport


class FenceMixin:
    # ---- worktree fence ----------------------------------------------------
    def _fence_repos(self, task: Task) -> list[tuple[str, Path]]:
        """Git repos a worker must never write: the live garden and the product clone. Its
        own worktree is a separate checkout, so it is not in this list."""
        out: list[tuple[str, Path]] = []
        root = self.store.root
        if gitops.is_repo(root):
            out.append(("the live garden", root))
        try:
            clone = Path(self.repo_for(task))
        except Exception:  # noqa: BLE001 - a missing/URL repo just means nothing to guard here
            return out
        if gitops.is_repo(clone) and clone.resolve() != root.resolve():
            out.append(("the product clone", clone))
        return out

    @staticmethod
    def _fence_owned(rel: str) -> bool:
        """No live-garden path is exempt from attribution.

        The scheduler can edit task files and state while a run is active, but those edits are
        left alone because they are absent from the worker transcript.  Exempting them here
        made a worker's named write invisible.
        """
        return False

    @staticmethod
    def _porcelain_path(line: str) -> str:
        body = line[3:] if len(line) > 3 else line
        if " -> " in body:  # rename shows as "old -> new"
            body = body.split(" -> ", 1)[1]
        return body.strip().strip('"')

    def _fence_snapshot(self, task: Task, run: Run | None = None) -> None:
        """Record HEAD and working-tree state of the guarded repos at dispatch, so finalize
        can tell what a worker changed. Also hash the live garden's config and side-store
        (garden*.yaml and .garden/state.json) into the run directory, so a worker write to
        them is caught even though they are gitignored / owned by the scheduler."""
        snap = {str(path): {"label": label, "head": gitops.head_sha(path), "status": gitops.status_lines(path)}
                for label, path in self._fence_repos(task)}
        st = self.state.get(task.id)
        if snap:
            st["fence"] = snap
        else:
            st.pop("fence", None)
        self._fence_guard_snapshot(run)

    def _fence_guard_targets(self, run: Run | None = None) -> list[tuple[str, Path, bool]]:
        """(relative path, absolute path, is_config) for the live garden files a worker must
        never write: every garden*.yaml at the root and .garden/state.json. Config files are
        snapshotted with their content so a write can be reverted; state.json (which the
        scheduler rewrites every tick) is hash-checked for a worker write but not reverted."""
        root = self.store.root
        out: list[tuple[str, Path, bool]] = [(p.name, p, True) for p in sorted(root.glob("garden*.yaml"))]
        # These are common harness policy/instruction files.  They are guarded even when they
        # do not yet exist, so a worker cannot create one for the next dispatch.
        out.extend((name, root / name, True) for name in
                   ("settings.json", "settings.local.json", "CLAUDE.md", "config.toml"))
        state_path = self.state.path
        try:
            rel = str(state_path.relative_to(root))
        except ValueError:
            rel = state_path.name
        out.append((rel, state_path, False))
        runs = self.cfg.garden_dir / "runs"
        current = run.path.resolve() if run is not None else None
        # A run in flight is mutable evidence used by another concurrent reap, so protect
        # it. Completed run directories are already the durable audit/accounting record;
        # copying all of them into every new fence made one manifest grow with all history.
        active_dirs = [active.path for active in self.active_runs()
                       if current is None or active.path.resolve() != current]
        for active_dir in active_dirs:
            for path in sorted(p for p in active_dir.rglob("*") if p.is_file()):
                # Snapshots of a sibling's snapshots grow recursively if treated as ordinary
                # run output. Its manifest remains guarded; its private copy is an
                # implementation detail of that earlier manifest.
                if "fence_guard" in path.relative_to(runs).parts:
                    continue
                out.append((str(path.relative_to(root)), path, True))
        return out

    def _fence_guard_snapshot(self, run: Run | None) -> None:
        """Hash garden*.yaml and .garden/state.json at dispatch into a manifest beside the run,
        keeping a copy of each config file for revert. A no-op with no run (a snapshot taken by
        a test without a run record)."""
        if run is None:
            return
        manifest: list[dict[str, Any]] = []
        root = self.store.root
        guard_dir = run.path / "fence_guard"
        cache_dir = self.cfg.garden_dir / "fence-guard-cache"
        for rel, path, is_config in self._fence_guard_targets(run):
            try:
                stat = path.stat() if path.exists() else None
            except OSError:
                continue
            try:
                data = path.read_bytes() if stat is not None else b""
            except OSError:
                continue
            sha = hashlib.sha256(data).hexdigest()
            snap = ""
            if rel.startswith(".garden/runs/"):
                cache_file = cache_dir / sha
                if not cache_file.exists():
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    if data is None:
                        data = path.read_bytes() if path.exists() else b""
                    cache_file.write_bytes(data)
                snap = f"cache:{sha}"
            elif is_config or rel == str(self.state.path.relative_to(root)):
                guard_dir.mkdir(parents=True, exist_ok=True)
                snap = rel.replace("/", "__")
                if data is None:
                    data = path.read_bytes() if path.exists() else b""
                (guard_dir / snap).write_bytes(data)
            manifest.append({"rel": rel, "abs": str(path), "config": is_config, "snap": snap,
                             "sha": sha})
        if manifest:
            manifest_text = json.dumps(manifest)
            (run.path / "fence_guard.json").write_text(manifest_text)
            # Keep a content-addressed authoritative copy outside state.json. The compact
            # digest in state detects tampering while routine CLI/status loads do not parse a
            # second copy of every (formerly history-sized) manifest.
            manifest_sha = hashlib.sha256(manifest_text.encode()).hexdigest()
            manifest_store = self.cfg.garden_dir / "fence-guard-manifests"
            manifest_store.mkdir(parents=True, exist_ok=True)
            manifest_copy = manifest_store / f"{manifest_sha}.json"
            if not manifest_copy.exists():
                manifest_copy.write_text(manifest_text)
            self.state.get(run.task_id)["fence_guard_manifest"] = {
                "run": run.run_id, "sha256": manifest_sha,
            }
            # This is deliberately separate from the broad file-hash manifest: a scheduler
            # starting after a worker's write needs the dispatch-time executable values before
            # it can safely parse and use the changed garden.yaml.
            (run.path / "executable_config.json").write_text(json.dumps(executable_signature(self.cfg.data)))

    @staticmethod
    def _fence_executable_signature(run: Run) -> dict[str, Any] | None:
        try:
            data = json.loads((run.path / "executable_config.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _hold_startup_config_against_fences(self) -> None:
        """Do not let a fresh process adopt a worker's changed executable config.

        Store normally has no earlier in-memory config on a process restart.  Active fence
        signatures fill that gap: restore their dispatch-time executable values in memory and
        record the same hold that the ordinary per-tick reload path uses.
        """
        runs = self._fenced_runs_in_flight()
        current = executable_signature(self.cfg.data)
        accepted = self.control().get("config_accept_signature")
        if accepted == current:
            return
        mismatched: list[tuple[Run, dict[str, Any], list[str]]] = []
        for run in runs:
            saved = self._fence_executable_signature(run)
            if saved is None:
                continue
            keys = sorted(k for k in set(current) | set(saved) if current.get(k) != saved.get(k))
            if keys:
                mismatched.append((run, saved, keys))
        if not mismatched:
            return
        # All normal dispatches share one value until an operator accepts a change.  If old
        # manifests disagree, the oldest one is the conservative value to keep in memory; the
        # hold still names every active run whose fence disagrees with the disk.
        baseline = mismatched[0][1]
        held_data = apply_executable_signature(self.cfg.data, baseline)
        self.store.config = Config(root=self.cfg.root, data=held_data, sources=self.cfg.sources, env=self.cfg.env)
        self.cfg = self.store.config
        keys = sorted({key for _, _, changed in mismatched for key in changed})
        run_ids = sorted(run.run_id for run, _, _ in mismatched)
        ctrl = self.control()
        existing = ctrl.get("config_hold") or {}
        if sorted(existing.get("keys") or []) != keys or sorted(existing.get("runs") or []) != run_ids:
            self.events.emit("config_reload_held", "", keys=keys, runs=run_ids)
            self.log(f"config reload held: {', '.join(keys)} disagrees with run(s) {', '.join(run_ids)}'s dispatch config")
        ctrl["config_hold"] = {"keys": keys, "runs": run_ids, "since": existing.get("since") or now_iso()}
        # tick() reloads State at its start so it sees web/CLI actions from another process.
        # Persist this bootstrap hold now; otherwise that reload would discard the only record
        # of the trusted startup baseline before the gate gets a chance to inspect it.
        self.state.save()

    def _fence_guard_check(self, task: Task, run: Run | None) -> list[dict[str, Any]]:
        """Compare garden*.yaml and .garden/state.json against the dispatch hashes. A change
        the worker's own transcript names is an escape: a config file is restored from its
        snapshot; state.json is left as the scheduler owns it but the run still fails and the
        card names it for a person to inspect. A change the worker did not name is the
        scheduler's own state.json write (every tick) or an operator's config edit — ignored."""
        if run is None:
            return []
        manifest_path = run.path / "fence_guard.json"
        saved = self.state.get(task.id).get("fence_guard_manifest")
        if saved is None and not manifest_path.exists():
            return []  # Older/unfenced runs have neither a reference nor an audit copy.
        try:
            if isinstance(saved, str):  # pre-CG-344 state, retained for an in-flight run
                manifest_text = saved
                manifest_sha = hashlib.sha256(saved.encode()).hexdigest()
            elif isinstance(saved, dict) and saved.get("run") == run.run_id:
                manifest_sha = str(saved.get("sha256") or "")
                if len(manifest_sha) != 64 or any(c not in "0123456789abcdef" for c in manifest_sha):
                    raise ValueError("invalid trusted manifest digest")
                copy_path = self.cfg.garden_dir / "fence-guard-manifests" / f"{manifest_sha}.json"
                manifest_text = copy_path.read_text()
                if hashlib.sha256(manifest_text.encode()).hexdigest() != manifest_sha:
                    raise ValueError("trusted manifest digest mismatch")
            else:
                raise ValueError("trusted manifest reference missing or belongs to another run")
            manifest = json.loads(manifest_text)
            if not isinstance(manifest, list) or any(
                not isinstance(entry, dict) or not {"abs", "rel"} <= entry.keys() for entry in manifest
            ):
                raise ValueError("invalid trusted manifest structure")
        except (OSError, ValueError) as exc:
            # The audit copy is worker-writable: never use it as restoration authority.
            # Without a trusted baseline we cannot safely restore or accept this run.
            return [{"label": "the live garden", "path": str(manifest_path), "commits": [],
                     "files": [], "foreign": [], "reverted": False,
                     "integrity_error": f"fence manifest unavailable or invalid: {exc}"}]
        transcript = run.stdout_text()
        worktree = self.worktree_for(task)
        root = self.store.root
        violations: list[dict[str, Any]] = []
        try:
            audit_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        except OSError:
            audit_sha = ""
        if audit_sha != manifest_sha:
            rel = str(manifest_path.relative_to(root))
            if self._worker_named(transcript, root, rel, worktree):
                manifest_path.write_text(manifest_text)
                violations.append({"label": "the live garden", "path": str(manifest_path), "commits": [],
                                   "files": [rel], "foreign": [], "reverted": True})
        for entry in manifest:
            path = Path(entry["abs"])
            rel = str(entry["rel"])
            try:
                now_sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
            except OSError:
                continue
            if now_sha == entry.get("sha"):
                continue  # unchanged
            if not self._worker_named(transcript, root, rel, worktree):
                continue  # the scheduler's own state.json write, or a person's config edit
            reverted = False
            if rel == str(self.state.path.relative_to(root)):
                try:
                    snapshot = json.loads((run.path / "fence_guard" / str(entry["snap"])).read_text())
                    self.state.restore_other_task_keys(snapshot, task.id, set(self.store.tasks()))
                    reverted = True
                except (OSError, json.JSONDecodeError, ValueError) as e:
                    self.log(f"fence: could not restore other state keys: {e}")
            elif entry.get("config") and entry.get("snap"):
                snap = str(entry["snap"])
                snap_file = (self.cfg.garden_dir / "fence-guard-cache" / snap.removeprefix("cache:")
                             if snap.startswith("cache:") else run.path / "fence_guard" / snap)
                try:
                    if snap_file.exists():
                        path.write_bytes(snap_file.read_bytes())
                        reverted = True
                except OSError as e:  # noqa: BLE001
                    self.log(f"fence: could not restore {rel}: {e}")
            elif entry.get("config"):
                try:
                    path.unlink(missing_ok=True)
                    reverted = True
                except OSError as e:  # noqa: BLE001
                    self.log(f"fence: could not remove {rel}: {e}")
            violations.append({"label": "the live garden", "path": str(path), "commits": [],
                               "files": [rel], "foreign": [], "reverted": reverted})
        return violations

    def _migrate_fence_bookkeeping(self) -> None:
        """Compact legacy manifests and discard fence data for runs no longer active.

        State.save performs the migration under its normal lock/merge protocol, so a CLI
        startup cannot rewrite live state concurrently with a tick.
        """
        active = {(r.task_id, r.run_id): r for r in self.runs.active()}
        tasks = self.store.tasks()
        for task_id, task_state in list(self.state.data.items()):
            if not isinstance(task_state, dict) or task_id.startswith("_"):
                continue
            saved = task_state.get("fence_guard_manifest")
            matching = next((r for (tid, _), r in active.items() if tid == task_id), None)
            if matching is None and task_id in tasks and tasks[task_id].status == Status.RUNNING:
                # A terminal run may still need finalization after an interrupted reap.
                matching = self.runs.latest(task_id)
            if matching is None:
                if "fence_guard_manifest" in task_state:
                    task_state.pop("fence_guard_manifest")
                if "fence" in task_state:
                    task_state.pop("fence")
            elif isinstance(saved, str):
                sha = hashlib.sha256(saved.encode()).hexdigest()
                store = self.cfg.garden_dir / "fence-guard-manifests"
                store.mkdir(parents=True, exist_ok=True)
                copy_path = store / f"{sha}.json"
                if not copy_path.exists():
                    copy_path.write_text(saved)
                task_state["fence_guard_manifest"] = {"run": matching.run_id, "sha256": sha}
        legacy_cache = self.state.data.get("_fence_guard_cache")
        if isinstance(legacy_cache, dict):
            for key in list(legacy_cache):
                legacy_cache.pop(key, None)
        self.state.save()

    def _release_fence_bookkeeping(self, task: Task) -> None:
        st = self.state.get(task.id)
        # Keep the trusted reference through the task transition: a crash after this
        # check but before push/finalization must be able to check the run again.
        # The next migration releases it once neither run nor task is in flight.
        st.pop("fence", None)

    # ---- the git-internals guard (CG-239) ----------------------------------
    def _git_guard_targets(self, task: Task) -> list[tuple[str, Path]]:
        """(label, path) for the git internals a write inside a worker's own worktree could
        turn into arbitrary code execution the next time the scheduler runs `git` against this
        clone: the clone's `.git/config` and hooks directory (shared by every task dispatched
        against it — a worktree shares its clone's config unless `extensions.worktreeConfig` is
        set), and this task's own worktree's `.git` file and the `.git/worktrees/<id>/` admin
        directory it names."""
        out: list[tuple[str, Path]] = []
        try:
            clone = Path(self.repo_for(task))
        except Exception:  # noqa: BLE001 - a missing/URL repo just means nothing to guard here
            clone = None
        if clone is not None and gitops.is_repo(clone):
            out.append(("clone .git/config", clone / ".git" / "config"))
            out.append(("clone .git/hooks", clone / ".git" / "hooks"))
        wt = self.worktree_for(task)
        dot_git = wt / ".git"
        if dot_git.exists():
            out.append(("worktree .git", dot_git))
            admin = gitops.worktree_admin_dir(wt)
            if admin is not None:
                out.append(("worktree .git/worktrees/<id>", admin))
        return out

    # Files inside `.git/worktrees/<id>/` a worktree's own git activity never rewrites: `gitdir`
    # (this worktree's own `.git` file location) and `commondir` (the shared repo it points
    # back to) never change once the worktree is created, and `config.worktree` only exists at
    # all when per-worktree config is in use. Everything else there — `HEAD`, `index`, `logs/`,
    # `ORIG_HEAD` — changes on every ordinary commit the worker makes, so hashing the whole
    # directory would flag a well-behaved run's own commits as tampering.
    _ADMIN_DIR_GUARDED_FILES = ("gitdir", "commondir", "config.worktree")

    @classmethod
    def _hash_admin_dir(cls, admin: Path) -> str:
        h = hashlib.sha256()
        for name in cls._ADMIN_DIR_GUARDED_FILES:
            f = admin / name
            try:
                data = f.read_bytes() if f.exists() else b"<absent>"
            except OSError:
                data = b"<absent>"
            h.update(name.encode())
            h.update(data)
        return h.hexdigest()

    # `git worktree add` sets up branch tracking (`branch.<name>.remote`/`.merge`) in the
    # clone's *shared* `.git/config` for every new task branch by default — expected churn on
    # a clone many tasks dispatch against concurrently, not tampering. Nothing dangerous (a
    # hooksPath, an alias, an include) is ever a `[branch "..."]` key, so those sections are
    # excluded before hashing the file.
    _CONFIG_SECTION_RE = re.compile(r'^\[[^\]]+\]\s*$')
    _CONFIG_BRANCH_SECTION_RE = re.compile(r'^\[branch\s+"[^"]*"\]\s*$')

    @classmethod
    def _hash_config(cls, path: Path) -> str:
        try:
            lines = path.read_text().splitlines(keepends=True)
        except OSError:
            lines = []
        kept = []
        skipping = False
        for line in lines:
            stripped = line.strip()
            if cls._CONFIG_SECTION_RE.match(stripped):
                skipping = bool(cls._CONFIG_BRANCH_SECTION_RE.match(stripped))
                if skipping:
                    continue
            if not skipping:
                kept.append(line)
        return hashlib.sha256("".join(kept).encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _hash_tree(path: Path) -> str:
        """A content hash of `path`: a file's bytes, or the sorted (relative path, content) of
        every file under a directory. Reads files directly rather than through `gitops.git`, so
        it stays meaningful even once a clone has been blocked (`gitops.block_repo`)."""
        h = hashlib.sha256()
        if path.is_dir():
            for f in sorted(p for p in path.rglob("*") if p.is_file()):
                try:
                    h.update(str(f.relative_to(path)).encode())
                    h.update(f.read_bytes())
                except OSError:
                    continue
        elif path.exists():
            try:
                h.update(path.read_bytes())
            except OSError:
                pass
        else:
            h.update(b"<absent>")
        return h.hexdigest()

    @classmethod
    def _hash_git_guard_target(cls, label: str, path: Path) -> str:
        if label == "worktree .git/worktrees/<id>":
            return cls._hash_admin_dir(path)
        if label == "clone .git/config":
            return cls._hash_config(path)
        return cls._hash_tree(path)

    def _git_guard_snapshot(self, task: Task, run: Run | None) -> None:
        """Hash the clone's git internals (see `_git_guard_targets`) into a manifest beside the
        run, so `_git_guard_check` at reap can tell whether any of them changed while this run
        was live."""
        if run is None:
            return
        manifest = [{"label": label, "path": str(path), "sha": self._hash_git_guard_target(label, path)}
                    for label, path in self._git_guard_targets(task)]
        (run.path / "git_guard.json").write_text(json.dumps(manifest))

    def _git_guard_check(self, task: Task, run: Run | None) -> list[dict[str, Any]]:
        """Compare the clone's git internals against the hashes taken at dispatch. Any change
        is reported here; the caller (`_git_guard_fail`) blocks every scheduler-side `git`
        command in that clone and attributes the change on the task. Unlike the worktree fence,
        there is no "was it the worker's" attribution question and nothing to revert: this is
        not a write a worker could plausibly make by accident, and reverting a hooks directory
        or an admin dir is not something to do blind — a person recreates the clone instead."""
        if run is None:
            return []
        manifest_path = run.path / "git_guard.json"
        if not manifest_path.exists():
            return []
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        violations: list[dict[str, Any]] = []
        for entry in manifest:
            path = Path(entry["path"])
            if self._hash_git_guard_target(str(entry["label"]), path) != entry.get("sha"):
                violations.append({"label": entry["label"], "path": str(path)})
        return violations

    def _git_guard_fail(self, task: Task, run: Run, violations: list[dict[str, Any]], rep: TickReport) -> None:
        """A clone's git internals changed while a run was live: block every scheduler-side
        `git` command in that clone (`gitops.block_repo`) so the next tick cannot trust it, and
        attribute the change on the task instead of letting it surface as an unexplained git
        failure later."""
        try:
            clone = Path(self.repo_for(task))
            gitops.block_repo(clone, f"{task.id}: git internals changed since dispatch (run {run.run_id})")
        except Exception as e:  # noqa: BLE001
            self.log(f"{task.id}: could not block the clone after a git-guard violation: {e}")
        names = ", ".join(f"{v['label']} ({v['path']})" for v in violations)
        card = (f"the clone's git internals changed since dispatch: {names}; every git command "
                "in this clone is refused until it is recreated by hand")
        self.state.get(task.id)["needs_human"] = card
        self.events.emit("git_guard_violation", task.id, run=run.run_id, changed=[v["label"] for v in violations])
        run.status = "failed"
        run.error = (run.error + " | " if run.error else "") + "clone git internals changed (blocked)"
        run.save()
        self._transition(task, Status.FAILED, f"git guard: {card}"[:400], needs_human=True)
        rep.transitions.append(f"{task.id} -> failed (git guard)")

    @staticmethod
    def _worker_named(transcript: str, repo: Path, rel: str, worktree: Path | None = None) -> bool:
        """True if the worker's transcript names this path, so the change is attributable to
        the worker rather than to a person editing the live garden or the scheduler's own git.

        `claude`'s output (a single result JSON, or stream-json tool events) carries the
        worker's `Edit`/`Write` `file_path`, the `Bash` commands it ran, and its final
        message — all as substrings of stdout. A path the worker touched appears there by its
        absolute form; a path a person changed while the run was live does not. We also match
        the path as it would be written relative to the worktree or its parent (the worker's
        likely cwd), so a fenced path named across tool calls as `../../../garden.yaml` is
        still attributed. Matching is always by a full path — never a bare filename — so a
        person's edit that merely shares a name is not swept up."""
        if not transcript:
            return False
        target = repo / rel
        candidates = {str(target)}
        try:
            candidates.add(str(target.resolve()))
        except OSError:
            pass
        anchors = [worktree, worktree.parent] if worktree else []
        for anchor in anchors:
            try:
                candidates.add(os.path.relpath(str(target), str(anchor)))
            except (OSError, ValueError):
                pass
        return any(c in transcript for c in candidates)

    def _fence_check(self, task: Task, run: Run | None = None) -> list[dict[str, Any]]:
        """Compare each guarded repo against its dispatch snapshot; revert and report only the
        writes the worker's own transcript names. Task files and .garden/ are the scheduler's
        own and are ignored; so is anything the worker did not name — a person editing the live
        garden while a run is live, or a HEAD the scheduler's own `git fetch` advanced. Only a
        path the worker's transcript names is reverted; a moved HEAD alone is not an escape, so
        an un-attributed change is reported on the card and left in place."""
        guard = self._fence_guard_check(task, run)
        snap = self.state.get(task.id).pop("fence", None)
        if not snap:
            return guard
        transcript = run.stdout_text() if run is not None else ""
        worktree = self.worktree_for(task)
        violations: list[dict[str, Any]] = []
        for path_str, before in snap.items():
            path = Path(path_str)
            if not gitops.is_repo(path):
                continue
            head_before = str(before.get("head") or "")
            head_now = gitops.head_sha(path)
            was = set(before.get("status") or [])
            wt_files = {self._porcelain_path(ln) for ln in gitops.status_lines(path) if ln not in was}
            moved = bool(head_before) and head_now != head_before
            committed = set(gitops.changed_files(path, head_before, head_now)) if moved else set()
            changed = sorted(p for p in (wt_files | committed) if p and not self._fence_owned(p))
            if not changed:
                # A HEAD move (or write) that only touched task files or .garden/ is the
                # scheduler's own (e.g. `garden sync`): not a worker escape.
                continue
            attributed = [p for p in changed if self._worker_named(transcript, path, p, worktree)]
            foreign = [p for p in changed if p not in attributed]
            if not attributed:
                # Nothing here is the worker's: a person's edit to the live garden, or a HEAD
                # the scheduler's own fetch/pull advanced. Leave it; a moved HEAD alone is not
                # an escape.
                continue
            # Drop the worker's commits only if one of its named paths is actually in them, so
            # an interleaved human commit in the same range is not swept away with a reset.
            reset = moved and bool(set(attributed) & committed)
            commits = gitops.commits_between(path, head_before, head_now) if reset else []
            self._fence_revert(path, head_before, reset, attributed)
            violations.append({"label": str(before.get("label") or path.name), "path": path_str,
                               "commits": commits, "files": attributed, "foreign": foreign, "reverted": True})
        return guard + violations

    def _fence_revert(self, repo: Path, head_before: str, reset: bool, touched: list[str]) -> None:
        """Undo a worker's escape: drop its commits (keeping unrelated in-flight edits) and
        restore or remove each path it wrote."""
        try:
            if reset and head_before:
                gitops.reset_soft(repo, head_before)
            for rel in touched:
                if head_before and gitops.path_at(repo, head_before, rel):
                    gitops.restore_path(repo, head_before, rel)
                else:
                    gitops.unstage_and_remove(repo, rel)
        except gitops.GitError as e:
            self.log(f"fence: revert in {repo} was incomplete: {e}")

    def _fence_fail(self, task: Task, run: Run, violations: list[dict[str, Any]], rep: TickReport) -> None:
        parts = []
        foreign_seen = False
        kept_seen = False
        for v in violations:
            # Lead with the operator-critical facts (which repo, which files) and put the
            # long absolute path last, so a truncated Inbox card still names what was touched.
            bits = []
            if v["files"]:
                if not v.get("reverted", True):
                    kept_seen = True
                bits.append("wrote " + ", ".join(v["files"]))
            if v["commits"]:
                bits.append(f"{len(v['commits'])} commit(s) [{'; '.join(v['commits'])}]")
            if v.get("foreign"):
                foreign_seen = True
                bits.append("also changed (left in place, not attributed to the worker): "
                            + ", ".join(v["foreign"]))
            parts.append(f"{v['label']}: " + " and ".join(bits) + f" ({v['path']})")
        integrity_errors = [v["integrity_error"] for v in violations if v.get("integrity_error")]
        card = "worker wrote outside its worktree; the writes it made were reverted. Touched " + " | ".join(parts)
        if integrity_errors:
            card = ("Cannot verify this run's worktree fence; inspect protected paths before retrying. "
                    "Restoration is unverified: " + "; ".join(integrity_errors))
        if kept_seen:
            card += " — some paths the scheduler owns (e.g. .garden/state.json) could not be reverted; inspect them."
        if foreign_seen:
            card += " — the un-attributed changes were left for a person to check."
        self.state.get(task.id)["needs_human"] = card
        self.events.emit("fence_violation", task.id, run=run.run_id, repos=[v["path"] for v in violations],
                         commits=sum(len(v["commits"]) for v in violations), files=sum(len(v["files"]) for v in violations))
        run.status = "failed"
        error = "fence integrity could not be verified" if integrity_errors else "wrote outside its worktree (reverted)"
        run.error = (run.error + " | " if run.error else "") + error
        run.save()
        self._transition(task, Status.FAILED, f"fenced: {card}"[:400], needs_human=True)
        rep.transitions.append(f"{task.id} -> failed (wrote outside worktree)")

    # ---- held config reload (CG-242) ----------------------------------------
    def _fenced_runs_in_flight(self) -> list[Run]:
        """Active runs whose dispatch left a fence manifest behind (work/revise/resume/rebase;
        see dispatch()'s `_fence_snapshot` call) — the runs a config reload's executable fields
        must not race ahead of."""
        return [r for r in self.active_runs() if (r.path / "fence_guard.json").exists()]

    def config_hold(self) -> dict[str, Any]:
        """The currently held config reload, if any: `keys` (the executable fields that
        disagree with an in-flight run's dispatch config), `runs` (the run ids holding it) and
        `since`. Empty when nothing is held. Read by the Inbox, the Config page and
        `garden config`."""
        return dict(self.control().get("config_hold") or {})

    def accept_config_reload(self, by: str = "cli") -> None:
        """Let a held reload apply on the next tick even though its runs are still in flight:
        the operator has looked at the change and vouches it is theirs, not a worker's write
        racing the fence (CG-242). Recorded even when nothing is currently held; the next
        tick's gate then simply finds no pending change to apply."""
        ctrl = self.control()
        ctrl["config_hold_accept"] = True
        self.state.save()
        self.events.emit("config_reload_accepted", "", by=by)
        self.log(f"held config reload accepted by {by}; applies next tick")

    def _reload_config_if_safe(self) -> None:
        """Re-read garden.yaml (CG-192) — unless doing so right now could hand an in-flight
        run's own config write a route to execute before its fence check (at reap) can revert
        it (CG-242). `executable_diff` compares notify.command, checks (including any
        retry_command), every product's setup.command, every harness's bin/command and
        worker_env.pass between what is loaded now and what is on disk; a mismatch while a
        fenced run is in flight holds the reload — logged and recorded once as `config_hold`,
        naming the keys and the runs — until every such run has been reaped (its own writes
        reverted or not) or `accept_config_reload` marks the change operator-confirmed. Every
        other key (budgets, review settings, observe cadence, ...) still reloads immediately,
        and a change with no fenced run in flight always applies at once, as before CG-242."""
        ctrl = self.control()
        if not self.store.config_changed_on_disk() and not ctrl.get("config_hold"):
            return
        new_cfg = self.store.load_config_from_disk()
        accepted = bool(ctrl.pop("config_hold_accept", False))
        exec_keys = executable_diff(self.cfg.data, new_cfg.data)
        in_flight = self._fenced_runs_in_flight() if exec_keys and not accepted else []
        if exec_keys and in_flight and not accepted:
            run_ids = sorted(r.run_id for r in in_flight)
            existing = ctrl.get("config_hold") or {}
            if sorted(existing.get("keys") or []) != exec_keys or sorted(existing.get("runs") or []) != run_ids:
                self.events.emit("config_reload_held", "", keys=exec_keys, runs=run_ids)
                self.log(f"config reload held: {', '.join(exec_keys)} disagrees with run(s) {', '.join(run_ids)}'s dispatch config")
            ctrl["config_hold"] = {"keys": exec_keys, "runs": run_ids, "since": existing.get("since") or now_iso()}
            return
        changed = self.store.adopt_config(new_cfg)
        self.cfg = self.store.config
        was_held = ctrl.pop("config_hold", None)
        if accepted:
            ctrl["config_accept_signature"] = executable_signature(new_cfg.data)
        if changed:
            keys = ", ".join(sorted(changed))
            note = " (operator-confirmed while runs were in flight)" if accepted and was_held else ""
            self.log(f"garden.yaml reloaded; changed: {keys}{note}")
            self.events.emit("config_reloaded", "", keys=sorted(changed), accepted=accepted)
