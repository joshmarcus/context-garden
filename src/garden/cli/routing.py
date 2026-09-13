"""Read-only worker routing explanations."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..routing import task_routing_view, worker_configuration_views
from .common import PANEL_DIAG, _store, _task, app, console


@app.command("route-explain", rich_help_panel=PANEL_DIAG)
def route_explain(
    task_id: str,
    activity: str = typer.Option("work", "--activity"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    """Explain a task's current worker route without creating or claiming a run."""
    store = _store(root)
    if activity not in {"work", "review", "check", "persona", "edit", "trial"}:
        raise typer.BadParameter("unsupported routing activity", param_hint="--activity")
    console.print_json(json.dumps(task_routing_view(store, _task(store, task_id), activity=activity)))


@app.command("worker-configurations", rich_help_panel=PANEL_DIAG)
def worker_configurations(root: Path | None = typer.Option(None, "--root")) -> None:
    """Show trusted worker profiles, cached readiness, and reserved capacity."""
    console.print_json(json.dumps(worker_configuration_views(_store(root))))
