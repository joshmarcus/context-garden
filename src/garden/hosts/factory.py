"""Build the durable scale operation for a declared pool's provider.

One builder serves every consumer — the CLI, the recurring fleet controller, and tests —
so a provider-neutral pool reaches the same public boundary as the EC2 adapter.  Nothing
provider-specific lives here beyond selecting an adapter: credential references, host
names and account settings stay in the caller's private enrollment configuration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .command import CommandProvider
from .config import pool_from_dict
from .core import HostLifecycle, JsonStateStore
from .drain import WorkerDrainStore
from .models import PoolDeclaration
from .scale import (
    DirectoryEnrollmentResolver,
    EnrollmentResolver,
    ScaleOperation,
    durable_worker_readiness,
)

DEFAULT_ENROLLMENT_DIR = Path(".garden/hosts/enrollment")


def operation_path_for(pool_name: str, garden_dir: Path) -> Path:
    """The conventional durable operation file for a pool."""
    return garden_dir / "hosts" / f"{pool_name}-scale.json"


def scale_operation(pool: PoolDeclaration, operation_path: Path,
                    enrollment_dir: Path | None = None,
                    enrollment_config: Path | None = None, *,
                    garden_dir: Path | None = None,
                    providers: dict[str, Any] | None = None,
                    enrollments: EnrollmentResolver | None = None,
                    health_check: Any = None,
                    interruption_drain: Any = None,
                    now: Any = None) -> ScaleOperation:
    """Return the operation for ``pool``, honouring the saved admission's own references.

    ``providers`` lets a consumer supply already-constructed adapters; otherwise the
    admitted declaration selects one.  A saved operation's enrollment directory and
    configuration win, so continuation cannot be redirected by a later argument.
    """
    saved = json.loads(operation_path.read_text()) if operation_path.exists() else {}
    saved_dir = str(saved.get("enrollment_dir") or "")
    if saved_dir:
        if enrollment_dir is not None and enrollment_dir.resolve() != Path(saved_dir):
            raise ValueError("continue with the operation's original enrollment directory")
        enrollment_dir = Path(saved_dir)
    enrollment_dir = enrollment_dir or DEFAULT_ENROLLMENT_DIR
    saved_config = str(saved.get("enrollment_config_path") or "")
    if saved_config:
        if enrollment_config is not None and enrollment_config.resolve() != Path(saved_config):
            raise ValueError("continue with the operation's original enrollment configuration")
        enrollment_config = Path(saved_config)
    declaration = pool_from_dict(saved["admitted_declaration"]) if saved else pool
    resolver: EnrollmentResolver = enrollments or DirectoryEnrollmentResolver(enrollment_dir)
    execution_context: dict[str, Any] = {}
    config: dict[str, Any] = {}
    if providers is not None:
        adapters = dict(providers)
    elif declaration.provider == "command":
        if enrollment_config is not None and enrollments is None:
            raise ValueError("a command-provider pool takes its identities from the enrollment "
                             "directory, not a provider credential configuration")
        adapters = {"command": CommandProvider()}
    elif declaration.provider == "ec2":
        adapters, resolver, execution_context, config = _ec2(
            declaration, operation_path, enrollment_dir, enrollment_config, saved, enrollments)
    else:
        raise ValueError(f"no controller adapter for provider {declaration.provider!r}; "
                         "configure a command wrapper for this pool")
    root = (garden_dir or Path(config.get("garden_dir") or operation_path.parent.parent)).resolve()
    lifecycle = HostLifecycle(
        adapters, JsonStateStore(operation_path.with_name(operation_path.stem + "-lifecycle.json")),
        health_check=durable_worker_readiness(root) if health_check is None else health_check,
        # The same durable fence covers provider interruptions and deliberate drains.
        # It is useful for on-demand pools too, even when they have no Spot event source.
        interruption_drain=WorkerDrainStore(root) if interruption_drain is None
        else interruption_drain,
    )
    return ScaleOperation(
        lifecycle, operation_path, resolver,
        enrollment_config_path=str(enrollment_config.resolve()) if enrollment_config else "",
        execution_context=execution_context,
        **({"now": now} if now is not None else {}),
    )


def _ec2(declaration: PoolDeclaration, operation_path: Path, enrollment_dir: Path,
         enrollment_config: Path | None, saved: dict[str, Any],
         supplied: EnrollmentResolver | None) -> tuple[dict[str, Any], EnrollmentResolver,
                                                       dict[str, Any], dict[str, Any]]:
    """Construct the EC2 adapter, its scoped resolver and its immutable execution context."""
    from .ec2 import EC2Provider, SQSEC2EventSource

    if declaration.purchase_policy == "spot":
        # Reject an unsafe explicit or implicit AWS request ceiling before credential setup.
        EC2Provider.validate_purchase_prices(declaration)
    try:
        import boto3
    except ImportError as exc:
        raise ValueError("EC2 scaling requires boto3 in the controller environment") from exc
    resolver: EnrollmentResolver = supplied or DirectoryEnrollmentResolver(enrollment_dir)
    config: dict[str, Any] = {}
    execution_context: dict[str, Any] = {}
    enforcer = None
    if enrollment_config is not None:
        from .deadline import ExternalDeadlineCommand
        from .enrollment import ProductionEnrollmentResolver
        from .enrollment_clients import clients_from_config

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
        if supplied is None:
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
            from .deadline_scheduler import LocalDeadlineScheduler

            enforcer = LocalDeadlineScheduler(
                Path(config.get("deadline_state_dir") or enrollment_dir / "deadlines"),
                config["aws_profile"], config["aws_region"], str(config["aws_account_id"]),
            )
    if declaration.purchase_policy == "spot" and not config.get("spot_event_queue_url"):
        raise ValueError(
            "Spot pools require --enrollment-config with spot_event_queue_url "
            "for interruption recovery"
        )
    EC2Provider.validate_purchase_prices(declaration)
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
    return {"ec2": provider}, resolver, execution_context, config
