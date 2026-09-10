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
    WorkerDrainStore,
    durable_worker_readiness,
    pool_from_dict,
    status_dict,
)
from ..hosts.ec2 import EC2Provider, SQSEC2EventSource
from .common import PANEL_LOOP, app, console, err

hosts_app = typer.Typer(help="Plan, resume, inspect, and clean up managed worker capacity.")
app.add_typer(hosts_app, name="hosts", rich_help_panel=PANEL_LOOP)


def _build_operation(pool, operation_path: Path, enrollment_dir: Path | None,
                     enrollment_config: Path | None = None) -> ScaleOperation:
    if pool.provider != "ec2":
        raise ValueError("the CLI currently supports the ec2 provider")
    try:
        import boto3
    except ImportError as exc:
        raise ValueError("EC2 scaling requires boto3 in the controller environment") from exc
    saved = json.loads(operation_path.read_text()) if operation_path.exists() else {}
    saved_dir = str(saved.get("enrollment_dir") or "")
    if saved_dir:
        if enrollment_dir is not None and enrollment_dir.resolve() != Path(saved_dir):
            raise ValueError("continue with the operation's original enrollment directory")
        enrollment_dir = Path(saved_dir)
    enrollment_dir = enrollment_dir or Path(".garden/hosts/enrollment")
    saved_config = str(saved.get("enrollment_config_path") or "")
    if saved_config:
        if enrollment_config is not None and enrollment_config.resolve() != Path(saved_config):
            raise ValueError("continue with the operation's original enrollment configuration")
        enrollment_config = Path(saved_config)
    declaration = pool_from_dict(saved["admitted_declaration"]) if saved else pool
    resolver = DirectoryEnrollmentResolver(enrollment_dir)
    config = {}
    execution_context = {}
    enforcer = None
    if enrollment_config is not None:
        from ..hosts.deadline import ExternalDeadlineCommand
        from ..hosts.enrollment import ProductionEnrollmentResolver
        from ..hosts.enrollment_clients import clients_from_config

        config = json.loads(enrollment_config.read_text())
        if not isinstance(config, dict):
            raise ValueError("enrollment configuration must be a JSON object of credential references")
        execution_context = {key: config.get(key) for key in (
            "aws_profile", "aws_region", "aws_account_id", "github_repo", "instance_tags",
            "spot_event_queue_url")}
        if saved.get("execution_context") and saved["execution_context"] != execution_context:
            raise ValueError("provider enrollment context changed; use the admitted account, "
                             "region and repository")
        clients = clients_from_config(config)
        resolver = ProductionEnrollmentResolver(
            enrollment_dir, config, declaration, operation_path,
            secrets_client=clients[0], tailscale_client=clients[1], github_client=clients[2],
        )
        command = config.get("deadline_command")
        if command:
            if not isinstance(command, list):
                raise ValueError("deadline_command must be an argument list, not shell text")
            enforcer = ExternalDeadlineCommand(command)
        else:
            from ..hosts.deadline_scheduler import LocalDeadlineScheduler

            enforcer = LocalDeadlineScheduler(
                Path(config.get("deadline_state_dir") or enrollment_dir / "deadlines"),
                config["aws_profile"], config["aws_region"], str(config["aws_account_id"]),
            )
    session = boto3.Session(
        **({"profile_name": config["aws_profile"]} if config.get("aws_profile") else {}),
        **({"region_name": config["aws_region"]} if config.get("aws_region") else {}),
    )
    if config:
        identity = session.client("sts").get_caller_identity()
        expected_role = (f"arn:aws:sts::{config['aws_account_id']}:"
                         "assumed-role/ContextGardenProvisioner/")
        if (str(identity.get("Account")) != str(config["aws_account_id"])
                or not str(identity.get("Arn", "")).startswith(expected_role)):
            raise ValueError("provisioning profile must assume the scoped ContextGardenProvisioner "
                             "role in the configured account")
    event_source = None
    queue_url = str(config.get("spot_event_queue_url") or "")
    if queue_url:
        event_source = SQSEC2EventSource(
            session.client("sqs"), queue_url,
            operation_path.with_name(operation_path.stem + "-spot-events.json"),
        )
    provider = EC2Provider(
        session.client("ec2"), deadline_enforcer=enforcer,
        required_tags=dict(config.get("instance_tags") or {}),
        event_source=event_source,
    )
    lifecycle_path = operation_path.with_name(operation_path.stem + "-lifecycle.json")
    garden_dir = Path(config.get("garden_dir") or operation_path.parent.parent).resolve()
    lifecycle = HostLifecycle(
        {"ec2": provider}, JsonStateStore(lifecycle_path),
        health_check=durable_worker_readiness(garden_dir),
        interruption_drain=WorkerDrainStore(garden_dir) if event_source is not None else None,
    )
    return ScaleOperation(
        lifecycle, operation_path, resolver,
        enrollment_config_path=str(enrollment_config.resolve()) if enrollment_config else "",
        execution_context=execution_context,
    )


@hosts_app.command("scale")
def scale(
    specification: Path = typer.Argument(..., exists=True, readable=True),
    deadline: str = typer.Option("", help="Future absolute ISO-8601 termination deadline."),
    continue_operation: bool = typer.Option(False, "--continue", help="Resume convergence."),
    cleanup: bool = typer.Option(False, help="Drain and tear down this operation."),
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
        operation_path = state or Path(f".garden/hosts/{pool.name}-scale.json")
        if sum((bool(deadline), cleanup, continue_operation)) > 1:
            raise ValueError("choose one of --deadline, --continue or --cleanup")
        operation = _build_operation(pool, operation_path, enrollment_dir, enrollment_config)
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
