"""Explicit, credential-authenticated multiplayer enrollment operations."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import typer
import yaml

from ..members import MemberRegistry
from ..multiplayer_client import MultiplayerClient, MultiplayerUnavailable
from .common import PANEL_LOOP, _store, app, console, err

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


def _save_local_enrollment(root: Path, values: dict[str, str]) -> None:
    """Store connection metadata only in the ignored local overlay, never the credential."""
    path = root / "garden.local.yaml"
    current = yaml.safe_load(path.read_text()) if path.exists() else {}
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise ValueError("garden.local.yaml: top level must be a mapping")
    multiplayer = current.setdefault("multiplayer", {})
    if not isinstance(multiplayer, dict):
        raise ValueError("garden.local.yaml: multiplayer must be a mapping")
    multiplayer.update({"enabled": True, **values})
    fd, temporary = tempfile.mkstemp(prefix=".garden.local.yaml.", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            yaml.safe_dump(current, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@members_app.command("enroll-administrator")
def enroll_administrator(garden_id: str, member_id: str, installation_id: str) -> None:
    """Bootstrap an empty garden; save the printed credential in private local storage."""
    token = _registry().enroll_administrator(garden_id, member_id, installation_id)
    console.print(token)


@members_app.command("connect")
def connect(garden_id: str, coordinator_url: str, member_id: str, installation_id: str,
            credential_env: str = typer.Option(...)) -> None:
    """Validate an installation credential and save its non-secret local connection settings."""
    store = _store()
    credential = os.environ.get(credential_env, "")
    if not credential:
        err.print(f"[red]{credential_env} is not set; export the installation credential first[/red]")
        raise typer.Exit(2)
    try:
        client = MultiplayerClient(
            root=store.root, garden_id=garden_id, endpoint=coordinator_url,
            member_id=member_id, installation_id=installation_id, credential=credential,
        )
        view = client.refresh(allow_stale=False)
    except MultiplayerUnavailable as exc:
        err.print(f"[red]could not connect this installation: {exc}[/red]")
        raise typer.Exit(2) from None
    _save_local_enrollment(store.root, {
        "garden_id": garden_id, "coordinator_url": coordinator_url,
        "member_id": member_id, "installation_id": installation_id,
        "credential_env": credential_env,
    })
    console.print(f"connected {view.snapshot['member_id']} ({view.snapshot['role']}) to {garden_id}")


@members_app.command("status")
def status() -> None:
    """Inspect the authenticated identity and execution assignment for this installation."""
    store = _store()
    try:
        client = MultiplayerClient.from_config(store.config)
        if client is None:
            raise MultiplayerUnavailable("multiplayer is not connected; run `garden members connect`")
        view = client.refresh()
    except MultiplayerUnavailable as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    assignment = view.snapshot.get("assignment")
    console.print(f"identity: {view.snapshot['member_id']} ({view.snapshot['role']})")
    console.print(f"installation: {view.snapshot['installation_id']}")
    if assignment and assignment.get("enabled"):
        console.print(f"execution: {assignment['project']}/{assignment['phase']}")
    elif assignment:
        console.print("execution: assignment paused")
    else:
        console.print("execution: No work assignment")
    if view.stale:
        console.print(f"[yellow]coordinator: disconnected; showing cached authority ({view.error})[/yellow]")
    else:
        console.print("coordinator: connected")


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


@members_app.command("assign")
def assign(member_id: str, project: str, phase: str,
           credential_env: str = typer.Option(...), generation: int = typer.Option(0),
           paused: bool = typer.Option(False)) -> None:
    """Explicitly set one member's execution scope (administrator credential required)."""
    registry = _registry()
    try:
        assignment = registry.set_assignment(
            _actor(registry, credential_env), member_id, project, phase,
            enabled=not paused, expected_generation=generation,
        )
    except (PermissionError, RuntimeError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    state = "paused" if not assignment.enabled else "active"
    console.print(f"{member_id}: {project}/{phase} ({state}, generation {assignment.generation})")
