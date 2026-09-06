"""The text equivalent of the Now 2 page."""
from __future__ import annotations

import typer

from ..now2 import WINDOWS, snapshot, text_view
from .common import PANEL_BOARD, _store, app, console


@app.command(name="now", rich_help_panel=PANEL_BOARD)
def now(page: int = typer.Option(2, help="Now page (2)"), window: str = typer.Option("hour"),
        phase: str = typer.Option("")) -> None:
    """Print either available Now page as text."""
    if page == 1:
        from ..now1 import render_text
        from ..now1 import snapshot as now1_snapshot
        from .common import _scheduler

        store = _store()
        print(render_text(now1_snapshot(store, _scheduler(store), window=window)), end="")
        return
    if page != 2:
        raise typer.BadParameter("page must be 1 or 2")
    if window not in WINDOWS:
        raise typer.BadParameter("window must be hour, today, 24h or phase")
    console.print(text_view(snapshot(_store(), window, phase)), markup=False)
