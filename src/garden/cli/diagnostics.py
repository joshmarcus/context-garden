"""`garden` runs and diagnostics: runs, log, doctor, serve, tui, version, upgrade."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.table import Table

from .. import __version__
from .common import _scheduler, _store, app, console, err


# --------------------------------------------------------------------------- runs / diagnostics
@app.command()
def runs(task_id: str | None = typer.Argument(None)):
    """List runs (all, or for one task)."""
    from ..runs import RunStore

    store = _store()
    rs = RunStore(store.config.garden_dir)
    unreaped = _scheduler(store).unreaped_run_ids()
    table = Table()
    for c in ("task", "run", "mode", "status", "runner", "min", "brief tok", "in", "cache-read", "out", "cost"):
        table.add_column(c)
    for r in (rs.runs_for(task_id) if task_id else rs.all_runs()):
        status = "finished, not yet reaped" if r.run_id in unreaped else r.status
        table.add_row(r.task_id, r.run_id, r.mode, status, r.runner, f"{r.elapsed_minutes():.0f}", str(r.brief_tokens),
                      str(r.usage.get("input_tokens", "")), str(r.usage.get("cache_read_input_tokens", "")),
                      str(r.usage.get("output_tokens", "")),
                      f"${r.cost_usd:.2f}" if r.cost_usd is not None else "")
    console.print(table)


@app.command("log")
def log_(task_id: str, lines: int = typer.Option(60, "-n")):
    """Tail the latest run's output for a task."""
    from ..runs import RunStore

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
    import subprocess

    from ..github import GitHub
    from ..graph import validate as _validate
    from ..runner import get_runner

    store = _store()
    ok = True
    console.print(f"root: {store.root}")
    self_products = [n for n in (store.config.data.get("products", {}) or {}) if store.config.product_self(n)]
    wd = store.config.work_dir
    inside = wd == store.config.garden_dir or store.root in wd.parents
    if inside and self_products:
        # A self product's clone and per-task worktrees are checkouts of the garden's own
        # repo; they must not sit inside the live garden. Refuse rather than warn.
        console.print(f"work dir: {wd}  [red](inside the live garden; product {', '.join(self_products)} "
                      "is the garden's own repo — set work_dir to a path outside the live garden so its "
                      "clone and worktrees never sit inside the live checkout)[/red]")
        ok = False
    else:
        console.print(f"work dir: {wd}" + ("  [yellow](inside the garden; set work_dir to keep workers' checkouts apart)[/yellow]" if inside else ""))
    console.print(f"config: {' < '.join(store.config.sources) or 'defaults only'}" + (f"  (GARDEN_ENV={store.config.env})" if store.config.env else "  (set GARDEN_ENV=work to add garden.work.yaml)"))
    gh = GitHub(use_gh=bool(store.config.get("github.use_gh", True)))
    gh_line = f"github: {gh.describe()}"
    if gh.available:
        if gh.is_authenticated():
            login = gh.me()
            gh_line += f" as {login}" if login else ""
            console.print(gh_line)
        else:
            console.print(f"[red]{gh_line} [NOT LOGGED IN][/red]")
            ok = False
    else:
        console.print(f"[red]{gh_line}[/red]")
        ok = False
    harness_names = {str(store.config.get("harness") or "claude")} | {
        str(p.get("harness")) for p in store.config.data.get("products", {}).values() if p and p.get("harness")}
    runner_names = {str(store.config.get("runner") or "local")} | {
        str(p.get("runner")) for p in store.config.data.get("products", {}).values() if p and p.get("runner")}
    for hn in sorted(harness_names):
        h = store.config.harness(hn)
        found = shutil.which(h.bin)
        if found:
            if h.is_authenticated():
                console.print(f"harness {hn}: [green]{found}[/green]  models={h.cfg.get('models') or 'cli default'}")
            else:
                console.print(f"harness {hn}: [red]{found} [NOT LOGGED IN][/red]  models={h.cfg.get('models') or 'cli default'}")
                ok = False
        else:
            console.print(f"harness {hn}: [red]{h.bin!r} not on PATH[/red]  models={h.cfg.get('models') or 'cli default'}")
            ok = False
    git_email = ""
    git_name = ""
    try:
        git_email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        pass
    try:
        git_name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        pass
    if git_email and git_name:
        console.print(f"git identity: [green]{git_name} <{git_email}>[/green]")
    else:
        console.print("[red]git identity: missing user.name or user.email[/red]")
        ok = False
    for name in sorted(runner_names):
        try:
            cfg = dict(store.config.get("ssh", {}) or {}) if name == "ssh" else {}
            r = get_runner(name, cfg, store.config.harness(str(store.config.get("harness") or "claude")))
            probs = r.doctor()
        except Exception as e:  # noqa: BLE001
            probs = [str(e)]
        console.print(f"runner {name}: " + ("[green]ok[/green]" if not probs else "[red]" + "; ".join(probs) + "[/red]"))
        ok = ok and not probs
    from ..scheduler import State
    ctrl = State(store.config.garden_dir / "state.json").get("_control")
    mp_live = (ctrl.get("overrides") or {}).get("max_parallel")
    mp = mp_live if mp_live is not None else store.config.get("max_parallel")
    review_parallel = store.config.get("review_parallel") or store.config.get("max_parallel")
    console.print(f"review pass: {'on' if store.config.get('review.enabled') else 'off'} (max {store.config.get('review.max_rounds')} rounds)  max_parallel={mp}"
                 + (f" (live override; garden.yaml: {store.config.get('max_parallel')})" if mp_live is not None else "")
                 + f"  review_parallel={review_parallel}")
    notify_cmd = store.config.get("notify.command")
    if not notify_cmd:
        console.print("[yellow]notify: not configured (set notify.command in garden.yaml so a human "
                      "gets pinged when a task needs one)[/yellow]")
    else:
        from ..notify import notify_test

        result = notify_test(store.config.data)
        if result is not None and result[0]:
            console.print(f"notify: [green]configured, test ok[/green]  command={notify_cmd!r}")
        else:
            detail = result[1] if result else "unknown error"
            console.print(f"notify: [red]configured but the test run failed ({detail})[/red]  command={notify_cmd!r}")
            ok = False
    if ctrl.get("dispatch") == "paused":
        at = ctrl.get("at", "")
        by = ctrl.get("by", "")
        reason = ctrl.get("reason", "")
        msg = f"dispatch paused (by {by} at {at[11:16]}"
        if reason:
            msg += f": {reason}"
        msg += ")"
        console.print(f"[yellow]{msg}[/yellow]")
    for p in store.products():
        repo = store.config.product_repo(p.name)
        is_self = store.config.product_self(p.name)
        tag = "  [cyan](self: the garden's own repo; tasks land as PRs to the garden)[/cyan]" if is_self else ""
        console.print(f"product {p.name}: repo={repo} phases={len(p.phases)} tasks={sum(len(ph.tasks) for ph in p.phases)}{tag}")
        if isinstance(repo, Path) and not (repo / ".git").exists():
            console.print(f"  [red]{repo} is not a git repo[/red]")
            ok = False
        if is_self and isinstance(repo, Path) and repo == store.root:
            console.print(f"  [red]self product {p.name} points at the live garden itself; set repo to the "
                          "garden's origin (a URL, or a separate clone) so a worker edits a fresh checkout, "
                          "never the live garden[/red]")
            ok = False
    from ..gitops import identity as _clone_identity

    repos_dir = store.config.repos_dir
    if repos_dir.is_dir():
        for clone in sorted(repos_dir.iterdir()):
            if not (clone / ".git").exists():
                continue
            name, email = _clone_identity(clone)
            if not name or not email:
                console.print(f"[red]clone {clone.name}: missing git identity ({clone}) — set "
                              "git.user_name / git.user_email in garden.yaml, or the garden checkout's own "
                              "git config, so a commit inside it never fails with "
                              "\"Author identity unknown\"[/red]")
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

    from ..web.app import create_app

    store = _store()
    uvicorn.run(create_app(store, watch=watch_), host=host, port=port, log_level="warning")


