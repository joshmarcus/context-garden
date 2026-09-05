"""The README is the front door: every command and page it names must exist."""

from __future__ import annotations

import re
from pathlib import Path

from typer.main import get_command

from garden.cli import app
from garden.store import Store
from garden.web.app import create_app

README = (Path(__file__).parents[1] / "README.md").read_text()

# The web pages the README names in bold, and the route each one is served at.
PAGES = {
    "Inbox": "/",
    "Board": "/board",
    "Trellis": "/trellis",
    "Timeline": "/events",
    "Trials": "/trials",
    "Runs": "/runs",
    "Costs": "/costs",
    "Config": "/config",
    "Herbarium": "/herbarium",
}


def test_readme_garden_commands_are_registered() -> None:
    """Every ``garden <verb>`` the README shows is a command in ``garden --help``."""
    documented = set(re.findall(r"`garden ([a-z][a-z-]*)", README))
    registered = set(get_command(app).commands)
    assert documented, "the README names no garden commands"
    assert documented <= registered, documented - registered


def test_readme_named_pages_have_routes(garden: Path) -> None:
    """Every page the README names in bold is one the web app serves."""
    named = set(re.findall(r"\*\*([A-Z][a-z]+)\*\*", README)) & set(PAGES)
    assert {"Inbox", "Board", "Trellis", "Costs", "Config", "Herbarium"} <= named
    routes = {route.path for route in create_app(Store(garden)).routes}
    missing = {page for page in named if PAGES[page] not in routes}
    assert not missing, missing
    assert {"/tasks/{task_id}", "/runs/{task_id}/{run_id}", "/phases/{product}/{phase}"} <= routes


def test_readme_links_resolve() -> None:
    """Every relative link and image in the README points at a file in the repo."""
    root = Path(__file__).parents[1]
    targets = re.findall(r"\]\((?!https?://)([^)#]+)\)", README)
    assert targets
    missing = [t for t in targets if not (root / t).exists()]
    assert not missing, missing
