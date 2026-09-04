"""Run a harness headlessly in the task worktree on this machine, detached from the
scheduler. Only the worker spends tokens, and it only sees the brief."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..runs import Run
from .base import Runner, RunnerError


class LocalRunner(Runner):
    name = "local"

    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        if self.harness is None:
            raise RunnerError("local runner needs a harness")
        d = run.path
        brief_path = d / "brief.md"
        brief_path.write_text(brief_text)
        final_path = d / "final.md"
        inner = self.harness.shell_command(run.model, final_path)
        timeout_min = int(self.config.get("timeout_minutes", 90) or 0)
        if timeout_min and shutil.which("timeout"):
            inner = f"timeout {timeout_min * 60} {inner}"
        script = (
            f"cd {shlex.quote(str(worktree))} && {inner} "
            f"< {shlex.quote(str(brief_path))} > {shlex.quote(str(d / 'stdout.json'))} "
            f"2> {shlex.quote(str(d / 'stderr.log'))}; echo $? > {shlex.quote(str(d / 'exit_code'))}"
        )
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow launching from inside another Claude Code session
        env["GARDEN_TASK_ID"] = run.task_id
        env["GARDEN_RUN_ID"] = run.run_id
        proc = subprocess.Popen(
            ["sh", "-c", script], cwd=str(worktree), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        run.pid = proc.pid
        run.harness = self.harness.name
        run.save()
        (d / "command.txt").write_text(script + "\n")

    def collect(self, run: Run) -> dict[str, Any]:
        assert self.harness is not None
        return self.harness.parse(run.stdout_text(), run.stderr_text(), run.path / "final.md")

    def doctor(self) -> list[str]:
        if self.harness and not shutil.which(self.harness.bin):
            return [f"harness {self.harness.name}: binary {self.harness.bin!r} not found on PATH"]
        return []
