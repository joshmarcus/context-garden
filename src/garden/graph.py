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


HIDDEN_STATUSES = ("done", "cancelled")


def visible_ids(tasks: dict[str, Task], stack: bool = False, hide_done: bool = False) -> set[str]:
    """Task ids to draw/list when `hide_done` filters out finished work. Status (and thus
    dependency resolution) is always computed against the full `tasks` dict; this only
    narrows what gets displayed."""
    if not hide_done:
        return set(tasks)
    return {tid for tid, t in tasks.items() if effective_status(t, tasks, stack) not in HIDDEN_STATUSES}


def dependents(task_id: str, tasks: dict[str, Task]) -> list[str]:
    return sorted(t.id for t in tasks.values() if task_id in t.depends_on)


def deps_in_later_phase(task: Task, tasks: dict[str, Task], phase_index: dict[str, int]) -> list[str]:
    """Dependencies that sit in a later phase of the same product than `task`. Such a dep can
    never merge before `task`, so `task` can never become ready in the earlier phase — a state
    a move between phases can create. `phase_index` maps 'product/phase' to its ordinal within
    the product; positions are only comparable within one product, so cross-product deps are
    ignored."""
    ti = phase_index.get(task.key)
    if ti is None:
        return []
    out = []
    for d in task.depends_on:
        dep = tasks.get(d)
        if dep is None or dep.product != task.product:
            continue
        di = phase_index.get(dep.key)
        if di is not None and di > ti:
            out.append(d)
    return out


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
    "awaiting_triage": "fill:#e8dff8,stroke:#5b3fa8,color:#333",
    "done": "fill:#d4edda,stroke:#155724,color:#333",
    "failed": "fill:#f8d7da,stroke:#721c24,color:#333",
    "wont_do": "fill:#efe7dd,stroke:#8a6d3b,color:#333",
    "cancelled": "fill:#e9ecef,stroke:#6c757d,color:#999",
}


def mermaid(tasks: dict[str, Task], direction: str = "LR", visible: set[str] | None = None) -> str:
    vis = set(tasks) if visible is None else visible
    lines = [f"graph {direction}"]
    for tid in sorted(vis):
        t = tasks[tid]
        label = t.title.replace('"', "'")
        if len(label) > 40:
            label = label[:37] + "..."
        lines.append(f'  {_mid(tid)}["{tid}<br/>{label}"]')
        lines.append(f"  style {_mid(tid)} {MERMAID_CLASS.get(effective_status(t, tasks), MERMAID_CLASS['draft'])}")
    for t in tasks.values():
        if t.id not in vis:
            continue
        for d in t.depends_on:
            if d in tasks and d in vis:
                lines.append(f"  {_mid(d)} --> {_mid(t.id)}")
        if t.discovered_from in tasks and t.discovered_from in vis:
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
    "awaiting_triage": ("#e8dff8", "#5b3fa8"),
    "done": ("#d4edda", "#155724"),
    "failed": ("#f8d7da", "#721c24"),
    "wont_do": ("#efe7dd", "#8a6d3b"),
    "cancelled": ("#e9ecef", "#6c757d"),
}


def layers(tasks: dict[str, Task], visible: set[str] | None = None) -> dict[str, int]:
    """Longest-path layering: layer = 1 + max(layer of deps). When `visible` narrows the
    node set, edges through hidden tasks are dropped so the layout closes up rather than
    leaving a gap where the hidden task's column was."""
    vis = set(tasks) if visible is None else visible
    order = topological_order(tasks)
    out: dict[str, int] = {}
    for tid in order:
        if tid not in vis:
            continue
        deps = [d for d in tasks[tid].depends_on if d in out]
        if tasks[tid].discovered_from in out:
            deps.append(tasks[tid].discovered_from)
        out[tid] = 1 + max((out[d] for d in deps), default=-1)
    return out


