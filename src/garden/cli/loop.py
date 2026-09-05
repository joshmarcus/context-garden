"""`garden` loop commands: pausing and resuming dispatch, live config, ticks, running and
finishing workers, reviews, triage, suggestions and the inbox."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.table import Table

from ..model import Status, now_iso
from .common import (
    PANEL_BOARD,
    PANEL_DECIDE,
    PANEL_INSIGHT,
    PANEL_LOOP,
    PANEL_PLAN,
    PANEL_QUALITY,
    PANEL_REVIEW,
    _phase,
    _scheduler,
    _split_target,
    _store,
    _style,
    _task,
    app,
    console,
    err,
)


# --------------------------------------------------------------------------- the loop
@app.command(rich_help_panel=PANEL_LOOP)
def pause(reason: str = typer.Option("", "--reason", "-r", help="Optional reason to record")):
    """Pause automatic dispatch (reap, poll and reviews keep running)."""
    store = _store()
    sched = _scheduler(store)
    sched.pause(by="cli", reason=reason)
    msg = "dispatch paused"
    if reason:
        msg += f": {reason}"
    console.print(f"[yellow]{msg}[/yellow]")


@app.command(rich_help_panel=PANEL_LOOP)
def budget(
    phase: str = typer.Argument(..., help="Phase key, e.g. context-garden/phase-02-friction"),
    value: str = typer.Argument(..., help="USD cap, or 'none' to remove the cap"),
):
    """Set or clear a phase's budget cap. Overrides garden.yaml; the running scheduler picks it
    up on the next tick and a paused phase resumes when the cap is raised or removed."""
    store = _store()
    if "/" not in phase:
        console.print("[red]phase must be a product/phase key, e.g. context-garden/phase-02-friction[/red]")
        raise typer.Exit(1)
    prod, ph = phase.split("/", 1)
    try:
        store.phase(prod, ph)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    sched = _scheduler(store)
    if value.strip().lower() in ("none", "off", "no", ""):
        sched.set_budget(phase, None, by="cli")
        console.print(f"{phase}: budget removed (no cap)")
        return
    try:
        usd = float(value)
    except ValueError:
        console.print(f"[red]budget must be a number or 'none', got {value!r}[/red]")
        raise typer.Exit(1) from None
    if usd < 0:
        console.print("[red]budget must not be negative[/red]")
        raise typer.Exit(1)
    sched.set_budget(phase, usd, by="cli")
    console.print(f"{phase}: budget set to ${usd:.2f}")


@app.command(rich_help_panel=PANEL_LOOP)
def unpause():
    """Resume automatic dispatch after a `garden pause` (the mirror of pause)."""
    store = _store()
    _scheduler(store).resume(by="cli")
    console.print("[green]dispatch resumed[/green]")


@app.command(rich_help_panel=PANEL_DECIDE)
def resume(task_id: str = typer.Argument(..., help="The task to clear")):
    """Clear a task's needs-human stop without starting a run: it goes back where it was.
    (To resume paused dispatch, use `garden unpause`.)"""
    store = _store()
    sched = _scheduler(store)
    t = _task(store, task_id)
    try:
        sched.resume_task(t)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: nothing to fix; resumed as {t.status.value}")


# keys settable live (garden set / the Configuration page) and their value type; see Scheduler.set_override
LIVE_OVERRIDES: dict[str, type] = {"max_parallel": int}


@app.command("set", rich_help_panel=PANEL_LOOP)
def set_live(key: str, value: str):
    """Set a config value live, effective next tick without a restart (currently: max_parallel).
    Overrides the garden.yaml value until cleared with `garden clear <key>`."""
    if key not in LIVE_OVERRIDES:
        err.print(f"[red]{key!r} can't be set live; only {', '.join(LIVE_OVERRIDES)} can[/red]")
        raise typer.Exit(1)
    caster = LIVE_OVERRIDES[key]
    try:
        cast_value = caster(value)
    except ValueError:
        err.print(f"[red]{value!r} is not a valid {caster.__name__} for {key}[/red]")
        raise typer.Exit(1) from None
    store = _store()
    sched = _scheduler(store)
    sched.set_override(key, cast_value, by="cli")
    console.print(f"[green]{key} = {cast_value}[/green] (live override; garden.yaml value unchanged, takes effect next tick)")


@app.command(rich_help_panel=PANEL_LOOP)
def clear(key: str):
    """Clear a live override set with `garden set`, going back to the garden.yaml value."""
    if key not in LIVE_OVERRIDES:
        err.print(f"[red]{key!r} can't be set live; only {', '.join(LIVE_OVERRIDES)} can[/red]")
        raise typer.Exit(1)
    store = _store()
    sched = _scheduler(store)
    sched.clear_override(key, by="cli")
    console.print(f"[green]{key} override cleared[/green] (back to the garden.yaml value)")


@app.command(rich_help_panel=PANEL_LOOP)
def tick(no_dispatch: bool = typer.Option(False, help="Only reap and poll; don't start workers")):
    """One scheduler pass: reap finished workers, poll PRs, dispatch ready tasks."""
    store = _store()
    rep = _scheduler(store).tick(dispatch=not no_dispatch)
    console.print(rep.summary())
    if rep.errors:
        raise typer.Exit(1) from None


@app.command(rich_help_panel=PANEL_LOOP)
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


@app.command(rich_help_panel=PANEL_LOOP)
def dispatch(task_id: str, mode: str = typer.Option("work", help="work|revise"), force: bool = typer.Option(False, help="Ignore deps/status")):
    """Start a worker for one task now."""
    from ..graph import blockers

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
    try:
        run = _scheduler(store).dispatch(t, mode=mode)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: run {run.run_id} started (worktree {run.worktree})")


@app.command(rich_help_panel=PANEL_LOOP)
def take(
    task_id: str,
    worktree: bool = typer.Option(False, help="Also create the git worktree and print its path"),
    quiet: bool = typer.Option(False, "-q", help="Only print the brief path"),
):
    """Claim a task for a human-driven session and print its brief (manual runner)."""
    from ..runner.manual import ManualRunner

    store = _store()
    t = _task(store, task_id)
    if t.status == Status.RUNNING:
        err.print(f"[red]{t.id} is already running[/red]")
        raise typer.Exit(1) from None
    if t.status == Status.DRAFT:
        t.status = Status.READY
    sched = _scheduler(store)
    mode = "revise" if t.status == Status.CHANGES_REQUESTED else "work"
    try:
        run = sched.dispatch(t, mode=mode, runner=ManualRunner({}), worktree=worktree)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
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


@app.command(rich_help_panel=PANEL_LOOP)
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


@app.command(rich_help_panel=PANEL_DECIDE)
def answer(task_id: str, text: str = typer.Argument(..., help="Your answer to the worker's question")):
    """Answer a waiting_human task; the worker resumes (same session when the harness supports it).
    If the worker reported wont_do / no_change, this rejects that call and sends your note back."""
    store = _store()
    t = _task(store, task_id)
    sched = _scheduler(store)
    if sched.pending_decision(t):
        try:
            sched.reject_decision(t, text)
        except RuntimeError as e:
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from None
        console.print(f"{t.id}: rejected; revise run will follow")
        return
    try:
        run = sched.answer(t, text)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: {'resumed session' if run.session_id else 'fresh run with the answer'} ({run.run_id})")


@app.command(rich_help_panel=PANEL_INSIGHT)
def digest(since: str = typer.Option("24h", help="Window: 90m, 24h, 3d, or an ISO timestamp")):
    """What happened while you were away: PRs opened and merged, tasks needing you, cost."""
    from ..events import EventLog, parse_since
    from ..events import digest as _digest

    store = _store()
    since_iso = parse_since(since)
    events = EventLog(store.config.garden_dir / "events.jsonl").read(since=since_iso)
    d = _digest(events)
    console.print(f"[bold]since {since_iso}[/bold]: {len(events)} events, {d['dispatched']} dispatches, ${d['cost_usd']:.2f} spent")
    n_decisions = len({(ev["task"], ev.get("kind")) for ev in d["needs_human"]})
    n_notices = len(d["failures"])
    console.print(f"  [bold]{n_decisions}[/bold] decision{'s' if n_decisions != 1 else ''} need you, "
                  f"[dim]{n_notices} notice{'s' if n_notices != 1 else ''} (no action needed)[/dim]")
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
        by_garden = {ev.get("task") for ev in d["automerged"]}
        console.print("\n[bold green]Merged[/bold green]"
                      + (f" [dim]({len(by_garden)} by the garden)[/dim]" if by_garden else ""))
        for ev in d["merged"]:
            tag = " [dim](by the garden)[/dim]" if ev["task"] in by_garden else ""
            console.print(f"  {ev['task']:<8} {title(ev['task'])}{tag}")
    if d["discovered"]:
        console.print("\n[bold cyan]Discovered work[/bold cyan]")
        for ev in d["discovered"]:
            console.print(f"  {ev.get('new_task', ''):<8} {ev.get('title', '')[:50]:<50} by {ev['task']}" + (" [blocking]" if ev.get("blocking") else ""))
    if d["failures"]:
        console.print("\n[bold yellow]Failed runs (auto-retried or gave up)[/bold yellow]")
        seen: set[str] = set()
        for ev in d["failures"]:
            key = (ev["task"], ev.get("status"), ev.get("mode"))
            if key in seen:
                continue
            seen.add(key)
            console.print(f"  {ev['task']:<8} {title(ev['task'])[:40]:<40} {ev.get('status', '')} ({ev.get('mode', '')})")
    if not any([d["needs_human"], d["prs_opened"], d["reviews"], d["merged"], d["discovered"], d["failures"]]):
        console.print("[dim]nothing notable[/dim]")


@app.command(rich_help_panel=PANEL_INSIGHT)
def metrics(target: str | None = typer.Argument(None, help="product/phase (default: all)")):
    """Lead time, revise rounds, first-pass approval and cost per task and per difficulty tier."""
    from ..events import EventLog
    from ..events import metrics as _metrics

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
    for c in ("tier", "tasks", "done", "avg revisions", "first-pass approve", "criteria met", "cost", "avg lead h"):
        table.add_column(c)
    for tier in ("easy", "medium", "hard"):
        d = m["by_difficulty"].get(tier)
        if not d:
            continue
        criteria = f"{d['criteria_met']}/{d['criteria_total']}" + (f" · {d['criteria_rate']:.0%}" if d["criteria_rate"] is not None else "") if d["criteria_total"] else ""
        table.add_row(tier, str(d["tasks"]), str(d["done"]), str(d["avg_revisions"]),
                      f"{d['first_pass_rate']:.0%}" if d["first_pass_rate"] is not None else "",
                      criteria,
                      f"${d['cost_usd']:.2f}", f"{d['avg_lead_hours']:.1f}" if d["avg_lead_hours"] is not None else "")
    console.print(table)
    rb = m.get("rebase") or {}
    table = Table(title="rebases (their own mode: cheapest thing that brings a PR forward)")
    for c in ("rebases", "mechanical", "agent", "merges", "rebases per merge", "rebase cost"):
        table.add_column(c)
    table.add_row(str(rb.get("rebases", 0)), str(rb.get("mechanical", 0)), str(rb.get("agent", 0)), str(rb.get("merges", 0)),
                  f"{rb['per_merge']:.2f}" if rb.get("per_merge") is not None else "",
                  f"${rb.get('cost_usd', 0.0):.2f}")
    console.print(table)


@app.command(rich_help_panel=PANEL_BOARD)
def events(task_id: str | None = typer.Argument(None), since: str = typer.Option("", help="90m, 24h, 3d or ISO"), limit: int = typer.Option(50, "-n")):
    """Timeline of scheduler events (all, or one task)."""
    from ..events import EventLog, parse_since

    store = _store()
    evs = EventLog(store.config.garden_dir / "events.jsonl").read(since=parse_since(since) if since else "", task_id=task_id or "")
    for ev in evs[-limit:]:
        extra = {k: v for k, v in ev.items() if k not in ("at", "kind", "task", "usage")}
        console.print(f"[dim]{ev['at'][5:19]}[/dim] {ev['task']:<8} [bold]{ev['kind']:<14}[/bold] " + " ".join(f"{k}={v}" for k, v in extra.items() if v not in ("", None, False, 0)))


@app.command(rich_help_panel=PANEL_INSIGHT)
def trial(
    task_id: str,
    contenders: list[str] = typer.Option(..., "--contender", "-c", help="harness:model, e.g. claude:opus, codex:gpt-5.6-terra (repeat)"),
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


@app.command(rich_help_panel=PANEL_INSIGHT)
def trials(task_id: str | None = typer.Argument(None, help="Show one task's trial instead of the leaderboard")):
    """Model leaderboard from every trial: wins, average score and cost per contender."""
    from ..trials import TrialLog, ranking_markdown

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


@app.command(rich_help_panel=PANEL_QUALITY)
def walkthrough(
    target: str = typer.Argument(..., help="product/phase"),
    out: Path | None = typer.Option(None, "--out", help="Output directory (default: <phase>/docs/walkthrough/<date>)"),
    url: str = typer.Option("", "--url", help="Base URL of a running app to capture (default: an in-process test app)"),
    screenshots: bool = typer.Option(True, "--screenshots/--no-screenshots", help="Capture PNGs with Playwright's Chromium when it is available"),
    include_stderr: bool = typer.Option(False, "--include-stderr", help="Include the run page's raw stderr (omitted by default: it can carry secrets or local paths, and this capture is committed to the garden repo)"),
):
    """Render the live web app's pages to screenshots, HTML and text, with an index.md that
    says what each page is for and what to look at — a persona review can then judge the real
    UI and a person can follow it as a QA script. Needs Playwright's Chromium for screenshots
    (pip install 'context-garden[walkthrough]' && playwright install chromium); with no
    browser it captures HTML and text only and notes it in the index. Absolute home-directory
    paths are redacted and the run page's stderr is omitted unless --include-stderr is given,
    since this capture is committed to the garden repo."""
    from datetime import date

    from ..walkthrough import capture

    store = _store()
    product, phase = _split_target(target)
    ph = _phase(store, product, phase)
    out_dir = out or (ph.path / "docs" / "walkthrough" / date.today().isoformat())
    console.print(f"capturing {ph.key} -> {out_dir}")
    result = capture(store, ph, out_dir, screenshots=screenshots, base_url=url,
                     log=lambda m: console.print(f"[dim]{m}[/dim]"), include_stderr=include_stderr)
    n = len(result.pages)
    kind = "screenshots + HTML + text" if result.screenshots else "HTML + text (no screenshots)"
    console.print(f"[green]wrote {n} page(s) and index.md[/green] ({kind}) to {out_dir}")
    if result.browser_note:
        console.print(f"[yellow]{result.browser_note}[/yellow]")


@app.command("persona-review", rich_help_panel=PANEL_QUALITY)
def persona_review(
    target: str = typer.Argument(..., help="A task id (reviews its PR) or product/phase (reviews the body of work)"),
    personas: list[str] = typer.Option(..., "--persona", "-p", help="Persona name (repeat); see `garden personas`"),
    file_tasks: bool = typer.Option(False, help="Phase reviews: turn every finding into a draft task, priority from severity"),
    min_severity: str = typer.Option("low", "--min-severity", help="With --file-tasks: lowest severity to file (low, medium, high)"),
    request_changes: bool = typer.Option(False, help="PR reviews: high findings trigger a revise run"),
):
    """Persona reviews (designer, project-manager, staff-engineer, usability-expert, user, security, or your own)."""
    if min_severity not in ("low", "medium", "high"):
        err.print(f"[red]--min-severity must be low, medium or high, got {min_severity!r}[/red]")
        raise typer.Exit(1)
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
            run = sched.dispatch_persona_phase(ph, name, file_tasks=file_tasks, min_severity=min_severity)
            console.print(f"{ph.key}: persona {name} -> run {run.run_id} (report lands in {ph.name}/docs/reviews/)")
        return
    t = _task(store, target)
    for name in personas:
        try:
            run = sched.dispatch_persona_pr(t, name, request_changes=request_changes)
        except RuntimeError as e:
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from None
        console.print(f"{t.id}: persona {name} -> run {run.run_id} (comment on {t.pr or 'the PR'})")


@app.command(rich_help_panel=PANEL_QUALITY)
def personas():
    """List available personas (personas/*.md, plus the built-in defaults)."""
    from ..personas import DEFAULT_PERSONAS, list_personas

    store = _store()
    have = list_personas(store)
    for name in sorted(set(have) | set(DEFAULT_PERSONAS)):
        where = "personas/" + name + ".md" if name in have else "built-in default (garden init writes it)"
        console.print(f"{name:<18} {where}")


@app.command(rich_help_panel=PANEL_REVIEW)
def check(task_id: str, stage: str = typer.Option("pre_pr", help="pre_pr | ci")):
    """Run the token-free checks for a task by hand (pre_pr in its worktree, or ci analysers)."""
    from ..checks import run_checks

    store = _store()
    t = _task(store, task_id)
    sched = _scheduler(store)
    # pre_pr goes through the same resolver as the automated gate: it falls back to the
    # product's setup.test/setup.lint and merges setup.env, so the manual command agrees
    # with what the scheduler actually runs. Other stages read checks.<stage> directly.
    specs = sched._pre_pr_specs(t) if stage == "pre_pr" else list(store.config.get(f"checks.{stage}", []) or [])
    if not specs:
        err.print(f"no checks configured under checks.{stage}")
        raise typer.Exit(1)
    wt = sched.worktree_for(t)
    results = run_checks(specs, sched.check_ctx(t, t.branch or t.default_branch(), sched.base_for(t), wt),
                         cwd=wt if wt.exists() else None, timeout=int(store.config.get("checks.timeout_seconds", 600)),
                         config=store.config.data)
    bad = 0
    for r in results:
        color = "green" if r.get("status") == "pass" else ("yellow" if r.get("status") == "flaky" else "red")
        console.print(f"[{color}]{r.get('status'):<6}[/{color}] {r.get('name')}: {r.get('summary', '')}")
        if r.get("details") and r.get("status") != "pass":
            print(r["details"])
        bad += r.get("status") in ("fail", "error")
    raise typer.Exit(1 if bad else 0)


@app.command(rich_help_panel=PANEL_DECIDE)
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


@app.command(rich_help_panel=PANEL_PLAN)
def suggest(
    task_id: str,
    text: str = typer.Argument(..., help="Your suggestion about the task (its goal, context, acceptance, etc.)"),
    by: str = typer.Option("cli", "--by", help="Who is suggesting (recorded with the line)"),
    applies_to: str = typer.Option("", "--applies-to", help="goal | context | acceptance | reading | priority | difficulty | anything"),
):
    """Suggest a change to a task's own spec; an `edit` run folds it in later."""
    from ..suggestions import record_suggestion

    store = _store()
    t = _task(store, task_id)
    record_suggestion(store, t, text, author=by, applies_to=applies_to)
    console.print(f"{t.id}: suggestion recorded; it will be integrated on the next tick (or `garden integrate {t.id}`)")


@app.command(rich_help_panel=PANEL_PLAN)
def integrate(task_id: str):
    """Start an edit run now that folds a task's pending suggestions into its body."""
    store = _store()
    t = _task(store, task_id)
    try:
        run = _scheduler(store).integrate_now(t)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: edit run {run.run_id} started")


@app.command(rich_help_panel=PANEL_DECIDE)
def inbox():
    """Everything that needs a human, with the command that resolves it."""
    from ..inbox import build_inbox, decisions, notices

    store = _store()
    items = build_inbox(store, _scheduler(store))
    decision_items = decisions(items)
    notice_items = notices(items)
    if decision_items:
        console.print(f"[bold]{len(decision_items)} need you[/bold]")
        current = ""
        for it in decision_items:
            if it["group"] != current:
                current = it["group"]
                console.print(f"\n[bold]{it['group_title']}[/bold]")
            console.print(f"  {it['task']:<8} {it['title'][:44]:<44} [dim]{it['why'][:60]}[/dim]")
            for a in it["actions"]:
                if a.get("command"):
                    detail = f"  [dim]{a['detail']}[/dim]" if a.get("detail") else ""
                    console.print(f"           [cyan]{a['command']}[/cyan]{detail}")
    else:
        console.print("[green]inbox zero[/green] — nothing needs you")
    if notice_items:
        console.print("\n[dim]Notices — no action needed[/dim]")
        current = ""
        for it in notice_items:
            if it["group"] != current:
                current = it["group"]
                count = sum(1 for x in notice_items if x["group"] == current)
                console.print(f"\n[dim]{it['group_title']} · {count}, no action needed[/dim]")
            console.print(f"  [dim]{it['task']:<8} {it['title'][:44]:<44} {it['why'][:60]}[/dim]")
