"""Dependency graph over tasks: ready set, cycles, topological order, mermaid export."""

from __future__ import annotations

from collections import deque

from .model import Status, Task


class GraphError(Exception):
    pass


def validate(tasks: dict[str, Task]) -> list[str]:
    """Return human-readable problems (unknown deps, cycles). Empty list = healthy."""
    problems: list[str] = []
    for t in tasks.values():
        for d in t.depends_on:
            if d not in tasks:
                problems.append(f"{t.id} depends on unknown task {d}")
    try:
        topological_order(tasks)
    except GraphError as e:
        problems.append(str(e))
    return problems


def topological_order(tasks: dict[str, Task]) -> list[str]:
    indeg = {tid: 0 for tid in tasks}
    children: dict[str, list[str]] = {tid: [] for tid in tasks}
    for t in tasks.values():
        for d in t.depends_on:
            if d in tasks:
                indeg[t.id] += 1
                children[d].append(t.id)
    q = deque(sorted(tid for tid, n in indeg.items() if n == 0))
    order: list[str] = []
    while q:
        tid = q.popleft()
        order.append(tid)
        for c in sorted(children[tid]):
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    if len(order) != len(tasks):
        stuck = sorted(tid for tid, n in indeg.items() if n > 0)
        raise GraphError(f"dependency cycle among: {', '.join(stuck)}")
    return order


def stackable(dep: Task) -> bool:
    """A dependency whose branch is pushed and PR is open can be stacked on."""
    return dep.status.has_branch and bool(dep.branch) and bool(dep.pr)


def blockers(task: Task, tasks: dict[str, Task], stack: bool = False) -> list[str]:
    """Deps that are not done (unknown deps count as blockers). With `stack`, deps whose PR is
    open count as satisfied: the task will branch from the dep's branch."""
    out = []
    for d in task.depends_on:
        dep = tasks.get(d)
        if dep is None or dep.status == Status.DONE:
            if dep is None:
                out.append(d)
            continue
        if stack and stackable(dep):
            continue
        out.append(d)
    return out


def stack_parents(task: Task, tasks: dict[str, Task]) -> list[Task]:
    """Open-PR dependencies this task would stack on (in dependency order)."""
    return [tasks[d] for d in task.depends_on if d in tasks and tasks[d].status != Status.DONE and stackable(tasks[d])]


def is_blocked(task: Task, tasks: dict[str, Task], stack: bool = False) -> bool:
    return bool(blockers(task, tasks, stack))


def ready(tasks: dict[str, Task], stack: bool = False) -> list[Task]:
    """Tasks that can be dispatched now, best first (priority, then id)."""
    out = [t for t in tasks.values() if t.status == Status.READY and not is_blocked(t, tasks, stack)]
    return sorted(out, key=lambda t: (t.priority, t.id))


def effective_status(task: Task, tasks: dict[str, Task], stack: bool = False) -> str:
    if task.status in (Status.READY, Status.DRAFT) and is_blocked(task, tasks, stack):
        return "blocked"
    return task.status.value


def dependents(task_id: str, tasks: dict[str, Task]) -> list[str]:
    return sorted(t.id for t in tasks.values() if task_id in t.depends_on)


def critical_path(tasks: dict[str, Task]) -> list[str]:
    """Longest chain of not-done tasks (by count), useful for 'what to unblock first'."""
    order = topological_order(tasks)
    best: dict[str, tuple[int, list[str]]] = {}
    for tid in order:
        t = tasks[tid]
        if t.status.terminal:
            best[tid] = (0, [])
            continue
        chain: tuple[int, list[str]] = (1, [tid])
        for d in t.depends_on:
            if d in best and best[d][0] + 1 > chain[0]:
                chain = (best[d][0] + 1, best[d][1] + [tid])
        best[tid] = chain
    if not best:
        return []
    return max(best.values(), key=lambda c: c[0])[1]


MERMAID_CLASS = {
    "draft": "fill:#eee,stroke:#999,color:#333",
    "blocked": "fill:#fff3cd,stroke:#c9a300,color:#333",
    "ready": "fill:#d1ecf1,stroke:#0c5460,color:#333",
    "running": "fill:#cce5ff,stroke:#004085,color:#333",
    "in_review": "fill:#e2d9f3,stroke:#4b2e83,color:#333",
    "changes_requested": "fill:#ffe5d0,stroke:#b35c00,color:#333",
    "waiting_human": "fill:#fde2f3,stroke:#8a1c5c,color:#333",
    "done": "fill:#d4edda,stroke:#155724,color:#333",
    "failed": "fill:#f8d7da,stroke:#721c24,color:#333",
    "cancelled": "fill:#e9ecef,stroke:#6c757d,color:#999",
}


