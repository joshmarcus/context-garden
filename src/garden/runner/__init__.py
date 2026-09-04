"""Runner backends. A runner starts a worker (given a brief and, for local runners, a
worktree) and later turns its raw output into a result dict. Runners never touch task files."""

from __future__ import annotations

from typing import Any

from ..harness import Harness
from .base import Runner, RunnerError
from .local import LocalRunner
from .manual import ManualRunner
from .ssh import SSHRunner

REGISTRY: dict[str, type[Runner]] = {
    LocalRunner.name: LocalRunner,
    SSHRunner.name: SSHRunner,
    ManualRunner.name: ManualRunner,
    "claude-local": LocalRunner,  # backwards-compatible alias
}


def get_runner(name: str, config: dict[str, Any], harness: Harness | None = None) -> Runner:
    try:
        cls = REGISTRY[name]
    except KeyError as e:
        raise RunnerError(f"unknown runner {name!r}; known: {', '.join(sorted(REGISTRY))}") from e
    return cls(config, harness)


__all__ = ["Runner", "RunnerError", "get_runner", "REGISTRY", "Harness"]
