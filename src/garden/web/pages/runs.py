"""The Runs page and one run's transcript."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...runs import RunStore
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/runs/{task_id}/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, task_id: str, run_id: str):
        s = hub.fresh()
        rs = RunStore(s.config.garden_dir)
        run = next((r for r in rs.runs_for(task_id) if r.run_id == run_id), None)
        if not run:
            raise HTTPException(404)
        try:
            task = s.task(task_id)
        except KeyError:
            task = None
        events = run.stdout_events(n=None)
        # A streamed transcript is claude's stream-json (assistant/user turns + a final result);
        # plain claude-json is one result object with no turns. Trust the harness config when it
        # is known (so a just-started run tails before its first event), and fall back to sniffing
        # the events for older runs whose harness is unrecorded.
        is_stream = any(e.get("type") in ("assistant", "user") for e in events)
        if not is_stream and run.harness:
            try:
                h = s.config.harness(run.harness)
                is_stream = h.output == "claude-json" and str(h.cfg.get("output_format") or "json") == "stream-json"
            except Exception:  # noqa: BLE001
                pass
        final_path = run.path / "final.md"
        final_text = final_path.read_text() if final_path.exists() else ""
        if not final_text:
            res = next((e for e in reversed(events) if e.get("type") == "result"), None)
            final_text = str((res or {}).get("result") or "")
        brief_path = run.path / "brief.md"
        brief_text = brief_path.read_text() if brief_path.exists() else ""
        return templates.TemplateResponse(request, "run.html", ctx(
            request, page="runs", run=run, task=task, task_id=task_id, events=events,
            is_stream=is_stream, final_text=final_text, brief_text=brief_text,
            stderr_text=run.stderr_text()))

    @app.get("/partials/runs/{task_id}/{run_id}/stdout", response_class=HTMLResponse)
    def run_stdout_partial(request: Request, task_id: str, run_id: str):
        s = hub.fresh()
        rs = RunStore(s.config.garden_dir)
        run = next((r for r in rs.runs_for(task_id) if r.run_id == run_id), None)
        events = run.stdout_events(n=None) if run else []
        return templates.TemplateResponse(request, "_stdout.html", ctx(request, events=events))

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        s = hub.fresh()
        rs = RunStore(s.config.garden_dir)
        return templates.TemplateResponse(request, "runs.html", ctx(
            request, page="runs", runs=list(reversed(rs.all_runs())), events=list(reversed(hub.events))[:100]))