def mermaid(tasks: dict[str, Task], direction: str = "LR") -> str:
    lines = [f"graph {direction}"]
    for tid in sorted(tasks):
        t = tasks[tid]
        label = t.title.replace('"', "'")
        if len(label) > 40:
            label = label[:37] + "..."
        lines.append(f'  {_mid(tid)}["{tid}<br/>{label}"]')
        lines.append(f"  style {_mid(tid)} {MERMAID_CLASS[effective_status(t, tasks)]}")
    for t in tasks.values():
        for d in t.depends_on:
            if d in tasks:
                lines.append(f"  {_mid(d)} --> {_mid(t.id)}")
        if t.discovered_from in tasks:
            lines.append(f"  {_mid(t.discovered_from)} -.->|discovered| {_mid(t.id)}")
    return "\n".join(lines)


def _mid(tid: str) -> str:
    return tid.replace("-", "_")


# ---- inline SVG rendering (no JS, no CDN) -------------------------------------
SVG_FILL = {
    "draft": ("#eeeeee", "#999999"),
    "blocked": ("#fff3cd", "#c9a300"),
    "ready": ("#d1ecf1", "#0c5460"),
    "running": ("#cce5ff", "#004085"),
    "in_review": ("#e2d9f3", "#4b2e83"),
    "changes_requested": ("#ffe5d0", "#b35c00"),
    "waiting_human": ("#fde2f3", "#8a1c5c"),
    "done": ("#d4edda", "#155724"),
    "failed": ("#f8d7da", "#721c24"),
    "cancelled": ("#e9ecef", "#6c757d"),
}


def layers(tasks: dict[str, Task]) -> dict[str, int]:
    """Longest-path layering: layer = 1 + max(layer of deps)."""
    order = topological_order(tasks)
    out: dict[str, int] = {}
    for tid in order:
        deps = [d for d in tasks[tid].depends_on if d in out]
        if tasks[tid].discovered_from in out:
            deps.append(tasks[tid].discovered_from)
        out[tid] = 1 + max((out[d] for d in deps), default=-1)
    return out


def svg(tasks: dict[str, Task], link_prefix: str = "/tasks/") -> str:
    """A left-to-right layered DAG as an inline SVG with clickable nodes."""
    if not tasks:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
    try:
        lay = layers(tasks)
    except GraphError:
        lay = {tid: 0 for tid in tasks}
    cols: dict[int, list[str]] = {}
    for tid, layer in lay.items():
        cols.setdefault(layer, []).append(tid)
    for c in cols.values():
        c.sort(key=lambda t: (tasks[t].priority, t))
    w, h, gx, gy, pad = 190, 46, 70, 16, 20
    pos: dict[str, tuple[float, float]] = {}
    max_rows = max(len(c) for c in cols.values())
    total_h = max_rows * (h + gy) - gy + 2 * pad
    for layer, ids in sorted(cols.items()):
        col_h = len(ids) * (h + gy) - gy
        y0 = (total_h - col_h) / 2
        for i, tid in enumerate(ids):
            pos[tid] = (pad + layer * (w + gx), y0 + i * (h + gy))
    total_w = pad * 2 + (max(cols) + 1) * (w + gx) - gx
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" height="{total_h:.0f}" viewBox="0 0 {total_w:.0f} {total_h:.0f}" font-family="ui-monospace, Menlo, monospace" font-size="11">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#888"/></marker></defs>',
    ]
    for t in tasks.values():
        edges = [(d, "") for d in t.depends_on if d in pos]
        if t.discovered_from in pos and t.discovered_from not in t.depends_on:
            edges.append((t.discovered_from, ' stroke-dasharray="5,4"'))
        for d, dash in edges:
            x1, y1 = pos[d][0] + w, pos[d][1] + h / 2
            x2, y2 = pos[t.id][0], pos[t.id][1] + h / 2
            cx = (x1 + x2) / 2
            parts.append(f'<path d="M {x1:.0f} {y1:.0f} C {cx:.0f} {y1:.0f}, {cx:.0f} {y2:.0f}, {x2:.0f} {y2:.0f}" fill="none" stroke="#888" stroke-width="1.3"{dash} marker-end="url(#arrow)"/>')
    for tid, (x, y) in pos.items():
        t = tasks[tid]
        fill, stroke = SVG_FILL[effective_status(t, tasks)]
        title = _esc(t.title)
        short = title if len(title) <= 26 else title[:24] + "…"
        parts.append(
            f'<a href="{link_prefix}{tid}"><g><title>{tid}: {title}</title>'
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + 10:.0f}" y="{y + 18:.0f}" fill="#333" font-weight="bold">{tid}</text>'
            f'<text x="{x + 10:.0f}" y="{y + 34:.0f}" fill="#333" font-family="-apple-system, Segoe UI, Roboto, sans-serif">{short}</text>'
            f'</g></a>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
