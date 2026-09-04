"""A human-driven session (e.g. an interactive Claude Code session using the
`garden-take` skill). `start` only records the brief; completion arrives through
`garden finish <task> --result ...`, which writes result.json and exit_code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..runs import Run
from .base import Runner


class ManualRunner(Runner):
    name = "manual"
    detached = False

    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        (run.path / "brief.md").write_text(brief_text)
        run.pid = None
        run.save()

    def collect(self, run: Run) -> dict[str, Any]:
        p = run.path / "result.json"
        out: dict[str, Any] = {"result": {}, "usage": {}, "cost_usd": None, "final_text": "", "error": ""}
        if p.exists():
            try:
                out["result"] = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                out["error"] = f"bad result.json: {e}"
        else:
            out["error"] = "no result recorded (use `garden finish`)"
        return out

    @staticmethod
    def finish(run: Run, result: dict[str, Any]) -> None:
        (run.path / "result.json").write_text(json.dumps(result, indent=2))
        (run.path / "exit_code").write_text("0\n")
