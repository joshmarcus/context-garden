"""Templates for `garden init`, `new-product`, `new-phase`, `new-task`."""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import CONFIG_NAME
from .store import Store

DIGEST_TEMPLATE = """\
# Principles digest

This file is inlined into every agent brief. Keep it under ~60 lines; put the long-form
reasoning in sibling files in this directory and link to them from tasks' reading lists.

- Small, shippable slices. Every PR leaves the product working.
- Tests before "done". Run the project's own checks before reporting.
- Say what you don't know. A precise blocked-report beats a confident guess.
- Don't widen scope. Note follow-ups in the PR body instead.
"""

PRODUCT_TEMPLATE = """\
# {name}

One paragraph on what this product is, who it is for, and what "good" looks like.

## Repo

Code lives in `{repo}` (base branch `{base}`). Describe how to run tests and checks here so
every task brief carries it.

## Conventions

- ...
"""

GOALS_TEMPLATE = """\
# {phase} goals

## Why this phase

What changes for users/the team when this phase ships.

## Goals

1. ...
2. ...

## Non-goals

- ...

## Definition of done

- ...
"""

TASK_TEMPLATE = """\
## Goal

One or two sentences.

## Context

What the agent needs to know that is not in the reading list.

## Acceptance criteria

- [ ] ...

## Out of scope

- ...
"""


def init_garden(directory: Path, name: str) -> list[Path]:
    created = []
    directory.mkdir(parents=True, exist_ok=True)
    cfg = directory / CONFIG_NAME
    if not cfg.exists():
        cfg.write_text(yaml.safe_dump({
            "name": name,
            "runner": "claude-local",
            "max_parallel": 2,
            "max_attempts": 2,
            "max_revisions": 3,
            "tick_interval": 60,
            "auto_revise": True,
            "products": {},
        }, sort_keys=False))
        created.append(cfg)
    pdir = directory / "principles"
    pdir.mkdir(exist_ok=True)
    digest = pdir / "00-index.md"
    if not digest.exists():
        digest.write_text(DIGEST_TEMPLATE)
        created.append(digest)
    gi = directory / ".gitignore"
    if not gi.exists() or ".garden/" not in gi.read_text():
        with gi.open("a") as f:
            f.write(".garden/\n")
        created.append(gi)
    return created


def new_product(store: Store, name: str, repo: str, base_branch: str) -> list[Path]:
    created = []
    d = store.root / name
    d.mkdir(exist_ok=True)
    overview = d / "product.md"
    if not overview.exists():
        overview.write_text(PRODUCT_TEMPLATE.format(name=name, repo=repo, base=base_branch))
        created.append(overview)
    cfg_path = store.root / CONFIG_NAME
    data = yaml.safe_load(cfg_path.read_text()) or {}
    products = data.setdefault("products", {}) or {}
    if name not in products:
        products[name] = {"repo": repo, "base_branch": base_branch}
        data["products"] = products
        cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
        created.append(cfg_path)
    return created


def new_phase(store: Store, product: str, phase: str) -> list[Path]:
    created = []
    d = store.root / product / phase
    for sub in ("specs", "tasks"):
        (d / sub).mkdir(parents=True, exist_ok=True)
        keep = d / sub / ".gitkeep"
        if not any((d / sub).iterdir()):
            keep.write_text("")
            created.append(keep)
    goals = d / "goals.md"
    if not goals.exists():
        goals.write_text(GOALS_TEMPLATE.format(phase=phase))
        created.append(goals)
    return created
