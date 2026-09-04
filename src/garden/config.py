"""garden.yaml loading with defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "garden.yaml"

DEFAULTS: dict[str, Any] = {
    "name": "garden",
    "principles_digest": "principles/00-index.md",
    "principles_dir": "principles",
    "runner": "claude-local",
    "max_parallel": 2,
    "max_attempts": 2,
    "tick_interval": 60,
    "auto_revise": True,
    "auto_dispatch": True,
    "brief": {
        "inline_max_chars": 24000,  # reading-list files larger than this are listed, not inlined
        "total_max_chars": 120000,
    },
    "claude": {
        "bin": "claude",
        "model": "",
        "max_turns": 60,
        "permission_mode": "acceptEdits",
        "allowed_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "MultiEdit"],
        "timeout_minutes": 90,
    },
    "github": {
        "use_gh": True,  # prefer the gh CLI when available, else REST with GITHUB_TOKEN
        "draft_pr": False,
        "reviewers": [],
    },
    "products": {},
}


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> Config:
        p = root / CONFIG_NAME
        raw: dict[str, Any] = {}
        if p.exists():
            raw = yaml.safe_load(p.read_text()) or {}
        return cls(root=root, data=_merge(DEFAULTS, raw))

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def product(self, name: str) -> dict[str, Any]:
        return dict(self.data.get("products", {}).get(name, {}) or {})

    def product_repo(self, name: str) -> Path | str:
        """A local path (resolved against root) or a URL for the product's code repo."""
        repo = self.product(name).get("repo", ".")
        if "://" in str(repo) or str(repo).startswith("git@"):
            return str(repo)
        return (self.root / str(repo)).resolve()

    def product_base_branch(self, name: str) -> str:
        return str(self.product(name).get("base_branch") or "main")

    def product_runner(self, name: str) -> str:
        return str(self.product(name).get("runner") or self.get("runner"))

    @property
    def garden_dir(self) -> Path:
        return self.root / ".garden"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) until a garden.yaml is found."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / CONFIG_NAME).exists():
            return candidate
    raise FileNotFoundError(
        f"no {CONFIG_NAME} found in {cur} or its parents (run `garden init` to create one)"
    )
