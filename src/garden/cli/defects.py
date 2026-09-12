"""Record, query, and analyze defects found after task closure."""

from __future__ import annotations

import json
import uuid

import typer

from ..defects import DefectConflict, DefectStore
from .common import PANEL_QUALITY, _store, _task, app, err


@app.command("defect-record", rich_help_panel=PANEL_QUALITY)
def defect_record(
    task_id: str, severity: str = typer.Option(..., help="minor or major"),
    description: str = typer.Option(...), reporter: str = typer.Option(...),
    expected: str = typer.Option(""), observed: str = typer.Option(""),
    impact: str = typer.Option(""), evidence_link: list[str] = typer.Option([], "--evidence-link"),
    affected_source: str = typer.Option(""), affected_release: str = typer.Option(""),
    affected_run: str = typer.Option(""), follow_up: str = typer.Option(""),
    idempotency_key: str = typer.Option("", help="Stable retry key; generated when omitted"),
) -> None:
    """Record a minor or major defect without reopening its closed task."""
    store = _store()
    try:
        row, created = DefectStore(store.config.garden_dir).create(
            _task(store, task_id), severity, description, reporter,
            idempotency_key=idempotency_key or uuid.uuid4().hex, expected=expected,
            observed=observed, impact=impact, evidence_links=evidence_link,
            affected_source=affected_source, affected_release=affected_release,
            affected_run=affected_run, follow_up=follow_up,
        )
    except (ValueError, DefectConflict) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    print(json.dumps({"defect": row, "created": created}, sort_keys=True))


@app.command("defect-list", rich_help_panel=PANEL_QUALITY)
def defect_list(
    severity: str = typer.Option(""), product: str = typer.Option(""),
    phase: str = typer.Option(""), task_id: str = typer.Option("", "--task"),
    disposition: str = typer.Option(""), discovered_from: str = typer.Option(""),
    discovered_to: str = typer.Option(""),
) -> None:
    """Export filterable defect records and distinct-defect counts as JSON."""
    store = _store()
    ledger = DefectStore(store.config.garden_dir)
    filters = {"severity": severity, "product": product, "phase": phase,
               "task_id": task_id, "disposition": disposition,
               "discovered_from": discovered_from, "discovered_to": discovered_to}
    print(json.dumps({"defects": ledger.list(**filters), "summary": ledger.summary(**filters)},
                     sort_keys=True))


@app.command("defect-update", rich_help_panel=PANEL_QUALITY)
def defect_update(
    defect_id: str, expected_revision: int = typer.Option(...), actor: str = typer.Option(...),
    severity: str | None = typer.Option(None), disposition: str | None = typer.Option(None),
    description: str | None = typer.Option(None), known_facts: str | None = typer.Option(None),
    hypotheses: str | None = typer.Option(None), unknowns: str | None = typer.Option(None),
    could_have_caught: str | None = typer.Option(None), prevention: str | None = typer.Option(None),
    proposed_follow_up: str | None = typer.Option(None), follow_up: str | None = typer.Option(None),
) -> None:
    """Correct or review a defect, retaining its attributed prior values."""
    changes = {name: value for name, value in locals().items()
               if name not in {"defect_id", "expected_revision", "actor"} and value is not None}
    try:
        row = DefectStore(_store().config.garden_dir).update(
            defect_id, actor, expected_revision, **changes
        )
    except KeyError:
        err.print(f"[red]no defect {defect_id!r}[/red]")
        raise typer.Exit(1) from None
    except (ValueError, DefectConflict) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    print(json.dumps(row, sort_keys=True))
