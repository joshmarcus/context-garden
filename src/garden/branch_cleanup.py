"""Conservative inventory and deletion of branches recorded in Garden run provenance."""

from __future__ import annotations

import re
from collections import deque
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


def _has_run_reference(state_text: str, run_id: str) -> bool:
    """Keep literal run references without claiming a longer ID's prefix.

    Same-second runs receive -2/-3 suffixes. Boundaries also preserve IDs recorded
    in backup paths, artifact filenames and stash names, not just bare JSON values.
    Punctuation can delimit a reference; native run-ID suffixes continue with word
    characters or hyphens.
    """
    if not run_id or run_id not in state_text:
        return False
    return re.search(rf"(?<![\w-]){re.escape(run_id)}(?![\w-])", state_text) is not None


def _run_references(state_text: str, run_ids: set[str]) -> set[str]:
    """Find boundary-delimited run IDs in one pass over a fresh state snapshot."""
    run_ids = {run_id for run_id in run_ids if run_id}
    if not state_text or not run_ids:
        return set()

    transitions: list[dict[str, int]] = [{}]
    failures = [0]
    outputs: list[list[str]] = [[]]
    for run_id in run_ids:
        node = 0
        for character in run_id:
            next_node = transitions[node].get(character)
            if next_node is None:
                next_node = len(transitions)
                transitions[node][character] = next_node
                transitions.append({})
                failures.append(0)
                outputs.append([])
            node = next_node
        outputs[node].append(run_id)

    pending = deque(transitions[0].values())
    while pending:
        node = pending.popleft()
        for character, child in transitions[node].items():
            pending.append(child)
            fallback = failures[node]
            while fallback and character not in transitions[fallback]:
                fallback = failures[fallback]
            failures[child] = transitions[fallback].get(character, 0)
            outputs[child].extend(outputs[failures[child]])

    referenced: set[str] = set()
    node = 0
    for end, character in enumerate(state_text, 1):
        while node and character not in transitions[node]:
            node = failures[node]
        node = transitions[node].get(character, 0)
        for run_id in outputs[node]:
            start = end - len(run_id)
            before = state_text[start - 1] if start else ""
            after = state_text[end] if end < len(state_text) else ""
            if (not before or re.match(r"[^\w-]", before)) and (
                    not after or re.match(r"[^\w-]", after)):
                referenced.add(run_id)
        if len(referenced) == len(run_ids):
            break
    return referenced


def classify_branches(
    tasks: dict[str, Task], runs: list[Run], repos: dict[str, Path], *, remote: str = "origin",
    open_pr_heads: set[tuple[str, str]] | None = None,
    claimed_bases: set[tuple[str, str]] | None = None,
    protected_branches: set[tuple[str, str]] | None = None,
    preserved_heads: set[tuple[str, str, str]] | None = None,
    base_branches: dict[str, str] | None = None,
    remote_heads: dict[str, dict[str, str]] | None = None,
    remote_errors: dict[str, str] | None = None,
    branch_filter: set[str] | None = None,
    state_text: str = "",
) -> list[BranchDisposition]:
    """Classify only branches whose Garden ownership is established by a managed run."""
    open_pr_heads = open_pr_heads or set()
    claimed_bases = claimed_bases or set()
    protected_branches = protected_branches or set()
    preserved_heads = preserved_heads or set()
    base_branches = base_branches or {}
    remote_errors = remote_errors or {}
    provenance: dict[tuple[str, str], set[str]] = {}
    task_product = {task.id: task.product for task in tasks.values()}
    for run in runs:
        product = task_product.get(run.task_id)
        if (product and run.branch and run.completion_mode in {"managed", "pushed"}
                and (branch_filter is None or run.branch in branch_filter)):
            provenance.setdefault((product, run.branch), set()).add(run.task_id)

    active = {(task_product.get(run.task_id, ""), run.branch) for run in runs
              if run.lifecycle_state != "finished" and run.branch}
    recovery_runs = [run for run in runs if run.branch
                     and (branch_filter is None or run.branch in branch_filter)]
    if branch_filter is None:
        referenced_run_ids = _run_references(state_text, {run.run_id for run in recovery_runs})
    else:
        # A delete recheck normally has one run for one branch. Searching only those few
        # IDs in the C regex engine is cheaper than rebuilding and walking the global index.
        referenced_run_ids = {
            run.run_id for run in recovery_runs if _has_run_reference(state_text, run.run_id)
        }
    recovery = {(task_product.get(run.task_id, ""), run.branch) for run in recovery_runs
                if run.run_id in referenced_run_ids}
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
        if product in remote_errors:
            result.append(BranchDisposition(
                product, branch, "uncertain",
                f"remote could not be inspected: {remote_errors[product]}",
                local_head=local, task_ids=tuple(sorted(ids)),
            ))
            continue
        if remote_heads is not None:
            remote_head = remote_heads.get(product, {}).get(branch, "")
        elif gitops.remote_url(repo, remote):
            try:
                gitops.fetch(repo, remote)
                remote_head = gitops.remote_branch_heads(repo, remote).get(branch, "")
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
        base = base_branches.get(product, "main")
        merged_record_heads = {
            head for head in heads if (product, branch, head) in preserved_heads
        }
        try:
            base_ref = gitops.base_ref(repo, base)
            base_heads = {head for head in heads if gitops.is_ancestor(repo, head, base_ref)}
        except gitops.GitError:
            base_heads = set()
        preserved = heads <= base_heads | merged_record_heads
        if preserved:
            reason = (f"branch head is preserved in {base}" if heads <= base_heads else
                      "branch head is preserved by its exact merged PR record")
            classification = "removable"
        else:
            classification, reason = "uncertain", "terminal work has commits not proven preserved or explicitly discarded"
        result.append(BranchDisposition(product, branch, classification, reason, local, remote_head, tuple(sorted(ids))))
    return result


def delete_disposition(
    item: BranchDisposition, repo: Path, *, remote: str = "origin",
    recheck: Callable[[BranchDisposition], str] | None = None,
    remote_timeout: float | None = None,
) -> dict[str, Any]:
    """Recheck claims, then independently compare-and-delete remote and local refs."""
    refusal = recheck(item) if recheck else ""
    if refusal:
        return {**item.to_dict(), "outcome": "retained", "reason": refusal}
    removed: list[str] = []
    errors: list[str] = []
    if item.remote_head:
        try:
            if gitops.delete_remote_branch(
                repo, remote, item.branch, item.remote_head, timeout=remote_timeout,
            ):
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
