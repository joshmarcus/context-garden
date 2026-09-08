"""`garden` state changes: move, approve, priority, difficulty, set-status, accept, reject,
cancel, retry, discuss, decide, commit, pr."""

from __future__ import annotations

import typer

from ..model import (
    STATUS_ORDER,
    Status,
    _owner_id,
    join_frontmatter,
    priority_label,
    split_frontmatter,
)
from .common import (
    PANEL_DECIDE,
    PANEL_LOOP,
    PANEL_PLAN,
    PANEL_REVIEW,
    _scheduler,
    _split_target,
    _store,
    _task,
    app,
    console,
    err,
)


# --------------------------------------------------------------------------- move
@app.command(rich_help_panel=PANEL_PLAN)
def move(task_id: str, target: str = typer.Argument(..., help="product/phase")):
    """Move a task to another phase of the same product, keeping its id, history and state."""
    store = _store()
    t = _task(store, task_id)
    product, phase = _split_target(target)
    try:
        _scheduler(store).move(t, product, phase)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id} -> {product}/{phase}")


# --------------------------------------------------------------------------- state changes
@app.command(rich_help_panel=PANEL_PLAN)
def approve(
    task_ids: list[str] = typer.Argument(None),
    all_in: str | None = typer.Option(None, "--all", help="Approve every draft in product/phase"),
):
    """draft -> ready."""
    store = _store()
    sched = _scheduler(store)
    targets = []
    if all_in:
        product, phase = _split_target(all_in)
        targets = [t for t in store.tasks().values() if t.key == f"{product}/{phase}" and t.status == Status.DRAFT]
    for tid in task_ids or []:
        targets.append(_task(store, tid))
    if not targets:
        err.print("nothing to approve")
        raise typer.Exit(1) from None
    phases = {p.key: p for prod in store.products() for p in prod.phases}
    refused = False
    for t in targets:
        try:
            warning = sched.approve(t, by="cli", phase=phases.get(t.key))
        except RuntimeError as e:
            err.print(f"[yellow]{e}[/yellow]")
            refused = True
            continue
        console.print(f"{t.id} -> ready")
        if warning:
            err.print(f"[yellow]{warning}[/yellow]")
    if refused:
        raise typer.Exit(1) from None


@app.command(rich_help_panel=PANEL_PLAN)
def priority(task_id: str, value: int = typer.Argument(..., help="lower dispatches first; ties by id")):
    """Set a task's priority (the queue order)."""
    store = _store()
    t = _task(store, task_id)
    old = t.priority
    t.priority = int(value)
    t.log(f"priority {old} -> {t.priority}")
    store.save(t)
    console.print(f"{t.id} priority {priority_label(old)} -> {priority_label(t.priority)}")


@app.command(rich_help_panel=PANEL_PLAN)
def difficulty(task_id: str, tier: str = typer.Argument(..., help="easy | medium | hard (picks the model tier at dispatch)")):
    """Set a task's difficulty tier."""
    from ..harness import DIFFICULTIES

    store = _store()
    t = _task(store, task_id)
    if tier not in DIFFICULTIES:
        err.print(f"[red]unknown tier; one of {', '.join(DIFFICULTIES)}[/red]")
        raise typer.Exit(1) from None
    old = t.difficulty
    t.difficulty = tier
    t.log(f"difficulty {old} -> {tier}")
    store.save(t)
    console.print(f"{t.id} difficulty {old} -> {tier}")


@app.command("assign", rich_help_panel=PANEL_PLAN)
def assign(task_id: str, owner: str = typer.Argument(..., help="Logical owner id; use '-' to unassign or 'inherit' to use the phase default")):
    """Assign planning ownership without changing access, execution, or approval rules."""
    store = _store()
    t = _task(store, task_id)
    explicit_unassigned = owner == "-"
    inherit = owner == "inherit"
    value = "" if explicit_unassigned or inherit else owner
    try:
        value = _owner_id(value, t.path)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    old = t.owner or "unassigned"
    t.owner = value
    t.owner_unassigned = explicit_unassigned
    t.log(f"owner {old} -> {value or 'unassigned'}")
    store.save(t)
    console.print(f"{t.id} owner {old} -> {'inherits phase default' if inherit else value or 'unassigned'}")


@app.command("assign-phase", rich_help_panel=PANEL_PLAN)
def assign_phase(target: str, owner: str = typer.Argument(..., help="Logical default owner id; use '-' to unassign")):
    """Set a phase default owner; task overrides continue to take precedence."""
    store = _store()
    product, phase_name = _split_target(target)
    try:
        phase = store.phase(product, phase_name)
    except KeyError:
        err.print(f"[red]unknown phase {target}[/red]")
        raise typer.Exit(1) from None
    if phase.goals_path is None:
        err.print(f"[red]{target} has no goals.md for phase metadata[/red]")
        raise typer.Exit(1) from None
    value = "" if owner == "-" else owner
    try:
        value = _owner_id(value, phase.goals_path)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    meta, body = split_frontmatter(phase.goals_path.read_text())
    old = phase.owner or "unassigned"
    if value:
        meta["owner"] = value
    else:
        meta.pop("owner", None)
        meta.pop("default_owner", None)
    phase.goals_path.write_text(join_frontmatter(meta, body))
    store.invalidate()
    console.print(f"{target} default owner {old} -> {value or 'unassigned'}")


