"""Local web UI: a board, task pages, the graph, runs and cost. FastAPI + Jinja + HTMX.

All logic lives in store/graph/scheduler; this package only renders and forwards actions.
`create_app` builds the app and the template environment, then each page module under
`pages/` and each action module under `actions/` registers its own routes. The scheduler
loop runs in a background thread when `watch=True` (the `garden serve` default).
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..harness import DIFFICULTIES
from ..members import MemberRegistry, authorize
from ..model import PRIORITY_SCALE, STATUS_ORDER, priority_label
from ..multiplayer_client import MultiplayerUnavailable
from ..now1 import board_run_fact_html, live_clock_html
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
from ..runs import HistoryUnavailable
from ..scheduler import MULTIPLAYER_EXECUTION_UNAVAILABLE
from ..store import Store
from . import actions, pages
from .access import (
    ADMINISTRATOR_READ_PATHS,
    ADMINISTRATOR_READ_PREFIXES,
    PROJECT_COLLECTION_PATHS,
    PROJECT_COLLECTION_PREFIXES,
    loopback_listener,
    route_access,
)
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


def multiplayer_tls_files(store: Store, host: str) -> tuple[str, str] | None:
    """Validate and return TLS material required by a non-local multiplayer listener."""
    if not store.config.get("multiplayer.enabled", False) or loopback_listener(host):
        return None
    if store.config.get("multiplayer.transport", "") != "https":
        raise RuntimeError(
            "multiplayer listeners outside local development require authenticated HTTPS transport"
        )
    values = []
    for setting in ("tls_certfile", "tls_keyfile"):
        configured = str(store.config.get(f"multiplayer.{setting}", "") or "")
        path = Path(configured)
        if configured and not path.is_absolute():
            path = store.root / path
        if not configured or not path.is_file():
            raise RuntimeError(f"multiplayer HTTPS requires a readable multiplayer.{setting}")
        values.append(str(path))
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(values[0], values[1])
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError("multiplayer HTTPS certificate/key pair is invalid") from exc
    return values[0], values[1]


def create_app(store: Store, watch: bool = False, plates_dir: Path | None = None, github: Any | None = None,
               host: str = "127.0.0.1", port: int | None = None) -> FastAPI:
    """The web app. `github` is an optional stand-in for `garden.github.GitHub` that every
    scheduler the app builds will use (`garden qa` passes its pretend GitHub). `host`/`port`
    are the address `garden serve` binds to; they fix the origins a POST may come from."""
    app = FastAPI(title="context-garden")
    tls = multiplayer_tls_files(store, host)
    # A POST from another site (a page open in the same browser) is refused; see web/trust.py.
    # The allowlist is the bound address plus any web.trusted_origins; the request's Host is
    # never trusted, so a DNS-rebound page is refused even when its Host and Origin agree.
    allowed = server_origins(host, port, scheme="https" if tls else "http") + [
        str(o) for o in (store.config.get("web.trusted_origins") or [])
    ]
    tokens = [os.environ.get(str(h.get("token_env") or ""), "")
              for h in (store.config.get("workers.hosts") or [])]
    from ..hosts.registry import authenticate_worker, worker_configuration

    operator_env = str(store.config.get("web.operator_token_env") or "")
    operator_token = os.environ.get(operator_env, "") if operator_env else ""
    multiplayer = bool(store.config.get("multiplayer.enabled", False))
    registry = MemberRegistry(store.config.garden_dir) if multiplayer else None
    multiplayer_tls_files(store, host)
    require_operator_auth = multiplayer or not loopback_listener(host) or bool(store.config.get("web.worker_ingress", False))
    if require_operator_auth and not operator_token and registry is None:
        raise RuntimeError(
            "operator authentication is required for this listener; set web.operator_token_env "
            "to an environment variable containing its bearer token"
        )
    if operator_token and authenticate_worker(worker_configuration(store.config), operator_token) is not None:
        raise RuntimeError("operator and worker credentials must be different")

    def authenticate_run_credential(token: str) -> Any | None:
        """Keep member installations and legacy worker enrollments distinct."""
        principal = registry.authenticate(token) if registry else None
        if principal is not None:
            return principal
        return authenticate_worker(worker_configuration(store.config), token)

    def member_authorizer(principal: Any, method: str, path: str) -> bool:
        if method in {"GET", "HEAD", "OPTIONS"}:
            # Project visibility governs garden content, not operational configuration.
            # Keep this explicit in the route inventory so administrative read surfaces
            # cannot accidentally inherit the generic project-read policy.
            normalized = path.rstrip("/") or "/"
            if (normalized in ADMINISTRATOR_READ_PATHS
                    or normalized.startswith(ADMINISTRATOR_READ_PREFIXES)):
                return authorize(principal, "administer")
            parts = path.rstrip("/").split("/")
            project = parts[2] if len(parts) > 2 and parts[1] in {"projects", "phases"} else ""
            task_detail_roots = {"tasks", "runs", "investigations"}
            if len(parts) > 2 and parts[1] in task_detail_roots:
                task = store.tasks().get(parts[2])
                project = task.product if task else ""
            if len(parts) > 3 and parts[1] == "partials" and parts[2] in {"runs", "tasks"}:
                task = store.tasks().get(parts[3])
                project = task.product if task else ""
            if len(parts) > 4 and parts[1:3] == ["api", "operations"]:
                task = store.tasks().get(parts[3])
                project = task.product if task else ""
            collection = (normalized in PROJECT_COLLECTION_PATHS
                          or path.startswith(PROJECT_COLLECTION_PREFIXES))
            if collection and principal.project_visibility == "assigned" and not principal.projects:
                # Empty assignment is a valid idle/view-only state; projected collections
                # render empty rather than turning it into implicit garden-wide access.
                return True
            if not project and collection and principal.projects:
                project = sorted(principal.projects)[0]
            if project or collection:
                return authorize(principal, "read", project=project)
            # Route templates are inventoried below, but concrete request aliases and new
            # dynamic shapes must also fail closed. An unscoped read is garden-wide and is
            # therefore administrative regardless of all-project visibility.
            return authorize(principal, "administer")
        # Phase-wide actions belong to the explicit phase owner; garden-wide configuration
        # remains administrative, while task actions remain bound to effective ownership.
        task_id = ""
        parts = path.split("/")
        if len(parts) > 4 and parts[1] == "phases":
            return registry.authorize_phase_operation(principal, parts[2], parts[3])
        if path.startswith("/tasks/") and len(parts) > 2:
            task_id = parts[2]
        elif path.startswith("/api/control/tasks/") and len(parts) > 4:
            task_id = parts[4]
        elif path.startswith("/api/tasks/") and len(parts) > 3:
            task_id = parts[3]
        if not task_id:
            return authorize(principal, "administer")
        task = store.tasks().get(task_id)
        if task is None:
            return False
        from ..model import effective_owner

        owner = effective_owner(task, store.phase(task.product, task.phase))[0]
        return authorize(principal, "mutate_work", owner_id=owner, project=task.product)

    app.add_middleware(
        OriginCheck, allowed_origins=allowed, worker_tokens=tokens,
        worker_authenticator=authenticate_run_credential,
        operator_token="" if multiplayer else operator_token,
        require_operator_auth=require_operator_auth,
        member_authenticator=registry.authenticate if registry else None,
        member_authorizer=member_authorizer if registry else None,
    )
    background_principal = registry.authenticate(os.environ.get("GARDEN_MEMBER_CREDENTIAL", "")) if registry else None
    hub = Hub(
        store,
        watch,
        github=github,
        scheduler_blocked_reason=(
            MULTIPLAYER_EXECUTION_UNAVAILABLE
            if multiplayer and (background_principal is None or not all((
                store.config.get("multiplayer.garden_id", ""),
                store.config.get("multiplayer.coordinator_url", ""),
                store.config.get("multiplayer.member_id", ""),
                store.config.get("multiplayer.installation_id", ""),
                os.environ.get(str(store.config.get("multiplayer.credential_env", "")), ""),
            ))) else ""
        ),
    )
    app.state.hub = hub

    @app.middleware("http")
    async def request_store_snapshot(request: Request, call_next: Any) -> Response:
        """Give each response a stable discovery generation.

        Safe requests borrow the current copy-on-write generation; actions get a private
        Store because schedulers intentionally mutate their task objects before saving.
        """
        safe = request.method in {"GET", "HEAD", "OPTIONS"}
        try:
            hub.prepare_authoritative_request(mutation=not safe)
        except MultiplayerUnavailable as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        token = hub.begin_request() if safe else hub.begin_action_request()
        try:
            return await call_next(request)
        finally:
            hub.end_request(token)

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
    templates.env.globals["board_run_fact"] = lambda run: Markup(board_run_fact_html(run))
    templates.env.globals["PRIORITY_SCALE"] = PRIORITY_SCALE
    templates.env.globals["priority_label"] = priority_label
    templates.env.globals["DIFFICULTIES"] = DIFFICULTIES

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        return Response(favicon_svg(), media_type="image/svg+xml")

    @app.get("/healthz", include_in_schema=False)
    async def health() -> PlainTextResponse:
        """Process liveness only: deliberately no Store, Scheduler, or history reads."""
        return PlainTextResponse("ok")

    @app.get("/api/control/status", include_in_schema=False)
    async def control_status() -> JSONResponse:
        """Bounded incident status from the small side-store, without task/history reads."""
        from ..scheduler.state import State

        ctrl = State(store.config.garden_dir / "state.json").get("_control")
        return JSONResponse({"ok": True, "dispatch": ctrl.get("dispatch", "running"),
                             "by": ctrl.get("by", ""), "at": ctrl.get("at", ""),
                             "reason": ctrl.get("reason", "")})

    site = Site(hub, templates, plates)
    pages.register(app, site)
    actions.register(app, site)

    # Make route additions fail at construction until their authority is selected in the
    # single policy inventory. FastAPI supplies HEAD alongside GET; the GET classification
    # covers both. A mounted static application is audited as one route.
    for route in app.routes:
        path = str(getattr(route, "path", ""))
        methods = getattr(route, "methods", None)
        if methods is None:
            route_access("MOUNT", path)
            continue
        for method in methods:
            if method == "HEAD" and "GET" in methods:
                continue
            route_access(str(method), path)

    @app.exception_handler(HistoryUnavailable)
    async def unavailable_history_page(_request: Request, exc: HistoryUnavailable) -> PlainTextResponse:
        return PlainTextResponse(
            f"Run history is temporarily unavailable: {exc}. "
            "Verify or rebuild .garden/run-archive/index.json before relying on totals.",
            status_code=503,
        )

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
