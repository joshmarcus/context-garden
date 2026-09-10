"""Bounded worker-branch cleanup after lifecycle and worktree reconciliation."""

from __future__ import annotations

import json
from typing import Any

from ..branch_cleanup import BranchDisposition, classify_branches, delete_disposition
from ..github import GitHubError
from ..model import now_iso
from .report import TickReport


class CleanupMixin:
    def _branch_cleanup_remote(self) -> str:
        return str(self.cfg.get("branches.remote", "origin") or "origin")

    def branch_cleanup_inventory(self) -> list[BranchDisposition]:
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
        return classify_branches(tasks, self.runs.all_runs(), repos,
                                 remote=self._branch_cleanup_remote(),
                                 open_pr_heads=open_heads, claimed_bases=claimed_bases,
                                 protected_branches=protected, state_text=state_text)

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
        current = next((row for row in self.branch_cleanup_inventory()
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
                                        recheck=self._branch_delete_recheck)
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
