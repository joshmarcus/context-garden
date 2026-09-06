"""`garden` read-only views: status, ls, show, ready, trellis, validate, brief."""

from __future__ import annotations

import json

import typer
from rich import box
from rich.table import Table

from ..model import STATUS_ORDER, Status, priority_label
from .common import PANEL_BOARD, _scheduler, _store, _style, _task, app, console


# --------------------------------------------------------------------------- read-only views
@app.command(rich_help_panel=PANEL_BOARD)
def status(
    product: str | None = typer.Option(None, "--product", "-p"),
    all_: bool = typer.Option(False, "--all", help="One row per closed phase too, instead of a summary line"),
):
    """Overview per phase, plus cost totals."""
    from ..graph import effective_status
    from ..inbox import build_inbox, needs_you
    from ..runs import RunStore

    store = _store()
    tasks = store.tasks()
    # Compact so a full lifecycle (now with wont_do) still fits an 80-column terminal: a
    # minimal box, no edge padding, two-letter headers, and a legend printed underneath.
    table = Table(title=f"garden: {store.config.get('name')}  ({store.root})", show_lines=False,
                  box=box.SIMPLE_HEAD, padding=(0, 1), pad_edge=False, collapse_padding=True)
    table.add_column("phase")
    cols = ["draft", "blocked", "ready", "running", "waiting_human", "awaiting_triage", "in_review",
            "merged_into_parent", "changes_requested", "done", "wont_do", "failed"]
    short = {"draft": "df", "blocked": "bl", "ready": "rd", "running": "rn", "waiting_human": "wh",
             "awaiting_triage": "tr", "in_review": "rv", "merged_into_parent": "mp", "changes_requested": "cr",
             "done": "dn", "wont_do": "wd", "failed": "fl"}
    for s in cols:
        table.add_column(short[s], justify="right")
    table.add_column("!", justify="right")  # non-terminal tasks flagged needs_human (stuck, capped, closed…)
    table.add_column("spent", justify="right")
    sched = _scheduler(store)
    inbox_items = build_inbox(store, sched)
    stack = bool(store.config.get("stack", True))
    closed_phases = []
    retro_waiting = []
    kickoff_missing = []
    for prod in store.products():
        if product and prod.name != product:
            continue
        for ph in prod.phases:
            if ph.closed and not all_:
                closed_phases.append(ph)
                continue
            counts = {s: 0 for s in STATUS_ORDER + ["blocked"]}
            attn = sum(1 for item in inbox_items if item.get("phase") == ph.key and needs_you(item))
            for t in ph.tasks:
                counts[effective_status(t, tasks, stack)] += 1
            budget = sched.budget_for(ph.key)
            spent = sched.spent_for(ph.key)
            money = f"${spent:.2f}" + (f" / ${budget:.2f}" if budget else "")
            if budget and spent >= budget:
                money = f"[red]{money}[/red]"
            attn_cell = f"[bold red]{attn}[/bold red]" if attn else "[dim]·[/dim]"
            table.add_row(ph.key, *[_count(counts[s], s) for s in cols], attn_cell, money)
            pending = sched.retro_pending(ph.key)
            if pending:
                retro_waiting.append((ph.key, pending))
            if any(t.status != Status.DRAFT for t in ph.tasks) and not sched.has_kickoff(ph):
                kickoff_missing.append(ph.key)
    if table.rows:
        console.print(table)
        legend = "  ".join(f"{short[s]} {s}" for s in cols) + "  ! needs you"
        console.print(f"[dim]{legend}[/dim]")
    for key, pending in retro_waiting:
        console.print(f"[yellow]{key} retro: waiting for personas ({pending['done']} of {pending['total']})[/yellow]")
    for key in kickoff_missing:
        console.print(f"[yellow]{key}: tasks approved with no kickoff report — run `garden kickoff {key}`[/yellow]")
    if closed_phases:
        n = len(closed_phases)
        listing = ", ".join(f"{ph.key} (closed {ph.closed})" for ph in closed_phases)
        console.print(f"[dim]{n} closed phase{'s' if n != 1 else ''}: {listing} — `garden status --all` for rows, /herbarium in the web UI[/dim]")
    tot = RunStore(store.config.garden_dir).totals()
    console.print(f"runs: {tot['runs']}  cost: ${tot['cost_usd']:.2f}  in: {tot['input_tokens']:,}  out: {tot['output_tokens']:,}  cache-read: {tot['cache_read_input_tokens']:,}")
    mp_live = sched.overrides().get("max_parallel")
    mp_line = f"workers: {len(sched.worker_runs_active())}/{sched.effective_max_parallel()}"
    if mp_live is not None:
        mp_line += f" (live override; garden.yaml: {store.config.get('max_parallel')})"
    mp_line += f"  reviews: {len(sched.review_runs_active())}/{sched.review_parallel_limit()}"
    active_profile = sched.operating_profile_name()
    mp_line += f"  operating profile: {active_profile or '(none)'}"
    console.print(mp_line)
    up = sched.upgrade_available()
    if up:
        sha = str(up.get("sha") or "")[:12]
        count = up.get("count")
        line = f"tool update available: {sha}"
        if count is not None:
            line += f", {count} merged PR{'s' if count != 1 else ''} since {str(up.get('from') or '')[:12] or 'the current install'}"
        console.print(f"[cyan]{line}[/cyan] — run `garden upgrade`")
    from ..scheduler import State
    ctrl = State(store.config.garden_dir / "state.json").get("_control")
    if ctrl.get("dispatch") == "paused":
        at = ctrl.get("at", "")
        by = ctrl.get("by", "")
        reason = ctrl.get("reason", "")
        msg = f"dispatch paused (by {by} at {at[11:16]}"
        if reason:
            msg += f": {reason}"
        msg += ")"
        console.print(f"[yellow]{msg}[/yellow]")
    for name, entry in sorted((ctrl.get("paused_harnesses") or {}).items()):
        at = str(entry.get("at") or "")
        reason = str(entry.get("reason") or "")
        console.print(f"[yellow]harness {name} paused (at {at[11:16]}){f': {reason}' if reason else ''}[/yellow]")
    from ..gitops import is_repo, uncommitted_task_files
    if is_repo(store.root):
        dirty = uncommitted_task_files(store.root)
        if dirty:
            n = len(dirty)
            console.print(f"[yellow]{n} task file{'s' if n != 1 else ''} with uncommitted changes — run `garden commit` to save them[/yellow]")


