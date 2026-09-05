"""The pages (GET routes), one module per page or page family, each with a
`register(app, site)` that adds its routes. Templates live in `../templates`."""

from __future__ import annotations

from fastapi import FastAPI

from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    from . import api, board, config, events, inbox, phase, runs, task, trellis, trials

    for module in (inbox, board, task, runs, trellis, trials, events, phase, config, api):
        module.register(app, site)
