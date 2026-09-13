"""Build the deliberately small, deployable public garden projection.

The projection is data, not a filtered view of a live :class:`Store`.  A public server can
therefore run with this directory as its only input and has no route to scheduler state,
credentials, source files, or the private garden checkout.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .model import split_frontmatter
from .store import Store

SCHEMA_VERSION = 1
SAFE_FIELDS = frozenset({"project.summary", "phase.summary", "task.summary", "task.dependencies"})
CONTENT_FIELDS = frozenset({"project.content", "phase.content", "task.content"})
PUBLICATION_FIELDS = SAFE_FIELDS | CONTENT_FIELDS


def _document(path: Path | None) -> str:
    if path is None:
        return ""
    return split_frontmatter(path.read_text())[1].strip()


def _task_content(body: str) -> str:
    """Free-form task prose excludes the scheduler's append-only operational log."""
    return re.split(r"^## Log\s*$", body, maxsplit=1, flags=re.MULTILINE)[0].strip()


def build_public_projection(store: Store) -> dict[str, Any]:
    """Return only explicitly published projects and fields.

    ``publication.projects`` is intentionally absent/empty by default. Unknown field names
    fail publication instead of being silently copied as future model fields are added.
    """
    configured = store.config.get("publication.projects") or {}
    if not isinstance(configured, dict):
        raise ValueError("publication.projects must be a mapping")
    projects: list[dict[str, Any]] = []
    for product in store.products():
        settings = configured.get(product.name)
        if settings is None:
            continue
        if not isinstance(settings, dict) or not isinstance(settings.get("fields", []), list):
            raise ValueError(f"publication.projects.{product.name}.fields must be a list")
        fields = set(settings.get("fields", []))
        unknown = fields - PUBLICATION_FIELDS
        if unknown:
            raise ValueError(f"unknown public fields for {product.name}: {', '.join(sorted(unknown))}")
        row: dict[str, Any] = {"id": product.name, "phases": []}
        if "project.summary" in fields:
            row["summary"] = str(product.config.get("summary") or product.name)
        if "project.content" in fields:
            row["content"] = _document(product.overview_path)
        published_task_ids = {
            task.id for phase in product.phases for task in phase.tasks
            if "task.summary" in fields
        }
        for phase in product.phases:
            phase_row: dict[str, Any] = {"id": phase.name, "tasks": []}
            if "phase.summary" in fields:
                phase_row["summary"] = str(phase.meta.get("summary") or phase.name)
            if "phase.content" in fields:
                phase_row["content"] = _document(phase.goals_path)
            if "task.summary" in fields:
                for task in phase.tasks:
                    task_row: dict[str, Any] = {
                        "id": task.id, "title": task.title, "status": task.status.value,
                    }
                    if "task.dependencies" in fields:
                        task_row["dependencies"] = [
                            dependency for dependency in task.depends_on
                            if dependency in published_task_ids
                        ]
                    if "task.content" in fields:
                        task_row["content"] = _task_content(task.body)
                    phase_row["tasks"].append(task_row)
            row["phases"].append(phase_row)
        projects.append(row)
    return {"schema": SCHEMA_VERSION, "projects": projects}


def write_public_projection(store: Store, destination: Path) -> Path:
    """Atomically replace the projection snapshot, making revocation immediate for readers."""
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = destination / "projection.json"
    payload = json.dumps(build_public_projection(store), ensure_ascii=False, sort_keys=True)
    fd, temporary = tempfile.mkstemp(prefix=".projection-", dir=destination)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, snapshot)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return snapshot