def _count(n: int, s: str) -> str:
    return _style(s, str(n)) if n else "[dim]·[/dim]"


@app.command("ls", rich_help_panel=PANEL_BOARD)
def ls(
    product: str | None = typer.Option(None, "--product", "-p"),
    phase: str | None = typer.Option(None, "--phase"),
    status_: str | None = typer.Option(None, "--status", "-s", help="draft|blocked|ready|running|waiting_human|in_review|merged_into_parent|changes_requested|done|failed|cancelled"),
    discovered: bool = typer.Option(False, help="Only tasks that workers discovered"),
    json_out: bool = typer.Option(False, "--json"),
):
    """List tasks."""
    from ..graph import blockers, effective_status

    store = _store()
    tasks = store.tasks()
    stack = bool(store.config.get("stack", True))
    rows = []
    for t in sorted(tasks.values(), key=lambda t: (t.product, t.phase, t.id)):
        if product and t.product != product:
            continue
        if phase and t.phase != phase:
            continue
        if discovered and not t.discovered_from:
            continue
        eff = effective_status(t, tasks, stack)
        if status_ and eff != status_:
            continue
        rows.append((t, eff))
    if json_out:
        print(json.dumps([{**t.to_frontmatter(), "effective_status": eff, "path": store.rel(t.path)} for t, eff in rows], indent=2))
        return
    table = Table(show_lines=False)
    for c in ("id", "status", "pri", "diff", "title", "phase", "deps", "pr"):
        table.add_column(c)
    for t, eff in rows:
        deps = ",".join(t.depends_on)
        if eff == "blocked":
            deps = "[yellow]" + ",".join(blockers(t, tasks, stack)) + "[/yellow]"
        elif stack and t.status.value in ("ready", "draft") and blockers(t, tasks, stack=False):
            deps = "[cyan]stack:" + ",".join(blockers(t, tasks, stack=False)) + "[/cyan]"
        title = t.title + (" [dim](discovered)[/dim]" if t.discovered_from else "")
        table.add_row(t.id, _style(eff), priority_label(t.priority), t.difficulty, title, t.key, deps, t.pr or "")
    console.print(table)


