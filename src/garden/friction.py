"""Harvest ## Friction sections from task PR bodies."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .github import GitHub
    from .model import Phase, Task
    from .runs import RunStore


def extract_friction(body: str) -> str:
    """Return the text under ## Friction in a markdown document, stripped."""
    if not body:
        return ""
    lines = body.splitlines()
    in_section = False
    out: list[str] = []
    for line in lines:
        if re.match(r"^##\s+Friction\s*$", line):
            in_section = True
            continue
        if in_section:
            if re.match(r"^#", line):
                break
            out.append(line)
    return "\n".join(out).strip()


def pr_body_for(task: Task, run_store: RunStore, github: GitHub | None = None, slug: str | None = None) -> str:
    """Return the PR body for a task: run.json first, GitHub fallback.

    Checks work and revise runs in reverse chronological order.
    Falls back to GitHub only when a PR URL and slug are available.
    """
    for run in reversed(run_store.runs_for(task.id)):
        if run.mode in ("work", "revise"):
            body = run.result.get("pr_body") or ""
            if body:
                return body
    if github and task.pr and github.available and slug:
        m = re.search(r"/pull/(\d+)", task.pr)
        if m:
            try:
                pr_info = github.get_pr(slug, int(m.group(1)))
                return pr_info.body or ""
            except Exception:
                pass
    return ""


def harvest(
    phase: Phase,
    run_store: RunStore,
    github: GitHub | None = None,
    slug: str | None = None,
) -> list[tuple[Any, str, str]]:
    """Collect (task, pr_url, friction_text) for tasks with friction.

    Includes every task that has a work/revise run with a non-empty ## Friction section.
    """
    results = []
    for task in phase.tasks:
        body = pr_body_for(task, run_store, github=github, slug=slug)
        text = extract_friction(body)
        if text:
            results.append((task, task.pr, text))
    return results


def write_friction_doc(path: Path, entries: list[tuple[Any, str, str]]) -> None:
    """Write friction.md grouped by task. Always fully regenerated so running twice is idempotent."""
    lines: list[str] = ["# Friction\n\n"]
    if not entries:
        lines.append("_No friction reported yet._\n")
    else:
        for task, pr_url, text in entries:
            lines.append(f"## {task.id}: {task.title}\n\n")
            if pr_url:
                lines.append(f"PR: {pr_url}\n\n")
            lines.append(text.strip() + "\n\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines))
