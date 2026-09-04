"""`garden` command line interface."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .model import STATUS_ORDER, Status, now_iso

app = typer.Typer(help="Tend a context garden: plan tasks, dispatch agents, track PRs.", no_args_is_help=True, pretty_exceptions_enable=False)
console = Console()
err = Console(stderr=True)


def _store(root: Path | None = None):
    from .store import Store

    try:
        s = Store(root)
        s.tasks()  # fail fast on broken task files
        return s
    except (FileNotFoundError, ValueError) as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None


def _scheduler(store):
    from .scheduler import Scheduler

    return Scheduler(store, log=lambda m: err.print(f"[dim]{m}[/dim]"))


def _task(store, task_id: str):
    try:
        return store.task(task_id)
    except KeyError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


STATUS_STYLE = {
    "draft": "dim",
    "blocked": "yellow",
    "ready": "cyan",
    "running": "blue",
    "awaiting_triage": "purple",
    "in_review": "magenta",
    "changes_requested": "dark_orange",
    "waiting_human": "deep_pink3",
    "done": "green",
    "failed": "red",
    "cancelled": "dim strike",
}


def _style(status: str, text: str | None = None) -> str:
    style = STATUS_STYLE.get(status) or "default"
    return f"[{style}]{text if text is not None else status}[/{style}]"


# --------------------------------------------------------------------------- init / scaffold
@app.command()
def init(
    directory: Path = typer.Argument(Path("."), help="Directory to turn into a garden"),
    name: str = typer.Option("garden", help="Garden name"),
):
    """Create garden.yaml and a principles digest in DIRECTORY."""
    from .personas import write_default_personas
    from .scaffold import init_garden

    created = init_garden(directory.resolve(), name) + write_default_personas(directory.resolve())
    for p in created:
        console.print(f"created {p}")
    console.print("Next: `garden new-product <name>` then `garden new-phase <product> <phase>`.")


@app.command("new-product")
def new_product(name: str, repo: str = typer.Option(".", help="Path (relative to garden) or URL of the code repo"),
                base_branch: str = typer.Option("main")):
    """Scaffold <name>/product.md and register it in garden.yaml."""
    from .scaffold import new_product as _np

    store = _store()
    for p in _np(store, name, repo, base_branch):
        console.print(f"created {p}")


@app.command("new-phase")
def new_phase(product: str, phase: str):
    """Scaffold <product>/<phase>/{goals.md,specs/,tasks/}."""
    from .scaffold import new_phase as _nph

    store = _store()
    for p in _nph(store, product, phase):
        console.print(f"created {p}")


@app.command("new-task")
def new_task(
    target: str = typer.Argument(..., help="product/phase"),
    title: str = typer.Argument(...),
    depends_on: list[str] = typer.Option([], "--dep", "-d"),
    reading: list[str] = typer.Option([], "--read", "-r"),
    priority: int = typer.Option(3),
    difficulty: str = typer.Option("medium", help="easy|medium|hard (picks the model tier)"),
    ready: bool = typer.Option(False, help="Create as ready instead of draft"),
):
    """Create a task file from a template."""
    from .scaffold import TASK_TEMPLATE

    store = _store()
    product, phase = _split_target(target)
    t = store.create_task(product, phase, title, TASK_TEMPLATE, depends_on=depends_on, reading=reading,
                          priority=priority, status="ready" if ready else "draft", difficulty=difficulty)
    console.print(f"created {t.id} at {store.rel(t.path)}")


def _split_target(target: str) -> tuple[str, str]:
    if "/" not in target:
        err.print("[red]expected product/phase[/red]")
        raise typer.Exit(1) from None
    product, phase = target.split("/", 1)
    return product, phase.strip("/")


# --------------------------------------------------------------------------- read-only views
@app.command()
def status(product: str | None = typer.Option(None, "--product", "-p")):
    """Overview per phase, plus cost totals."""
    from .graph import effective_status
    from .runs import RunStore

    store = _store()
    tasks = store.tasks()
    table = Table(title=f"garden: {store.config.get('name')}  ({store.root})", show_lines=False)
    table.add_column("product/phase")
    cols = ["draft", "blocked", "ready", "running", "waiting_human", "awaiting_triage", "in_review", "changes_requested", "done", "failed"]
    short = {"blocked": "blkd", "running": "run", "waiting_human": "wait", "awaiting_triage": "triage", "in_review": "review", "changes_requested": "chg", "failed": "fail"}
    for s in cols:
        table.add_column(short.get(s, s), justify="right")
    table.add_column("spent", justify="right")
    sched = _scheduler(store)
    stack = bool(store.config.get("stack", True))
    for prod in store.products():
        if product and prod.name != product:
            continue
        for ph in prod.phases:
            counts = {s: 0 for s in STATUS_ORDER + ["blocked"]}
            for t in ph.tasks:
                counts[effective_status(t, tasks, stack)] += 1
            budget = sched.budget_for(ph.key)
            spent = sched.spent_for(ph.key)
            money = f"${spent:.2f}" + (f" / ${budget:.2f}" if budget else "")
            if budget and spent >= budget:
                money = f"[red]{money}[/red]"
            table.add_row(ph.key, *[_count(counts[s], s) for s in cols], money)
    console.print(table)
    tot = RunStore(store.config.garden_dir).totals()
    console.print(f"runs: {tot['runs']}  cost: ${tot['cost_usd']:.2f}  in: {tot['input_tokens']:,}  out: {tot['output_tokens']:,}  cache-read: {tot['cache_read_input_tokens']:,}")


def _count(n: int, s: str) -> str:
    return _style(s, str(n)) if n else "[dim]·[/dim]"


@app.command("ls")
def ls(
    product: str | None = typer.Option(None, "--product", "-p"),
    phase: str | None = typer.Option(None, "--phase"),
    status_: str | None = typer.Option(None, "--status", "-s", help="draft|blocked|ready|running|waiting_human|in_review|changes_requested|done|failed|cancelled"),
    discovered: bool = typer.Option(False, help="Only tasks that workers discovered"),
    json_out: bool = typer.Option(False, "--json"),
):
    """List tasks."""
    from .graph import blockers, effective_status

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
        table.add_row(t.id, _style(eff), str(t.priority), t.difficulty, title, t.key, deps, t.pr or "")
    console.print(table)


@app.command()
def show(task_id: str, raw: bool = typer.Option(False, help="Print the file verbatim")):
    """Show a task, its blockers and its runs."""
    from rich.markdown import Markdown

    from .graph import blockers, dependents
    from .runs import RunStore

    store = _store()
    t = _task(store, task_id)
    if raw:
        print(t.render())
        return
    tasks = store.tasks()
    console.print(f"[bold]{t.id}[/bold] {t.title}  {_style(t.status.value)}  pri={t.priority}  difficulty={t.difficulty}  {t.key}")
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
    from .scheduler import State

    st = State(store.config.garden_dir / "state.json").get(t.id)
    if st.get("stack_parent"):
        console.print(f"stacked on: {st['stack_parent']} (PR targets {st.get('pr_base')})")
    if t.status == Status.WAITING_HUMAN and st.get("question"):
        console.print(f"[bold deep_pink3]question:[/bold deep_pink3] {st['question']}\n  answer with: garden answer {t.id} \"...\"")
    if st.get("needs_human"):
        console.print(f"[bold red]needs a human:[/bold red] {st['needs_human']}  (garden retry {t.id} to resume)")
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


@app.command()
def ready():
    """Tasks that could be dispatched right now."""
    from .graph import ready as _ready

    store = _store()
    for t in _ready(store.tasks()):
        console.print(f"{t.id}  pri={t.priority}  {t.title}")


@app.command()
@app.command("graph", hidden=True)
def trellis(
    fmt: str = typer.Option("text", "--format", "-f", help="text|mermaid|json"),
    product: str | None = typer.Option(None, "--product", "-p"),
    phase: str | None = typer.Option(None, "--phase"),
):
    """The trellis: the dependency and stacking structure the work grows along (text, mermaid, or json)."""
    from .graph import critical_path, effective_status, mermaid, topological_order

    store = _store()
    tasks = {k: v for k, v in store.tasks().items()
             if (not product or v.product == product) and (not phase or v.phase == phase)}
    if fmt == "mermaid":
        print(mermaid(tasks))
        return
    if fmt == "json":
        print(json.dumps({"nodes": [{"id": t.id, "title": t.title, "status": effective_status(t, tasks)} for t in tasks.values()],
                          "edges": [{"from": d, "to": t.id} for t in tasks.values() for d in t.depends_on]}, indent=2))
        return
    stack = bool(store.config.get("stack", True))
    for tid in topological_order(tasks):
        t = tasks[tid]
        arrows = f"  <- {', '.join(t.depends_on)}" if t.depends_on else ""
        if t.discovered_from:
            arrows += f"  (discovered by {t.discovered_from})"
        console.print(f"{tid:<10} {_style(effective_status(t, tasks, stack)):<22} {t.title}{arrows}")
    cp = critical_path(tasks)
    if cp:
        console.print(f"\ncritical path: {' -> '.join(cp)}")


@app.command()
def validate():
    """Check the graph and reading lists for problems."""
    from .graph import validate as _validate

    store = _store()
    problems = _validate(store.tasks())
    for t in store.tasks().values():
        for r in t.reading:
            if not (store.root / r).exists():
                problems.append(f"{t.id}: reading path {r!r} does not exist")
    for p in problems:
        console.print(f"[red]![/red] {p}")
    if problems:
        raise typer.Exit(1) from None
    console.print("[green]ok[/green]")


@app.command()
def brief(
    task_id: str,
    revise: bool = typer.Option(False, help="Include pending review feedback"),
    stats: bool = typer.Option(False, help="Print size stats instead of the brief"),
    no_rules: bool = typer.Option(False, help="Omit the operating rules (for reading)"),
):
    """Print the exact brief a worker would receive."""
    from .brief import build_brief
    from .scheduler import State

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


# --------------------------------------------------------------------------- state changes
@app.command()
def approve(
    task_ids: list[str] = typer.Argument(None),
    all_in: str | None = typer.Option(None, "--all", help="Approve every draft in product/phase"),
):
    """draft -> ready."""
    store = _store()
    targets = []
    if all_in:
        product, phase = _split_target(all_in)
        targets = [t for t in store.tasks().values() if t.key == f"{product}/{phase}" and t.status == Status.DRAFT]
    for tid in task_ids or []:
        targets.append(_task(store, tid))
    if not targets:
        err.print("nothing to approve")
        raise typer.Exit(1) from None
    for t in targets:
        if t.status != Status.DRAFT:
            err.print(f"[yellow]{t.id} is {t.status.value}, skipping[/yellow]")
            continue
        t.status = Status.READY
        t.log("approved")
        store.save(t)
        console.print(f"{t.id} -> ready")


@app.command("set-status")
def set_status(task_id: str, new_status: str, note: str = typer.Option("", help="Log note")):
    """Escape hatch: force a task's status."""
    store = _store()
    t = _task(store, task_id)
    try:
        s = Status(new_status)
    except ValueError:
        err.print(f"[red]unknown status; one of {', '.join(STATUS_ORDER)}[/red]")
        raise typer.Exit(1) from None
    t.status = s
    t.log(note or f"status forced to {s.value}")
    store.save(t)
    console.print(f"{t.id} -> {s.value}")


