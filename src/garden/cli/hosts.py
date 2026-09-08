"""Commands for one resumable managed-host scaling operation."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import typer

from ..hosts import (
    DirectoryEnrollmentResolver,
    HostLifecycle,
    JsonStateStore,
    ScaleOperation,
    durable_worker_readiness,
    pool_from_dict,
    status_dict,
)
from ..hosts.ec2 import EC2Provider
from .common import PANEL_LOOP, app, console, err

hosts_app = typer.Typer(help="Plan, resume, inspect, and clean up managed worker capacity.")
app.add_typer(hosts_app, name="hosts", rich_help_panel=PANEL_LOOP)


def _build_operation(pool, operation_path: Path, enrollment_dir: Path) -> ScaleOperation:
    if pool.provider != "ec2":
        raise ValueError("the CLI currently supports the ec2 provider")
    try:
        import boto3
    except ImportError as exc:
        raise ValueError("EC2 scaling requires boto3 in the controller environment") from exc
    provider = EC2Provider(boto3.client("ec2"))
    lifecycle_path = operation_path.with_name(operation_path.stem + "-lifecycle.json")
    garden_dir = operation_path.parent.parent
    lifecycle = HostLifecycle(
        {"ec2": provider}, JsonStateStore(lifecycle_path),
        health_check=durable_worker_readiness(garden_dir),
    )
    return ScaleOperation(lifecycle, operation_path, DirectoryEnrollmentResolver(enrollment_dir))


@hosts_app.command("scale")
def scale(
    specification: Path = typer.Argument(..., exists=True, readable=True),
    deadline: str = typer.Option("", help="Future absolute ISO-8601 termination deadline."),
    continue_operation: bool = typer.Option(False, "--continue", help="Resume convergence."),
    cleanup: bool = typer.Option(False, help="Drain and tear down this operation."),
    aggregate_limit: float = typer.Option(80.0, help="Aggregate admitted worker allocation."),
    state: Path | None = typer.Option(None),
    enrollment_dir: Path = typer.Option(Path(".garden/hosts/enrollment")),
):
    """Request or resume a bounded pool scale operation; output contains no secrets."""
    try:
        pool = pool_from_dict(json.loads(specification.read_text()))
        operation_path = state or Path(f".garden/hosts/{pool.name}-scale.json")
        operation = _build_operation(pool, operation_path, enrollment_dir)
        if deadline:
            parsed = dt.datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            status = operation.request(pool, deadline=parsed,
                                       aggregate_spend_limit_usd=aggregate_limit)
        elif cleanup:
            status = operation.cleanup(pool)
        elif continue_operation:
            status = operation.continue_(pool)
        else:
            status = operation.status(pool)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print_json(data=status_dict(status))
