"""Auditable HTTP route classes and listener exposure policy."""

from __future__ import annotations

import re

PUBLIC_READ = "public_read"
WORKER_PROTOCOL = "worker_protocol"
OPERATOR_READ = "operator_read"
OPERATOR_MUTATION = "operator_mutation"

PUBLIC_PATHS = frozenset({"/healthz", "/favicon.svg"})
ADMINISTRATOR_READ_PATHS = frozenset({
    "/api/control/status", "/api/maintenance", "/api/worker-diagnostics", "/api/workers",
    "/config", "/design", "/docs", "/docs/oauth2-redirect", "/now/workers",
    "/openapi.json", "/redoc", "/trials",
})
ADMINISTRATOR_READ_PREFIXES = ("/design/",)
# Authenticated members may open these project collections.  Their handlers must project
# every row and aggregate through ``Site.allowed_projects``; admission alone is not a data
# boundary. Garden-operational endpoints which cannot be attributed to a project stay admin.
PROJECT_COLLECTION_PATHS = frozenset({
    "/", "/api/decisions", "/api/defects", "/api/tasks", "/board", "/costs", "/events", "/graph",
    "/herbarium", "/inbox", "/now", "/now1", "/now2", "/now/stream", "/partials/board",
    "/runs", "/trellis",
})
PROJECT_COLLECTION_PREFIXES = ("/partials/now/",)
WORKER_PATHS = frozenset({
    "/api/runs/claim", "/api/runs/{run_id}/heartbeat", "/api/runs/{run_id}/finish",
})
OPERATOR_READ_PATHS = frozenset({
    "/", "/api/control/status", "/api/decisions", "/api/defects", "/api/events", "/api/maintenance",
    "/api/operations/{task_id}/{run_id}", "/api/tasks", "/api/workers", "/board", "/config",
    "/api/worker-diagnostics",
    "/costs", "/design", "/design/{path:path}", "/docs", "/docs/oauth2-redirect", "/events", "/graph", "/herbarium",
    "/inbox", "/investigations/{task_id}/{run_id}/{name}", "/now", "/now/stream", "/now/workers",
    "/now1", "/now2", "/openapi.json", "/partials/board",
    "/partials/now/strip/{task_id}/{run_id}", "/partials/now/{region}",
    "/partials/runs/{task_id}/{run_id}/stdout", "/partials/tasks/{task_id}/runs",
    "/partials/tasks/{task_id}/stdout", "/phases/{product}/{phase}",
    "/phases/{product}/{phase}/doc/{name:path}", "/phases/{product}/{phase}/retro", "/redoc", "/runs",
    "/runs/{task_id}/{run_id}", "/runs/{task_id}/{run_id}/captures/{path:path}",
    "/runs/{task_id}/{run_id}/ui/{name}", "/tasks/{task_id}", "/tasks/{task_id}/brief",
    "/tasks/{task_id}/log", "/tasks/{task_id}/packet", "/trellis", "/trials",
})
OPERATOR_MUTATION_PATHS = frozenset({
    "/api/control/tasks/{task_id}/launch", "/api/tasks/{task_id}/manual-mode", "/config/accept-reload",
    "/api/tasks/{task_id}/defects", "/api/tasks/{task_id}/defects/{defect_id}",
    "/config/max-parallel", "/config/observe-profile", "/config/operating-profile", "/config/save",
    "/decisions/{decision_id}/{action}", "/friction-report", "/investigations", "/maintenance/pause",
    "/maintenance/resume", "/pause", "/phases/{product}/{phase}/approve-all",
    "/phases/{product}/{phase}/budget", "/phases/{product}/{phase}/close",
    "/phases/{product}/{phase}/kickoff", "/phases/{product}/{phase}/new-task",
    "/phases/{product}/{phase}/persona", "/phases/{product}/{phase}/plan",
    "/phases/{product}/{phase}/retro-decide", "/resume", "/tasks/{task_id}/brief",
    "/tasks/{task_id}/{action}", "/tasks/{task_id}/defects", "/tick", "/upgrade",
})


def route_access(method: str, path: str) -> str:
    """Classify a declared route, failing closed when a registrar adds a new one."""
    if method == "MOUNT" and path == "/static/plates":
        return PUBLIC_READ
    policies = (
        ("GET", PUBLIC_PATHS, PUBLIC_READ),
        ("POST", WORKER_PATHS, WORKER_PROTOCOL),
        ("GET", OPERATOR_READ_PATHS, OPERATOR_READ),
        ("POST", OPERATOR_MUTATION_PATHS, OPERATOR_MUTATION),
        ("PATCH", OPERATOR_MUTATION_PATHS, OPERATOR_MUTATION),
    )
    for expected_method, paths, access in policies:
        if method == expected_method and path in paths:
            return access
    raise RuntimeError(f"web route is not classified: {method} {path}")


def loopback_listener(host: str) -> bool:
    """Whether a bind is confined to the local host (`testserver` is TestClient's sentinel)."""
    return (host or "").strip().lower() in {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}


def request_access(method: str, path: str) -> str:
    """Classify a concrete request using the same four route-policy classes."""
    method = method.upper()
    if method in {"GET", "HEAD", "OPTIONS"} and (
        path in PUBLIC_PATHS or path.startswith("/static/plates/")
    ):
        return PUBLIC_READ
    if method == "POST" and (
        path == "/api/runs/claim" or re.fullmatch(r"/api/runs/[^/]+/(?:heartbeat|finish)", path)
    ):
        return WORKER_PROTOCOL
    return OPERATOR_READ if method in {"GET", "HEAD", "OPTIONS"} else OPERATOR_MUTATION