@app.command()
def cancel(task_id: str, note: str = typer.Option("cancelled by hand")):
    """Cancel a task (kills a running worker)."""
    store = _store()
    _scheduler(store).cancel(_task(store, task_id), note)


@app.command()
def retry(task_id: str):
    """Reset attempts and mark ready."""
    store = _store()
    _scheduler(store).retry(_task(store, task_id))


@app.command()
def pr(task_id: str, url: str):
    """Attach a PR URL to a task (when the PR was opened by hand)."""
    store = _store()
    t = _task(store, task_id)
    t.pr = url
    if t.status in (Status.RUNNING, Status.READY, Status.DRAFT, Status.FAILED):
        t.status = Status.IN_REVIEW
    t.log(f"PR attached: {url}")
    store.save(t)
    console.print(f"{t.id}: pr={url} status={t.status.value}")


# --------------------------------------------------------------------------- the loop
@app.command()
def tick(no_dispatch: bool = typer.Option(False, help="Only reap and poll; don't start workers")):
    """One scheduler pass: reap finished workers, poll PRs, dispatch ready tasks."""
    store = _store()
    rep = _scheduler(store).tick(dispatch=not no_dispatch)
    console.print(rep.summary())
    if rep.errors:
        raise typer.Exit(1) from None


@app.command()
def watch(interval: int = typer.Option(0, help="Seconds between ticks (default: garden.yaml tick_interval)")):
    """Loop `tick` forever. Sleeping costs nothing; only workers spend tokens."""
    store = _store()
    interval = interval or int(store.config.get("tick_interval", 60))
    sched = _scheduler(store)
    console.print(f"watching {store.root} every {interval}s (ctrl-c to stop)")
    try:
        while True:
            rep = sched.tick()
            if rep.changed:
                console.print(f"[dim]{now_iso()}[/dim] {rep.summary()}")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("stopped")


