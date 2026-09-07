"""The architecture module map stays in step with the Python package."""

from __future__ import annotations

import re
from pathlib import Path


def _documented_modules(architecture: str) -> set[str]:
    module_map = architecture.split("## Module map", 1)[1].split("## Where state lives", 1)[0]
    return set(re.findall(r"`((?:[^`]+/)*[^`]+\.py)`", module_map))


def _source_modules(root: Path) -> set[str]:
    return {
        path.relative_to(root / "src/garden").as_posix()
        for path in (root / "src/garden").rglob("*.py")
    }


def test_every_python_module_appears_in_architecture_map() -> None:
    root = Path(__file__).parents[1]
    architecture = (root / "docs/architecture.md").read_text()
    modules = _source_modules(root)
    documented = _documented_modules(architecture)

    missing = sorted(modules - documented)

    assert not missing, f"modules missing from docs/architecture.md: {missing}"


def test_architecture_map_detects_a_removed_module_row() -> None:
    root = Path(__file__).parents[1]
    architecture = (root / "docs/architecture.md").read_text()
    modules = _source_modules(root)
    row = next(line for line in architecture.splitlines() if "| `kickoff.py` |" in line)
    documented = _documented_modules(architecture.replace(row + "\n", ""))

    assert "kickoff.py" in modules - documented
