"""Planning: turn a phase's goals + specs into draft task files.

Two entry points, both token-frugal:
  * `plan_prompt()`    builds the prompt (goals, specs, existing task ids, principles digest)
  * `import_plan()`    writes task files from a JSON list (produced by `claude -p` or by a
                       human-driven session using the garden-plan skill)
The LLM sees goals/specs once and emits structured JSON; nothing else in the loop needs it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .model import Status, Task, estimate_tokens, goals_text
from .store import Store

PLAN_INSTRUCTIONS = """\
# Planning request

You are the planner for a context garden. Turn the phase goals and specs below into a set of
small, independently shippable tasks for autonomous coding agents. Each task will be executed by
a fresh agent session that sees ONLY: the principles digest, the product overview, the phase
goals, the task body, and the files on the task's reading list. Write tasks accordingly.

Rules:
- 3-12 tasks. Each should be completable in one focused session (roughly 1-3 hours of agent work) and result in ONE pull request.
- Prefer vertical slices that leave the product working after every merge.
- Use `depends_on` only for real ordering constraints (a task needs another's code). Fewer dependencies = more parallelism.
- `reading` is a list of paths relative to the garden root (specs, docs) the agent must read. Keep it minimal; the digest/product/goals are included automatically.
- The body must contain: `## Goal` (1-2 sentences), `## Context` (what the agent needs to know that isn't in the reading list), `## Acceptance criteria` (checklist, testable), `## Out of scope`.
- Do not include tasks that already exist (see existing task list). You may depend on existing ids.
- `difficulty` picks the model tier: "easy" (mechanical, well-specified, small blast radius), "medium" (typical feature work), "hard" (design judgment, cross-cutting, subtle correctness). Be honest; it controls cost.
- Output ONLY a JSON array (no prose, no fences) of objects with keys:
  title, priority (1-5, 1 highest), estimate ("S"|"M"|"L"), difficulty ("easy"|"medium"|"hard"), depends_on (list of ids or titles from this batch), reading (list of paths), body (markdown string).
  Reference batch-internal dependencies by exact title; they are resolved to ids on import.
"""


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _task_log_lines(task: Task, n: int = 3) -> list[str]:
    """Return the last n bullet lines from the task's ## Log section."""
    m = re.search(r"^## Log\s*$(.*?)(?=^## |\Z)", task.body, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip().startswith("-")]
    return lines[-n:]


def _replan_section(tasks: list[Task]) -> str:
    """Build a prompt section summarising failed/blocked tasks for --replan."""
    troubled = [t for t in tasks if t.status in (Status.FAILED, Status.WAITING_HUMAN)]
    if not troubled:
        return ""
    lines = [
        "## Failed and blocked tasks",
        "",
        "These tasks did not complete. When replanning you may fix them in place, split them,",
        "or supersede them with new tasks. To supersede, include `\"supersedes\": [\"CG-001\"]`",
        "in the replacement task's JSON; the import will cancel the listed ids.",
        "",
    ]
    for t in troubled:
        lines.append(f"### {t.id} [{t.status.value}] {t.title}")
        lines.append("")
        log = _task_log_lines(t)
        if log:
            lines.append("Last log entries:")
            lines.extend(log)
        if t.status == Status.WAITING_HUMAN:
            for entry in reversed(log):
                m = re.match(r"^- \S+ worker asks: (.+)", entry)
                if m:
                    lines.append(f"Blocked question: {m.group(1)}")
                    break
        lines.append("")
    return "\n".join(lines)


def plan_prompt(store: Store, product: str, phase: str, extra: str = "", replan: bool = False) -> str:
    prod = store.product(product)
    ph = store.phase(product, phase)
    parts = [PLAN_INSTRUCTIONS]
    digest = store.root / str(store.config.get("principles_digest"))
    if digest.exists():
        parts.append("## Principles (digest)\n\n" + _read(digest))
    if prod.overview_path:
        parts.append(f"## Product: {product}\n\n" + _read(prod.overview_path))
    if ph.goals_path:
        parts.append(f"## Phase goals: {phase}\n\n" + goals_text(ph.goals_path))
    for spec in ph.specs:
        parts.append(f"## Spec: {store.rel(spec)}\n\n" + _read(spec))
    for doc in ph.docs:
        if doc.suffix.lower() in (".md", ".txt"):
            parts.append(f"## Doc: {store.rel(doc)}\n\n" + _read(doc))
    if ph.tasks:
        lines = [f"- {t.id} [{t.status.value}] {t.title}" for t in ph.tasks]
        parts.append("## Existing tasks in this phase\n\n" + "\n".join(lines))
    other = [t for t in store.tasks().values() if t.product == product and t.phase != phase]
    if other:
        lines = [f"- {t.id} [{t.status.value}] {t.phase}: {t.title}" for t in other[-40:]]
        parts.append("## Tasks in other phases of this product (for dependencies / avoiding repeats)\n\n" + "\n".join(lines))
    if replan:
        replan_text = _replan_section(ph.tasks)
        if replan_text:
            parts.append(replan_text)
    if extra:
        parts.append("## Additional guidance from the human\n\n" + extra.strip())
    parts.append("Now output the JSON array.")
    return "\n\n".join(parts) + "\n"


def parse_plan(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    # strip a fence if the model added one anyway
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e <= s:
        raise ValueError("no JSON array found in planner output")
    data = json.loads(text[s : e + 1])
    if not isinstance(data, list):
        raise ValueError("planner output is not a list")
    out = []
    for item in data:
        if not isinstance(item, dict) or not item.get("title"):
            raise ValueError(f"bad task item: {item!r}")
        out.append(item)
    return out


def import_plan(store: Store, product: str, phase: str, items: list[dict[str, Any]], status: str | None = None) -> list[Task]:
    """Create task files; resolve batch-internal dependencies by title."""
    if status is None:
        status = "ready" if bool(store.config.get("plan.auto_approve", True)) else "draft"
    existing_titles = {t.title.strip().lower(): t.id for t in store.tasks().values()}
    created: list[Task] = []
    title_to_id: dict[str, str] = {}
    # first pass: allocate ids in order
    pending = []
    for item in items:
        title = str(item["title"]).strip()
        if title.lower() in existing_titles:
            continue
        tid = store.next_id(product)
        # reserve the id by creating the file immediately (next_id scans files)
        t = store.create_task(
            product, phase, title, str(item.get("body") or ""),
            priority=int(item.get("priority", 3) or 3),
            estimate=str(item.get("estimate") or ""),
            reading=[str(r) for r in (item.get("reading") or [])],
            status=status,
            task_id=tid,
            difficulty=str(item.get("difficulty") or "medium"),
        )
        title_to_id[title.lower()] = tid
        pending.append((t, item))
        created.append(t)
    # second pass: dependencies
    all_ids = {t.id for t in store.tasks().values()}
    for t, item in pending:
        deps = []
        for d in item.get("depends_on") or []:
            d = str(d).strip()
            if d in all_ids:
                deps.append(d)
            elif d.lower() in title_to_id:
                deps.append(title_to_id[d.lower()])
            elif d.lower() in existing_titles:
                deps.append(existing_titles[d.lower()])
            else:
                t.log(f"planner referenced unknown dependency {d!r}; dropped")
        t.depends_on = deps
        store.save(t)
    # third pass: supersedes — cancel tasks the planner wants to replace
    tasks_by_id = store.tasks()
    for t, item in pending:
        for sid in [str(s) for s in (item.get("supersedes") or [])]:
            sup = tasks_by_id.get(sid)
            if sup is None:
                t.log(f"supersedes references unknown task {sid!r}; ignored")
                store.save(t)
            elif not sup.status.terminal:
                sup.status = Status.CANCELLED
                sup.log(f"superseded by {t.id}")
                store.save(sup)
    store.invalidate()
    return created


def run_planner(store: Store, prompt: str, harness_name: str = "", difficulty: str = "hard") -> str:
    """One headless harness call. Returns the raw final text."""
    import os

    harness = store.config.harness(harness_name or str(store.config.get("harness") or "claude"))
    model = harness.model_for(difficulty)
    cmd = harness.command(model, None)
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, cwd=str(store.root), check=False)
    parsed = harness.parse(proc.stdout, proc.stderr, None)
    if proc.returncode != 0 and not parsed["final_text"]:
        raise RuntimeError(f"planner failed ({proc.returncode}): {proc.stderr.strip()[-1000:]}")
    return parsed["final_text"] or proc.stdout


def prompt_tokens(prompt: str) -> int:
    return estimate_tokens(prompt)
