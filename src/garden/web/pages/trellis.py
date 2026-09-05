"""The Trellis: the dependency graph, drawn."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...graph import critical_path, mermaid, ready, svg, validate, visible_ids
from ..common import Site, closed_phase_keys


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/trellis", response_class=HTMLResponse)
    @app.get("/graph", response_class=HTMLResponse, include_in_schema=False)
    def trellis_page(request: Request, product: str | None = None, phase: str | None = None, closed: bool = False, hide: str | None = None):
        s = hub.fresh()
        closed_keys = closed_phase_keys(s)
        tasks = {k: v for k, v in s.tasks().items()
                 if (not product or v.product == product) and (not phase or v.phase == phase)
                 and (closed or v.key not in closed_keys or (v.product, v.phase) == (product, phase))}
        try:
            cp = critical_path(tasks)
        except Exception:  # noqa: BLE001
            cp = []
        stack = bool(s.config.get("stack", True))
        hide_done = hide == "done"
        vis = visible_ids(tasks, stack, hide_done)
        hidden_count = len(tasks) - len(visible_ids(tasks, stack, hide_done=True))
        qs = {k: v for k, v in {"product": product, "phase": phase, "closed": "1" if closed else None}.items() if v}
        show_url = "/trellis" + ("?" + urlencode(qs) if qs else "")
        hide_url = "/trellis?" + urlencode({**qs, "hide": "done"})
        return templates.TemplateResponse(request, "trellis.html", ctx(
            request, page="trellis", svg=svg(tasks, stack=stack, hide_done=hide_done), mermaid=mermaid(tasks, visible=vis), product=product, phase=phase,
            closed=closed, critical=cp, ready=[t.id for t in ready(tasks)], problems=validate(tasks),
            hide_done=hide_done, hidden_count=hidden_count, show_url=show_url, hide_url=hide_url))
