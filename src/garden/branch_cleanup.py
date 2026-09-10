"""Conservative inventory and deletion of branches recorded in Garden run provenance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import gitops
from .model import Status, Task
from .runs import Run


@dataclass(frozen=True)
class BranchDisposition:
    product: str
    branch: str
    classification: str  # needed | removable | uncertain
    reason: str
    local_head: str = ""
    remote_head: str = ""
    task_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_branches(
    tasks: dict[str, Task], runs: list[Run], repos: dict[str, Path], *, remote: str = "origin",
    open_pr_heads: set[tuple[str, str]] | None = None,
    claimed_bases: set[tuple[str, str]] | None = None,
    protected_branches: set[tuple[str, str]] | None = None,
    state_text: str = "",
) -> list[BranchDisposition]:
    """Classify only branches whose Garden ownership is established by a managed run."""
    open_pr_heads = open_pr_heads or set()
    claimed_bases = claimed_bases or set()
    protected_branches = protected_branches or set()
    provenance: dict[tuple[str, str], set[str]] = {}
    task_product = {task.id: task.product for task in tasks.values()}
    for run in runs:
        product = task_product.get(run.task_id)
        if product and run.branch and run.completion_mode in {"managed", "pushed"}:
            provenance.setdefault((product, run.branch), set()).add(run.task_id)

    active = {(task_product.get(run.task_id, ""), run.branch) for run in runs
              if run.lifecycle_state != "finished" and run.branch}
    recovery = {(task_product.get(run.task_id, ""), run.branch) for run in runs
                if run.branch and run.run_id and run.run_id in state_text}
    stack_bases = claimed_bases | {
        (task.product, run.base) for run in runs if run.base and run.lifecycle_state != "finished"
        for task in [tasks.get(run.task_id)] if task is not None
    }
    result: list[BranchDisposition] = []
    for (product, branch), ids in sorted(provenance.items()):
        repo = repos.get(product)
        if repo is None:
            result.append(BranchDisposition(product, branch, "uncertain", "repository is unavailable", task_ids=tuple(sorted(ids))))
            continue
        local = gitops.local_head(repo, branch)
        remote_head = ""
        if gitops.remote_url(repo, remote):
            try:
                gitops.fetch(repo, remote)
                remote_sha = gitops.git("ls-remote", "--heads", remote, f"refs/heads/{branch}", cwd=repo).strip()
                remote_head = remote_sha.split()[0] if remote_sha else ""
            except gitops.GitError as exc:
                result.append(BranchDisposition(product, branch, "uncertain", f"remote could not be inspected: {exc}", local_head=local, task_ids=tuple(sorted(ids))))
                continue
        heads = {head for head in (local, remote_head) if head}
        if not heads:
            result.append(BranchDisposition(product, branch, "removable", "branch is already absent", task_ids=tuple(sorted(ids))))
            continue
        related = [tasks[task_id] for task_id in ids if task_id in tasks]
        reason = ""
        if (product, branch) in protected_branches:
            reason = "the branch is a default or configured protected branch"
        elif (product, branch) in active:
            reason = "an active or queued run owns the branch"
        elif (product, branch) in open_pr_heads:
            reason = "an open PR uses the branch"
        elif (product, branch) in stack_bases:
            reason = "an active run uses the branch as its stack base"
        elif branch in gitops.worktree_branches(repo):
            reason = "the branch is checked out in a worktree"
        elif any(task.status not in {Status.DONE, Status.CANCELLED, Status.WONT_DO, Status.FAILED} for task in related):
            reason = "a non-terminal task still claims the branch"
        elif any((task.runner == "manual") for task in related) or any(
            run.completion_mode == "external" for run in runs if run.task_id in ids and run.branch == branch
        ):
            reason = "manual or external ownership is recorded"
        elif (product, branch) in recovery:
            reason = "scheduler recovery state still references a run on the branch"
        if reason:
            result.append(BranchDisposition(product, branch, "needed", reason, local, remote_head, tuple(sorted(ids))))
            continue
        if related and all(task.status == Status.DONE for task in related):
            classification, reason = "removable", "all recorded tasks are complete"
        else:
            base = related[0].default_branch() if related else "main"
            try:
                base_ref = gitops.base_ref(repo, base)
                preserved = all(gitops.is_ancestor(repo, head, base_ref) for head in heads)
            except gitops.GitError:
                preserved = False
            if preserved:
                classification, reason = "removable", f"branch head is preserved in {base}"
            else:
                classification, reason = "uncertain", "terminal work has commits not proven preserved or explicitly discarded"
        result.append(BranchDisposition(product, branch, classification, reason, local, remote_head, tuple(sorted(ids))))
    return result


def delete_disposition(
    item: BranchDisposition, repo: Path, *, remote: str = "origin",
    recheck: Callable[[BranchDisposition], str] | None = None,
) -> dict[str, Any]:
    """Recheck claims, then independently compare-and-delete remote and local refs."""
    refusal = recheck(item) if recheck else ""
    if refusal:
        return {**item.to_dict(), "outcome": "retained", "reason": refusal}
    removed: list[str] = []
    errors: list[str] = []
    if item.remote_head:
        try:
            if gitops.delete_remote_branch(repo, remote, item.branch, item.remote_head):
                removed.append("remote")
        except gitops.GitError as exc:
            errors.append(f"remote: {exc}")
    # If the remote comparison failed, retain the local ref as the recovery copy. Partial
    # cleanup is retried later, after the remote identity can be established again.
    if not errors:
        try:
            if gitops.delete_local_branch(repo, item.branch, item.local_head):
                removed.append("local")
        except gitops.GitError as exc:
            errors.append(f"local: {exc}")
    outcome = "partial" if errors and removed else "failed" if errors else "removed" if removed else "absent"
    return {**item.to_dict(), "outcome": outcome, "removed": removed, "errors": errors}
