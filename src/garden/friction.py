"""Harvest ## Friction sections from task PR bodies, and record reported friction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .github import GitHub
    from .model import Phase, Task
    from .runs import RunStore
    from .store import Store


FRICTION_COMMENT_MARKER = "<!-- garden-friction -->"


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


def friction_items(result: dict) -> list[str]:
    """Normalise a worker result's `friction` field to a list of non-empty strings.

    Friction now travels in its own result field (a list of short items), not in the PR
    body. A bare string is tolerated as a single item.
    """
    raw = result.get("friction") if isinstance(result, dict) else None
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(i).strip() for i in raw if str(i).strip()]


def friction_comment(items: list[str]) -> str:
    """Render friction items as the body of one marked PR comment."""
    bullets = "\n".join(f"- {i}" for i in items)
    return f"**Friction reported**\n\n{bullets}\n\n{FRICTION_COMMENT_MARKER}"


def friction_from_comment(body: str) -> list[str]:
    """Extract friction items from a marked friction comment, or [] if it isn't one."""
    if not body or FRICTION_COMMENT_MARKER not in body:
        return []
    items: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^\s*[-*]\s+(.*\S)\s*$", line)
        if m:
            items.append(m.group(1).strip())
    return items


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


def _extract_reported_section(text: str) -> str:
    """Return the raw '## Reported' section from an existing friction.md, or ''."""
    lines = text.splitlines(keepends=True)
    in_section = False
    out: list[str] = []
    for line in lines:
        if re.match(r"^##\s+Reported\s*$", line.rstrip()):
            in_section = True
            out.append(line)
            continue
        if in_section:
            if re.match(r"^##\s+\S", line) and not re.match(r"^###", line):
                break
            out.append(line)
    return "".join(out).rstrip()


def write_friction_doc(path: Path, entries: list[tuple[Any, str, str]]) -> None:
    """Write friction.md grouped by task; preserves any existing '## Reported' section."""
    reported = ""
    if path.exists():
        reported = _extract_reported_section(path.read_text())

    lines: list[str] = ["# Friction\n\n"]
    if not entries:
        lines.append("_No friction reported yet._\n")
    else:
        for task, pr_url, text in entries:
            lines.append(f"## {task.id}: {task.title}\n\n")
            if pr_url:
                lines.append(f"PR: {pr_url}\n\n")
            lines.append(text.strip() + "\n\n")
    if reported:
        lines.append("\n" + reported + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines))


def record_friction(path: Path, items: list[str], provenance: str, date: str) -> list[str]:
    """Append friction `items` under ## Reported in `path`, skipping any already present.

    Returns the items that were newly written. Used by the scheduler when a worker reports
    friction, and by `garden friction` when reconciling marked PR comments into the record.
    """
    existing = path.read_text() if path.exists() else ""
    fresh = [i for i in items if i and i not in existing]
    if fresh:
        append_friction_report(path, "\n".join(f"- {i}" for i in fresh), provenance, date)
    return fresh


def collect_comment_friction(phase: Phase, github: GitHub | None, slug: str | None) -> list[tuple[Any, list[str]]]:
    """Marked friction comments on each task's PR: [(task, [items])]. Empty without GitHub."""
    out: list[tuple[Any, list[str]]] = []
    if github is None or not slug or not getattr(github, "available", False):
        return out
    fn = getattr(github, "issue_comments", None)
    if not callable(fn):
        return out
    for task in phase.tasks:
        m = re.search(r"/pull/(\d+)", getattr(task, "pr", "") or "")
        if not m:
            continue
        items: list[str] = []
        try:
            bodies = list(fn(slug, int(m.group(1))))
        except Exception:
            bodies = []
        for body in bodies:
            for it in friction_from_comment(body):
                if it not in items:
                    items.append(it)
        if items:
            out.append((task, items))
    return out


def append_friction_report(path: Path, text: str, provenance: str, date: str) -> None:
    """Append a reported friction entry under '## Reported' in friction.md."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else "# Friction\n\n_No friction reported yet._\n"

    entry = f"### {date} · {provenance}\n\n{text.strip()}\n"

    if "## Reported" in existing:
        new_text = existing.rstrip() + "\n\n" + entry
    else:
        new_text = existing.rstrip() + "\n\n## Reported\n\n" + entry

    path.write_text(new_text)


def create_friction_draft_task(store: Store, product: str, phase: str, text: str, provenance: str, date: str) -> Any:
    """Create a draft task from a friction report; returns the new Task, or None if the phase
    is closed. The friction text is always recorded in friction.md by the caller; a closed
    phase just takes no new tasks."""
    ph = store.phase(product, phase)
    if ph.closed:
        return None
    first_line = text.strip().splitlines()[0] if text.strip() else "Friction report"
    title = first_line[:120]
    body = (
        f"## Goal\n\n{title}\n\n"
        f"## Context\n\nReported from {provenance} on {date}.\n\n{text.strip()}\n\n"
        "## Acceptance criteria\n\n- [ ] ...\n\n"
        "## Out of scope\n\n- ...\n"
    )
    return store.create_task(product, phase, title, body, status="draft")
