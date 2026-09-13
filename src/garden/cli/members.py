"""Explicit local enrollment for Git-coordinated multiplayer gardens."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import typer
import yaml

from ..git_coordination import GitCoordinationError, GitMultiplayerClient
from ..members import MemberRegistry, operating_system_username
from ..multiplayer_client import MultiplayerClient, MultiplayerUnavailable
from ..scheduler import Scheduler
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


def _save_local_enrollment(root: Path, values: dict[str, object]) -> None:
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
    typer.echo(token)


@members_app.command("connect")
def connect(garden_id: str, member_id: str, installation_id: str,
            remote: str = typer.Option("origin"),
            state_ref: str = typer.Option("refs/heads/garden-state")) -> None:
    """Validate an admitted installation on the Garden state ref and bind this checkout."""
    store = _store()
    try:
        client = GitMultiplayerClient.connect(
            store.root, garden_id=garden_id, member_id=member_id,
            installation_id=installation_id, remote=remote, state_ref=state_ref,
        )
        principal = client.authenticate_local_session()
        if principal is None:
            raise GitCoordinationError("installation is not admitted for this member")
        view = client.refresh(allow_stale=False)
    except (GitCoordinationError, MultiplayerUnavailable) as exc:
        err.print(f"[red]could not connect this installation: {exc}[/red]")
        raise typer.Exit(2) from None
    _save_local_enrollment(store.root, {
        "garden_id": garden_id, "git": {"remote": remote, "state_ref": state_ref},
        "member_id": member_id, "installation_id": installation_id,
        "authentication": "credential", "credential_env": "",
        "coordinator_url": "",
    })
    console.print(
        f"connected {view.snapshot['member_id']} ({view.snapshot['role']}) to {garden_id} "
        f"at {view.snapshot['observed_revision']}"
    )


@members_app.command("current-username")
def current_username() -> None:
    """Print the OS account name used by temporary username authentication."""
    typer.echo(operating_system_username())


@members_app.command("connect-username")
def connect_username(garden_id: str, installation_id: str = typer.Option(""),
                     remote: str = typer.Option("origin"),
                     state_ref: str = typer.Option("refs/heads/garden-state")) -> None:
    """Bind an already-admitted installation to the effective local OS account."""
    store = _store()
    existing = store.config.get("multiplayer.installation_id", "")
    installation_id = installation_id or str(existing) or f"local-{uuid.uuid4().hex}"
    username = operating_system_username()
    try:
        client = GitMultiplayerClient.connect(
            store.root, garden_id=garden_id, member_id=username,
            installation_id=installation_id, remote=remote, state_ref=state_ref,
            authentication="temporary-username",
        )
        if client.authenticate_local_session() is None:
            raise GitCoordinationError("installation is not admitted for this OS account")
        view = client.refresh(allow_stale=False)
    except (GitCoordinationError, MultiplayerUnavailable) as exc:
        err.print(f"[red]could not connect this username installation: {exc}[/red]")
        raise typer.Exit(2) from None
    _save_local_enrollment(store.root, {
        "garden_id": garden_id, "git": {"remote": remote, "state_ref": state_ref},
        "member_id": username, "installation_id": installation_id,
        "authentication": "temporary-username", "credential_env": "",
        "coordinator_url": "",
    })
    console.print(f"connected {view.snapshot['member_id']} ({view.snapshot['role']}) to {garden_id}")


@members_app.command("status")
def status() -> None:
    """Inspect the authenticated identity and execution assignment for this installation."""
    store = _store()
    provenance = "default"
    for source, document in store.config.source_documents:
        setting = document.get("multiplayer", {})
        if isinstance(setting, dict) and "enabled" in setting:
            provenance = source
    try:
        client = MultiplayerClient.from_config(store.config)
        if client is None:
            raise MultiplayerUnavailable("multiplayer is not connected; run `garden members connect`")
        view = client.refresh()
    except MultiplayerUnavailable as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    assignment = view.snapshot.get("assignment")
    console.print(f"mode: multiplayer Git (multiplayer.enabled from {provenance})")
    console.print(f"identity: {view.snapshot['member_id']} ({view.snapshot['role']})")
    console.print(f"installation: {view.snapshot['installation_id']}")
    if assignment and assignment.get("enabled"):
        console.print(f"execution: {assignment['project']}/{assignment['phase']}")
    elif assignment:
        console.print("execution: assignment paused")
    else:
        console.print("execution: No work assignment")
    if view.stale:
        console.print(f"[yellow]Git: unavailable; showing cached authority ({view.error})[/yellow]")
    else:
        console.print(f"Git: synchronized at {view.snapshot.get('observed_revision', '')}")


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
    typer.echo(registry.issue_installation(
        _actor(registry, credential_env), member_id, installation_id,
    ))


@members_app.command("rotate-installation")
def rotate_installation(installation_id: str, credential_env: str = typer.Option(...)) -> None:
    """Invalidate an installation's old secret and print its replacement once."""
    registry = _registry()
    typer.echo(registry.rotate_installation(_actor(registry, credential_env), installation_id))


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
@members_app.command("assign-phase")
def assign_phase(project: str, phase: str, member_id: str = typer.Argument(""),
                 credential_env: str = typer.Option(...), generation: int = typer.Option(0)) -> None:
    """Set explicit phase-workflow ownership; omit MEMBER_ID to leave it unassigned."""
    registry = _registry()
    try:
        owner = Scheduler(_store()).set_phase_owner(
            _actor(registry, credential_env), project, phase, member_id or None,
            expected_generation=generation,
        )
    except (PermissionError, RuntimeError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    label = owner.owner_id or "unassigned"
    console.print(f"{project}/{phase}: {label} (generation {owner.generation})")
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
    row = Scheduler(_store()).set_phase_owner(
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