@app.command()
def dispatch(task_id: str, mode: str = typer.Option("work", help="work|revise"), force: bool = typer.Option(False, help="Ignore deps/status")):
    """Start a worker for one task now."""
    from .graph import blockers

    store = _store()
    t = _task(store, task_id)
    if not force:
        if mode == "work" and t.status not in (Status.READY, Status.DRAFT):
            err.print(f"[red]{t.id} is {t.status.value}; use --force[/red]")
            raise typer.Exit(1) from None
        b = blockers(t, store.tasks())
        if b and mode == "work":
            err.print(f"[red]{t.id} is blocked by {', '.join(b)}; use --force[/red]")
            raise typer.Exit(1) from None
    run = _scheduler(store).dispatch(t, mode=mode)
    console.print(f"{t.id}: run {run.run_id} started (worktree {run.worktree})")


@app.command()
def take(
    task_id: str,
    worktree: bool = typer.Option(False, help="Also create the git worktree and print its path"),
    quiet: bool = typer.Option(False, "-q", help="Only print the brief path"),
):
    """Claim a task for a human-driven session and print its brief (manual runner)."""
    from .runner.manual import ManualRunner

    store = _store()
    t = _task(store, task_id)
    if t.status == Status.RUNNING:
        err.print(f"[red]{t.id} is already running[/red]")
        raise typer.Exit(1) from None
    if t.status == Status.DRAFT:
        t.status = Status.READY
    sched = _scheduler(store)
    mode = "revise" if t.status == Status.CHANGES_REQUESTED else "work"
    run = sched.dispatch(t, mode=mode, runner=ManualRunner({}), worktree=worktree)
    brief_path = run.path / "brief.md"
    if quiet:
        print(brief_path)
        return
    console.print(f"[bold]{t.id}[/bold] claimed. brief: {brief_path}")
    if worktree:
        console.print(f"worktree: {run.worktree} (branch {run.branch})")
    else:
        console.print(f"work on branch [bold]{run.branch}[/bold] from {run.base}; when done: garden finish {t.id} --pr <url> --summary '...'")
    print()
    print(brief_path.read_text())


