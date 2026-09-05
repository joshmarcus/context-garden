"""`garden` planning and reporting: plan, prs, qa, friction-report, friction, retro, usage, review."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ..model import now_iso
from .common import _phase, _scheduler, _split_target, _store, _style, _task, app, console, err


# --------------------------------------------------------------------------- planning
@app.command()
def plan(
    target: str = typer.Argument(..., help="product/phase"),
    dry_run: bool = typer.Option(False, help="Print the planning prompt and exit"),
    import_file: Path | None = typer.Option(None, "--import", help="Import a JSON task list instead of calling the model"),
    guidance: str = typer.Option("", help="Extra instructions for the planner"),
    draft: bool = typer.Option(False, help="Create tasks as draft (default follows plan.auto_approve)"),
    approve_all: bool = typer.Option(False, "--approve", help="Create tasks as ready"),
    replan: bool = typer.Option(False, "--replan", help="Include failed/blocked task logs so the planner can propose fixes or replacements"),
    reopen: bool = typer.Option(False, "--reopen", help="Reopen a closed phase to take these tasks"),
):
    """Turn goals + specs into task files (one model call, or --import). Ready by default."""
    from ..planner import import_plan, parse_plan, plan_prompt, prompt_tokens, run_planner

    store = _store()
    product, phase = _split_target(target)
    try:
        ph = store.phase(product, phase)
    except KeyError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    if ph.closed and not reopen and not dry_run:
        err.print(f"[red]{ph.key} is closed ({ph.closed}); pass --reopen or run `garden reopen-phase {ph.key}` first[/red]")
        raise typer.Exit(1) from None
    if ph.frozen and not dry_run:
        err.print(f"[red]{ph.key} is frozen ({ph.frozen}); planning is blocked while frozen -- run `garden unfreeze {ph.key}` first[/red]")
        raise typer.Exit(1) from None
    if import_file:
        items = parse_plan(import_file.read_text())
    else:
        prompt = plan_prompt(store, product, phase, extra=guidance, replan=replan)
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
    if ph.closed and reopen:
        console.print(f"{ph.key} reopened")
    created = import_plan(store, product, phase, items, status=status, reopen=reopen)
    for t in created:
        console.print(f"created {t.id} {_style(t.status.value)} {t.title}" + (f"  <- {', '.join(t.depends_on)}" if t.depends_on else ""))
    if not created:
        console.print("no new tasks (all titles already existed)")


@app.command()
def prs(target: str | None = typer.Argument(None, help="product/phase (default: all)")):
    """Every tracked PR: state, review decision, CI, revisions, last poll."""
    from ..scheduler import State

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
def qa(
    scripted: bool = typer.Option(False, "--scripted", help="Drive the flows with the built-in script instead of an agent (no tokens)"),
    phase: str = typer.Option("", "--phase", help="product/phase in this garden to file the findings on as friction reports"),
    out: Path | None = typer.Option(None, "--out", help="Run directory (default: .garden/qa/<time> with --phase, else a temp dir)"),
    keep: bool = typer.Option(False, "--keep", help="Keep the throwaway garden under the run directory"),
    no_task: bool = typer.Option(False, "--no-task", help="File findings in friction.md only, no draft tasks"),
    harness_name: str = typer.Option("", "--harness", help="Harness that plays the person (default: this garden's, or claude)"),
    model: str = typer.Option("", "--model", help="Model for the agent (default: the harness's medium tier)"),
    timeout: int = typer.Option(30, "--timeout", help="Minutes the agent may take"),
    port: int = typer.Option(0, "--port", help="Port for the throwaway web app (default: any free port)"),
):
    """An agent drives the loop end to end through the web app on a throwaway garden: add a
    task, approve, dispatch, answer a question, send back, triage, accept a nothing-to-change
    card, merge, close the phase. Exits non-zero when a flow cannot be completed."""
    import tempfile

    from ..harness import Harness
    from ..qa import file_findings, run_qa

    store = None
    if phase or not scripted or out is None:
        try:
            from ..store import Store

            store = Store()
            store.tasks()
        except (FileNotFoundError, ValueError) as e:
            if phase:
                err.print(f"[red]{e}[/red]")
                raise typer.Exit(2) from None
    product = phase_name = ""
    if phase:
        product, phase_name = _split_target(phase)
        _phase(store, product, phase_name)
    if out is None:
        stamp = now_iso()[:19].replace(":", "-")
        out = store.config.garden_dir / "qa" / stamp if store is not None else Path(tempfile.mkdtemp(prefix="garden-qa-"))
    harness = None
    if not scripted:
        name = harness_name or (str(store.config.get("harness") or "claude") if store is not None else "claude")
        harness = store.config.harness(name) if store is not None else Harness(name, {})
    report = run_qa(out, scripted=scripted, harness=harness, model=model, timeout_minutes=timeout, keep=keep, port=port,
                    log=lambda m: err.print(f"[dim]{m}[/dim]"))
    if phase and report.findings:
        report.filed = file_findings(store, product, phase_name, report.findings, draft_tasks=not no_task)
    console.print(report.summary(), markup=False, highlight=False, soft_wrap=True)
    if not report.ok:
        raise typer.Exit(1)


@app.command("friction-report")
def friction_report(
    target: str = typer.Argument(..., help="product/phase"),
    text: str = typer.Argument(..., help="Friction description"),
    page: str = typer.Option("cli", help="Page or context where friction was noticed"),
    no_task: bool = typer.Option(False, help="Only append to friction.md, do not create a draft task"),
):
    """File a friction report: appends to <phase>/docs/friction.md and creates a draft task."""
    import datetime as _dt

    from ..friction import append_friction_report, create_friction_draft_task

    store = _store()
    product, phase_name = _split_target(target)
    try:
        ph = store.phase(product, phase_name)
    except KeyError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    doc = ph.path / "docs" / "friction.md"
    date = _dt.date.today().isoformat()
    append_friction_report(doc, text, page, date)
    console.print(f"appended to {store.rel(doc)}")
    if not no_task:
        t = create_friction_draft_task(store, product, phase_name, text, page, date)
        if t:
            console.print(f"created draft task {t.id}: {t.title}")
        else:
            console.print(f"[dim]{ph.key} is closed; friction recorded but no draft task created[/dim]")


@app.command()
def friction(target: str = typer.Argument(..., help="product/phase")):
    """Write <phase>/docs/friction.md: reported friction from the record and marked PR comments,
    plus any legacy ## Friction sections in old PR bodies."""
    import datetime as _dt

    from ..friction import collect_comment_friction, harvest, record_friction, write_friction_doc
    from ..github import GitHub
    from ..runs import RunStore

    store = _store()
    product, phase_name = _split_target(target)
    try:
        ph = store.phase(product, phase_name)
    except KeyError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    rs = RunStore(store.config.garden_dir)
    cfg = store.config

    github_slug = str(cfg.product(product).get("github") or "")
    if not github_slug:
        try:
            from ..gitops import slug as _git_slug
            repo = cfg.product_repo(product)
            github_slug = _git_slug(repo) or ""
        except Exception:
            github_slug = ""

    gh = GitHub(use_gh=bool(cfg.get("github.use_gh", True))) if github_slug else None

    doc = ph.path / "docs" / "friction.md"
    date = _dt.date.today().isoformat()
    # Reconcile marked friction comments into the record; write_friction_doc preserves it.
    reconciled = 0
    for task, items in collect_comment_friction(ph, gh, github_slug or None):
        reconciled += len(record_friction(doc, items, f"reported on {task.pr or task.id}", date))
    entries = harvest(ph, rs, github=gh, slug=github_slug or None)
    write_friction_doc(doc, entries)
    n = len(entries)
    extra = f", reconciled {reconciled} from PR comments" if reconciled else ""
    console.print(f"wrote {doc} ({n} task{'s' if n != 1 else ''} with friction{extra})")


