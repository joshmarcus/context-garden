"""The Board: every task by column, or as a list, with a live partial."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..common import Site, board_view


def register(app: FastAPI, site: Site) -> None:
    templates, ctx = site.templates, site.ctx
    board_data, _board_view = site.board_data, board_view

    @app.get("/board", response_class=HTMLResponse)
    def board(request: Request, product: str | None = None, phase: str | None = None, closed: bool = False, view: str | None = None):
        return templates.TemplateResponse(request, "board.html", ctx(request, page="board", view=_board_view(view), **board_data(product, phase, closed)))

    @app.get("/partials/board", response_class=HTMLResponse)
    def board_partial(request: Request, product: str | None = None, phase: str | None = None, closed: bool = False, view: str | None = None):
        return templates.TemplateResponse(request, "_board.html", ctx(request, view=_board_view(view), **board_data(product, phase, closed)))
