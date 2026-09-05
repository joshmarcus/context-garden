"""The Trials leaderboard."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...trials import TrialLog, ranking_markdown
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/trials", response_class=HTMLResponse)
    def trials_page(request: Request, harness: str = "", model: str = ""):
        s = hub.fresh()
        log = TrialLog(s.config.garden_dir / "trials.jsonl")
        rows = log.leaderboard()
        trials = [(t, ranking_markdown(t)) for t in reversed(log.read())]
        if harness:
            def matches(label: str) -> bool:
                return label == f"{harness}:{model}" if model else label == harness or label.startswith(f"{harness}:")

            rows = [r for r in rows if matches(r["label"])]
            trials = [(t, md) for t, md in trials if any(matches(c["label"]) for c in t.get("contenders", []))]
        return templates.TemplateResponse(request, "trials.html", ctx(
            request, page="trials", rows=rows, trials=trials, harness_choices=s.config.harness_choices(),
            filter_harness=harness, filter_model=model))