@app.command()
def retro(
    target: str = typer.Argument(..., help="product/phase"),
    personas: list[str] = typer.Option([], "--persona", "-p", help="Persona name (repeat); default: all configured/built-in"),
    skip_personas: bool = typer.Option(False, "--skip-personas", help="Reuse persona reports that already exist instead of running them"),
    next_phase: str = typer.Option("", "--next-phase", help="Name for the next phase's goals draft (default: next number up)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan and the estimated cost, then exit"),
):
    """Run a phase's retrospective as one process: harvest the PR-body friction, run (or reuse)
    the persona reviews, reconcile every friction item against what merged, and open a PR to
    the garden's own repo with the retro document and a draft of the next phase's goals."""
    store = _store()
    product, phase_name = _split_target(target)
    ph = _phase(store, product, phase_name)
    sched = _scheduler(store)
    names = list(personas) or None
    if dry_run:
        plan = sched.retro_plan(ph, names, skip_personas=skip_personas, next_phase=next_phase)
        console.print(f"[bold]retro plan for {plan['phase']}[/bold]")
        pending = sched.retro_pending(ph.key)
        if pending:
            console.print(f"  [yellow]retro: waiting for personas ({pending['done']} of {pending['total']})[/yellow]")
        console.print(f"  harvest: {plan['friction']} PR-body friction item(s), {plan['reported']} reported "
                     f"in friction.md, {plan['comment_friction']} from PR comments; "
                     f"{plan['merged']} merged PR(s), {plan['tasks']} task(s)")
        if plan["personas_run"]:
            console.print(f"  personas to run: {', '.join(plan['personas_run'])}")
        if plan["personas_reuse"]:
            console.print(f"  personas to reuse: {', '.join(plan['personas_reuse'])}")
        if not plan["personas_run"] and not plan["personas_reuse"]:
            console.print("  personas: none")
        console.print("  reconcile: 1 run -> retro document + next-goals draft")
        console.print(f"  output: PR to the {plan['self_product'] or '(missing self product!)'} repo; next phase draft {plan['next_phase']}")
        cost = f"${plan['est_cost']:.2f}" if plan["have_cost_history"] else "unknown (no run history yet)"
        console.print(f"  estimated: ~{plan['est_tokens']:,} tokens, {cost}")
        if not plan["self_product"]:
            err.print("[yellow]no product has `self: true`; retro cannot open a PR to the garden repo (see docs/architecture.md)[/yellow]")
        return
    try:
        entry = sched.start_retro(ph, names, skip_personas=skip_personas, next_phase=next_phase)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    if entry["stage"] == "personas":
        running = ", ".join(entry["persona_runs"])
        console.print(f"{ph.key}: running persona review(s) ({running}); reconciliation follows on the next tick")
    else:
        console.print(f"{ph.key}: reconciliation run started; retro PR will open to the {entry['self_product']} repo on the next tick")
    console.print("[dim]run `garden tick` (or `garden watch`) to let it finish[/dim]")


