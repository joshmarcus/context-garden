"""The text equivalent of the Now 2 page."""
from __future__ import annotations

import typer

from ..now2 import WINDOWS, snapshot, text_view
from .common import PANEL_BOARD, _store, app, console


@app.command(name="now", rich_help_panel=PANEL_BOARD)
def now(page: int = typer.Option(2, help="Now page (2)"), window: str = typer.Option("hour"),
        phase: str = typer.Option("")) -> None:
    """Live work, next queues, phase specimens and last-period metrics in text."""
    if page != 2:
        raise typer.BadParameter("This build provides page 2")
    if window not in WINDOWS:
        raise typer.BadParameter("window must be hour, today, 24h or phase")
    console.print(text_view(snapshot(_store(), window, phase)), markup=False)
