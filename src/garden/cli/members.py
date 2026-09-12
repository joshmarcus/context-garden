"""Explicit, credential-authenticated multiplayer enrollment operations."""

from __future__ import annotations

import os

import typer

from ..members import MemberRegistry
from .common import PANEL_LOOP, _store, app, console

members_app = typer.Typer(help="Enroll garden members and manage private installation credentials.")
app.add_typer(members_app, name="members", rich_help_panel=PANEL_LOOP)


def _registry() -> MemberRegistry:
    return MemberRegistry(_store().config.garden_dir)


def _actor(registry: MemberRegistry, credential_env: str):
    token = os.environ.get(credential_env, "")
    actor = registry.authenticate(token)
    if actor is None:
        raise typer.BadParameter("credential environment variable is missing or rejected")
    return actor


@members_app.command("enroll-administrator")
def enroll_administrator(garden_id: str, member_id: str, installation_id: str) -> None:
    """Bootstrap an empty garden; save the printed credential in private local storage."""
    token = _registry().enroll_administrator(garden_id, member_id, installation_id)
    console.print(token)


@members_app.command("add")
def add(member_id: str, role: str = typer.Option("member"),
        visibility: str = typer.Option("all"), projects: str = typer.Option(""),
        credential_env: str = typer.Option(...)) -> None:
    """Add a member. The caller credential must belong to an administrator."""
    registry = _registry()
    registry.add_member(_actor(registry, credential_env), member_id, role, visibility,
                        tuple(value.strip() for value in projects.split(",") if value.strip()))  # type: ignore[arg-type]
    console.print(f"{member_id} enrolled as {role}")


@members_app.command("issue-installation")
def issue_installation(member_id: str, installation_id: str,
                       credential_env: str = typer.Option(...)) -> None:
    """Issue an installation and print its credential exactly once."""
    registry = _registry()
    console.print(registry.issue_installation(
        _actor(registry, credential_env), member_id, installation_id,
    ))


@members_app.command("rotate-installation")
def rotate_installation(installation_id: str, credential_env: str = typer.Option(...)) -> None:
    """Invalidate an installation's old secret and print its replacement once."""
    registry = _registry()
    console.print(registry.rotate_installation(_actor(registry, credential_env), installation_id))


@members_app.command("revoke-installation")
def revoke_installation(installation_id: str, credential_env: str = typer.Option(...)) -> None:
    registry = _registry()
    registry.revoke_installation(_actor(registry, credential_env), installation_id)
    console.print(f"{installation_id} revoked")


@members_app.command("set-active")
def set_active(member_id: str, active: bool, credential_env: str = typer.Option(...)) -> None:
    registry = _registry()
    registry.set_member_active(_actor(registry, credential_env), member_id, active)
    console.print(f"{member_id} {'enabled' if active else 'disabled'}")


@members_app.command("assign-work")
def assign_work(member_id: str, target: str, enabled: bool = typer.Option(True),
                advance: bool = typer.Option(False), generation: int = typer.Option(0),
                credential_env: str = typer.Option(...)) -> None:
    """Set one versioned project/phase execution cursor for a member."""
    if "/" not in target:
        raise typer.BadParameter("target must be project/phase")
    project, phase = target.split("/", 1)
    registry = _registry()
    row = registry.set_assignment(
        _actor(registry, credential_env), member_id, project, phase,
        enabled=enabled, advance=advance, expected_generation=generation,
    )
    console.print(
        f"{member_id}: {row.project}/{row.phase} generation {row.generation} "
        f"({'enabled' if row.enabled else 'paused'}, advance={'on' if row.advance else 'off'})"
    )


@members_app.command("assign-phase-owner")
def assign_phase_owner(target: str, owner_id: str,
                       generation: int = typer.Option(0),
                       credential_env: str = typer.Option(...)) -> None:
    """Assign, transfer, or explicitly vacate phase-operation ownership."""
    if "/" not in target:
        raise typer.BadParameter("target must be project/phase")
    project, phase = target.split("/", 1)
    registry = _registry()
    row = registry.set_phase_owner(
        _actor(registry, credential_env), project, phase,
        None if owner_id == "-" else owner_id, expected_generation=generation,
    )
    console.print(
        f"{target}: phase owner {row.owner_id or 'unassigned'} generation {row.generation} "
        f"(changed by {row.changed_by})"
    )


@members_app.command("advance-work")
def advance_work(member_id: str, next_phase: str, generation: int = typer.Option(...),
                 credential_env: str = typer.Option(...)) -> None:
    """Explicitly advance a configured cursor after all work in its phase is terminal."""
    registry = _registry()
    row = registry.advance_assignment(
        _actor(registry, credential_env), member_id, next_phase, _store().tasks(),
        expected_generation=generation,
    )
    console.print(f"{member_id}: advanced to {row.project}/{row.phase} generation {row.generation}")