@app.command()
def usage(
    target: str | None = typer.Argument(None, help="task id, product/phase, or nothing for everything"),
    by_mode: bool = typer.Option(False, help="Split each task's usage by run mode (work/revise/review/…)"),
):
    """Tokens and cost per task, rolled up from every run."""
    from ..brief import estimate_brief_tokens, phase_fixed_tokens
    from ..runs import RunStore

    store = _store()
    rs = RunStore(store.config.garden_dir)
    tasks = store.tasks()
    product = phase = None
    if target and "/" in target:
        product, phase = _split_target(target)
    elif target:
        t = _task(store, target)
        u = rs.usage_for(t.id)
        fixed, reading = estimate_brief_tokens(store, t)
        console.print(f"[bold]{t.id}[/bold] {t.title}  brief ~{fixed + reading:,} (fixed {fixed:,} + reading {reading:,})  runs={u['runs']}  in={u['input_tokens']:,}  out={u['output_tokens']:,}  cache-read={u['cache_read_input_tokens']:,}  cost=${u['cost_usd']:.2f}  minutes={u['minutes']}")
        table = Table()
        for c in ("mode", "runs", "in", "cache-read", "out", "cost"):
            table.add_column(c, justify="right" if c != "mode" else "left")
        for mode, m in sorted(u["by_mode"].items()):
            table.add_row(mode, str(m["runs"]), f"{m['input_tokens']:,}", f"{m['cache_read_input_tokens']:,}", f"{m['output_tokens']:,}", f"${m['cost_usd']:.2f}")
        console.print(table)
        return
    per = rs.usage_by_task()
    if product and phase:
        try:
            ph = store.phase(product, phase)
        except KeyError as e:
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from None
        rows = [(t.id, t) for t in ph.tasks]
        if rows:
            phase_fixed = phase_fixed_tokens(store, ph.tasks)
            console.print(f"[bold]{product}/{phase}[/bold] fixed brief cost: ~{phase_fixed:,} tokens (head + rules + digest + product + goals)")
    else:
        rows = [(tid, tasks.get(tid)) for tid in sorted(per)]

    table = Table(title="usage per task")
    for c in ("task", "tier", "status", "runs", "brief", "in", "out", "cache-read", "cost", "$/run"):
        table.add_column(c, justify="right" if c not in ("task", "tier", "status") else "left")
    tot = {"runs": 0, "in": 0, "out": 0, "cache": 0, "cost": 0.0}
    brief_tot = 0
    for tid, t in rows:
        u = per.get(tid) or rs.usage_for(tid)
        fixed, reading = estimate_brief_tokens(store, t) if t else (0, 0)
        brief_est = fixed + reading
        brief_tot += brief_est
        table.add_row(tid, t.difficulty if t else "", _style(t.status.value) if t else "", str(u["runs"]), f"~{brief_est:,}",
                      f"{u['input_tokens']:,}", f"{u['output_tokens']:,}", f"{u['cache_read_input_tokens']:,}", f"${u['cost_usd']:.2f}",
                      f"${u['cost_usd'] / u['runs']:.2f}" if u["runs"] else "")
        if by_mode:
            for mode, m in sorted(u["by_mode"].items()):
                table.add_row(f"  {mode}", "", "", str(m["runs"]), "", f"{m['input_tokens']:,}", f"{m['cache_read_input_tokens']:,}", f"{m['output_tokens']:,}", f"${m['cost_usd']:.2f}", "")
        tot["runs"] += u["runs"]
        tot["in"] += u["input_tokens"]
        tot["out"] += u["output_tokens"]
        tot["cache"] += u["cache_read_input_tokens"]
        tot["cost"] += u["cost_usd"]
    table.add_row("[bold]total[/bold]", "", "", str(tot["runs"]), f"~{brief_tot:,}", f"{tot['in']:,}", f"{tot['out']:,}", f"{tot['cache']:,}", f"${tot['cost']:.2f}", "")
    console.print(table)


@app.command()
def review(task_id: str):
    """Start an automated review run for a task's open PR now. If the task's review cap
    was already reached, this raises it by one round and clears the needs-human stop."""
    store = _store()
    t = _task(store, task_id)
    if not t.pr:
        err.print(f"[red]{t.id} has no PR[/red]")
        raise typer.Exit(1)
    try:
        run = _scheduler(store).review_again(t)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: review run {run.run_id} started (model {run.model or 'default'})")
