"""Local web UI: a board, task pages, the graph, runs and cost. FastAPI + Jinja + HTMX.

All logic lives in store/graph/scheduler; this package only renders and forwards actions.
`create_app` builds the app and the template environment, then each page module under
`pages/` and each action module under `actions/` registers its own routes. The scheduler
loop runs in a background thread when `watch=True` (the `garden serve` default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..harness import DIFFICULTIES
from ..model import PRIORITY_SCALE, STATUS_ORDER, priority_label
from ..now1 import live_clock_html
from ..plants import (
    DEFS,
    PLATE_CREDIT,
    favicon_svg,
    mark_svg,
    plant_info,
    plant_svg,
    plate_filename,
    stage_svg,
    stage_word,
    vine_svg,
)
from ..store import Store
from . import actions, pages
from .common import COLUMNS, LIST_ORDER, LOGGER, PLATES_DIR, TEMPLATES, Hub, Site, render_md
from .trust import OriginCheck, safe_json, server_origins

__all__ = ["Hub", "Site", "create_app", "render_md"]


def _tojson(value: Any) -> Markup:
    """The `tojson` filter: an Undefined value (a page that forgot to pass the context key a
    `data-*` attribute serialises) becomes `null` instead of crashing `json.dumps` with
    "Object of type Undefined is not JSON serializable" — the incident CG-185 fixes. Every
    known call site also passes an explicit `|default(...)`, and the strict template
    environment below should already have raised before a bare Undefined reaches here; this
    is the last line of defense for a site neither of those catches."""
    if isinstance(value, jinja2.Undefined):
        value = None
    return Markup(safe_json(value))


def create_app(store: Store, watch: bool = False, plates_dir: Path | None = None, github: Any | None = None,
               host: str = "127.0.0.1", port: int | None = None) -> FastAPI:
    """The web app. `github` is an optional stand-in for `garden.github.GitHub` that every
    scheduler the app builds will use (`garden qa` passes its pretend GitHub). `host`/`port`
    are the address `garden serve` binds to; they fix the origins a POST may come from."""
    app = FastAPI(title="context-garden")
    # A POST from another site (a page open in the same browser) is refused; see web/trust.py.
    # The allowlist is the bound address plus any web.trusted_origins; the request's Host is
    # never trusted, so a DNS-rebound page is refused even when its Host and Origin agree.
    allowed = server_origins(host, port) + [str(o) for o in (store.config.get("web.trusted_origins") or [])]
    app.add_middleware(OriginCheck, allowed_origins=allowed)
    hub = Hub(store, watch, github=github)
    app.state.hub = hub
    templates = Jinja2Templates(directory=str(TEMPLATES))
    # A missing context key reads as an error, not a silent falsy: the one incident this
    # caught (CG-185) was a `tojson` site fed an Undefined because its page forgot to pass
    # the value. Every legitimate "not set on this task/event/item" case already reads a
    # real value (see `_TaskState.__missing__`, `events.Event.__missing__`, the tojson
    # sites' `|default(...)`); an exception that does escape is caught by
    # `unhandled_error_page` below and shown as a flash, not a traceback.
    templates.env.undefined = jinja2.StrictUndefined
    templates.env.filters["md"] = render_md
    templates.env.filters["tojson"] = _tojson
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
    templates.env.globals["mark"] = lambda *a, **k: Markup(mark_svg(*a, **k))
    # A running run's elapsed time as the markup the clock in base.html ticks (the Board's
    # running cards, a task page's run row): trusted markup built from the run record.
    templates.env.globals["live_clock"] = lambda run: Markup(live_clock_html(run))
    templates.env.globals["PRIORITY_SCALE"] = PRIORITY_SCALE
    templates.env.globals["priority_label"] = priority_label
    templates.env.globals["DIFFICULTIES"] = DIFFICULTIES

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        return Response(favicon_svg(), media_type="image/svg+xml")

    site = Site(hub, templates, plates)
    pages.register(app, site)
    actions.register(app, site)

    @app.exception_handler(Exception)
    async def unhandled_error_page(request: Request, exc: Exception) -> Any:
        """A page that raises while rendering (a template error, or anything else an action's
        own try/except in `actions/tasks.py` does not already catch) shows the person the same
        flash the action routes use, with the header and navigation still up, instead of a bare
        Internal Server Error. The traceback and the request path go to the log either way."""
        LOGGER.exception("unhandled error rendering %s %s", request.method, request.url.path)
        try:
            context = site.ctx(request, page="", flash="Something went wrong rendering this page; the error is in the log.")
            return templates.TemplateResponse(request, "error.html", context, status_code=500)
        except Exception:
            LOGGER.exception("also failed to render the error page for %s", request.url.path)
            return PlainTextResponse("Internal Server Error", status_code=500)

    return app