@app.command()
def tui():
    """Terminal UI."""
    from ..tui.app import GardenTUI

    GardenTUI(_store()).run()


@app.command()
def version():
    """The tool version and, for a pinned git install, the installed commit."""
    from ..upgrade import installed_commit

    sha = installed_commit()
    print(f"{__version__} ({sha[:12]})" if sha else __version__)


@app.command()
def upgrade(
    restart: bool = typer.Option(False, "--restart", help="Re-exec `garden serve` on success (usually the running loop does this)"),
    force: bool = typer.Option(False, "--force", help="Reinstall even if no upgrade is recorded, using the tool product's base sha"),
):
    """Move the pinned tool install forward onto a merged commit (see `garden status`)."""
    store = _store()
    sched = _scheduler(store)
    if force and not sched.upgrade_available():
        product = store.config.tool_product()
        if not product:
            err.print("[red]no product has provides_tool: true[/red]")
            raise typer.Exit(1) from None
        from ..model import Task

        probe = Task(path=store.root, id=f"_{product}", title="", product=product, phase="")
        try:
            sched._note_tool_upgrade(probe)
            sched.state.save()
        except Exception as e:  # noqa: BLE001
            err.print(f"[red]could not resolve the tool sha: {e}[/red]")
            raise typer.Exit(1) from None
    if not sched.upgrade_available():
        console.print("[green]up to date[/green] — no tool upgrade recorded")
        return
    info = sched.upgrade_available()
    console.print(f"upgrading to {str(info['sha'])[:12]} ...")
    result = sched.upgrade(restart=restart)
    if result.get("ok"):
        console.print(f"[green]installed {str(info['sha'])[:12]}[/green]"
                      + ("; restarting" if result.get("restarted") else "; restart `garden serve` to run the new code"))
    else:
        err.print(f"[red]upgrade failed: {result.get('reason')}[/red]; the current install is unchanged")
        if result.get("output"):
            print(str(result["output"])[-2000:])
        raise typer.Exit(1) from None
