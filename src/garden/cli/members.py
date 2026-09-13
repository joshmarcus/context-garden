"""Explicit, credential-authenticated multiplayer enrollment operations."""

from __future__ import annotations

import os

import typer

from ..coordination import Coordinator
from ..members import MemberRegistry
from .common import PANEL_LOOP, _store, app, console

members_app = typer.Typer(help="Enroll garden members and manage private installation credentials.")
app.add_typer(members_app, name="members", rich_help_panel=PANEL_LOOP)


def _registry() -> MemberRegistry:
    garden_dir = _store().config.garden_dir
    return MemberRegistry(garden_dir, Coordinator(garden_dir / "coordination.db"))


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
