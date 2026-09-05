"""The Configuration page."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...scheduler import State
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request):
        s = hub.fresh()
        cfg = s.config
        sched = hub.reader()
        effective = {
            "review_parallel": sched.review_parallel_limit(),
            "auto_dispatch": cfg.get("auto_dispatch"),
            "auto_revise": cfg.get("auto_revise"),
            "tick_interval": cfg.get("tick_interval"),
            "review.enabled": cfg.get("review.enabled"),
            "review.max_rounds": cfg.get("review.max_rounds"),
            "review.difficulty": cfg.get("review.difficulty") or "(task tier)",
            "github.draft_pr": cfg.get("github.draft_pr"),
            "stack": cfg.get("stack"),
        }
        budgets = dict(cfg.get("budgets") or {})
        for pname, pdata in (cfg.data.get("products") or {}).items():
            if isinstance(pdata, dict) and pdata.get("budget_usd"):
                budgets.setdefault(pname, pdata["budget_usd"])
        # runtime overrides from state.json (set via the phase page or `garden budget`) win
        overrides = dict(State(cfg.garden_dir / "state.json").get("_budgets"))
        budgets.update(overrides)
        return templates.TemplateResponse(request, "config.html", ctx(
            request, page="config", sources=cfg.sources, effective=effective, budgets=budgets,
            budget_overrides=sorted(overrides),
            max_parallel_file=cfg.get("max_parallel"), max_parallel_override=sched.overrides().get("max_parallel")))