def svg(tasks: dict[str, Task], link_prefix: str = "/tasks/", stack: bool = False, hide_done: bool = False) -> str:
    """The trellis: a lattice with the work climbing it. Layered left to right; each task is a
    growth-stage glyph (symbols from plants.DEFS, which the page must inline) at a lattice
    crossing, dependencies as vine, discovered work as a dashed tendril. With `hide_done`, done
    and cancelled tasks are dropped from the drawing and it is re-laid out without them; a task
    whose only dependency was hidden draws as a root, with the hidden dependency named on hover."""
    vis = visible_ids(tasks, stack, hide_done)
    if not vis:
        return '<svg xmlns="http://www.w3.org/2000/svg" class="trellis" width="10" height="10"></svg>'
    from .plants import STAGE, stage_word

    try:
        lay = layers(tasks, vis)
    except GraphError:
        lay = {tid: 0 for tid in vis}
    cols: dict[int, list[str]] = {}
    for tid, layer in lay.items():
        cols.setdefault(layer, []).append(tid)
    for c in cols.values():
        c.sort(key=lambda t: (tasks[t].priority, t))
    dx, dy, pad_x, pad_top, pad_bottom = 230, 118, 110, 44, 34
    max_rows = max(len(c) for c in cols.values())
    total_h = pad_top + pad_bottom + max_rows * dy
    total_w = pad_x * 2 + (max(cols) + 1) * dx - (dx - 190)
    pos: dict[str, tuple[float, float]] = {}
    for layer, ids in sorted(cols.items()):
        col_h = len(ids) * dy
        y0 = pad_top + (total_h - pad_top - pad_bottom - col_h) / 2
        for i, tid in enumerate(ids):
            pos[tid] = (pad_x + layer * dx, y0 + i * dy + 30)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="trellis" viewBox="0 0 {total_w:.0f} {total_h:.0f}" width="{total_w:.0f}" style="max-width:100%; height:auto; display:block" role="img" aria-label="trellis">',
        "<g class=\"lattice\">",
    ]
    for i in range(-4, int(total_w / 110) + 6):
        xa = -60 + i * 110
        if xa + total_h * 0.9 < 0 or xa > total_w:
            continue
        parts.append(f'<line x1="{xa}" y1="{total_h:.0f}" x2="{xa + total_h * 0.9:.0f}" y2="0"/>')
        parts.append(f'<line x1="{xa + total_h * 0.9:.0f}" y1="{total_h:.0f}" x2="{xa}" y2="0"/>')
    parts.append("</g>")
    parts.append(f'<line class="post" x1="{pad_x - 70}" y1="{total_h - 8:.0f}" x2="{pad_x - 70}" y2="8"/><line class="post" x1="{total_w - 40:.0f}" y1="{total_h - 8:.0f}" x2="{total_w - 40:.0f}" y2="8"/>')
    vine = []
    for tid in vis:
        t = tasks[tid]
        x, y = pos[t.id]
        deps = [d for d in t.depends_on if d in pos]
        if not deps and t.discovered_from not in pos:
            x1, y1 = x - 60, total_h - 6
            vine.append(f'<path d="M{x1:.0f} {y1:.0f} C{x1:.0f} {y1 - 50:.0f} {x - 24:.0f} {y + 50:.0f} {x:.0f} {y:.0f}"/>')
        for d in deps:
            dxp, dyp = pos[d]
            vine.append(f'<path d="M{dxp:.0f} {dyp:.0f} C{dxp + dx * .5:.0f} {dyp:.0f} {x - dx * .5:.0f} {y:.0f} {x:.0f} {y:.0f}"/>')
        if t.discovered_from in pos and t.discovered_from not in t.depends_on:
            dxp, dyp = pos[t.discovered_from]
            vine.append(f'<path class="tendril" d="M{dxp:.0f} {dyp:.0f} C{dxp + dx * .35:.0f} {dyp + 10:.0f} {x - dx * .4:.0f} {y - 30:.0f} {x:.0f} {y:.0f}"/>')
    parts.append("<g class=\"vine\">" + "".join(vine) + "</g>")
    for tid, (x, y) in pos.items():
        t = tasks[tid]
        st = effective_status(t, tasks, stack)
        title = _esc(t.title)
        short = title if len(title) <= 26 else title[:24] + "…"
        hidden_deps = [d for d in t.depends_on if d in tasks and d not in vis]
        dep_note = f" — depends on hidden: {', '.join(hidden_deps)}" if hidden_deps else ""
        parts.append(
            f'<a href="{link_prefix}{tid}"><g><title>{tid}: {title} — {st.replace("_", " ")} ({stage_word(st)}){_esc(dep_note)}</title>'
            f'<circle class="halo" cx="{x:.0f}" cy="{y:.0f}" r="19"/>'
            f'<use href="#{STAGE.get(st, "st-seed")}" transform="translate({x - 15:.0f} {y - 15:.0f}) scale(1.25)"/>'
            f'<text class="nid" x="{x:.0f}" y="{y + 36:.0f}" text-anchor="middle">{tid}</text>'
            f'<text class="ntitle" x="{x:.0f}" y="{y + 52:.0f}" text-anchor="middle">{short}</text>'
            f'<text class="nstate" x="{x:.0f}" y="{y + 66:.0f}" text-anchor="middle">{st.replace("_", " ")}</text>'
            f'</g></a>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