@app.command()
def finish(
    task_id: str,
    pr_url: str = typer.Option("", "--pr", help="PR URL if you opened it yourself"),
    summary: str = typer.Option("", help="One-line summary"),
    result_json: str = typer.Option("", "--result", help="Full GARDEN_RESULT JSON"),
    result_file: Path | None = typer.Option(None, "--result-file"),
    blocked: bool = typer.Option(False, help="Report the task as blocked"),
):
    """Complete a manually-taken task: pushes and opens the PR if the runner made a worktree."""
    store = _store()
    t = _task(store, task_id)
    result: dict = {}
    if result_file:
        result = json.loads(result_file.read_text())
    elif result_json:
        result = json.loads(result_json)
    result.setdefault("status", "blocked" if blocked else "done")
    if summary:
        result["summary"] = summary
    if pr_url:
        result["pr"] = pr_url
        t.pr = pr_url
    rep = _scheduler(store).finish_manual(t, result)
    console.print(rep.summary())


@app.command()
def answer(task_id: str, text: str = typer.Argument(..., help="Your answer to the worker's question")):
    """Answer a waiting_human task; the worker resumes (same session when the harness supports it)."""
    store = _store()
    t = _task(store, task_id)
    try:
        run = _scheduler(store).answer(t, text)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: {'resumed session' if run.session_id else 'fresh run with the answer'} ({run.run_id})")


@app.command()
def digest(since: str = typer.Option("24h", help="Window: 90m, 24h, 3d, or an ISO timestamp")):
    """What happened while you were away: PRs opened and merged, tasks needing you, cost."""
    from .events import EventLog, parse_since
    from .events import digest as _digest

    store = _store()
    since_iso = parse_since(since)
    events = EventLog(store.config.garden_dir / "events.jsonl").read(since=since_iso)
    d = _digest(events)
    console.print(f"[bold]since {since_iso}[/bold]: {len(events)} events, {d['dispatched']} dispatches, ${d['cost_usd']:.2f} spent")
    tasks = store.tasks()

    def title(tid: str) -> str:
        return tasks[tid].title if tid in tasks else ""

    if d["needs_human"]:
        console.print("\n[bold red]Needs you[/bold red]")
        seen = set()
        for ev in d["needs_human"]:
            key = (ev["task"], ev.get("kind"))
            if key in seen:
                continue
            seen.add(key)
            what = ev.get("question") or ev.get("reason") or ev.get("note") or ev.get("to") or ev.get("kind")
            console.print(f"  {ev['task']:<8} {title(ev['task'])[:40]:<40} {what}")
    if d["prs_opened"]:
        console.print("\n[bold magenta]PRs opened[/bold magenta]")
        for ev in d["prs_opened"]:
            console.print(f"  {ev['task']:<8} {title(ev['task'])[:40]:<40} {ev.get('pr', '')}")
    if d["reviews"]:
        console.print("\n[bold]Automated reviews[/bold]")
        for ev in d["reviews"]:
            console.print(f"  {ev['task']:<8} {ev.get('verdict', ''):<16} {ev.get('summary', '')[:70]}")
    if d["merged"]:
        console.print("\n[bold green]Merged[/bold green]")
        for ev in d["merged"]:
            console.print(f"  {ev['task']:<8} {title(ev['task'])}")
    if d["discovered"]:
        console.print("\n[bold cyan]Discovered work[/bold cyan]")
        for ev in d["discovered"]:
            console.print(f"  {ev.get('new_task', ''):<8} {ev.get('title', '')[:50]:<50} by {ev['task']}" + (" [blocking]" if ev.get("blocking") else ""))
    if not any([d["needs_human"], d["prs_opened"], d["reviews"], d["merged"], d["discovered"]]):
        console.print("[dim]nothing notable[/dim]")