@app.command(rich_help_panel=PANEL_BOARD)
def show(task_id: str, raw: bool = typer.Option(False, help="Print the file verbatim")):
    """Show a task, its blockers and its runs."""
    from rich.markdown import Markdown

    from ..graph import blockers, dependents
    from ..runs import RunStore

    store = _store()
    t = _task(store, task_id)
    if raw:
        print(t.render())
        return
    tasks = store.tasks()
    console.print(f"[bold]{t.id}[/bold] {t.title}  {_style(t.status.value)}  pri={priority_label(t.priority)}  difficulty={t.difficulty}  {t.key}")
    console.print(f"file: {store.rel(t.path)}")
    if t.depends_on:
        console.print(f"depends_on: {', '.join(t.depends_on)}  blockers: {', '.join(blockers(t, tasks)) or '-'}")
    deps = dependents(t.id, tasks)
    if deps:
        console.print(f"unblocks: {', '.join(deps)}")
    if t.branch:
        console.print(f"branch: {t.branch}")
    if t.pr:
        console.print(f"pr: {t.pr}")
    if t.reading:
        console.print("reading: " + ", ".join(t.reading))
    if t.discovered_from:
        console.print(f"discovered by: {t.discovered_from}")
    from ..scheduler import State

    st = State(store.config.garden_dir / "state.json").get(t.id)
    if st.get("stack_parent"):
        console.print(f"stacked on: {st['stack_parent']} (PR targets {st.get('pr_base')})")
    if t.status == Status.WAITING_HUMAN and st.get("decision"):
        dec = st["decision"]
        console.print(f"[bold deep_pink3]worker decision ({dec.get('kind')}):[/bold deep_pink3] {dec.get('reason', '')}")
        if dec.get("final"):
            console.print(f"[dim]{dec['final']}[/dim]")
        console.print(f"  accept with: garden accept {t.id}   ·   reject with: garden reject {t.id} \"...\"")
    elif t.status == Status.WAITING_HUMAN and st.get("question"):
        console.print(f"[bold deep_pink3]question:[/bold deep_pink3] {st['question']}\n  answer with: garden answer {t.id} \"...\"")
    if st.get("needs_human") or t.status == Status.FAILED:
        from ..inbox import attention_view

        view = attention_view(t, st, RunStore(store.config.garden_dir))
        if view:
            console.print(f"[bold red]needs a decision — {view['kind_title'].lower()}:[/bold red] {view['reason']}")
            for line in view["evidence"]:
                console.print(f"  [dim]{line}[/dim]")
            for a in view["actions"]:
                if a.get("command"):
                    console.print(f"  [cyan]{a['command']}[/cyan]  [dim]{a['detail']}[/dim]")
    u = RunStore(store.config.garden_dir).usage_for(t.id)
    if u["runs"]:
        console.print(f"usage: {u['runs']} run(s), in {u['input_tokens']:,} / out {u['output_tokens']:,} / cache-read {u['cache_read_input_tokens']:,} tokens, ${u['cost_usd']:.2f}")
    console.print(Markdown(t.body))
    runs = RunStore(store.config.garden_dir).runs_for(t.id)
    if runs:
        table = Table(title="runs")
        for c in ("run", "mode", "status", "runner", "min", "brief tok", "cost", "error"):
            table.add_column(c)
        for r in runs:
            table.add_row(r.run_id, r.mode, r.status, r.runner, f"{r.elapsed_minutes():.0f}", str(r.brief_tokens),
                          f"${r.cost_usd:.2f}" if r.cost_usd is not None else "", (r.error or "")[:60])
        console.print(table)


def render_now1(window: str) -> None:
    """Render Now 1 for the shared ``garden now`` command."""
    store = _store()
    from ..now1 import render_text, snapshot

    print(render_text(snapshot(store, _scheduler(store), window=window)), end="")


