"""The Inbox: what needs a person, and the phase burn-up."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...charts import burnup_svg, tier_bars_svg
from ...events import EventLog, digest, parse_since
from ...inbox import build_inbox
from ..common import Site, tier_rows


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/", response_class=HTMLResponse)
    @app.get("/inbox", response_class=HTMLResponse, include_in_schema=False)
    def inbox_page(request: Request):
        from ...inbox import GROUPS

        s = hub.fresh()
        sched = hub.reader()
        items = build_inbox(s, sched)
        tasks = s.tasks()
        evs = EventLog(s.config.garden_dir / "events.jsonl")
        open_tasks = [t for t in tasks.values() if not t.status.terminal and t.status.value != "cancelled"]
        in_scope = [t for t in tasks.values() if t.status.value != "cancelled"]
        spent_24h = digest(evs.read(since=parse_since("24h")))["cost_usd"]
        from ...suggestions import has_pending

        suggestions_pending = sum(1 for t in open_tasks if has_pending(t.body))
        return templates.TemplateResponse(request, "inbox.html", ctx(
            request, page="inbox", items=items, groups=GROUPS, prs_open=sum(1 for t in open_tasks if t.pr),
            spent_24h=spent_24h, suggestions_pending=suggestions_pending,
            burnup=burnup_svg(evs.read(), len(in_scope), done_ids={t.id for t in in_scope if t.status.value == 'done'}), tiers=tier_bars_svg(tier_rows(s, tasks))))