@app.command()
def metrics(target: str | None = typer.Argument(None, help="product/phase (default: all)")):
    """Lead time, revise rounds, first-pass approval and cost per task and per difficulty tier."""
    from .events import EventLog
    from .events import metrics as _metrics

    store = _store()
    tasks = store.tasks()
    if target:
        product, phase = _split_target(target)
        tasks = {k: v for k, v in tasks.items() if v.product == product and v.phase == phase}
    events = EventLog(store.config.garden_dir / "events.jsonl").read()
    m = _metrics(events, tasks)
    table = Table(title="per task")
    for c in ("id", "difficulty", "status", "runs", "revisions", "first review", "cost", "lead h"):
        table.add_column(c)
    for r in m["tasks"]:
        table.add_row(r["id"], r["difficulty"], _style(str(r["status"].value if hasattr(r["status"], "value") else r["status"])),
                      str(r["runs"]), str(r["revisions"]), r["first_review"], f"${r['cost_usd']:.2f}",
                      f"{r['lead_hours']:.1f}" if r["lead_hours"] is not None else "")
    console.print(table)
    table = Table(title="per difficulty tier (is 'easy' really easy?)")
    for c in ("tier", "tasks", "done", "avg revisions", "first-pass approve", "cost", "avg lead h"):
        table.add_column(c)
    for tier in ("easy", "medium", "hard"):
        d = m["by_difficulty"].get(tier)
        if not d:
            continue
        table.add_row(tier, str(d["tasks"]), str(d["done"]), str(d["avg_revisions"]),
                      f"{d['first_pass_rate']:.0%}" if d["first_pass_rate"] is not None else "",
                      f"${d['cost_usd']:.2f}", f"{d['avg_lead_hours']:.1f}" if d["avg_lead_hours"] is not None else "")
    console.print(table)


@app.command()
def events(task_id: str | None = typer.Argument(None), since: str = typer.Option("", help="90m, 24h, 3d or ISO"), limit: int = typer.Option(50, "-n")):
    """Timeline of scheduler events (all, or one task)."""
    from .events import EventLog, parse_since

    store = _store()
    evs = EventLog(store.config.garden_dir / "events.jsonl").read(since=parse_since(since) if since else "", task_id=task_id or "")
    for ev in evs[-limit:]:
        extra = {k: v for k, v in ev.items() if k not in ("at", "kind", "task", "usage")}
        console.print(f"[dim]{ev['at'][5:19]}[/dim] {ev['task']:<8} [bold]{ev['kind']:<14}[/bold] " + " ".join(f"{k}={v}" for k, v in extra.items() if v not in ("", None, False, 0)))


@app.command()
def trial(
    task_id: str,
    contenders: list[str] = typer.Option(..., "--contender", "-c", help="harness:model, e.g. claude:opus, codex:gpt-5 (repeat)"),
):
    """Run a task with several models; a comparison run scores the PRs and keeps the best one."""
    store = _store()
    t = _task(store, task_id)
    try:
        runs = _scheduler(store).start_trial(t, contenders)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    for r in runs:
        console.print(f"{t.id}: {r.harness}:{r.model or 'default'} -> run {r.run_id} on {r.branch}")


@app.command()
def trials(task_id: str | None = typer.Argument(None, help="Show one task's trial instead of the leaderboard")):
    """Model leaderboard from every trial: wins, average score and cost per contender."""
    from .trials import TrialLog, ranking_markdown

    store = _store()
    log = TrialLog(store.config.garden_dir / "trials.jsonl")
    if task_id:
        for tr in log.read():
            if tr.get("task") == task_id:
                console.print(ranking_markdown(tr))
        return
    rows = log.leaderboard()
    if not rows:
        console.print("no trials yet (garden trial ID -c claude:sonnet -c claude:opus)")
        return
    table = Table(title="model trials")
    for c in ("contender", "trials", "wins", "win rate", "avg score", "avg cost", "$ / point", "avg tokens in / out", "failed"):
        table.add_column(c)
    for r in rows:
        table.add_row(r["label"], str(r["trials"]), str(r["wins"]), f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "",
                      f"{r['avg_score']:.1f}" if r["avg_score"] is not None else "", f"${r['avg_cost']:.2f}" if r["avg_cost"] is not None else "",
                      f"${r['cost_per_point']:.3f}" if r["cost_per_point"] is not None else "",
                      f"{r['avg_input_tokens']:,} / {r['avg_output_tokens']:,}", str(r["failed"]))
    console.print(table)


@app.command("persona-review")
def persona_review(
    target: str = typer.Argument(..., help="A task id (reviews its PR) or product/phase (reviews the body of work)"),
    personas: list[str] = typer.Option(..., "--persona", "-p", help="Persona name (repeat); see `garden personas`"),
    file_tasks: bool = typer.Option(False, help="Phase reviews: turn high-severity findings into draft tasks"),
    request_changes: bool = typer.Option(False, help="PR reviews: high findings trigger a revise run"),
):
    """Persona reviews (designer, project-manager, staff-engineer, usability-expert, user, security, or your own)."""
    store = _store()
    sched = _scheduler(store)
    if "/" in target:
        product, phase = _split_target(target)
        try:
            ph = store.phase(product, phase)
        except KeyError as e:
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from None
        for name in personas:
            run = sched.dispatch_persona_phase(ph, name, file_tasks=file_tasks)
            console.print(f"{ph.key}: persona {name} -> run {run.run_id} (report lands in {ph.name}/docs/reviews/)")
        return
    t = _task(store, target)
    for name in personas:
        run = sched.dispatch_persona_pr(t, name, request_changes=request_changes)
        console.print(f"{t.id}: persona {name} -> run {run.run_id} (comment on {t.pr or 'the PR'})")


