"""Runner backends. A runner starts a worker on a prepared worktree and, later,
turns its raw output into a result dict. Runners never touch task files."""

from __future__ import annotations

from .base import Runner, RunnerError
from .claude_local import ClaudeLocalRunner
from .manual import ManualRunner

REGISTRY: dict[str, type[Runner]] = {
    ClaudeLocalRunner.name: ClaudeLocalRunner,
    ManualRunner.name: ManualRunner,
}


def get_runner(name: str, config: dict) -> Runner:
    try:
        cls = REGISTRY[name]
    except KeyError as e:
        raise RunnerError(f"unknown runner {name!r}; known: {', '.join(REGISTRY)}") from e
    return cls(config)


__all__ = ["REGISTRY", "Runner", "RunnerError", "get_runner"]
