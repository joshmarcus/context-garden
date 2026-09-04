"""garden.yaml loading with defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "garden.yaml"

DEFAULTS: dict[str, Any] = {
    "work_dir": "",               # product clones and worktrees; empty = .garden (see Config.work_dir)
    "name": "garden",
    "principles_digest": "principles/00-index.md",
    "principles_dir": "principles",
    "runner": "local",
    "harness": "claude",
    "max_parallel": 10,
    "max_attempts": 2,
    "max_revisions": 3,
    "timeout_minutes": 90,
    "tick_interval": 60,
    "auto_revise": True,
    "auto_dispatch": True,
    "plan": {"auto_approve": True},
    "stack": True,                # start tasks on top of a dependency's open PR branch
    "discovered": {"auto_approve_blocking": True},  # blocking discovered work is created ready
    "stall": {"enabled": True},   # escalate to a human when revise rounds stop changing the diff
    "budgets": {},                # "<product>/<phase>": usd cap; also products.<name>.budget_usd
    "checks": {"pre_pr": [], "ci": [], "timeout_seconds": 600},
    "review": {
        "enabled": True,
        "max_rounds": 2,          # automated review rounds per PR
        "max_diff_chars": 60000,  # bigger diffs are read by the reviewer from git
        "harness": "",            # empty = default harness
        "difficulty": "",         # empty = the task's difficulty tier; or easy|medium|hard
        "personas": [],           # persona reviews to run on every new PR round, e.g. [security]
    },
    "harnesses": {},
    "ssh": {"hosts": []},
    "brief": {
        "inline_max_chars": 24000,  # reading-list files larger than this are listed, not inlined
        "total_max_chars": 120000,
    },
    "github": {
        "use_gh": True,  # prefer the gh CLI when available, else REST with GITHUB_TOKEN
        "draft_pr": True,         # open PRs as drafts; the human's triage marks them ready for review
        "reviewers": [],
    },
    "notify": {
        "command": "",            # shell command to run when a task needs a human; empty = disabled
        "timeout_seconds": 30,    # timeout for the command
    },
    "products": {},
}


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    env: str = ""

    @classmethod
    def load(cls, root: Path, env: str | None = None) -> Config:
        """garden.yaml, then garden.<env>.yaml (env from GARDEN_ENV, e.g. work/home), then
        garden.local.yaml (per machine, gitignored). Later files override earlier keys;
        dict values merge, lists and scalars replace."""
        import os

        env = os.environ.get("GARDEN_ENV", "") if env is None else env
        data = dict(DEFAULTS)
        sources: list[str] = []
        names = [CONFIG_NAME] + ([f"garden.{env}.yaml"] if env else []) + ["garden.local.yaml"]
        for name in names:
            p = root / name
            if p.exists():
                raw = yaml.safe_load(p.read_text()) or {}
                if not isinstance(raw, dict):
                    raise ValueError(f"{name}: top level must be a mapping")
                data = _merge(data, raw)
                sources.append(name)
        return cls(root=root, data=data, sources=sources, env=env)

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
        r = str(self.product(name).get("runner") or self.get("runner"))
        return "local" if r == "claude-local" else r

    def product_harness(self, name: str) -> str:
        return str(self.product(name).get("harness") or self.get("harness") or "claude")

    def harness(self, name: str):
        from .harness import Harness

        return Harness(name, dict((self.data.get("harnesses") or {}).get(name) or {}))

    @property
    def garden_dir(self) -> Path:
        """The garden's own state: state.json, events.jsonl, runs/, trials.jsonl."""
        return self.root / ".garden"

    @property
    def work_dir(self) -> Path:
        """Where product clones and per-task worktrees live. `work_dir` in config (absolute, or
        relative to the garden root); default `.garden`, the historical location. Putting it
        outside the garden keeps a worker that walks up from its checkout away from the garden,
        its venv and its state."""
        wd = self.get("work_dir")
        if not wd:
            return self.garden_dir
        return (self.root / str(wd)).resolve()

    @property
    def repos_dir(self) -> Path:
        return self.work_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.work_dir / "worktrees"

    def worktree_path(self, name: str) -> Path:
        """The worktree for `name` (a task id or a trial/phase name): the work dir, unless one
        already exists at the old location under .garden, which keeps running workers valid
        when work_dir changes."""
        new = self.worktrees_dir / name
        old = self.garden_dir / "worktrees" / name
        if new != old and old.exists() and not new.exists():
            return old
        return new


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) until a garden.yaml is found.

    Refuses to return a root whose .garden/ directory contains the starting path,
    so code running inside a worktree (.garden/worktrees/<id>) cannot act on the
    enclosing live garden.  The GARDEN_ROOT environment variable overrides the
    search entirely; if it points to a non-existent garden, the function raises
    with a message explaining that workers must not run garden commands.
    """
    import os

    env_root = os.environ.get("GARDEN_ROOT")
    if env_root:
        p = Path(env_root).resolve()
        if not (p / CONFIG_NAME).exists():
            raise FileNotFoundError(
                f"GARDEN_ROOT={env_root!r} does not contain {CONFIG_NAME}; "
                "workers must not run garden commands against the live garden"
            )
        # GARDEN_ROOT points to a real garden (e.g. set by check_ctx so check commands can
        # reference the live garden's venv via $GARDEN_ROOT).  Do NOT use it as the root:
        # fall through to the normal cwd walk so that tests running inside a check subprocess
        # find their own temp garden, not the live one.

    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / CONFIG_NAME).exists():
            # Refuse if the starting path is inside this candidate's .garden/ tree.
            try:
                cur.relative_to(candidate / ".garden")
                raise FileNotFoundError(
                    f"refusing to use {candidate} as the garden root: "
                    f"{cur} is inside its .garden/ directory — "
                    "workers must not act on the enclosing live garden"
                )
            except ValueError:
                return candidate
    raise FileNotFoundError(
        f"no {CONFIG_NAME} found in {cur} or its parents (run `garden init` to create one)"
    )
