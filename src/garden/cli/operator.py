"""`garden operator-spend`: the operator's own session spend, recorded beside the workers'."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from .. import operator_spend as ops
from .common import PANEL_INSIGHT, _store, app, console, err

operator_spend_app = typer.Typer(
    help="The operator's own session spend, recorded beside the workers' (docs/operator-spend.jsonl).",
    invoke_without_command=True, no_args_is_help=False)
app.add_typer(operator_spend_app, name="operator-spend", rich_help_panel=PANEL_INSIGHT)


@operator_spend_app.callback(invoke_without_command=True)
def operator_spend_default(ctx: typer.Context, json_out: bool = typer.Option(False, "--json")) -> None:
    """Sessions and totals from docs/operator-spend.jsonl."""
    if ctx.invoked_subcommand is not None:
        return
    store = _store()
    path = ops.default_path(store.root)
    rows = ops.session_rows(ops.read_records(path))
    if json_out:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print(f"[dim]no operator spend recorded yet ({store.rel(path)})[/dim]")
        return
    table = Table(title="operator sessions")
    for c in ("session", "last heartbeat", "turns", "avg context", "cost", "compactions"):
        table.add_column(c)
    for r in rows:
        table.add_row(r["session"][:12], r["at"][:16], str(r["turns"]), f"{r['avg_context']:,}",
                      f"${r['cost_usd']:.2f}", str(r["compactions"]) if r["compactions"] else "")
    console.print(table)
    total = round(sum(r["cost_usd"] for r in rows), 2)
    console.print(f"[dim]{len(rows)} session(s), ${total:.2f} total, from {store.rel(path)}[/dim]")


@operator_spend_app.command("record")
def operator_spend_record(
    transcript: str = typer.Option("", "--transcript", help="A specific transcript .jsonl file (default: the newest under the project's Claude Code directory)"),
    project: str = typer.Option("", "--project", help="Override the Claude Code project directory (default: derived from the garden root)"),
    session: str = typer.Option("", "--session", help="Match transcripts whose filename contains this; required with --compacted"),
    out: str = typer.Option("", "--out", help="Override docs/operator-spend.jsonl"),
    compacted: bool = typer.Option(False, "--compacted", help="Append a compaction marker instead of parsing a transcript"),
) -> None:
    """Append one heartbeat record of the operator's own session spend, or a compaction marker."""
    store = _store()
    out_path = Path(out) if out else ops.default_path(store.root)
    if compacted:
        if not session:
            err.print("[red]--compacted needs --session[/red]")
            raise typer.Exit(2)
        rec = ops.compacted_record(session)
        ops.append(out_path, rec)
        console.print(f"{rec['at']} session {session[:8]} compacted")
        return
    try:
        path = Path(transcript) if transcript else ops.find_transcript(
            Path(project) if project else ops.project_dir_for(store.root), session)
    except FileNotFoundError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    rec = ops.record_from_transcript(path)
    ops.append(out_path, rec)
    console.print(f"{rec['at']} session {rec['session'][:8]} turns {rec['turns']} "
                 f"avg context {rec['avg_context']:,} list ${rec['list_price_usd']:,.2f}")
