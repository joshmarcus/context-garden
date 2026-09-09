"""Manual, local-only release candidate checks."""

from __future__ import annotations

from pathlib import Path

import typer

from .common import PANEL_QUALITY, app, console, err

release_app = typer.Typer(help="Validate a release candidate before a human publishes it.")


@release_app.command("validate")
def validate(
    manifest: Path = typer.Option(..., "--manifest", exists=True, readable=True),
    github_host: str = typer.Option("github.com", "--github-host", help="GitHub or GitHub Enterprise web host."),
    repository: str = typer.Option("", "--repository", help="owner/repository; overrides the manifest."),
):
    """Check tag, source, CI evidence, notes and locally hashed release artifacts."""
    from ..release import validate_candidate

    try:
        checks = validate_candidate(manifest, root=Path.cwd(), github_host=github_host,
                                    repository=repository)
    except ValueError as exc:
        err.print(f"[red]release candidate invalid: {exc}[/red]")
        raise typer.Exit(1) from None
    for check in checks:
        console.print(f"[green]ok[/green] {check}")


app.add_typer(release_app, name="release", rich_help_panel=PANEL_QUALITY)