@app.command("set-status", rich_help_panel=PANEL_DECIDE)
def set_status(task_id: str, new_status: str, note: str = typer.Option("", help="Log note"),
               reason: str = typer.Option("", "--reason", help="Reason (for wont_do): recorded and posted when the PR is closed"),
               force: bool = typer.Option(False, "--force", help="Required to move a task out of done or cancelled"),
               actor: str = typer.Option("human_owner", "--actor", help="human_owner, delegated_operator, automated_scheduler, or unknown")):
    """Escape hatch: force a task's status. `wont_do` closes any open PR with the reason and records it.
    Moving a task out of done or cancelled needs --force: those are the loop's terminal states."""
    store = _store()
    t = _task(store, task_id)
    try:
        s = Status(new_status)
    except ValueError:
        err.print(f"[red]unknown status; one of {', '.join(STATUS_ORDER)}[/red]")
        raise typer.Exit(1) from None
    if t.status in (Status.DONE, Status.CANCELLED) and s != t.status and not force:
        err.print(f"[red]{t.id} is {t.status.value}; use --force to move it to {s.value}[/red]")
        raise typer.Exit(1) from None
    if s == Status.WONT_DO:
        _scheduler(store).mark_wont_do(t, reason=reason or note)
        console.print(f"{t.id} -> wont_do")
        return
    sched = _scheduler(store)
    try:
        if s == Status.DONE:
            sched.mark_done(t, note or "status forced to done", force=force, actor=actor)
        else:
            sched.set_status(t, s, note or f"status forced to {s.value}", actor=actor)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id} -> {s.value}")


@app.command(rich_help_panel=PANEL_DECIDE)
def accept(task_id: str, note: str = typer.Option("", help="Optional note recorded with your decision")):
    """Accept a worker's wont_do / no_change call: wont_do ends the task, no_change resumes the round."""
    store = _store()
    t = _task(store, task_id)
    sched = _scheduler(store)
    dec = sched.pending_decision(t)
    if not dec:
        err.print(f"[red]{t.id} has no pending worker decision to accept[/red]")
        raise typer.Exit(1) from None
    try:
        sched.accept_decision(t, note)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: accepted {dec['kind']} -> {sched.store.task(task_id).status.value}")


@app.command(rich_help_panel=PANEL_DECIDE)
def reject(task_id: str, note: str = typer.Argument(..., help="Why you disagree; carried into the next revise round")):
    """Reject a worker's wont_do / no_change call: its reasoning goes back with your note for a revise run."""
    store = _store()
    t = _task(store, task_id)
    try:
        _scheduler(store).reject_decision(t, note)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{t.id}: rejected; revise run will follow")


@app.command(rich_help_panel=PANEL_DECIDE)
def cancel(task_id: str, note: str = typer.Option("cancelled by hand")):
    """Cancel a task (kills a running worker)."""
    store = _store()
    try:
        _scheduler(store).cancel(_task(store, task_id), note)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


@app.command(rich_help_panel=PANEL_DECIDE)
def retry(task_id: str, actor: str = typer.Option("human_owner", "--actor",
                                                   help="human_owner, delegated_operator, automated_scheduler, or unknown")):
    """Continue the loop: with an open PR, queue a revise run on the branch; otherwise reset attempts and start over."""
    store = _store()
    try:
        _scheduler(store).retry(_task(store, task_id), actor=actor)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


@app.command("troubled-continue", rich_help_panel=PANEL_DECIDE)
def troubled_continue(task_id: str, difficulty: str = typer.Option("", help="Optional same-or-higher tier")):
    """Grant one bounded revision while preserving lifetime counts and existing work."""
    store = _store()
    try:
        _scheduler(store).continue_troubled(_task(store, task_id), difficulty=difficulty)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{task_id}: one preserved revision queued")


@app.command("investigate", rich_help_panel=PANEL_DECIDE)
def investigate(
    task_id: str,
    reason: str = typer.Argument(..., help="Question or suspected cause to investigate"),
    owner: str = typer.Option("operator", help="operator or agent"),
    scope: str = typer.Option("read-only diagnosis", help="Explicit investigation boundary"),
    budget: str = typer.Option("one bounded investigation", help="Explicit time or cost budget"),
):
    """Pause a task at a safe boundary and create a durable investigation request."""
    if owner not in ("operator", "agent"):
        err.print("[red]owner must be operator or agent[/red]")
        raise typer.Exit(1)
    store = _store()
    try:
        _scheduler(store).pause_for_investigation(
            _task(store, task_id), reason, owner=owner, scope=scope, budget=budget
        )
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{task_id}: investigation requested for {owner}")


