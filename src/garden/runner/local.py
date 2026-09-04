"""Run a harness headlessly in the task worktree on this machine, detached from the
scheduler. Only the worker spends tokens, and it only sees the brief."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import no_live_garden_root
from ..runs import Run
from .base import Runner, RunnerError, run_setup


class LocalRunner(Runner):
    name = "local"

    def harness_shell(self, run: Run, final_path: Path | None) -> str:
        """Build the shell command with the harness binary resolved to its absolute path."""
        assert self.harness is not None
        if run.mode == "resume" and run.session_id:
            cmd = self.harness.resume_command(run.session_id, run.model, final_path)
        else:
            cmd = self.harness.command(run.model, final_path)
        resolved = shutil.which(self.harness.bin) or self.harness.bin
        if cmd and cmd[0] == self.harness.bin and resolved != self.harness.bin:
            cmd = [resolved] + cmd[1:]
        return " ".join(shlex.quote(c) for c in cmd)

    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        if self.harness is None:
            raise RunnerError("local runner needs a harness")
        d = run.path
        setup = dict(self.config.get("setup") or {})
        run_setup(worktree, setup, log_path=d / "setup.log")  # prepare the env before the worker starts
        brief_path = d / "brief.md"
        brief_path.write_text(brief_text)
        final_path = d / "final.md"
        inner = self.harness_shell(run, final_path)
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
        for k, v in (setup.get("env") or {}).items():  # the prepared environment the worker runs in
            env[str(k)] = str(v)
        env["GARDEN_TASK_ID"] = run.task_id
        env["GARDEN_RUN_ID"] = run.run_id
        # Prevent the worker from finding and mutating the live garden: any `garden`
        # command run inside the worktree will hit find_root() which checks this variable
        # and fails loudly because the path below does not contain a garden.yaml.
        env["GARDEN_ROOT"] = no_live_garden_root(d)
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
        if os.name == "nt":
            return ["local runner: Windows is not supported; run garden in WSL (Windows Subsystem for Linux) instead"]
        if self.harness and not shutil.which(self.harness.bin):
            return [f"harness {self.harness.name}: binary {self.harness.bin!r} not found on PATH"]
        return []
