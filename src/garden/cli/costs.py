"""`garden costs`: the same spend-over-time aggregation as the `/costs` page, printed."""

from __future__ import annotations

import json

import typer
from rich import box
from rich.table import Table

from .. import operator_spend as ops
from ..costs import BUCKET_CHOICES, GROUP_BY_CHOICES, cost_series
from ..events import EventLog, parse_since
from ..runs import RunStore
from .common import PANEL_INSIGHT, _store, app, console


@app.command(rich_help_panel=PANEL_INSIGHT)
def costs(
    since: str = typer.Option("", "--since", help="24h, 3d, an ISO timestamp, or empty for all time"),
    bucket: str = typer.Option("day", "--bucket", help="day | hour"),
    by: str = typer.Option("activity", "--by", help="activity | difficulty | model | harness | pool_member | phase | task | session"),
    difficulty: str = typer.Option("", "--difficulty", help="easy | medium | hard"),
    model: str = typer.Option("", "--model"),
    harness: str = typer.Option("", "--harness"),
    phase: str = typer.Option("", "--phase", help="product/phase"),
    product: str = typer.Option("", "--product"),
    task: str = typer.Option("", "--task", help="a single task id"),
    session: str = typer.Option("", "--session", help="an operator session id"),
    json_out: bool = typer.Option(False, "--json"),
    backfill: bool = typer.Option(False, "--backfill",
                                  help="recompute cost_usd for existing codex runs from their stored transcripts, then exit"),
) -> None:
    """Spend over time, sliced one way and filtered by the rest — the same numbers /costs shows."""
    store = _store()
    if backfill:
        events = EventLog(store.config.garden_dir / "events.jsonl")
        updated = RunStore(store.config.garden_dir).backfill_codex_costs(store.config, events)
        console.print(f"[green]backfilled cost_usd for {updated} codex run(s)[/green]")
        return
    if bucket not in BUCKET_CHOICES:
        console.print(f"[red]--bucket must be one of {', '.join(BUCKET_CHOICES)}[/red]")
        raise typer.Exit(2)
    if by not in GROUP_BY_CHOICES:
        console.print(f"[red]--by must be one of {', '.join(GROUP_BY_CHOICES)}[/red]")
        raise typer.Exit(2)
    tasks = store.tasks()
    events = EventLog(store.config.garden_dir / "events.jsonl").read()
    events += ops.to_cost_events(ops.read_records(ops.default_path(store.root)))
    series = cost_series(events, tasks, since=parse_since(since) if since else "", bucket=bucket, group_by=by,
                         difficulty=difficulty, model=model, harness=harness, phase=phase, product=product, task=task,
                         session=session)
    if json_out:
        print(json.dumps(series, indent=2))
        return
    table = Table(title=f"Cost by {by}", box=box.SIMPLE_HEAD)
    table.add_column(by)
    table.add_column("runs", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("mean/run", justify="right")
    table.add_column("share", justify="right")
    for g in series["groups"]:
        row = series["totals"][g]
        table.add_row(g, str(row["runs"]), f"${row['cost_usd']:.2f}",
                      f"${row['mean_cost_usd']:.2f}" if row["mean_cost_usd"] is not None else "",
                      f"{row['share'] * 100:.0f}%" if row["share"] is not None else "")
    console.print(table)
    grand = series["grand_total"]
    console.print(f"[dim]grand total: ${grand['cost_usd']:.2f} over {grand['runs']} run(s), bucketed by {bucket}[/dim]")
