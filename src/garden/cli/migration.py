"""Explicit, recoverable standalone-to-multiplayer cutover commands."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import typer

from ..coordination_api import create_coordination_app
from ..members import MemberRegistry
from ..migration import GardenMigration, MigrationRefused
from .common import PANEL_LOOP, _store, app, console, err

migration_app = typer.Typer(help="Preview, commit, recover, or reverse multiplayer enrollment.")
app.add_typer(migration_app, name="migration", rich_help_panel=PANEL_LOOP)


def _choices(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"could not read migration choices: {exc}") from exc
    if not isinstance(value, dict):
        raise typer.BadParameter("migration choices must be a JSON object")
    return value


def _actor(credential_env: str):
    registry = MemberRegistry(_store().config.garden_dir)
    principal = registry.authenticate(os.environ.get(credential_env, ""))
    if principal is None or principal.role != "administrator":
        raise typer.BadParameter("an administrator credential environment variable is required")
    return principal


def _run(action):
    try:
        return action()
    except MigrationRefused as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None


@migration_app.command("preview")
def preview(choices: Path = typer.Option(..., "--choices", exists=True, dir_okay=False)) -> None:
    """Persist a non-mutating plan from explicit owner, phase-owner and installation choices."""
    result = _run(lambda: GardenMigration(_store()).preview(_choices(choices)))
    console.print_json(data=result)


@migration_app.command("commit")
def commit(preview_id: str = typer.Option(..., "--preview-id"),
           credential_env: str = typer.Option(..., "--credential-env")) -> None:
    """Commit exactly one selected, still-current preview under the controller lock."""
    store = _store()
    lock_path = store.config.garden_dir / "tick.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = _run(lambda: GardenMigration(store).commit(preview_id, _actor(credential_env)))
    console.print(f"multiplayer authority committed; recovery snapshot: {result['snapshot']}")


@migration_app.command("status")
def status() -> None:
    """Show an interrupted or completed cutover journal."""
    path = _store().config.garden_dir / "migration" / "cutover.json"
    if not path.exists():
        console.print("No migration has started")
        return
    console.print_json(path.read_text())


@migration_app.command("export-standalone")
def export_standalone(destination: Path = typer.Option(..., "--destination")) -> None:
    """Export one quiescent standalone authority archive for explicit reversal."""
    store = _store()
    lock_path = store.config.garden_dir / "tick.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = _run(lambda: GardenMigration(store).export_standalone(destination.resolve()))
    console.print(f"standalone authority exported to {path}")


@migration_app.command("serve-coordinator")
def serve_coordinator(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8770),
                      tls_certfile: Path | None = typer.Option(None, "--tls-certfile"),
                      tls_keyfile: Path | None = typer.Option(None, "--tls-keyfile")) -> None:
    """Run the private authority service; non-loopback listeners require a TLS pair."""
    import ipaddress

    import uvicorn

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback and (tls_certfile is None or tls_keyfile is None):
        raise typer.BadParameter(
            "non-loopback coordinator listeners require --tls-certfile and --tls-keyfile"
        )
    store = _store()
    uvicorn.run(create_coordination_app(store.config.garden_dir), host=host, port=port,
                ssl_certfile=str(tls_certfile) if tls_certfile else None,
                ssl_keyfile=str(tls_keyfile) if tls_keyfile else None)