@app.command()
def personas():
    """List available personas (personas/*.md, plus the built-in defaults)."""
    from .personas import DEFAULT_PERSONAS, list_personas

    store = _store()
    have = list_personas(store)
    for name in sorted(set(have) | set(DEFAULT_PERSONAS)):
        where = "personas/" + name + ".md" if name in have else "built-in default (garden init writes it)"
        console.print(f"{name:<18} {where}")


@app.command()
def check(task_id: str, stage: str = typer.Option("pre_pr", help="pre_pr | ci")):
    """Run the token-free checks for a task by hand (pre_pr in its worktree, or ci analysers)."""
    from .checks import run_checks

    store = _store()
    t = _task(store, task_id)
    sched = _scheduler(store)
    specs = list(store.config.get(f"checks.{stage}", []) or [])
    if not specs:
        err.print(f"no checks configured under checks.{stage}")
        raise typer.Exit(1)
    wt = sched.worktree_for(t)
    results = run_checks(specs, sched.check_ctx(t, t.branch or t.default_branch(), sched.base_for(t), wt),
                         cwd=wt if wt.exists() else None, timeout=int(store.config.get("checks.timeout_seconds", 600)))
    bad = 0
    for r in results:
        color = "green" if r.get("status") == "pass" else ("yellow" if r.get("status") == "flaky" else "red")
        console.print(f"[{color}]{r.get('status'):<6}[/{color}] {r.get('name')}: {r.get('summary', '')}")
        if r.get("details") and r.get("status") != "pass":
            print(r["details"])
        bad += r.get("status") in ("fail", "error")
    raise typer.Exit(1 if bad else 0)


@app.command()
def triage(
    task_id: str,
    ready: bool = typer.Option(False, help="The draft PR is good enough for review: mark it ready"),
    changes: str = typer.Option("", help="Send it back with this feedback (a revise run follows)"),
    note: str = typer.Option("", help="Optional note for the log"),
):
    """Your first look at a draft PR: mark it ready for review, or send it back."""
    store = _store()
    t = _task(store, task_id)
    try:
        _scheduler(store).triage(t, ready=ready, changes=changes, note=note)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: {'ready for review' if ready else 'sent back for changes'}")


@app.command()
def inbox():
    """Everything that needs a human, with the command that resolves it."""
    from .inbox import build_inbox

    store = _store()
    items = build_inbox(store, _scheduler(store))
    if not items:
        console.print("[green]inbox zero[/green] — nothing needs you")
        return
    current = ""
    for it in items:
        if it["group"] != current:
            current = it["group"]
            console.print(f"\n[bold]{it['group_title']}[/bold]")
        console.print(f"  {it['task']:<8} {it['title'][:44]:<44} [dim]{it['why'][:60]}[/dim]")
        for a in it["actions"]:
            if a.get("command"):
                console.print(f"           [cyan]{a['command']}[/cyan]")


# --------------------------------------------------------------------------- planning
@app.command()
def plan(
    target: str = typer.Argument(..., help="product/phase"),
    dry_run: bool = typer.Option(False, help="Print the planning prompt and exit"),
    import_file: Path | None = typer.Option(None, "--import", help="Import a JSON task list instead of calling the model"),
    guidance: str = typer.Option("", help="Extra instructions for the planner"),
    draft: bool = typer.Option(False, help="Create tasks as draft (default follows plan.auto_approve)"),
    approve_all: bool = typer.Option(False, "--approve", help="Create tasks as ready"),
):
    """Turn goals + specs into task files (one model call, or --import). Ready by default."""
    from .planner import import_plan, parse_plan, plan_prompt, prompt_tokens, run_planner

    store = _store()
    product, phase = _split_target(target)
    try:
        store.phase(product, phase)
    except KeyError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    if import_file:
        items = parse_plan(import_file.read_text())
    else:
        prompt = plan_prompt(store, product, phase, extra=guidance)
        if dry_run:
            print(prompt)
            err.print(f"[dim]~{prompt_tokens(prompt):,} tokens[/dim]")
            return
        err.print(f"[dim]planning {target} (~{prompt_tokens(prompt):,} prompt tokens)...[/dim]")
        raw = run_planner(store, prompt)
        (store.config.garden_dir / "plans").mkdir(parents=True, exist_ok=True)
        out = store.config.garden_dir / "plans" / f"{product}-{phase}-{now_iso().replace(':', '')}.json"
        out.write_text(raw)
        try:
            items = parse_plan(raw)
        except ValueError as e:
            err.print(f"[red]{e}; raw output saved to {out}[/red]")
            raise typer.Exit(1) from None
    status = "draft" if draft else ("ready" if approve_all else None)
    created = import_plan(store, product, phase, items, status=status)
    for t in created:
        console.print(f"created {t.id} {_style(t.status.value)} {t.title}" + (f"  <- {', '.join(t.depends_on)}" if t.depends_on else ""))
    if not created:
        console.print("no new tasks (all titles already existed)")


