"""Shared pieces for the `garden` CLI: the Typer app, the consoles and the small helpers
every command family reuses (store/scheduler/task lookups, status styling, target parsing).

The CLI is split by command family — one module per family under `garden.cli`, each defining
its commands against the single `app` here. `garden.cli.__init__` imports them so their
`@app.command()` decorators register, then re-exports `app`/`main`."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(help="Tend a context garden: plan tasks, dispatch agents, track PRs.", no_args_is_help=True, pretty_exceptions_enable=False)
console = Console()
err = Console(stderr=True)


# The help panels `garden --help` groups its commands under. One vocabulary, one place: every
# command names its panel from this list so the ~50 commands read as a handful of groups
# instead of a flat wall. Rich shows the panels in the order their first command registers.
PANEL_SETUP = "Setting up"
PANEL_PHASES = "Phases"
PANEL_BOARD = "Seeing the board"
PANEL_PLAN = "Planning work"
PANEL_LOOP = "Running the loop"
PANEL_DECIDE = "Needs you"
PANEL_REVIEW = "PRs and review"
PANEL_INSIGHT = "Cost and insight"
PANEL_QUALITY = "Quality and retros"
PANEL_DIAG = "Diagnostics"


def version_string() -> str:
    """The tool version, with the installed commit for a pinned git install. One place so
    `garden version` and `garden --version` agree."""
    from .. import __version__
    from ..upgrade import installed_commit

    sha = installed_commit()
    return f"{__version__} ({sha[:12]})" if sha else __version__


def _version_callback(value: bool) -> None:
    if value:
        print(version_string())
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(  # noqa: ARG001 - consumed by the eager callback before the command runs
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show the tool version and exit."),
) -> None:
    """Tend a context garden: plan tasks, dispatch agents, track PRs."""


def _store(root: Path | None = None):
    from ..store import Store

    try:
        s = Store(root)
        s.tasks()  # fail fast on broken task files
        return s
    except (FileNotFoundError, ValueError) as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None


def _scheduler(store):
    from ..scheduler import Scheduler

    return Scheduler(store, log=lambda m: err.print(f"[dim]{m}[/dim]"))


def _task(store, task_id: str):
    try:
        return store.task(task_id)
    except KeyError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


def _phase(store, product: str, phase: str):
    try:
        return store.phase(product, phase)
    except KeyError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


def _split_target(target: str) -> tuple[str, str]:
    if "/" not in target:
        err.print("[red]expected product/phase[/red]")
        raise typer.Exit(1) from None
    product, phase = target.split("/", 1)
    return product, phase.strip("/")


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
    "wont_do": "tan",
    "cancelled": "dim strike",
}


def _style(status: str, text: str | None = None) -> str:
    style = STATUS_STYLE.get(status) or "default"
    return f"[{style}]{text if text is not None else status}[/{style}]"
