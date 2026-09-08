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
    path = ops.default_path(store.root, store.config)
    rows = ops.session_rows(ops.read_records(path))
    if json_out:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print(f"[dim]no operator spend recorded yet ({store.rel(path)})[/dim]")
        return
    table = Table(title="operator sessions")
    for c in ("session", "harness", "last heartbeat", "turns", "avg context", "cost", "compactions"):
        table.add_column(c)
    for r in rows:
        avg_context = f"{r['avg_context']:,}" if isinstance(r["avg_context"], int) else "unavailable"
        cost = f"${r['cost_usd']:.2f}" if r["cost_usd"] is not None else "unavailable"
        table.add_row(r["session"][:12], r["harness"], r["at"][:16], str(r["turns"]), avg_context,
                      cost, str(r["compactions"]) if r["compactions"] else "")
    console.print(table)
    priced = [r for r in rows if r["cost_usd"] is not None]
    unavailable = len(rows) - len(priced)
    if unavailable and not priced:
        summary = f"0 priced session(s), {unavailable} unavailable"
    elif unavailable:
        total = round(sum(r["cost_usd"] for r in priced), 2)
        summary = f"${total:.2f} known total; {unavailable} session(s) unavailable"
    else:
        total = round(sum(r["cost_usd"] for r in priced), 2)
        summary = f"{len(rows)} session(s), ${total:.2f} total"
    console.print(f"[dim]{summary}, from {store.rel(path)}[/dim]")


@operator_spend_app.command("record")
def operator_spend_record(
    transcript: str = typer.Option("", "--transcript", help="A specific Claude Code or Codex transcript .jsonl file"),
    project: str = typer.Option("", "--project", help="Override the transcript directory (default: Codex sessions or the garden's Claude Code directory)"),
    harness: str = typer.Option("codex", "--harness", help="Transcript source: codex (default) or claude"),
    session: str = typer.Option("", "--session", help="Match transcripts whose filename contains this; required with --compacted"),
    out: str = typer.Option("", "--out", help="Override docs/operator-spend.jsonl"),
    compacted: bool = typer.Option(False, "--compacted", help="Append a compaction marker instead of parsing a transcript"),
) -> None:
    """Append one heartbeat record of the operator's own session spend, or a compaction marker."""
    store = _store()
    out_path = Path(out) if out else ops.default_path(store.root, store.config)
    if compacted:
        if not session:
            err.print("[red]--compacted needs --session[/red]")
            raise typer.Exit(2)
        rec = ops.compacted_record(session)
        ops.append(out_path, rec)
        console.print(f"{rec['at']} session {session[:8]} compacted")
        return
    if harness not in {"codex", "claude"}:
        err.print("[red]--harness must be codex or claude[/red]")
        raise typer.Exit(2)
    try:
        if transcript:
            path = Path(transcript)
        elif harness == "codex":
            path = ops.find_codex_transcript(Path(project) if project else ops.codex_session_dir(), session)
        else:
            path = ops.find_transcript(Path(project) if project else ops.project_dir_for(store.root), session)
    except FileNotFoundError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    rec = ops.record_from_transcript(path)
    ops.append(out_path, rec)
    if rec["usage_status"] == "unavailable":
        console.print(f"{rec['at']} {rec['harness']} session {rec['session'][:8]} usage unavailable")
    elif rec["list_price_usd"] is None:
        console.print(f"{rec['at']} {rec['harness']} session {rec['session'][:8]} turns {rec['turns']} "
                      f"avg context {rec['avg_context']:,} list unavailable")
    else:
        console.print(f"{rec['at']} {rec['harness']} session {rec['session'][:8]} turns {rec['turns']} "
                      f"avg context {rec['avg_context']:,} list ${rec['list_price_usd']:,.2f}")
