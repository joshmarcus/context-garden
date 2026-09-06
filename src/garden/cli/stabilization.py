"""Token-free stabilization evidence recorder and report commands."""

from __future__ import annotations

import json

import typer

from .common import PANEL_QUALITY, _phase, _split_target, _store, app, console, err

stabilization_app = typer.Typer(help="Record and inspect stabilization evidence without an agent session.")
app.add_typer(stabilization_app, name="stabilization", rich_help_panel=PANEL_QUALITY)


def _target(target: str):
    store = _store()
    product, name = _split_target(target)
    return store, _phase(store, product, name)


@stabilization_app.command("start")
def start_recording(target: str, build_sha: str = typer.Option("", "--build-sha")):
    """Start (or restart) a candidate unattended window on the pinned build."""
    from ..stabilization import start

    _, phase = _target(target)
    data = start(phase, build_sha or None)
    console.print(f"recording {phase.key} on {data['build_sha'][:12]} from {data['started_at']}")


@stabilization_app.command("sample")
def take_sample(target: str):
    """Append one resource/progress observation; suitable for cron or a service timer."""
    from ..events import EventLog
    from ..stabilization import sample

    store, phase = _target(target)
    try:
        row = sample(phase, EventLog(store.config.garden_dir / "events.jsonl"))
    except RuntimeError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print(json.dumps(row, sort_keys=True))


@stabilization_app.command("intervene")
def record_intervention(target: str, reason: str, kind: str = typer.Option("operator_repair", "--kind")):
    """Count an operator repair and reset the candidate four-hour window."""
    from ..stabilization import intervene

    _, phase = _target(target)
    intervene(phase, reason, kind=kind)
    console.print(f"recorded {kind}; {phase.key}'s unattended window restarted")


@stabilization_app.command("outcome")
def outcome(target: str, name: str, status: str = typer.Option(...), command: str = typer.Option(...),
            observed: str = typer.Option(...), artifact: list[str] = typer.Option(..., "--artifact"),
            evidence_type: str = typer.Option(..., "--type"), real_user: bool = typer.Option(False, "--real-user"),
            exercise: list[str] = typer.Option([], "--exercise"),
            fixture_isolated: bool = typer.Option(False, "--fixture-isolated")):
    """Record a cited automated check or actual interaction outcome."""
    from ..stabilization import record_outcome

    _, phase = _target(target)
    try:
        record_outcome(phase, name, status.upper(), command=command, observed=observed,
                       artifacts=artifact, evidence_type=evidence_type, real_user=real_user,
                       exercises=exercise, fixture_isolated=fixture_isolated)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print(f"recorded {name}: {status.upper()}")


@stabilization_app.command("report")
def report(target: str):
    """Render the evidence report and print its PASS/UNPROVEN gate result."""
    from ..stabilization import evidence_paths, gate, render_report

    _, phase = _target(target)
    text = render_report(phase)
    _, path = evidence_paths(phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    ok, missing = gate(phase)
    console.print(text)
    console.print(f"artifact: {path}")
    if not ok:
        raise typer.Exit(1)