@app.command()
def prs(target: str | None = typer.Argument(None, help="product/phase (default: all)")):
    """Every tracked PR: state, review decision, CI, revisions, last poll."""
    from .scheduler import State

    store = _store()
    st = State(store.config.garden_dir / "state.json")
    product = phase = None
    if target:
        product, phase = _split_target(target)
    table = Table()
    for c in ("id", "status", "pr", "review", "ci", "rev", "auto-review", "polled", "title"):
        table.add_column(c)
    for t in sorted(store.tasks().values(), key=lambda t: (t.product, t.phase, t.id)):
        if not t.pr or (product and t.product != product) or (phase and t.phase != phase):
            continue
        s_ = st.get(t.id)
        ci = s_.get("checks") or ""
        if s_.get("failed_checks"):
            ci += " (" + ", ".join(s_["failed_checks"]) + ")"
        last = s_.get("last_review") or {}
        table.add_row(t.id, _style(t.status.value), t.pr, s_.get("review_decision") or "", ci, str(s_.get("revisions", 0)),
                      f"{last.get('verdict', '')} ({s_.get('review_rounds', 0)})" if s_.get("review_rounds") else "",
                      (s_.get("last_polled") or "")[11:19], t.title[:40])
    console.print(table)


@app.command()
def usage(
    target: str | None = typer.Argument(None, help="task id, product/phase, or nothing for everything"),
    by_mode: bool = typer.Option(False, help="Split each task's usage by run mode (work/revise/review/…)"),
):
    """Tokens and cost per task, rolled up from every run."""
    from .runs import RunStore

    store = _store()
    rs = RunStore(store.config.garden_dir)
    tasks = store.tasks()
    product = phase = None
    if target and "/" in target:
        product, phase = _split_target(target)
    elif target:
        t = _task(store, target)
        u = rs.usage_for(t.id)
        console.print(f"[bold]{t.id}[/bold] {t.title}  runs={u['runs']}  in={u['input_tokens']:,}  out={u['output_tokens']:,}  cache-read={u['cache_read_input_tokens']:,}  cost=${u['cost_usd']:.2f}  minutes={u['minutes']}")
        table = Table()
        for c in ("mode", "runs", "in", "out", "cost"):
            table.add_column(c, justify="right" if c != "mode" else "left")
        for mode, m in sorted(u["by_mode"].items()):
            table.add_row(mode, str(m["runs"]), f"{m['input_tokens']:,}", f"{m['output_tokens']:,}", f"${m['cost_usd']:.2f}")
        console.print(table)
        return
    per = rs.usage_by_task()
    table = Table(title="usage per task")
    for c in ("task", "tier", "status", "runs", "in", "out", "cache-read", "cost", "$/run"):
        table.add_column(c, justify="right" if c not in ("task", "tier", "status") else "left")
    tot = {"runs": 0, "in": 0, "out": 0, "cache": 0, "cost": 0.0}
    for tid, u in sorted(per.items()):
        t = tasks.get(tid)
        if product and (not t or t.product != product or t.phase != phase):
            continue
        table.add_row(tid, t.difficulty if t else "", _style(t.status.value) if t else "", str(u["runs"]), f"{u['input_tokens']:,}",
                      f"{u['output_tokens']:,}", f"{u['cache_read_input_tokens']:,}", f"${u['cost_usd']:.2f}",
                      f"${u['cost_usd'] / u['runs']:.2f}" if u["runs"] else "")
        if by_mode:
            for mode, m in sorted(u["by_mode"].items()):
                table.add_row(f"  {mode}", "", "", str(m["runs"]), f"{m['input_tokens']:,}", f"{m['output_tokens']:,}", "", f"${m['cost_usd']:.2f}", "")
        tot["runs"] += u["runs"]
        tot["in"] += u["input_tokens"]
        tot["out"] += u["output_tokens"]
        tot["cache"] += u["cache_read_input_tokens"]
        tot["cost"] += u["cost_usd"]
    table.add_row("[bold]total[/bold]", "", "", str(tot["runs"]), f"{tot['in']:,}", f"{tot['out']:,}", f"{tot['cache']:,}", f"${tot['cost']:.2f}", "")
    console.print(table)


@app.command()
def review(task_id: str):
    """Start an automated review run for a task's open PR now."""
    store = _store()
    t = _task(store, task_id)
    if not t.pr:
        err.print(f"[red]{t.id} has no PR[/red]")
        raise typer.Exit(1)
    run = _scheduler(store).dispatch_review(t)
    console.print(f"{t.id}: review run {run.run_id} started (model {run.model or 'default'})")


