"""Run a harness headlessly in the task worktree on this machine, detached from the
scheduler. Only the worker spends tokens, and it only sees the brief."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..config import no_live_garden_root
from ..runs import Run
from .base import Runner, RunnerError, run_setup, scrubbed_env


class LocalRunner(Runner):
    name = "local"

    def harness_argv(self, run: Run, worktree: Path, final_path: Path | None) -> list[str]:
        """The harness argv for this run, with the binary resolved to its absolute path.

        The worktree and `run.fence_paths` are handed to the harness so it can scope the
        worker's writes to the worktree and deny edits to the live garden and the product
        clone (see Harness.fence_settings); the runner's fence, not the brief's."""
        assert self.harness is not None
        deny = list(run.fence_paths or [])
        if run.mode == "resume" and run.session_id:
            cmd = self.harness.resume_command(run.session_id, run.model, final_path, deny_paths=deny, worktree=worktree)
        else:
            cmd = self.harness.command(run.model, final_path, deny_paths=deny, worktree=worktree)
        resolved = shutil.which(self.harness.bin) or self.harness.bin
        if cmd and cmd[0] == self.harness.bin and resolved != self.harness.bin:
            cmd = [resolved] + cmd[1:]
        return cmd

    def harness_shell(self, run: Run, worktree: Path, final_path: Path | None) -> str:
        """`harness_argv` as one shell-quoted command line."""
        return " ".join(shlex.quote(c) for c in self.harness_argv(run, worktree, final_path))

    def worker_env(self, run: Run, setup: dict[str, Any], worktree: Path | None = None) -> dict[str, str]:
        """The environment a worker runs in: the scrubbed one (`runner.base.scrubbed_env`:
        an allowlist of this process's variables plus `worker_env.pass` and the product's
        `setup.env`, never the scheduler's GitHub token, cloud credentials or home directory —
        HOME is an isolated scratch home beside the worktree), plus the run's identity and a
        GARDEN_ROOT that keeps `garden` commands off the live garden: any `garden` command run
        inside the worktree hits find_root(), which checks this variable and fails loudly
        because the path below does not contain a garden.yaml."""
        wt = worktree if worktree is not None else (Path(run.worktree) if run.worktree else None)
        env = scrubbed_env(self.config, setup, worktree=wt)
        env["GARDEN_TASK_ID"] = run.task_id
        env["GARDEN_RUN_ID"] = run.run_id
        env["GARDEN_ROOT"] = no_live_garden_root(run.path)
        return env

    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        if self.harness is None:
            raise RunnerError("local runner needs a harness")
        d = run.path
        setup = dict(self.config.get("setup") or {})
        # The scrubbed environment (see worker_env): what the setup command and the worker
        # get, and nothing else of the scheduler's.
        env = self.worker_env(run, setup, worktree)
        run_setup(worktree, setup, log_path=d / "setup.log", env=env)  # prepare the env before the worker starts
        brief_path = d / "brief.md"
        brief_path.write_text(brief_text)
        self.launch(run, worktree, brief_path, env)

    def launch(self, run: Run, worktree: Path, brief_path: Path, env: dict[str, str]) -> None:
        """Start the harness detached: a shell runs it in the worktree with the brief on
        stdin, captures stdout/stderr beside the run record and writes `exit_code` when it
        ends (the completion signal reap waits for). The test suite's in-process runner
        overrides only this step."""
        assert self.harness is not None
        d = run.path
        inner = self.harness_shell(run, worktree, d / "final.md")
        timeout_min = int(self.config.get("timeout_minutes", 90) or 0)
        if timeout_min and shutil.which("timeout"):
            inner = f"timeout {timeout_min * 60} {inner}"
        script = (
            f"cd {shlex.quote(str(worktree))} && {inner} "
            f"< {shlex.quote(str(brief_path))} > {shlex.quote(str(d / 'stdout.json'))} "
            f"2> {shlex.quote(str(d / 'stderr.log'))}; echo $? > {shlex.quote(str(d / 'exit_code'))}"
        )
        proc = subprocess.Popen(
            ["sh", "-c", script], cwd=str(worktree), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        run.pid = proc.pid
        run.harness = self.harness.name
        run.save()
        (d / "command.txt").write_text(script + "\n")

    def start_checks(self, run: Run, worktree: Path, payload: dict[str, Any]) -> None:
        """Launch a check run detached: a shell runs `garden.checkrun` on the payload in the
        worktree and writes `exit_code` when it ends, so the tick starts the checks and reaps
        them later instead of running the product's suite in-process (CG-182). Overridden by
        the in-process test runner to run the same job synchronously."""
        d = run.path
        (d / "checks_input.json").write_text(json.dumps(payload))
        script = (
            f"{shlex.quote(sys.executable)} -m garden.checkrun {shlex.quote(str(d))} "
            f"> {shlex.quote(str(d / 'stdout.json'))} 2> {shlex.quote(str(d / 'stderr.log'))}; "
            f"echo $? > {shlex.quote(str(d / 'exit_code'))}"
        )
        proc = subprocess.Popen(
            ["sh", "-c", script], cwd=str(worktree),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        run.pid = proc.pid
        run.save()
        (d / "command.txt").write_text(script + "\n")

    def collect(self, run: Run) -> dict[str, Any]:
        assert self.harness is not None
        return self.harness.parse(run.stdout_text(), run.stderr_text(), run.path / "final.md", model=run.model)

    def doctor(self) -> list[str]:
        if os.name == "nt":
            return ["local runner: Windows is not supported; run garden in WSL (Windows Subsystem for Linux) instead"]
        if self.harness and not shutil.which(self.harness.bin):
            return [f"harness {self.harness.name}: binary {self.harness.bin!r} not found on PATH"]
        return []