@app.command(rich_help_panel=PANEL_BOARD)
def ready():
    """Tasks that could be dispatched right now."""
    from ..graph import ready as _ready

    store = _store()
    for t in _ready(store.tasks()):
        console.print(f"{t.id}  pri={priority_label(t.priority)}  {t.title}")


@app.command(rich_help_panel=PANEL_BOARD)
@app.command("graph", hidden=True, rich_help_panel=PANEL_BOARD)
def trellis(
    fmt: str = typer.Option("text", "--format", "-f", help="text|mermaid|json"),
    product: str | None = typer.Option(None, "--product", "-p"),
    phase: str | None = typer.Option(None, "--phase"),
    open_only: bool = typer.Option(False, "--open", help="hide done and cancelled tasks"),
):
    """The trellis: the dependency and stacking structure the work grows along (text, mermaid, or json)."""
    from ..graph import critical_path, effective_status, mermaid, topological_order, visible_ids

    store = _store()
    tasks = {k: v for k, v in store.tasks().items()
             if (not product or v.product == product) and (not phase or v.phase == phase)}
    stack = bool(store.config.get("stack", True))
    vis = visible_ids(tasks, stack, open_only)
    if fmt == "mermaid":
        print(mermaid(tasks, visible=vis))
        return
    if fmt == "json":
        print(json.dumps({"nodes": [{"id": t.id, "title": t.title, "status": effective_status(t, tasks)} for t in tasks.values() if t.id in vis],
                          "edges": [{"from": d, "to": t.id} for t in tasks.values() if t.id in vis for d in t.depends_on if d in vis]}, indent=2))
        return
    for tid in topological_order(tasks):
        if tid not in vis:
            continue
        t = tasks[tid]
        arrows = f"  <- {', '.join(t.depends_on)}" if t.depends_on else ""
        if t.discovered_from:
            arrows += f"  (discovered by {t.discovered_from})"
        console.print(f"{tid:<10} {_style(effective_status(t, tasks, stack)):<22} {t.title}{arrows}")
    cp = critical_path(tasks)
    if cp:
        console.print(f"\ncritical path: {' -> '.join(cp)}")


@app.command(rich_help_panel=PANEL_BOARD)
def validate():
    """Check the graph and reading lists for problems."""
    from ..graph import validate as _validate

    store = _store()
    problems = _validate(store.tasks())
    for tid, paths in sorted(store.duplicate_ids().items()):
        problems.append(f"duplicate task id {tid}: claimed by {', '.join(paths)} "
                        "(both are quarantined from dispatch until one is renamed or removed)")
    from ..brief import resolve_reading

    for t in store.tasks().values():
        for r in t.reading:
            if resolve_reading(store, t, r)[0] is None:
                problems.append(f"{t.id}: reading path {r!r} does not exist in the garden or the product checkout")
    for p in problems:
        console.print(f"[red]![/red] {p}")
    if problems:
        raise typer.Exit(1) from None
    console.print("[green]ok[/green]")


@app.command(rich_help_panel=PANEL_BOARD)
def brief(
    task_id: str,
    revise: bool = typer.Option(False, help="Include pending review feedback"),
    stats: bool = typer.Option(False, help="Print size stats instead of the brief"),
    no_rules: bool = typer.Option(False, help="Omit the operating rules (for reading)"),
):
    """Print the exact brief a worker would receive."""
    from ..brief import build_brief
    from ..scheduler import State

    store = _store()
    t = _task(store, task_id)
    fb = ""
    if revise:
        fb = str(State(store.config.garden_dir / "state.json").get(t.id).get("pending_feedback") or "")
    b = build_brief(store, t, review_feedback=fb, include_rules=not no_rules)
    if stats:
        console.print(f"~{b.tokens:,} tokens ({b.chars:,} chars)")
        for name, n in b.sections.items():
            console.print(f"  {name:<12} {n:>7} chars")
        if b.inlined:
            console.print("inlined: " + ", ".join(b.inlined))
        if b.referenced:
            console.print("[yellow]referenced (too big to inline): " + ", ".join(b.referenced))
        if b.missing:
            console.print("[red]missing: " + ", ".join(b.missing))
        return
    print(b.text)