# --------------------------------------------------------------------------- runs / diagnostics
@app.command()
def runs(task_id: str | None = typer.Argument(None)):
    """List runs (all, or for one task)."""
    from .runs import RunStore

    store = _store()
    rs = RunStore(store.config.garden_dir)
    table = Table()
    for c in ("task", "run", "mode", "status", "runner", "min", "brief tok", "in", "out", "cost"):
        table.add_column(c)
    for r in (rs.runs_for(task_id) if task_id else rs.all_runs()):
        table.add_row(r.task_id, r.run_id, r.mode, r.status, r.runner, f"{r.elapsed_minutes():.0f}", str(r.brief_tokens),
                      str(r.usage.get("input_tokens", "")), str(r.usage.get("output_tokens", "")),
                      f"${r.cost_usd:.2f}" if r.cost_usd is not None else "")
    console.print(table)


@app.command("log")
def log_(task_id: str, lines: int = typer.Option(60, "-n")):
    """Tail the latest run's output for a task."""
    from .runs import RunStore

    store = _store()
    r = RunStore(store.config.garden_dir).latest(task_id)
    if not r:
        err.print("no runs")
        raise typer.Exit(1) from None
    console.print(f"[bold]{r.run_id}[/bold] status={r.status} dir={r.dir}")
    final = r.path / "final.md"
    if final.exists():
        console.print("[bold]final message:[/bold]")
        print("\n".join(final.read_text().splitlines()[-lines:]))
    stderr = r.stderr_text()
    if stderr.strip():
        console.print("[bold]stderr:[/bold]")
        print("\n".join(stderr.splitlines()[-lines:]))
    if r.error:
        console.print(f"[red]error:[/red] {r.error}")


@app.command()
def doctor():
    """Check config, tools (claude, gh/token), repos and the task graph."""
    from .github import GitHub
    from .graph import validate as _validate
    from .runner import get_runner

    store = _store()
    ok = True
    console.print(f"root: {store.root}")
    console.print(f"config: {' < '.join(store.config.sources) or 'defaults only'}" + (f"  (GARDEN_ENV={store.config.env})" if store.config.env else "  (set GARDEN_ENV=work to add garden.work.yaml)"))
    gh = GitHub(use_gh=bool(store.config.get("github.use_gh", True)))
    console.print(f"github: {gh.describe()}" + (f" as {gh.me()}" if gh.available and gh.me() else ""))
    if not gh.available:
        ok = False
    harness_names = {str(store.config.get("harness") or "claude")} | {
        str(p.get("harness")) for p in store.config.data.get("products", {}).values() if p and p.get("harness")}
    runner_names = {str(store.config.get("runner") or "local")} | {
        str(p.get("runner")) for p in store.config.data.get("products", {}).values() if p and p.get("runner")}
    for hn in sorted(harness_names):
        h = store.config.harness(hn)
        found = shutil.which(h.bin)
        console.print(f"harness {hn}: " + (f"[green]{found}[/green]" if found else f"[red]{h.bin!r} not on PATH[/red]")
                      + f"  models={h.cfg.get('models') or 'cli default'}")
        ok = ok and bool(found)
    for name in sorted(runner_names):
        try:
            cfg = dict(store.config.get("ssh", {}) or {}) if name == "ssh" else {}
            r = get_runner(name, cfg, store.config.harness(str(store.config.get("harness") or "claude")))
            probs = r.doctor()
        except Exception as e:  # noqa: BLE001
            probs = [str(e)]
        console.print(f"runner {name}: " + ("[green]ok[/green]" if not probs else "[red]" + "; ".join(probs) + "[/red]"))
        ok = ok and not probs
    console.print(f"review pass: {'on' if store.config.get('review.enabled') else 'off'} (max {store.config.get('review.max_rounds')} rounds)  max_parallel={store.config.get('max_parallel')}")
    for p in store.products():
        repo = store.config.product_repo(p.name)
        console.print(f"product {p.name}: repo={repo} phases={len(p.phases)} tasks={sum(len(ph.tasks) for ph in p.phases)}")
        if isinstance(repo, Path) and not (repo / ".git").exists():
            console.print(f"  [red]{repo} is not a git repo[/red]")
            ok = False
    problems = _validate(store.tasks())
    for pr_ in problems:
        console.print(f"[red]graph: {pr_}[/red]")
    ok = ok and not problems
    console.print("[green]all good[/green]" if ok else "[yellow]see above[/yellow]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8765),
    watch_: bool = typer.Option(True, "--watch/--no-watch", help="Run the scheduler loop inside the server"),
):
    """Local web UI (and, by default, the scheduler loop)."""
    import uvicorn

    from .web.app import create_app

    store = _store()
    uvicorn.run(create_app(store, watch=watch_), host=host, port=port, log_level="warning")


@app.command()
def tui():
    """Terminal UI."""
    from .tui.app import GardenTUI

    GardenTUI(_store()).run()


@app.command()
def version():
    print(__version__)


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
