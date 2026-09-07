"""The pages (GET routes), one module per page or page family, each with a
`register(app, site)` that adds its routes. Templates live in `../templates`."""

from __future__ import annotations

from fastapi import FastAPI

from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    from . import (
        api,
        board,
        config,
        costs,
        design,
        events,
        inbox,
        now1,
        now2,
        phase,
        runs,
        task,
        trellis,
        trials,
    )

    for module in (now1, now2, inbox, board, task, runs, design, trellis, trials, events, phase, costs, config, api):
        module.register(app, site)