@app.command("investigation-report", rich_help_panel=PANEL_DECIDE)
def investigation_report(task_id: str, report: str = typer.Argument(...)):
    """Publish a completed diagnosis without changing the task outcome."""
    store = _store()
    try:
        _scheduler(store).complete_investigation(_task(store, task_id), report)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{task_id}: investigation report ready for decision")


@app.command("recover", rich_help_panel=PANEL_DECIDE)
def recover(task_id: str):
    """Use one delegated, bounded recovery continuation without changing product scope."""
    store = _store()
    try:
        outcome = _scheduler(store).delegate_recovery(_task(store, task_id))
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{task_id}: {outcome}")


@app.command("evidence", rich_help_panel=PANEL_DECIDE)
def evidence(task_id: str, text: str = typer.Argument(..., help="What the operator verified outside the worker checkout")):
    """Record proof for an operator-owned live configuration prerequisite."""
    store = _store()
    try:
        _scheduler(store).submit_operator_evidence(_task(store, task_id), text)
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{task_id}: operator evidence recorded")


@app.command("recover-check", rich_help_panel=PANEL_DECIDE)
def recover_check(task_id: str):
    """Atomically clear a terminal check stop or retain a live check continuation."""
    store = _store()
    try:
        outcome = _scheduler(store).recover_waiting_check(_task(store, task_id))
    except RuntimeError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"{task_id}: {outcome}")


@app.command(rich_help_panel=PANEL_DECIDE)
def discuss(task_id: str):
    """Print a ready-made prompt about a stopped task (the task, the reason, the PR, the runs), for a chat session or `garden take`."""
    from ..inbox import attention_view
    from ..runs import RunStore
    from ..scheduler import State

    store = _store()
    t = _task(store, task_id)
    st = State(store.config.garden_dir / "state.json").get(t.id)
    view = attention_view(t, st, RunStore(store.config.garden_dir))
    if view is None:
        console.print(f"{t.id} is {t.status.value}; nothing is waiting on a decision")
        raise typer.Exit(1)
    print(view["discuss"])


@app.command(rich_help_panel=PANEL_DECIDE)
def decide(
    decision_id: str,
    accept: bool = typer.Option(False, "--accept", help="Cancel the named task"),
    reject: bool = typer.Option(False, "--reject", help="Dismiss the card; log the disagreement"),
    answer: str | None = typer.Option(None, "--answer", help="Answer a kickoff or retro question card"),
    dismiss: bool = typer.Option(False, "--dismiss", help="Dismiss a kickoff or retro question card without answering"),
):
    """Resolve a worker decision card, or answer/dismiss a kickoff or retro question."""
    if sum([accept, reject, answer is not None, dismiss]) != 1:
        err.print("[red]choose exactly one of --accept / --reject / --answer / --dismiss[/red]")
        raise typer.Exit(1) from None
    store = _store()
    sched = _scheduler(store)
    try:
        if answer is not None:
            sched.answer_question(decision_id, answer, by="cli")
            console.print(f"decision {decision_id} answered")
        elif dismiss:
            sched.dismiss_question(decision_id, by="cli")
            console.print(f"decision {decision_id} dismissed")
        else:
            d = sched.resolve_decision(decision_id, accept=accept)
            verb = "accepted" if accept else "rejected"
            console.print(f"decision {decision_id} {verb} (target {d.get('target', '')})")
    except KeyError:
        err.print(f"[red]no pending decision {decision_id!r}[/red]")
        raise typer.Exit(1) from None


@app.command("commit", rich_help_panel=PANEL_LOOP)
def commit_tasks():
    """Commit task-file state changes (status, log lines) to the garden's git history."""
    from ..gitops import GitError, commit_task_files, is_repo

    store = _store()
    if not is_repo(store.root):
        err.print("[red]garden root is not a git repository[/red]")
        raise typer.Exit(1) from None
    try:
        committed = commit_task_files(store.root, "garden: update task state")
    except GitError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    if not committed:
        console.print("[dim]nothing to commit (task files are clean)[/dim]")
    else:
        for f in committed:
            console.print(f"committed {f}")
        n = len(committed)
        console.print(f"[green]committed {n} task file{'s' if n != 1 else ''}[/green]")


@app.command(rich_help_panel=PANEL_REVIEW)
def pr(task_id: str, url: str):
    """Attach a PR URL to a task (when the PR was opened, or reopened, by hand). Resets the
    scheduler's cached PR state so the next poll follows the new PR instead of the old one."""
    store = _store()
    t = _task(store, task_id)
    sched = _scheduler(store)
    sched.attach_pr(t, url)
    console.print(f"{t.id}: pr={url} status={t.status.value}")
