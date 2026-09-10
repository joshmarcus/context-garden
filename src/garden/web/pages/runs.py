"""The Runs page and one run's transcript."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...runs import RunStore
from ..artifacts import artifact_response
from ..common import Site
from .design import recorded_captures


def _is_streamed_transcript(events: list[dict[str, object]], output: str = "") -> bool:
    """Whether stdout is a turn-by-turn Claude or Codex conversation.

    The configured harness answers this before a newly-started worker has emitted its first
    event.  Event shapes retain transcripts from older records that predate the harness field.
    """
    if output in {"claude-stream-json", "codex-jsonl"}:
        return True
    for event in events:
        event_type = event.get("type")
        if event_type in {"assistant", "user", "item.completed", "item.started",
                          "thread.started", "turn.started", "turn.completed", "turn.failed"}:
            return True
    return False


def _transcript_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse Codex lifecycle updates into the latest event for each item.

    Codex emits a command item when it starts and again when it completes.  The completed
    event carries its aggregated output, so replacing the earlier event keeps the command's
    place in the conversation while rendering one command and its result.
    """
    rendered: list[dict[str, Any]] = []
    item_positions: dict[str, int] = {}
    for event in events:
        item = event.get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
        if event.get("type") in {"item.started", "item.completed"} and isinstance(item_id, str):
            position = item_positions.get(item_id)
            if position is not None:
                rendered[position] = event
                continue
            item_positions[item_id] = len(rendered)
        rendered.append(event)
    return rendered


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
        # A mechanical rebase is git-only: no harness, no model, no cost, and no transcript. Its
        # page says what it is and what git did, and finds the pre-PR check run that followed it
        # (the check runs as its own record; the nearest later `check` run is that result).
        mechanical = run.mode == "rebase" and not run.harness
        check_view = run.check_view()
        check_result = None
        if mechanical:
            later = sorted((r for r in rs.runs_for(task_id)
                            if r.mode == "check" and r.started_at >= run.started_at),
                           key=lambda r: r.started_at)
            if later:
                cr = later[0]
                check_result = {"run_id": cr.run_id, "status": cr.status,
                                "checks": (cr.result or {}).get("checks", [])}
        events = run.stdout_events(n=None)
        # Trust the configured harness before a new worker has written stdout, then retain
        # older transcript records by recognizing their Claude/Codex event envelopes.
        output = ""
        if run.harness:
            try:
                h = s.config.harness(run.harness)
                output = h.output
                if output == "claude-json" and str(h.cfg.get("output_format") or "json") == "stream-json":
                    output = "claude-stream-json"
            except Exception:  # noqa: BLE001
                pass
        is_stream = _is_streamed_transcript(events, output)
        events = _transcript_events(events)
        final_text = run.read_text("final.md")
        if not final_text:
            res = next((e for e in reversed(events) if e.get("type") == "result"), None)
            final_text = str((res or {}).get("result") or "")
        brief_text = run.read_text("brief.md")
        captures = [{"name": p.relative_to(run.path).as_posix(),
                     "href": f"/runs/{task_id}/{run_id}/captures/{p.relative_to(run.path).as_posix()}"}
                    for p in recorded_captures(run)]
        recovery = None
        if run.runner == "remote" and run.status == "running" and run.lease_expires_at:
            recovery_expires_at = run.recovery_expires_at or (
                dt.datetime.fromisoformat(run.lease_expires_at) + dt.timedelta(
                    seconds=int(s.config.get("workers.recovery_seconds", 300))
                )
            ).isoformat()
            remaining = max(0, int((dt.datetime.fromisoformat(recovery_expires_at)
                                    - dt.datetime.now(dt.UTC)).total_seconds()))
            if run.lease_expires_at <= dt.datetime.now(dt.UTC).isoformat() and remaining:
                recovery = {"remaining": remaining}
        return templates.TemplateResponse(request, "run.html", ctx(
            request, page="runs", run=run, task=task, task_id=task_id, events=events,
            is_stream=is_stream, final_text=final_text, brief_text=brief_text,
            stderr_text=run.stderr_text(), mechanical=mechanical, check_result=check_result,
            check_view=check_view, captures=captures, recovery=recovery))

    @app.get("/runs/{task_id}/{run_id}/ui/{name}")
    def run_capture(task_id: str, run_id: str, name: str):
        run = next((r for r in RunStore(hub.fresh().config.garden_dir).runs_for(task_id)
                    if r.run_id == run_id), None)
        path = run.path / "ui" / Path(name).name if run else None
        if path is None or path.resolve() not in (recorded_captures(run) if run else []):
            raise HTTPException(404)
        return artifact_response(path.read_bytes(), path.name)

    @app.get("/partials/runs/{task_id}/{run_id}/stdout", response_class=HTMLResponse)
    def run_stdout_partial(request: Request, task_id: str, run_id: str):
        s = hub.fresh()
        rs = RunStore(s.config.garden_dir)
        run = next((r for r in rs.runs_for(task_id) if r.run_id == run_id), None)
        events = _transcript_events(run.stdout_events(n=None)) if run else []
        return templates.TemplateResponse(request, "_stdout.html", ctx(request, events=events))

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        s = hub.fresh()
        rs = RunStore(s.config.garden_dir)
        return templates.TemplateResponse(request, "runs.html", ctx(
            request, page="runs", runs=list(reversed(rs.all_runs())), archive_warning=rs.archive_health(),
            events=list(reversed(hub.events))[:100]))
