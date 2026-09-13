"""`garden` runs and diagnostics: runs, log, doctor, serve, tui, version, upgrade, canary."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path

import typer
from rich.table import Table

from .common import PANEL_BOARD, PANEL_DIAG, PANEL_LOOP, _scheduler, _store, app, console, err


@app.command(rich_help_panel=PANEL_DIAG)
def worker(
    garden: str = typer.Option(..., "--garden", help="Garden web app URL."),
    host: str = typer.Option(..., "--host", help="Configured worker host name."),
    token_env: str = typer.Option("GARDEN_WORKER_TOKEN", help="Environment variable holding the bearer token."),
    work_dir: Path = typer.Option(Path(".garden-worker"), help="Clone and scratch directory."),
    harness: list[str] = typer.Option([], "--harness"),
    tier: list[str] = typer.Option(["easy", "medium", "hard"], "--tier"),
    capacity: int = typer.Option(1, min=1),
    once: bool = typer.Option(False, "--once"),
    doctor: bool = typer.Option(False, "--doctor"),
    repo: str = typer.Option("", "--repo"),
    setup_command: str = typer.Option("", "--setup-command", help="Host-owned product setup command."),
):
    """Claim and execute runs from a garden on this independent host."""
    from ..remote_worker import doctor_worker, run_worker

    token = os.environ.get(token_env, "")
    offered = harness or [name for name in ("claude", "codex") if shutil.which(name)]
    if doctor:
        problems = doctor_worker(token, repo, offered)
        if problems:
            for problem in problems:
                err.print(f"[red]{problem}[/red]")
            raise typer.Exit(1)
        console.print("[green]worker host ok: token present, git access and harnesses available[/green]")
        return
    if not token:
        err.print(f"[red]{token_env} is not set[/red]")
        raise typer.Exit(2)
    run_worker(garden, host, token, work_dir.resolve(), offered, tier, capacity, once,
               setup_command=setup_command)


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


@app.command("set-validation-capacity", rich_help_panel=PANEL_DIAG)
def set_validation_capacity(
    limit: int = typer.Argument(..., min=0, help="New per-user heavy-validation capacity."),
):
    """Change the shared heavy-validation capacity after all gardens are idle."""
    from ..run_supervisor import reset_authoritative_limit

    try:
        old, new = reset_authoritative_limit(limit)
    except RuntimeError as exc:
        err.print(f"[red]validation capacity unchanged: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"shared validation capacity changed from {old} to {new}")


@app.command("archive-runs", rich_help_panel=PANEL_DIAG)
def archive_runs(
    older_than_days: int = typer.Option(30, min=1, help="Archive terminal runs finished before this many days ago."),
    apply: bool = typer.Option(False, "--apply", help="Apply the previewed archive migration."),
    limit: int = typer.Option(100, min=1, help="Maximum runs to move or resume per invocation."),
):
    """Preview or compact old terminal runs into the lossless history archive.

    Running, unreaped, and run ids still referenced by recovery state are always retained.
    The operation is atomic per run and safe to retry; its index is rebuilt and verified
    on every invocation.
    """
    from ..runs import HistoryUnavailable, RunStore
    from ..scheduler import StateCorruptionError

    store = _store()
    rs = RunStore(store.config.garden_dir)
    # Scheduler construction reads run history, which deliberately fails closed while
    # an interrupted transaction is pending. Repair that single transaction first so
    # the documented apply command can reach the normal locked reference analysis.
    if apply:
        try:
            rs.repair_pending_archive()
        except (OSError, ValueError, HistoryUnavailable) as exc:
            err.print(f"[red]archive maintenance stopped safely: {exc}[/red]")
            raise typer.Exit(2) from None
    scheduler = _scheduler(store)
    before = dt.datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)

    def reference_analysis():
        store.invalidate_tasks()
        current_tasks = store.tasks()
        scheduler.state = type(scheduler.state)(scheduler.state.path)
        state_text = json.dumps(scheduler.state.data)
        current_records = rs.all_runs()
        current_protected = {run.run_id for run in current_records if run.run_id in state_text}
        current_protected.update(scheduler.unreaped_run_ids())
        current_protected.update(
            run.run_id for run in current_records
            if run.task_id in current_tasks and not current_tasks[run.task_id].status.terminal
        )
        return current_tasks, current_protected

    with scheduler.tick_lock():
        # Freeze task finalization for the reference snapshot used by the preview.
        try:
            tasks, protected = reference_analysis()
            preview = rs.archive_preview(before, protected, limit=limit)
            fence_report = scheduler.fence_history_report(apply=False, limit=limit)
        except (OSError, ValueError, json.JSONDecodeError, HistoryUnavailable,
                StateCorruptionError) as exc:
            err.print(f"[red]archive reference analysis failed; nothing deleted: {exc}[/red]")
            raise typer.Exit(2) from None
        console.print(
            f"eligible: {preview['eligible_runs']} run(s), {preview['eligible_bytes']} logical bytes; "
            f"estimated stored bytes: {preview['estimated_stored_bytes']}; "
            f"skipped: {preview['skipped']}"
        )
        console.print(f"fence history: {fence_report}")
        if not apply:
            console.print("preview only; pass --apply to archive and compact these runs")
            return
        # A first migration creates CAS directories during unlocked preparation. Publish
        # the valid empty ledger first so concurrent history readers never mistake those
        # prepared (but not yet referenced) blobs for an index-less archive.
        if not (rs.archive_dir / "index.json").exists():
            rs.rebuild_archive_index()

    # Hashing, gzip compression, fsync and byte verification can be lengthy. They retain
    # the live originals and deliberately run without the scheduler tick lock. The actual
    # move below compares all source identities and repeats the authoritative reference
    # analysis while the scheduler is briefly stopped.
    min_free_mb = int(store.config.get("doctor.min_free_mb", 2048) or 0)
    try:
        prepared = rs.prepare_terminal_archive(
            before, protected, limit=limit, min_free_bytes=min_free_mb * 1024 * 1024
        )
        prepared_state = scheduler.state.prepare_completed(
            {task.id for task in tasks.values() if task.status.terminal},
            limit=limit,
            min_free_bytes=min_free_mb * 1024 * 1024,
        )
    except (OSError, ValueError, HistoryUnavailable) as exc:
        err.print(f"[red]archive preparation stopped safely: {exc}[/red]")
        raise typer.Exit(2) from None

    with scheduler.tick_lock():
        try:
            tasks, protected = reference_analysis()
            moved = rs.commit_terminal_archive(prepared, before, protected)
            state_report = scheduler.state.commit_completed(
                prepared_state, {task.id for task in tasks.values() if task.status.terminal}
            )
            fence_report = scheduler.fence_history_report(apply=True, limit=limit)
        except (OSError, ValueError, HistoryUnavailable, StateCorruptionError) as exc:
            err.print(f"[red]archive maintenance stopped safely: {exc}[/red]")
            raise typer.Exit(2) from None
    try:
        rs.retire_prepared_archive(prepared)
    except (OSError, ValueError, HistoryUnavailable) as exc:
        err.print(f"[red]archive verification stopped safely; originals retained: {exc}[/red]")
        raise typer.Exit(2) from None
    console.print(
        f"archived {moved} terminal run(s) finished before {before.isoformat()} to "
        f"{rs.archive_dir}; retained {len(protected)} recovery-referenced run(s)"
    )
    console.print(f"archive storage: {rs.archive_report()}")
    console.print(f"scheduler state history: {state_report}; fence history: {fence_report}")


@app.command("cleanup-branches", rich_help_panel=PANEL_DIAG)
def cleanup_branches(
    apply: bool = typer.Option(False, "--apply", help="Delete candidates after guarded rechecks."),
    limit: int = typer.Option(20, min=1, help="Maximum removable branches to process."),
):
    """Preview Garden-owned worker branches and their retention reasons."""
    store = _store()
    scheduler = _scheduler(store)
    rows = scheduler.branch_cleanup_inventory()
    table = Table("repository", "branch", "head", "classification", "reason")
    for row in rows:
        table.add_row(row.product, row.branch, (row.remote_head or row.local_head)[:12],
                      row.classification, row.reason)
    console.print(table)
    if apply:
        from ..scheduler import State
        from ..scheduler.report import TickReport

        with scheduler.tick_lock():
            store.invalidate_tasks()
            scheduler.state = State(scheduler.state.path)
            results = scheduler.sweep_worker_branches(TickReport(), limit=limit)
            scheduler.state.save()
        for result in results:
            console.print(f"{result['branch']}: {result['outcome']}")


@app.command("cleanup-storage", rich_help_panel=PANEL_DIAG)
def cleanup_storage(
    apply: bool = typer.Option(False, "--apply", help="Remove eligible items after guarded rechecks."),
    limit: int = typer.Option(20, min=1, help="Maximum directories/caches to process."),
):
    """Inventory Garden-owned local storage and optionally sweep eligible leftovers.

    Preview is the default. Active, dirty, unique, external and recovery-owned work is
    retained with a concrete reason; each invocation writes a durable JSON audit record.
    """
    from ..scheduler import State
    from ..scheduler.report import TickReport

    store = _store()
    scheduler = _scheduler(store)
    with scheduler.tick_lock():
        store.invalidate_tasks()
        scheduler.state = State(scheduler.state.path)
        report = scheduler.sweep_storage(TickReport(), apply=apply, limit=limit)
        scheduler.state.save()
    table = Table("category", "bytes", "eligible", "path", "reason")
    for row in report["inventory"]["items"]:
        table.add_row(str(row["category"]), str(row["bytes"]), "yes" if row["eligible"] else "no",
                      str(row["path"]), str(row["reason"]))
    console.print(table)
    space = report["inventory"]["space"]
    console.print(f"guest free: {space.get('guest_free_bytes')} bytes; "
                  f"host free: {space.get('host_free_bytes')} bytes ({space.get('host_reason')})")
    console.print(f"reclaimed {report['bytes_reclaimed']} bytes; audit: {report['audit_path']}")


@app.command("restore-run", rich_help_panel=PANEL_DIAG)
def restore_run(task_id: str, run_id: str):
    """Restore one archived run to the active run directory for recovery work."""
    from ..runs import RunStore

    rs = RunStore(_store().config.garden_dir)
    if not rs.restore_archived(task_id, run_id):
        err.print("[red]archived run not found, or an active record already exists[/red]")
        raise typer.Exit(1) from None
    console.print(f"restored {task_id}/{run_id} to {rs.dir}")


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
    session = r.env_snapshot.get("ssh_tmux_session")
    if session:
        console.print(f"On remote host {r.host}: tmux attach-session -r -t {session}")
        state_path = r.path / "ssh-state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            console.print(f"SSH: {state.get('status', 'unknown')} {state.get('reason', '')}")
    final = r.read_text("final.md")
    if final:
        console.print("[bold]final message:[/bold]")
        print("\n".join(final.splitlines()[-lines:]))
    stderr = r.stderr_text()
    if stderr.strip():
        console.print("[bold]stderr:[/bold]")
        print("\n".join(stderr.splitlines()[-lines:]))
    if r.error:
        console.print(f"[red]error:[/red] {r.error}")


@app.command("ssh-recover", rich_help_panel=PANEL_DIAG)
def ssh_recover(task_id: str):
    """Resume bounded collection of the same remote run, without launching a worker."""
    scheduler = _scheduler(_store())
    with scheduler.tick_lock():
        task = scheduler.store.task(task_id)
        scheduler.resume_ssh_collection(task)
    console.print(f"{task_id}: resumed remote collection; no new implementation launched")


@app.command(rich_help_panel=PANEL_DIAG)
def doctor():
    """Check config, tools (agent harness, gh/token), repos and the task graph."""
    import subprocess

    from ..github import GitHub
    from ..graph import validate as _validate
    from ..host_identity import tracked_connection_target_fields
    from ..runner import BUILTIN_NAMES, adapter_registration_problem, get_runner
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
    min_free_mb = int(store.config.get("doctor.min_free_mb", 2048) or 0)
    for label, path in (("work dir", wd), ("/tmp", Path("/tmp"))):
        try:
            disk_path = path if path.exists() else path.parent
            free_mb = shutil.disk_usage(disk_path).free // (1024 * 1024)
            warning = f"  [yellow](below doctor.min_free_mb={min_free_mb} MB)[/yellow]" if free_mb < min_free_mb else ""
            console.print(f"free space {label}: {free_mb} MB{warning}")
        except OSError as e:
            console.print(f"[yellow]free space {label}: unavailable ({e})[/yellow]")
    console.print(f"config: {' < '.join(store.config.sources) or 'defaults only'}" + (f"  (GARDEN_ENV={store.config.env})" if store.config.env else "  (set GARDEN_ENV=work to add garden.work.yaml)"))
    connection_fields = tracked_connection_target_fields(store.root)
    if connection_fields:
        console.print("[red]host identities: tracked configuration contains connection targets at "
                      + ", ".join(connection_fields)
                      + " (use a logical alias here and put its target in ignored garden.local.yaml)[/red]")
        fail("host identities")
    browser_authorized = store.config.browser_enabled() or any(
        store.config.browser_enabled(product.name) for product in store.products()
    )
    if not browser_authorized:
        console.print("browser: not checked (disabled by policy; set browser.enabled: true to opt in)")
    else:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch()
                browser.close()
            console.print("browser: [green]Chromium available[/green]")
        except Exception as exc:  # noqa: BLE001 - doctor reports missing package, binary, or libs alike
            console.print("[yellow]browser: enabled but unavailable; provision the optional "
                          f"walkthrough dependency and Chromium runtime [{type(exc).__name__}][/yellow]")
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
    for product in (store.config.data.get("products") or {}):
        route = store.config.product_github(str(product))
        if not isinstance(store.config.product(str(product)).get("github"), dict):
            continue
        enterprise = GitHub(use_gh=bool(store.config.get("github.use_gh", True)), host=route["host"],
                            api_base=route.get("api_base", ""), token_env=route.get("token_env", ""))
        line = f"github ({product}): {enterprise.describe()}"
        if enterprise.available and enterprise.is_authenticated() and enterprise.me():
            console.print(f"{line} as {enterprise.me()}")
        else:
            console.print(f"[red]{line} [NOT LOGGED IN][/red]")
            fail("github")
    harness_names = {str(store.config.get("harness") or "claude")} | {
        str(p.get("harness")) for p in store.config.data.get("products", {}).values() if p and p.get("harness")}
    if "openrouter" in (store.config.data.get("harnesses") or {}):
        harness_names.add("openrouter")
    runner_names = {str(store.config.get("runner") or "local")} | {
        str(p.get("runner")) for p in store.config.data.get("products", {}).values() if p and p.get("runner")}
    for hn in sorted(harness_names):
        h = store.config.harness(hn)
        if h.api_key_env and not os.environ.get(h.api_key_env):
            console.print(
                f"harness {hn}: [red]{h.api_key_env} is not set[/red]  "
                f"models={h.cfg.get('models') or 'cli default'}"
            )
            fail(f"harness {hn}")
            continue
        found = shutil.which(h.bin)
        if found:
            # Check login through the same scrubbed environment a worker gets (runner.base.
            # scrubbed_env), not doctor's own shell: a harness reachable there is what
            # actually dispatches. A trivial one-line prompt, not an "auth status" probe, so
            # a custom harness with no such subcommand is checked the same way.
            try:
                worker_environment = scrubbed_env(store.config.data)
                if h.api_key_env:
                    worker_environment[h.api_key_env] = os.environ[h.api_key_env]
            except Exception as exc:  # policy errors are reported without source paths/content
                console.print(f"harness {hn}: [red]worker configuration unavailable[/red] "
                              f"({type(exc).__name__}: {exc})")
                fail(f"harness {hn}")
                continue
            ok, detail = h.check_login(worker_environment)
            if ok:
                console.print(f"harness {hn}: [green]{found}[/green]  models={h.cfg.get('models') or 'cli default'}")
            else:
                console.print(f"harness {hn}: [red][NOT LOGGED IN][/red]  "
                              f"(fix: run {h.bin}'s login command)  "
                              f"models={h.cfg.get('models') or 'cli default'}  {found}")
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
            cfg = dict(store.config.get("ssh" if name == "ssh" else "workers", {}) or {}) \
                if name in {"ssh", "remote"} else {}
            if name == "remote":
                from ..hosts.registry import worker_configuration

                cfg = worker_configuration(store.config)
            cfg["worker_env"] = dict(store.config.get("worker_env") or {})
            adapters = store.config.get("runner_adapters") or {}
            registration = adapters.get(name) if isinstance(adapters, dict) else None
            if registration is not None:
                # Private modules can execute arbitrary code at import time.  Doctor is a
                # read-only controller diagnostic, so it only checks the declarative
                # registration; runner construction validates the runtime contract later.
                problem = adapter_registration_problem(name, registration)
                probs = [problem] if problem else []
                deferred = name not in BUILTIN_NAMES
            else:
                cfg["_runner_adapters"] = dict(adapters) if isinstance(adapters, dict) else adapters
                r = get_runner(name, cfg, store.config.harness(str(store.config.get("harness") or "claude")))
                probs = r.doctor()
                deferred = False
        except Exception as e:  # noqa: BLE001
            probs = [str(e)]
            deferred = False
        status = "[yellow]registration syntax ok; runtime validation deferred until dispatch[/yellow]" if deferred else (
            "[green]ok[/green]" if not probs else "[red]" + "; ".join(probs) + "[/red]"
        )
        console.print(f"runner {name}: {status}")
        if probs:
            fail(f"runner {name}")
    from ..scheduler import State
    ctrl = State(store.config.garden_dir / "state.json").get("_control")
    mp_live = (ctrl.get("overrides") or {}).get("max_parallel")
    mp = mp_live if mp_live is not None else store.config.get("max_parallel")
    review_parallel = store.config.get("review_parallel") or store.config.get("max_parallel")
    review_cap = store.config.review_max_rounds()
    cap_label = str(review_cap) if review_cap is not None else "unlimited"
    console.print(f"review pass: {'on' if store.config.get('review.enabled') else 'off'} (max {cap_label} rounds)  max_parallel={mp}"
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
    dups = store.duplicate_ids()
    for tid, paths in sorted(dups.items()):
        console.print(f"[red]duplicate task id {tid}: claimed by {', '.join(paths)}[/red]  "
                      "(fix: rename or remove one; both are quarantined from dispatch until then)")
    if dups:
        fail("duplicate ids")
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
    public_projection: Path | None = typer.Option(
        None, "--public-projection", help="Serve only an exported public projection directory.",
    ),
):
    """Local web UI (and, by default, the scheduler loop)."""
    import copy

    import uvicorn

    from ..web.app import create_app, multiplayer_tls_files

    if public_projection is not None:
        from ..web.public import create_public_app

        if watch_:
            err.print("[red]public projection serving requires --no-watch[/red]")
            raise typer.Exit(2)
        uvicorn.run(create_public_app(public_projection), host=host, port=port, log_level="warning")
        return

    store = _store()
    # `log_level="warning"` used to also silence uvicorn.access (it logs at INFO), so a 500
    # never showed which request caused it. Quiet uvicorn's own chatter but keep every
    # request line — method, path, status — reaching the journal (or the serve log).
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["loggers"]["uvicorn"]["level"] = "WARNING"
    log_config["loggers"]["uvicorn.error"]["level"] = "WARNING"
    tls = multiplayer_tls_files(store, host)
    uvicorn.run(
        create_app(store, watch=watch_, host=host, port=port),
        host=host,
        port=port,
        log_config=log_config,
        ssl_certfile=tls[0] if tls else None,
        ssl_keyfile=tls[1] if tls else None,
    )


@app.command("publish", rich_help_panel=PANEL_DIAG)
def publish_public_projection(
    output: Path = typer.Option(..., "--output", help="Isolated directory to replace with public data."),
):
    """Export the explicitly allowlisted anonymous garden projection."""
    from ..publication import write_public_projection

    path = write_public_projection(_store(), output.resolve())
    console.print(f"public projection written to {path}")


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


@app.command(rich_help_panel=PANEL_LOOP)
def pin(
    sha: str = typer.Argument(..., help="git commit to canary, install, and run"),
    url: str = typer.Option("", "--url", help="git URL or local path for the tool product"),
):
    """Canary a commit, then queue it for the running scheduler's next tick boundary."""
    from ..canary import run_canary

    store = _store()
    if not url:
        product = store.config.tool_product()
        if product:
            url = str(store.config.product_repo(product))
    if not url:
        err.print("[red]no product has provides_tool: true; pass --url[/red]")
        raise typer.Exit(1) from None
    report = run_canary(sha, url=url, out=store.config.garden_dir / "canary" / sha[:12],
                        log=lambda m: err.print(f"[dim]{m}[/dim]"))
    console.print(report.summary(), markup=False, highlight=False, soft_wrap=True)
    if not report.ok:
        raise typer.Exit(1)
    sched = _scheduler(store)
    result = sched.pin(sha, url, product=store.config.tool_product() or "")
    if not result.get("ok"):
        err.print(f"[red]pin failed: {result.get('reason')}[/red]; the current install is unchanged")
        raise typer.Exit(1)
    console.print(f"[green]queued {sha[:12]}[/green]; the scheduler installs and restarts after its current tick")


@app.command(rich_help_panel=PANEL_DIAG)
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
