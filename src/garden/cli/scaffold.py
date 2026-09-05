"""`garden` commands that scaffold and gate phases: init, new-product, new-phase, plants,
new-task, close-phase, reopen-phase, freeze, unfreeze."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from .common import _phase, _scheduler, _split_target, _store, _style, app, console, err


# --------------------------------------------------------------------------- init / scaffold
@app.command()
def init(
    directory: Path = typer.Argument(Path("."), help="Directory to turn into a garden"),
    name: str = typer.Option("garden", help="Garden name"),
):
    """Create garden.yaml and a principles digest in DIRECTORY."""
    from ..personas import write_default_personas
    from ..scaffold import init_garden

    created = init_garden(directory.resolve(), name) + write_default_personas(directory.resolve())
    for p in created:
        console.print(f"created {p}")
    console.print("Next: `garden new-product <name>` then `garden new-phase <product> <phase>`.")


@app.command("new-product")
def new_product(name: str, repo: str = typer.Option(".", help="Path (relative to garden) or URL of the code repo"),
                base_branch: str = typer.Option("main")):
    """Scaffold <name>/product.md and register it in garden.yaml."""
    from ..scaffold import new_product as _np

    store = _store()
    for p in _np(store, name, repo, base_branch):
        console.print(f"created {p}")


@app.command("new-phase")
def new_phase(product: str, phase: str, plant: str = typer.Option("", help="Botanical emblem: pea|bramble|foxglove|fern|poppy (default: next unused)")):
    """Scaffold <product>/<phase>/{goals.md,specs/,tasks/}; assigns the phase its plant."""
    from ..scaffold import new_phase as _nph

    store = _store()
    try:
        created = _nph(store, product, phase, plant=plant)
    except ValueError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    for p in created:
        console.print(f"created {p}")
    store.invalidate()
    try:
        ph = store.phase(product, phase)
        console.print(f"plate {ph.plate} · {ph.latin} ({ph.common})")
    except KeyError:
        pass


@app.command()
def plants(
    fetch: bool = typer.Option(False, "--fetch", help="Download the scanned plates (Thomé, 1885, public domain) from Wikimedia Commons into the web UI's static plates directory"),
    height: int = typer.Option(900, help="Pixel height of the prepared plates"),
):
    """The botanical key: which plant each phase carries, whether its scanned plate is present, and the growth-stage names."""
    from ..plants import PLANTS, STAGE_WORD, plant_info, plate_filename
    from ..web.app import PLATES_DIR

    plates_dir = PLATES_DIR
    if fetch:
        try:
            from ..platefetch import fetch_all

            rows = fetch_all(plates_dir, height=height, log=lambda m: console.print(f"[dim]{m}[/dim]"))
        except ImportError as e:
            err.print(f"[red]{e}[/red]\nPillow is needed: pip install 'context-garden[plates]'")
            raise typer.Exit(1) from None
        except Exception as e:  # noqa: BLE001 - network, API or image errors, all reported the same way
            err.print(f"[red]could not fetch plates: {e}[/red]")
            raise typer.Exit(1) from None
        console.print(f"wrote {len(rows)} plate(s) and SOURCES.md to {plates_dir}")
    store = _store()
    table = Table(title="plates")
    for c in ("product/phase", "plate", "plant", "latin", "scan", "note"):
        table.add_column(c)
    for prod in store.products():
        for ph in prod.phases:
            info = plant_info(ph.plant)
            scan = "yes" if (plates_dir / plate_filename(ph.plant)).exists() else "drawing"
            table.add_row(ph.key, ph.plate, ph.plant, info["latin"], scan, info["note"])
    console.print(table)
    if not any((plates_dir / plate_filename(p["key"])).exists() for p in PLANTS):
        console.print("[dim]no scanned plates yet: `garden plants --fetch` downloads them (needs network access to Wikimedia Commons)[/dim]")
    console.print("seed packet (unassigned, in order): " + ", ".join(p["latin"] for p in PLANTS))
    console.print("stages: " + " · ".join(f"{k} = {v}" for k, v in STAGE_WORD.items() if k != "blocked"))


@app.command("new-task")
def new_task(
    target: str = typer.Argument(..., help="product/phase"),
    title: str = typer.Argument(...),
    depends_on: list[str] = typer.Option([], "--dep", "-d"),
    reading: list[str] = typer.Option([], "--read", "-r"),
    priority: int = typer.Option(3),
    difficulty: str = typer.Option("medium", help="easy|medium|hard (picks the model tier)"),
    ready: bool = typer.Option(False, help="Create as ready instead of draft"),
    reopen: bool = typer.Option(False, "--reopen", help="Reopen a closed phase to take this task"),
    from_retro: str = typer.Option("", "--from-retro", help="Mark this task as arising from a phase's retro (product/phase); it shows on that phase's retro page"),
):
    """Create a task file from a template."""
    from ..scaffold import TASK_TEMPLATE

    store = _store()
    product, phase = _split_target(target)
    ph = _phase(store, product, phase)
    discovered_from = ""
    if from_retro:
        rp, rn = _split_target(from_retro)
        _phase(store, rp, rn)  # refuse an unknown phase up front, not at page-render time
        discovered_from = f"retro:{rp}/{rn}"
    if ph.closed:
        if not reopen:
            err.print(f"[red]{ph.key} is closed ({ph.closed}); pass --reopen or run `garden reopen-phase {ph.key}` first[/red]")
            raise typer.Exit(1) from None
        _set_phase_closed(store, ph, "")
        console.print(f"{ph.key} reopened")
    t = store.create_task(product, phase, title, TASK_TEMPLATE, depends_on=depends_on, reading=reading,
                          priority=priority, status="ready" if ready else "draft", difficulty=difficulty,
                          discovered_from=discovered_from)
    console.print(f"created {t.id} at {store.rel(t.path)}")


def _set_phase_closed(store, ph, closed: str) -> None:
    """Write or clear `closed:` in goals.md and record the event."""
    sched = _scheduler(store)
    if closed:
        sched.close_phase(ph, force=True, date=closed)
    else:
        sched.reopen_phase(ph)


@app.command("close-phase")
def close_phase(
    target: str = typer.Argument(..., help="product/phase"),
    force: bool = typer.Option(False, "--force", help="Close even with open tasks"),
):
    """Close a phase once every task is done or cancelled: it leaves the rail and joins the herbarium."""
    import datetime as _dt

    store = _store()
    product, phase = _split_target(target)
    ph = _phase(store, product, phase)
    if ph.closed:
        console.print(f"{ph.key} is already closed ({ph.closed})")
        return
    open_tasks = [t for t in ph.tasks if not t.status.terminal]
    if open_tasks and not force:
        err.print(f"[red]{ph.key} still has {len(open_tasks)} open task(s):[/red]")
        for t in open_tasks:
            err.print(f"  {t.id}  {_style(t.status.value)}  {t.title}")
        err.print("finish or cancel them, or close anyway with --force")
        raise typer.Exit(1) from None
    date = _dt.date.today().isoformat()
    _set_phase_closed(store, ph, date)
    console.print(f"{ph.key} closed ({date}); it now appears in the herbarium")


@app.command("reopen-phase")
def reopen_phase(target: str = typer.Argument(..., help="product/phase")):
    """Reopen a closed phase: it returns to the rail and can take work again."""
    store = _store()
    product, phase = _split_target(target)
    ph = _phase(store, product, phase)
    if not ph.closed:
        err.print(f"[yellow]{ph.key} is not closed[/yellow]")
        raise typer.Exit(1) from None
    _set_phase_closed(store, ph, "")
    console.print(f"{ph.key} reopened")


def _set_phase_frozen(store, ph, frozen: str) -> None:
    """Write or clear `frozen:` in goals.md and record the event."""
    from ..events import EventLog

    store.set_phase_frozen(ph, frozen)
    log = EventLog(store.config.garden_dir / "events.jsonl")
    if frozen:
        log.emit("phase_frozen", "", phase=ph.key, frozen=frozen)
    else:
        log.emit("phase_unfrozen", "", phase=ph.key)


@app.command()
def freeze(target: str = typer.Argument(..., help="product/phase")):
    """Freeze a phase: approve and dispatch refuse its tasks until unfrozen, unless a task
    carries a freeze exception."""
    import datetime as _dt

    store = _store()
    product, phase = _split_target(target)
    ph = _phase(store, product, phase)
    if ph.closed:
        err.print(f"[red]{ph.key} is closed; nothing to freeze[/red]")
        raise typer.Exit(1) from None
    if ph.frozen:
        console.print(f"{ph.key} is already frozen ({ph.frozen})")
        return
    date = _dt.date.today().isoformat()
    _set_phase_frozen(store, ph, date)
    console.print(f"{ph.key} frozen ({date}); approve/dispatch now refuse its tasks without a freeze exception")


@app.command()
def unfreeze(target: str = typer.Argument(..., help="product/phase")):
    """Clear a phase's freeze."""
    store = _store()
    product, phase = _split_target(target)
    ph = _phase(store, product, phase)
    if not ph.frozen:
        err.print(f"[yellow]{ph.key} is not frozen[/yellow]")
        raise typer.Exit(1) from None
    _set_phase_frozen(store, ph, "")
    console.print(f"{ph.key} unfrozen")
