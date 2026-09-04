from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..harness import Harness
from ..runs import Run


class RunnerError(Exception):
    pass


class Runner(ABC):
    name: str = "base"
    detached: bool = True  # False = a human drives the session; completion comes via `garden finish`
    remote: bool = False  # True = the worker pushes the branch itself; no local worktree during the run

    def __init__(self, config: dict[str, Any], harness: Harness | None = None):
        self.config = config
        self.harness = harness

    def assign(self, run: Run, active: list[Run]) -> None:  # noqa: B027
        """Optional: pick a host / slot before start (ssh runner)."""

    @abstractmethod
    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        """Launch the worker. Must return immediately; must arrange for run.dir/exit_code."""

    @abstractmethod
    def collect(self, run: Run) -> dict[str, Any]:
        """After the process finished: {"result": {...}, "usage": {...}, "cost_usd": float|None,
        "final_text": str, "error": str}."""

    def harness_shell(self, run: Run, final_path: Path | None) -> str:
        """The harness command for this run: a resume when the run carries a session id."""
        assert self.harness is not None
        if run.mode == "resume" and run.session_id:
            return self.harness.shell_resume_command(run.session_id, run.model, final_path)
        return self.harness.shell_command(run.model, final_path)

    def doctor(self) -> list[str]:
        return []
