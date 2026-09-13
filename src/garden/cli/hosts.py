"""Commands for one resumable managed-host scaling operation."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import typer

from ..hosts import ScaleOperation, pool_from_dict, status_dict
from ..hosts.factory import operation_path_for, scale_operation
from .common import PANEL_LOOP, _store, app, console, err

hosts_app = typer.Typer(help="Plan, resume, inspect, and clean up managed worker capacity.")
app.add_typer(hosts_app, name="hosts", rich_help_panel=PANEL_LOOP)


def _build_operation(pool, operation_path: Path, enrollment_dir: Path | None,
                     enrollment_config: Path | None = None) -> ScaleOperation:
    return scale_operation(pool, operation_path, enrollment_dir, enrollment_config)


@hosts_app.command("fleet")
def fleet(
    resume: bool = typer.Option(False, "--resume",
        help="Clear a tripped replacement breaker after fixing what broke."),
    converge: bool = typer.Option(False, "--converge",
        help="Take one reconciliation step now instead of waiting for the next pass."),
    root: Path | None = typer.Option(None, "--garden", help="Garden root (default: cwd)."),
):
    """Show the recurring worker-pool reading, or clear its breaker; output has no secrets."""
    from ..fleet import FleetController, fleet_projection

    store = _store(root)
    controller = FleetController(store.config)
    if controller.settings is None:
        err.print("[yellow]no workers.pool block configured; this garden uses static "
                  "workers.hosts[/yellow]")
        raise typer.Exit(1)
    try:
        if resume:
            controller.resume()
        if converge:
            controller.converge(force=True)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    except Exception as exc:
        # A provider exception can retain credential-bearing request state; report the type.
        err.print(f"[red]reconciliation stopped ({type(exc).__name__}); durable progress is "
                  "retained. Verify the scoped provider access and retry.[/red]")
        raise typer.Exit(1) from None
    console.print_json(data=fleet_projection(store.config))


@hosts_app.command("scale")
def scale(
    specification: Path = typer.Argument(..., exists=True, readable=True),
    deadline: str = typer.Option("", help="Future absolute ISO-8601 termination deadline."),
    continue_operation: bool = typer.Option(False, "--continue", help="Resume convergence."),
    cleanup: bool = typer.Option(False, help="Drain and tear down this operation."),
    emergency_stop: bool = typer.Option(False, "--emergency-stop",
        help="Immediately tear down capacity, interrupting active work."),
    aggregate_limit: float = typer.Option(80.0, help="Aggregate admitted worker allocation."),
    state: Path | None = typer.Option(None),
    enrollment_dir: Path | None = typer.Option(None,
        help="Private enrollment directory (default: .garden/hosts/enrollment)."),
    enrollment_config: Path | None = typer.Option(None, exists=True, readable=True,
        help="Production enrollment configuration with private credential file references."),
):
    """Request or resume a bounded pool scale operation; output contains no secrets."""
    try:
        pool = pool_from_dict(json.loads(specification.read_text()))
        # One convention, shared with the recurring controller: the operation is named for
        # the pool, so a declaration file with any name reaches the same admission.
        operation_path = state or operation_path_for(pool.name, Path(".garden"))
        if sum((bool(deadline), cleanup, continue_operation, emergency_stop)) > 1:
            raise ValueError("choose one of --deadline, --continue, --cleanup or --emergency-stop")
        operation = _build_operation(pool, operation_path, enrollment_dir, enrollment_config)
        if deadline:
            parsed = dt.datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            status = operation.request(pool, deadline=parsed,
                                       aggregate_spend_limit_usd=aggregate_limit)
        elif cleanup:
            status = operation.cleanup(pool)
        elif emergency_stop:
            status = operation.emergency_stop(pool)
        elif continue_operation:
            status = operation.continue_(pool)
        else:
            status = operation.status(pool)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    except Exception as exc:
        # Provider exceptions may retain credential-bearing request payloads. Never let
        # Typer's traceback renderer include their locals or dump a bootstrap envelope.
        err.print(f"[red]scale operation stopped ({type(exc).__name__}); durable progress "
                  "is retained. Verify the scoped provider access and retry.[/red]")
        raise typer.Exit(1) from None
    console.print_json(data=status_dict(status))
