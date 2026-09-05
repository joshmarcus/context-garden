"""Local web UI: a board, task pages, the graph, runs and cost. FastAPI + Jinja + HTMX.

All logic lives in store/graph/scheduler; this package only renders and forwards actions.
`create_app` builds the app and the template environment, then each page module under
`pages/` and each action module under `actions/` registers its own routes. The scheduler
loop runs in a background thread when `watch=True` (the `garden serve` default).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..model import PRIORITY_SCALE, STATUS_ORDER, priority_label
from ..plants import (
    DEFS,
    PLATE_CREDIT,
    plant_info,
    plant_svg,
    plate_filename,
    stage_svg,
    stage_word,
    vine_svg,
)
from ..store import Store
from . import actions, pages
from .common import COLUMNS, LIST_ORDER, PLATES_DIR, TEMPLATES, Hub, Site, render_md

__all__ = ["Hub", "Site", "create_app", "render_md"]


def create_app(store: Store, watch: bool = False, plates_dir: Path | None = None, github: Any | None = None) -> FastAPI:
    """The web app. `github` is an optional stand-in for `garden.github.GitHub` that every
    scheduler the app builds will use (`garden qa` passes its pretend GitHub)."""
    app = FastAPI(title="context-garden")
    hub = Hub(store, watch, github=github)
    app.state.hub = hub
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["md"] = render_md
    templates.env.filters["tojson"] = lambda v: Markup(json.dumps(v))
    templates.env.globals["columns"] = COLUMNS
    templates.env.globals["list_order"] = LIST_ORDER
    templates.env.globals["statuses"] = STATUS_ORDER
    templates.env.globals["DEFS"] = DEFS
    templates.env.globals["VINE"] = Markup(vine_svg())
    plates = plates_dir or PLATES_DIR
    plates.mkdir(parents=True, exist_ok=True)
    app.mount("/static/plates", StaticFiles(directory=str(plates)), name="plates")

    def plate_url(key: str, thumb: bool = False) -> str:
        """The scanned plate for a plant when it has been fetched, else '' (the drawing is used)."""
        name = plate_filename(key, thumb=thumb)
        if (plates / name).exists():
            return f"/static/plates/{name}"
        if thumb and (plates / plate_filename(key)).exists():
            return f"/static/plates/{plate_filename(key)}"
        return ""

    templates.env.globals["plate"] = plate_url
    templates.env.globals["PLATE_CREDIT"] = PLATE_CREDIT
    # The drawings are trusted SVG built from fixed symbols; mark them safe so Jinja does not escape them.
    templates.env.globals["plant"] = lambda *a, **k: Markup(plant_svg(*a, **k))
    templates.env.globals["stage"] = lambda *a, **k: Markup(stage_svg(*a, **k))
    templates.env.globals["stage_word"] = stage_word
    templates.env.globals["plant_info"] = plant_info
    templates.env.globals["PRIORITY_SCALE"] = PRIORITY_SCALE
    templates.env.globals["priority_label"] = priority_label

    site = Site(hub, templates, plates)
    pages.register(app, site)
    actions.register(app, site)
    return app
