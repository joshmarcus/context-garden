"""`garden` runs and diagnostics: runs, log, doctor, serve, tui, version, upgrade, canary."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.table import Table

from .common import PANEL_BOARD, PANEL_DIAG, _scheduler, _store, app, console, err


# --------------------------------------------------------------------------- runs / diagnostics
@app.command(rich_help_panel=PANEL_BOARD)
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


@app.command("log", rich_help_panel=PANEL_BOARD)
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


@app.command(rich_help_panel=PANEL_DIAG)
def doctor():
    """Check config, tools (agent harness, gh/token), repos and the task graph."""
    import subprocess

    from ..github import GitHub
    from ..graph import validate as _validate
    from ..runner import get_runner
    from ..runner.base import scrubbed_env

    store = _store()
    failures: list[str] = []

    def fail(name: str) -> None:
        failures.append(name)

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
        fail("work dir")
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
            console.print(f"[red]{gh_line} [NOT LOGGED IN][/red]  (fix: run `gh auth login`, or set GITHUB_TOKEN)")
            fail("github")
    else:
        console.print(f"[red]{gh_line}[/red]")
        fail("github")
    harness_names = {str(store.config.get("harness") or "claude")} | {
        str(p.get("harness")) for p in store.config.data.get("products", {}).values() if p and p.get("harness")}
    runner_names = {str(store.config.get("runner") or "local")} | {
        str(p.get("runner")) for p in store.config.data.get("products", {}).values() if p and p.get("runner")}
    for hn in sorted(harness_names):
        h = store.config.harness(hn)
        found = shutil.which(h.bin)
        if found:
            # Check login through the same scrubbed environment a worker gets (runner.base.
            # scrubbed_env), not doctor's own shell: a harness reachable there is what
            # actually dispatches. A trivial one-line prompt, not an "auth status" probe, so
            # a custom harness with no such subcommand is checked the same way.
            ok, detail = h.check_login(scrubbed_env(store.config.data))
            if ok:
                console.print(f"harness {hn}: [green]{found}[/green]  models={h.cfg.get('models') or 'cli default'}")
            else:
                fix = detail or f"run {h.bin}'s login command"
                console.print(f"harness {hn}: [red]{found} [NOT LOGGED IN][/red]  models={h.cfg.get('models') or 'cli default'}"
                              f"  (fix: {fix})")
                fail(f"harness {hn}")
        else:
            console.print(f"harness {hn}: [red]{h.bin!r} not on PATH[/red]  models={h.cfg.get('models') or 'cli default'}"
                          f"  (fix: install {h.bin} and add it to PATH)")
            fail(f"harness {hn}")
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
        console.print("[red]git identity: missing user.name or user.email[/red]  (fix: run "
                      "`git config --global user.name \"Your Name\"` and `git config --global "
                      "user.email you@example.com`, or set git.user_name / git.user_email in garden.yaml)")
        fail("git identity")
    for name in sorted(runner_names):
        try:
            cfg = dict(store.config.get("ssh", {}) or {}) if name == "ssh" else {}
            r = get_runner(name, cfg, store.config.harness(str(store.config.get("harness") or "claude")))
            probs = r.doctor()
        except Exception as e:  # noqa: BLE001
            probs = [str(e)]
        console.print(f"runner {name}: " + ("[green]ok[/green]" if not probs else "[red]" + "; ".join(probs) + "[/red]"))
        if probs:
            fail(f"runner {name}")
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
        from ..notify import notify_test, unquoted_message_warning

        result = notify_test(store.config.data)
        if result is not None and result[0]:
            console.print(f"notify: [green]configured, test ok[/green]  command={notify_cmd!r}")
        else:
            detail = result[1] if result else "unknown error"
            console.print(f"notify: [red]configured but the test run failed ({detail})[/red]  command={notify_cmd!r}"
                          "  (fix: fix or replace notify.command in garden.yaml)")
            fail("notify")
        warning = unquoted_message_warning(str(notify_cmd))
        if warning:
            console.print(f"[yellow]notify: {warning}[/yellow]")
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
            console.print(f"  [red]{repo} is not a git repo[/red]  (fix: run `git init` there, or point "
                          f"products.{p.name}.repo at an existing clone)")
            fail(f"product {p.name} repo")
        if is_self and isinstance(repo, Path) and repo == store.root:
            console.print(f"  [red]self product {p.name} points at the live garden itself; set repo to the "
                          "garden's origin (a URL, or a separate clone) so a worker edits a fresh checkout, "
                          "never the live garden[/red]")
            fail(f"product {p.name} self-repo")
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
                fail(f"clone {clone.name} identity")
    problems = _validate(store.tasks())
    for pr_ in problems:
        console.print(f"[red]graph: {pr_}[/red]  (fix: correct or remove the offending depends_on)")
    if problems:
        fail("graph")
    console.print("[green]all good[/green]" if not failures else f"[yellow]failed: {', '.join(failures)}[/yellow]")
    raise typer.Exit(0 if not failures else 1)


@app.command(rich_help_panel=PANEL_DIAG)
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8765),
    watch_: bool = typer.Option(True, "--watch/--no-watch", help="Run the scheduler loop inside the server"),
):
    """Local web UI (and, by default, the scheduler loop)."""
    import copy

    import uvicorn

    from ..web.app import create_app

    store = _store()
    # `log_level="warning"` used to also silence uvicorn.access (it logs at INFO), so a 500
    # never showed which request caused it. Quiet uvicorn's own chatter but keep every
    # request line — method, path, status — reaching the journal (or the serve log).
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["loggers"]["uvicorn"]["level"] = "WARNING"
    log_config["loggers"]["uvicorn.error"]["level"] = "WARNING"
    uvicorn.run(create_app(store, watch=watch_, host=host, port=port), host=host, port=port, log_config=log_config)


@app.command(rich_help_panel=PANEL_DIAG)
def tui():
    """Terminal UI."""
    from ..tui.app import GardenTUI

    GardenTUI(_store()).run()


@app.command(rich_help_panel=PANEL_DIAG)
def version():
    """The tool version and, for a pinned git install, the installed commit."""
    from .common import version_string

    print(version_string())


@app.command(rich_help_panel=PANEL_DIAG)
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


@app.command()
def canary(
    sha: str = typer.Argument("", help="git commit to install and check (default: the pending tool upgrade)"),
    url: str = typer.Option("", "--url", help="git URL or local path to install from (default: the tool product's repo)"),
    out: Path | None = typer.Option(None, "--out", help="Run directory (default: .garden/canary/<sha>, else a temp dir)"),
    keep: bool = typer.Option(False, "--keep", help="Keep the throwaway venv and gardens"),
    skip_install: bool = typer.Option(False, "--skip-install", help="Check the current build instead of installing a pin"),
    self_check: bool = typer.Option(False, "--self-check", hidden=True, help="Run the checks in this interpreter (used inside the throwaway venv)"),
):
    """Check a freshly-pinned build before trusting it with real PRs: install it into a throwaway
    venv and drive the scripted QA flows plus a stacked-PR and a merge-queue scenario against an
    in-memory GitHub that behaves like the real one. Exits non-zero on any failure. Run it before
    moving the pin (see the garden-operate skill)."""
    from ..canary import run_canary

    store = None
    try:
        from ..store import Store

        store = Store()
    except (FileNotFoundError, ValueError):
        store = None
    in_process = self_check or skip_install
    if not in_process and not sha and store is not None:
        pending = _scheduler(store).upgrade_available()
        if pending:
            sha = str(pending.get("sha") or "")
            url = url or str(pending.get("url") or "")
    if not in_process and sha and not url and store is not None:
        product = store.config.tool_product()
        if product:
            url = str(store.config.product_repo(product))
    if not in_process and not sha:
        err.print("[red]no build to check: pass a SHA, or --skip-install to check the current build[/red]")
        raise typer.Exit(2) from None
    if out is None:
        import tempfile

        if store is not None and sha:
            out = store.config.garden_dir / "canary" / sha[:12]
        else:
            out = Path(tempfile.mkdtemp(prefix="garden-canary-"))
    report = run_canary(sha, url=url, out=out, keep=keep, skip_install=in_process,
                        log=lambda m: err.print(f"[dim]{m}[/dim]"))
    console.print(report.summary(), markup=False, highlight=False, soft_wrap=True)
    if not report.ok:
        raise typer.Exit(1)
