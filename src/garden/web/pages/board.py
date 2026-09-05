"""The Board: every task by column, as a list, or as a per-phase backlog, with a live partial."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..common import Site, board_view


def register(app: FastAPI, site: Site) -> None:
    templates, ctx = site.templates, site.ctx
    _board_view = board_view

    def _data(view: str, product: str | None, phase: str | None, closed: bool) -> dict:
        if view == "backlog":
            return site.backlog_data(product, closed)
        return site.board_data(product, phase, closed)

    @app.get("/board", response_class=HTMLResponse)
    def board(request: Request, product: str | None = None, phase: str | None = None, closed: bool = False, view: str | None = None):
        v = _board_view(view)
        return templates.TemplateResponse(request, "board.html", ctx(request, page="board", view=v, **_data(v, product, phase, closed)))

    @app.get("/partials/board", response_class=HTMLResponse)
    def board_partial(request: Request, product: str | None = None, phase: str | None = None, closed: bool = False, view: str | None = None):
        v = _board_view(view)
        return templates.TemplateResponse(request, "_board.html", ctx(request, view=v, **_data(v, product, phase, closed)))
