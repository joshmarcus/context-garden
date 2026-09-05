"""The worktree fence: snapshot the guarded repos at dispatch, revert a worker's writes outside its worktree."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .. import gitops
from ..model import Status, Task
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
        """Paths the scheduler itself may change in the live garden, so a worker touching
        them is not counted as an escape: task files and the .garden/ state dir."""
        parts = rel.split("/")
        return "tasks" in parts or (bool(parts) and parts[0] == ".garden")

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

    def _fence_guard_targets(self) -> list[tuple[str, Path, bool]]:
        """(relative path, absolute path, is_config) for the live garden files a worker must
        never write: every garden*.yaml at the root and .garden/state.json. Config files are
        snapshotted with their content so a write can be reverted; state.json (which the
        scheduler rewrites every tick) is hash-checked for a worker write but not reverted."""
        root = self.store.root
        out: list[tuple[str, Path, bool]] = [(p.name, p, True) for p in sorted(root.glob("garden*.yaml"))]
        state_path = self.state.path
        try:
            rel = str(state_path.relative_to(root))
        except ValueError:
            rel = state_path.name
        out.append((rel, state_path, False))
        return out

    def _fence_guard_snapshot(self, run: Run | None) -> None:
        """Hash garden*.yaml and .garden/state.json at dispatch into a manifest beside the run,
        keeping a copy of each config file for revert. A no-op with no run (a snapshot taken by
        a test without a run record)."""
        if run is None:
            return
        manifest: list[dict[str, Any]] = []
        guard_dir = run.path / "fence_guard"
        for rel, path, is_config in self._fence_guard_targets():
            if not path.exists():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            snap = ""
            if is_config:
                guard_dir.mkdir(parents=True, exist_ok=True)
                snap = rel.replace("/", "__")
                (guard_dir / snap).write_bytes(data)
            manifest.append({"rel": rel, "abs": str(path), "config": is_config, "snap": snap,
                             "sha": hashlib.sha256(data).hexdigest()})
        if manifest:
            (run.path / "fence_guard.json").write_text(json.dumps(manifest))

    def _fence_guard_check(self, task: Task, run: Run | None) -> list[dict[str, Any]]:
        """Compare garden*.yaml and .garden/state.json against the dispatch hashes. A change
        the worker's own transcript names is an escape: a config file is restored from its
        snapshot; state.json is left as the scheduler owns it but the run still fails and the
        card names it for a person to inspect. A change the worker did not name is the
        scheduler's own state.json write (every tick) or an operator's config edit — ignored."""
        if run is None:
            return []
        manifest_path = run.path / "fence_guard.json"
        if not manifest_path.exists():
            return []
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        transcript = run.stdout_text()
        worktree = self.worktree_for(task)
        root = self.store.root
        violations: list[dict[str, Any]] = []
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
            if entry.get("config") and entry.get("snap"):
                snap_file = run.path / "fence_guard" / str(entry["snap"])
                try:
                    if snap_file.exists():
                        path.write_bytes(snap_file.read_bytes())
                        reverted = True
                except OSError as e:  # noqa: BLE001
                    self.log(f"fence: could not restore {rel}: {e}")
            violations.append({"label": "the live garden", "path": str(path), "commits": [],
                               "files": [rel], "foreign": [], "reverted": reverted})
        return violations

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
        card = "worker wrote outside its worktree; the writes it made were reverted. Touched " + " | ".join(parts)
        if kept_seen:
            card += " — some paths the scheduler owns (e.g. .garden/state.json) could not be reverted; inspect them."
        if foreign_seen:
            card += " — the un-attributed changes were left for a person to check."
        self.state.get(task.id)["needs_human"] = card
        self.events.emit("fence_violation", task.id, run=run.run_id, repos=[v["path"] for v in violations],
                         commits=sum(len(v["commits"]) for v in violations), files=sum(len(v["files"]) for v in violations))
        run.status = "failed"
        run.error = (run.error + " | " if run.error else "") + "wrote outside its worktree (reverted)"
        run.save()
        self._transition(task, Status.FAILED, f"fenced: {card}"[:400], needs_human=True)
        rep.transitions.append(f"{task.id} -> failed (wrote outside worktree)")
