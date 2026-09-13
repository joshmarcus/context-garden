"""Plugin compatibility lock commands."""

from __future__ import annotations

import typer

from ..plugins import PluginConfigurationError, write_lock
from .common import PANEL_SETUP, _store, console, err

plugins_app = typer.Typer(help="Inspect and update the enabled plugin compatibility set.")


@plugins_app.command("lock")
def lock_plugins() -> None:
    """Atomically create or refresh garden.lock from the installed configured plugins."""
    store = _store()
    try:
        loaded = store.config.load_plugins()
        previous, current = write_lock(store.root, loaded)
    except PluginConfigurationError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    if previous == current:
        console.print("garden.lock already matches the enabled plugin set")
        return
    before = {item["name"]: item for item in (previous or {}).get("plugins", [])}
    after = {item["name"]: item for item in current["plugins"]}
    changes = []
    for name in sorted(before.keys() | after.keys()):
        if name not in before:
            changes.append(f"added {name} ({after[name]['distribution']}=={after[name]['distribution_version']})")
        elif name not in after:
            changes.append(f"removed {name} ({before[name]['distribution']}=={before[name]['distribution_version']})")
        elif before[name] != after[name]:
            fields = sorted(key for key in before[name].keys() | after[name].keys() if before[name].get(key) != after[name].get(key))
            changes.append(f"changed {name}: {', '.join(fields)}")
    if previous and previous.get("core_version") != current["core_version"]:
        changes.insert(0, f"changed core_version: {previous.get('core_version')} -> {current['core_version']}")
    console.print(("updated" if previous else "created") + " garden.lock")
    for change in changes:
        console.print(f"- {change}")
    if not changes:
        console.print("- recorded the core/plugin compatibility set")
from .common import app  # noqa: E402 - register after command definition

app.add_typer(plugins_app, name="plugins", rich_help_panel=PANEL_SETUP)
