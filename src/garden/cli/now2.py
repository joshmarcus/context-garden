"""The text equivalent of the Now 2 page."""
from __future__ import annotations

import typer

from ..now2 import WINDOWS, snapshot, text_view
from .common import PANEL_BOARD, _store, app, console
from .views import render_now1


@app.command(name="now", rich_help_panel=PANEL_BOARD)
def now(page: int = typer.Option(1, help="Now page (1 or 2)"), window: str = typer.Option("hour"),
        phase: str = typer.Option("")) -> None:
    """Print either available Now page as text."""
    if window not in WINDOWS:
        raise typer.BadParameter("window must be hour, today, 24h or phase")
    if page == 1:
        render_now1(window)
        return
    if page != 2:
        raise typer.BadParameter("page must be 1 or 2")
    console.print(text_view(snapshot(_store(), window, phase)), markup=False)
