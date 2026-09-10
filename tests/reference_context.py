from __future__ import annotations

from garden.reference_snapshot import read_reference_files
from garden.runs import Run


def agent_context(run: Run, brief: str | None = None) -> str:
    """Return the launch note plus the immutable files an agent can open."""
    brief = brief if brief is not None else (run.path / "brief.md").read_text()
    references = read_reference_files(run.path)
    return brief + "\n" + "\n".join(references.values())
