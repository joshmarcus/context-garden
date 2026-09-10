"""Bounded worker-branch cleanup after lifecycle and worktree reconciliation."""

from __future__ import annotations

import json
import time
from itertools import chain
from pathlib import Path
from typing import Any

from .. import gitops
from ..branch_cleanup import BranchDisposition, classify_branches, delete_disposition
from ..github import GitHubError
from ..model import Status, now_iso
from ..storage_cleanup import (
    DISPOSABLE_HOME_PATHS,
    StorageItem,
    cleanup_home_caches,
    owned_child,
    owned_directory,
    path_identity,
    remove_owned_tree,
    space_status,
    tree_bytes,
    write_audit,
)
from .report import TickReport


class CleanupMixin:
    def storage_inventory(self, *, measure: bool = True,
                          branch_rows: dict[tuple[str, str], BranchDisposition] | None = None,
                          ) -> dict[str, Any]:
        """Account for bounded Garden-owned worktrees, isolated homes and run temp data."""
        self.runs.invalidate()
        tasks = self.store.tasks()
        runs = self.runs.all_runs()
        active = {run.task_id for run in self.runs.active()}
        runs_by_worktree: dict[str, list[Any]] = {}
        for run in runs:
            if run.worktree:
                key = str(Path(run.worktree).absolute())
                runs_by_worktree.setdefault(key, []).append(run)
        if branch_rows is None:
            branch_rows = {
                (row.product, row.branch): row for row in self.branch_cleanup_inventory()
            }
        keep_days = float(self.cfg.get("worktrees.keep_days", 2) or 0)
        home_keep_days = float(self.cfg.get("storage_cleanup.home_keep_days", keep_days) or 0)
        now = time.time()
        items: list[StorageItem] = []
        inventory_limit = int(self.cfg.get("storage_cleanup.inventory_limit", 2000) or 0)
        truncated = False

        def size(path: Path) -> int:
            return tree_bytes(path) if measure else 0

        roots = {self.cfg.worktrees_dir, self.cfg.garden_dir / "worktrees"}
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for path in sorted(root.iterdir()):
                if len(items) >= inventory_limit:
                    truncated = True
                    break
                if path.is_symlink():
                    items.append(StorageItem(str(path), "foreign/link", "unknown", 0, False,
                                             "symlink is never followed"))
                    continue
                if not path.is_dir():
                    continue
                if path.name.startswith(".garden-home-"):
                    worktree_name = path.name.removeprefix(".garden-home-")
                    matching_runs = runs_by_worktree.get(str((root / worktree_name).absolute()), [])
                    task_id = matching_runs[-1].task_id if matching_runs else worktree_name
                    task = tasks.get(task_id)
                    worktree = root / worktree_name
                    age = self._storage_age_days(path, now)
                    terminal_owner = bool(task and task.status.terminal) or bool(
                        matching_runs and all(run.status not in ("requested", "preparing", "running")
                                              and run.completion_mode == "managed" for run in matching_runs)
                    )
                    eligible = bool(terminal_owner and task_id not in active and not worktree.exists()
                                    and age >= home_keep_days)
                    disposable = any(owned_directory(path.parent, path / relative)
                                     for relative in DISPOSABLE_HOME_PATHS)
                    eligible = eligible and disposable
                    reason = ("completed task has no remaining worktree; disposable caches eligible" if eligible
                              else "no disposable cache; private data retained" if terminal_owner and not disposable
                              else self._home_retention_reason(task, task_id, active, worktree, age, home_keep_days))
                    items.append(StorageItem(str(path), "worker_home", task_id, size(path), eligible, reason))
                    continue
                matching_runs = runs_by_worktree.get(str(path.absolute()), [])
                task_id = matching_runs[-1].task_id if matching_runs else path.name
                task = tasks.get(task_id)
                if task is None:
                    items.append(StorageItem(str(path), "worktree", "unknown", size(path), False,
                                             "no Garden task provenance"))
                    continue
                eligible, reason = self._worktree_disposition(
                    task, path, matching_runs, active, branch_rows, now, keep_days
                )
                items.append(StorageItem(str(path), "worktree", task.id, size(path), eligible, reason))
                if task.status.terminal and task.id not in active:
                    caches = chain((path / ".venv", path / ".pytest_cache"), path.rglob("__pycache__"))
                    for cache in caches:
                        if len(items) >= inventory_limit:
                            truncated = True
                            break
                        if cache.is_dir() and not cache.is_symlink():
                            items.append(StorageItem(str(cache), "worktree_cache", task.id,
                                                     size(cache), True,
                                                     "disposable cache in inactive terminal worktree"))
        tmp_root = self.cfg.work_dir / "tmp"
        known_runs = {run.run_id: run for run in self.runs.all_runs()}
        if tmp_root.is_dir() and not tmp_root.is_symlink():
            for path in sorted(tmp_root.iterdir()):
                if len(items) >= inventory_limit:
                    truncated = True
                    break
                if not path.is_dir() or path.is_symlink():
                    continue
                run = known_runs.get(path.name)
                eligible = bool(run and run.status != "running" and run.process_finished())
                reason = ("terminal local run" if eligible else
                          "active run" if run and run.status == "running" else
                          "no terminal Garden run provenance")
                items.append(StorageItem(str(path), "run_temp", run.task_id if run else "unknown",
                                         size(path), eligible, reason))
        return {"space": space_status(self.cfg.work_dir, probe_host=measure),
                "items": [item.to_dict() for item in items],
                "truncated": truncated, "inventory_limit": inventory_limit,
                "bytes": {category: sum(item.bytes for item in items if item.category == category)
                          for category in sorted({item.category for item in items})}}

    @staticmethod
    def _storage_age_days(path: Path, now: float) -> float:
        try:
            return max(0.0, (now - path.stat(follow_symlinks=False).st_mtime) / 86400)
        except OSError:
            return 0.0

    @staticmethod
    def _home_retention_reason(task: Any, task_id: str, active: set[str], worktree: Path,
                               age: float, keep_days: float) -> str:
        if task is None:
            return "no Garden task provenance; private data retained"
        if task_id in active:
            return "active or queued run"
        if not task.status.terminal:
            return f"task is {task.status.value}"
        if worktree.exists():
            return "worktree must be reconciled first"
        if age < keep_days:
            return f"retained for {keep_days:g} days"
        return "uncertain ownership"

    def _worktree_disposition(self, task: Any, path: Path, matching_runs: list[Any], active: set[str],
                              branches: dict[tuple[str, str], BranchDisposition], now: float,
                              keep_days: float) -> tuple[bool, str]:
        if task.id in active or self._manual_reserved(task):
            return False, "active, queued or manually reserved run"
        if matching_runs and any(run.completion_mode != "managed" for run in matching_runs):
            return False, "external or pushed checkout ownership"
        managed_attempt = bool(matching_runs) and all(
            run.status not in ("requested", "preparing", "running")
            and run.completion_mode == "managed"
            for run in matching_runs
        )
        if task.status not in (Status.DONE, Status.CANCELLED) and not managed_attempt:
            return False, f"task is {task.status.value}"
        if str(self.cfg.product_checkout(task.product).get("strategy") or "worktree") == "in_place":
            return False, "canonical/external checkout ownership"
        if gitops.has_uncommitted_changes(path):
            return False, "dirty worktree"
        age = self._storage_age_days(path, now)
        if age < keep_days:
            return False, f"retained for {keep_days:g} days"
        attempt_branch = matching_runs[-1].branch if matching_runs else ""
        branch = attempt_branch or task.branch
        row = branches.get((task.product, branch)) if branch else None
        detached_check = bool(matching_runs) and all(run.mode == "check" for run in matching_runs)
        checked_out_only = bool(row and row.classification == "needed"
                                and row.reason == "the branch is checked out in a worktree")
        if row and row.classification == "needed" and not checked_out_only and not detached_check:
            return False, f"branch retained: {row.reason}"
        if row is None or row.classification == "uncertain" or checked_out_only or detached_check:
            try:
                base = gitops.base_ref(path, self.cfg.product_base_branch(task.product))
                if not gitops.is_ancestor(path, "HEAD", base):
                    detail = f": {row.reason}" if row else ""
                    return False, f"worktree head has unique unmerged commits{detail}"
            except gitops.GitError as exc:
                return False, f"Git ownership could not be proven: {exc}"
        return True, "clean inactive managed worktree with preserved/reachable head"

    def sweep_storage(self, rep: TickReport, *, apply: bool = True,
                      limit: int | None = None, measure: bool = True) -> dict[str, Any]:
        """Preview or incrementally reclaim eligible storage, with immediate rechecks."""
        limit = int(self.cfg.get("storage_cleanup.limit", 20) or 0) if limit is None else max(0, limit)
        if apply and limit <= 0:
            return {"at": now_iso(), "status": "disabled", "preview": False, "limit": 0,
                    "inventory": None, "results": [], "bytes_reclaimed": 0}
        before = self.storage_inventory(measure=measure)
        self._reconcile_storage_audits()
        results: list[dict[str, Any]] = []
        report = {"at": now_iso(), "status": "in_progress" if apply else "complete",
                  "preview": not apply, "limit": limit, "inventory": before,
                  "results": results, "bytes_reclaimed": 0}
        audit_keep = int(self.cfg.get("storage_cleanup.audit_keep", 20) or 1)
        audit_path = write_audit(self.cfg.garden_dir, report, keep=audit_keep)
        report["audit_path"] = str(audit_path)
        write_audit(self.cfg.garden_dir, report, keep=audit_keep, destination=audit_path)

        def record(result: dict[str, Any]) -> None:
            operation_id = result.get("operation_id")
            pending = next((row for row in results
                            if operation_id and row.get("operation_id") == operation_id), None)
            if pending is None:
                results.append(result)
            else:
                pending.update(result)
            report["bytes_reclaimed"] = sum(
                int(row.get("bytes_reclaimed", 0)) for row in results
            )
            write_audit(self.cfg.garden_dir, report, keep=audit_keep, destination=audit_path)

        def pending(path: Path, category: str, size: int) -> str:
            operation_id = f"op-{len(results) + 1:04d}"
            record({"operation_id": operation_id, "path": str(path), "category": category,
                    "outcome": "pending", "bytes_before": size,
                    "target_identity": path_identity(path), "bytes_reclaimed": 0})
            return operation_id

        if apply:
            for item in before["items"]:
                if len(results) >= limit or not item["eligible"]:
                    continue
                path = Path(str(item["path"]))
                # The preview may be older than an operator edit made during this sweep.
                # Refresh task ownership immediately before the destructive recheck.
                self.store.invalidate_tasks()
                current_branch_rows: dict[tuple[str, str], BranchDisposition] = {}
                if item["category"] == "worktree":
                    task = self.store.task(str(item["owner"]))
                    matching = [
                        run for run in self.runs.all_runs()
                        if run.worktree and str(Path(run.worktree).absolute()) == str(path.absolute())
                    ]
                    branch = matching[-1].branch if matching else task.branch
                    if branch:
                        current_branch_rows = {
                            (row.product, row.branch): row
                            for row in self.branch_cleanup_inventory(only_remote_branch=branch)
                        }
                current = next((row for row in self.storage_inventory(
                                    measure=False, branch_rows=current_branch_rows,
                                )["items"]
                                if row["path"] == str(path)), None)
                if not current or not current["eligible"]:
                    record({"path": str(path), "outcome": "retained",
                            "reason": current["reason"] if current else "ownership changed"})
                    continue
                if item["category"] == "worktree":
                    task = self.store.task(str(item["owner"]))
                    size = tree_bytes(path)
                    operation_id = pending(path, "worktree", size)
                    try:
                        gitops.remove_worktree(self.repo_for(task), path)
                        outcome = "removed" if not path.exists() else "failed"
                        record({"operation_id": operation_id, "path": str(path), "outcome": outcome,
                                "bytes_reclaimed": size if outcome == "removed" else 0,
                                **({"error": "worktree remained after Git removal"}
                                   if outcome == "failed" else {})})
                    except (OSError, gitops.GitError) as exc:
                        record({"operation_id": operation_id, "path": str(path),
                                "outcome": "failed", "bytes_reclaimed": 0, "error": str(exc)})
                elif item["category"] == "worker_home":
                    remaining = limit - len(results)
                    cleanup_home_caches(
                        path, path.parent, limit=remaining,
                        on_pending=lambda candidate, size: pending(candidate, "worker_home_cache", size),
                        on_result=record,
                    )
                elif item["category"] == "run_temp":
                    size = tree_bytes(path)
                    operation_id = pending(path, "run_temp", size)
                    try:
                        reclaimed = remove_owned_tree(path.parent, path)
                        record({"operation_id": operation_id, "path": str(path), "outcome": "removed",
                                "bytes_reclaimed": reclaimed})
                    except (OSError, ValueError) as exc:
                        record({"operation_id": operation_id, "path": str(path), "outcome": "failed",
                                "bytes_reclaimed": 0, "error": str(exc)})
                elif item["category"] == "worktree_cache":
                    size = tree_bytes(path)
                    operation_id = pending(path, "worktree_cache", size)
                    try:
                        root = next(root for root in (self.cfg.worktrees_dir,
                                                     self.cfg.garden_dir / "worktrees")
                                    if owned_child(root, path))
                        reclaimed = remove_owned_tree(root, path)
                        record({"operation_id": operation_id, "path": str(path), "outcome": "removed",
                                "bytes_reclaimed": reclaimed})
                    except (OSError, ValueError) as exc:
                        record({"operation_id": operation_id, "path": str(path), "outcome": "failed",
                                "bytes_reclaimed": 0, "error": str(exc)})
        report["status"] = "complete"
        write_audit(self.cfg.garden_dir, report, keep=audit_keep, destination=audit_path)
        self.state.get("__storage_cleanup__")["last_sweep"] = report
        for row in results:
            rep.transitions.append(f"{row['path']}: storage cleanup {row['outcome']}")
        return report

    def _reconcile_storage_audits(self) -> None:
        """Make a prior interrupted sweep explicit before beginning another one."""
        audit_dir = self.cfg.garden_dir / "storage-cleanup"
        if not audit_dir.is_dir() or audit_dir.is_symlink():
            return
        for path in sorted(audit_dir.glob("*.json")):
            try:
                report = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if report.get("status") != "in_progress":
                continue
            for operation in report.get("results", []):
                if operation.get("outcome") != "pending":
                    continue
                target = Path(str(operation.get("path") or ""))
                before_identity = operation.get("target_identity")
                current_identity = path_identity(target)
                current_bytes = tree_bytes(target) if current_identity is not None else 0
                if current_identity is None:
                    outcome = "removed_after_interruption"
                    reason = "target is absent; exact reclaimed bytes are unknown"
                elif current_identity != before_identity:
                    outcome = "unknown_after_interruption"
                    reason = "target identity changed; removal outcome is unknown"
                elif current_bytes < int(operation.get("bytes_before", 0)):
                    outcome = "partial_after_interruption"
                    reason = "target remains with fewer allocated bytes; exact reclaimed bytes are unknown"
                else:
                    outcome = "unknown_after_interruption"
                    reason = "target remains; no completed deletion result was published"
                operation.update({"outcome": outcome, "bytes_reclaimed": 0,
                                  "bytes_after": current_bytes, "reconciliation_reason": reason})
            report["bytes_reclaimed"] = sum(
                int(row.get("bytes_reclaimed", 0)) for row in report.get("results", [])
            )
            report["status"] = "interrupted"
            report["reconciled_at"] = now_iso()
            report["interruption_reason"] = "sweep did not publish a completion update"
            write_audit(self.cfg.garden_dir, report,
                        keep=int(self.cfg.get("storage_cleanup.audit_keep", 20) or 1),
                        destination=path)

    def _branch_cleanup_remote(self) -> str:
        return str(self.cfg.get("branches.remote", "origin") or "origin")

    def branch_cleanup_inventory(self, *, only_remote_branch: str = "") -> list[BranchDisposition]:
        tasks = self.store.tasks()
        repos = {}
        for product in {task.product for task in tasks.values()}:
            representative = next(task for task in tasks.values() if task.product == product)
            try:
                repos[product] = self.repo_for(representative)
            except Exception:  # an unavailable repo is classified explicitly
                continue
        observations = self.state.get("__open_prs__")
        open_heads = {
            (str(product), str(row.get("head") or ""))
            for product, record in observations.items()
            for row in (record.get("prs") or [])
            if str(row.get("state") or "OPEN") == "OPEN" and row.get("head")
        }
        try:
            state_text = json.dumps({key: value for key, value in self.state.data.items()
                                     if key != "__branch_cleanup__"})
        except (TypeError, ValueError):
            state_text = ""
        claimed_bases = {
            (task.product, str(self.state.get(task.id).get("pr_base") or ""))
            for task in tasks.values() if not task.status.terminal
            if self.state.get(task.id).get("pr_base")
        }
        configured = self.cfg.get("branches.protected", []) or []
        protected = {
            (task.product, branch)
            for task in tasks.values()
            for branch in {self.cfg.product_base_branch(task.product), *map(str, configured)}
        }
        preserved_heads = {
            (task.product, task.branch, str(self.state.get(task.id).get("head_sha") or ""))
            for task in tasks.values()
            if task.branch and self.state.get(task.id).get("pr_state") == "MERGED"
            and self.state.get(task.id).get("head_sha")
        }
        remote_heads: dict[str, dict[str, str]] = {}
        remote_errors: dict[str, str] = {}
        snapshots: dict[Path, dict[str, str]] = {}
        snapshot_errors: dict[Path, str] = {}
        timeout = float(self.cfg.get("branches.remote_timeout_seconds", 5) or 0)
        for product, repo in repos.items():
            if repo not in snapshots and repo not in snapshot_errors:
                try:
                    if not gitops.fetch(
                        repo, self._branch_cleanup_remote(), timeout=timeout,
                    ):
                        raise gitops.GitError("remote fetch failed")
                    heads = gitops.remote_branch_heads(
                        repo, self._branch_cleanup_remote(), timeout=timeout,
                    )
                    snapshots[repo] = (
                        {only_remote_branch: heads[only_remote_branch]}
                        if only_remote_branch and only_remote_branch in heads else
                        {} if only_remote_branch else heads
                    )
                except gitops.GitError as exc:
                    snapshot_errors[repo] = str(exc)
            if repo in snapshot_errors:
                remote_errors[product] = snapshot_errors[repo]
            else:
                remote_heads[product] = snapshots[repo]
        return classify_branches(tasks, self.runs.all_runs(), repos,
                                 remote=self._branch_cleanup_remote(),
                                 open_pr_heads=open_heads, claimed_bases=claimed_bases,
                                 protected_branches=protected, preserved_heads=preserved_heads,
                                 base_branches={product: self.cfg.product_base_branch(product)
                                                for product in repos},
                                 remote_heads=remote_heads, remote_errors=remote_errors,
                                 branch_filter={only_remote_branch} if only_remote_branch else None,
                                 state_text=state_text)

    def _branch_delete_recheck(self, item: BranchDisposition) -> str:
        """Refresh task PRs and rerun all local/run/ref classification immediately."""
        tasks = self.store.tasks()
        representative = next((tasks[task_id] for task_id in item.task_ids if task_id in tasks), None)
        if representative is None:
            return "recorded provenance changed"
        if not self.github.available:
            return "open PR claims could not be rechecked: provider is unavailable"
        slug = self.slug_for(representative)
        if not slug:
            return "open PR claims could not be rechecked: repository provider is not configured"
        try:
            open_pr = self.github.find_open_pr(slug, item.branch)
        except (GitHubError, OSError, ValueError) as exc:
            return f"open PR claims could not be rechecked: {exc}"
        if open_pr is not None:
            return f"PR #{open_pr.number} is open"
        try:
            dependent_pr = self.github.find_open_pr_by_base(slug, item.branch)
        except (GitHubError, OSError, ValueError) as exc:
            return f"open PR claims could not be rechecked: {exc}"
        if dependent_pr is not None:
            return f"PR #{dependent_pr.number} depends on the branch"
        for task_id in item.task_ids:
            task = tasks.get(task_id)
            if not task or not task.pr:
                continue
            number = self._pr_number(task)
            if not (number and slug):
                continue
            try:
                if self.github.get_pr(slug, number).state == "OPEN":
                    return f"PR #{number} is open"
            except GitHubError as exc:
                return f"PR state could not be rechecked: {exc}"
        current = next((row for row in self.branch_cleanup_inventory(only_remote_branch=item.branch)
                        if row.product == item.product and row.branch == item.branch), None)
        if current is None:
            return "recorded provenance changed"
        if current.classification != "removable":
            return current.reason
        if current.local_head != item.local_head or current.remote_head != item.remote_head:
            return "branch head changed after preview"
        return ""

    def sweep_worker_branches(self, rep: TickReport, *, limit: int | None = None) -> list[dict[str, Any]]:
        limit = limit if limit is not None else int(self.cfg.get("branches.cleanup_limit", 20) or 0)
        if limit <= 0:
            return []
        inventory = self.branch_cleanup_inventory()
        candidates = [row for row in inventory
                      if row.classification == "removable" and (row.local_head or row.remote_head)][:max(0, limit)]
        tasks = self.store.tasks()
        results = []
        for item in candidates:
            representative = next((tasks[task_id] for task_id in item.task_ids if task_id in tasks), None)
            if representative is None:
                continue
            result = delete_disposition(item, self.repo_for(representative),
                                        remote=self._branch_cleanup_remote(),
                                        recheck=self._branch_delete_recheck,
                                        remote_timeout=float(self.cfg.get(
                                            "branches.remote_timeout_seconds", 5,
                                        )))
            results.append(result)
            self.events.emit("branch_cleanup", ",".join(item.task_ids), product=item.product,
                             branch=item.branch, head=item.remote_head or item.local_head,
                             outcome=result["outcome"], reason=result.get("reason", item.reason),
                             errors=result.get("errors", []))
            rep.transitions.append(f"{item.branch}: branch cleanup {result['outcome']}")
        audit = {
            "counts": {kind: sum(row.classification == kind for row in inventory)
                       for kind in ("needed", "removable", "uncertain")},
            "inventory": [row.to_dict() for row in inventory[:500]],
            "results": results,
        }
        cleanup_state = self.state.get("__branch_cleanup__")
        prior = dict(cleanup_state.get("last_sweep") or {})
        prior.pop("at", None)
        if audit != prior:
            cleanup_state["last_sweep"] = {"at": now_iso(), **audit}
        return results
