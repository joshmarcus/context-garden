"""Run-scoped, read-only reference bundles used by concise agent launch notes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .host_identity import scrub_shared_text

REFERENCE_DIR = "references"


def write_reference_files(
    run_path: Path, files: dict[str, str], config: dict[str, Any] | None = None,
) -> Path | None:
    """Materialize scrubbed text below a run-owned directory without following escapes."""
    if not files:
        return None
    root = run_path / REFERENCE_DIR
    if root.exists():
        root.chmod(0o755)
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = (root / rel).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"reference path escapes snapshot: {rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scrub_shared_text(content, config or {}))
        path.chmod(0o444)
    for directory, _, _ in os.walk(root, topdown=False):
        Path(directory).chmod(0o555)
    return root


def read_reference_files(run_path: Path) -> dict[str, str]:
    """Return the bounded reference payload for an authenticated remote claim."""
    root = run_path / REFERENCE_DIR
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def materialize_remote_references(execution_dir: Path, payload: Any) -> Path | None:
    files = payload if isinstance(payload, dict) else {}
    return write_reference_files(execution_dir, {
        str(key): str(value) for key, value in files.items()
    })
