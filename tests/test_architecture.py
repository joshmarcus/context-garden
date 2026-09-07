"""The architecture module map stays in step with the Python package."""

from __future__ import annotations

from pathlib import Path


def test_every_python_module_appears_in_architecture_map() -> None:
    root = Path(__file__).parents[1]
    architecture = (root / "docs/architecture.md").read_text()
    modules = {
        path.relative_to(root / "src/garden").as_posix()
        for path in (root / "src/garden").rglob("*.py")
    }

    missing = sorted(module for module in modules if module not in architecture)

    assert not missing, f"modules missing from docs/architecture.md: {missing}"
