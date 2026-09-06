"""The in-process runner: the suite's stand-in for the local runner.

A scheduler test must never wait on a worker. The local runner launches the harness as a
detached shell and reap polls for its `exit_code` file, so a test driving the real thing
had to sleep-and-poll until the fake finished: timing-fragile, and the source of four
flake fixes in one phase. This runner keeps everything the local runner does (setup, the
brief, the worker environment, the resolved harness argv, `command.txt`, the captured
output and the exit code beside the run record) but runs the fake harness synchronously in
this process instead of spawning it. By the time `Scheduler.dispatch()` returns, the run
has finished and the next `tick()` reaps it; nothing sleeps and nothing is left running.

Records look exactly like a local run's (`run.runner == "local"`), so reap, the run pages
and the CLI see no difference. The current test process stands in for the worker pid, and
a `stall` worker is simply a run with no `exit_code`, which is what the idle and timeout
checks look for anyway.

The `in_process_workers` autouse fixture in tests/conftest.py swaps this class into the
runner registry under `local` for every test. Tests of the real local runner's launch
mechanics construct `LocalRunner` directly and stub `subprocess.Popen`.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from garden.runner.base import RunnerError
from garden.runner.local import LocalRunner
from garden.runs import Run

from . import fake_claude, fake_codex

Fake = Callable[[list[str], str, Path, Mapping[str, str]], tuple[str, str, int | None]]

# The harness binary's file name -> the fake that stands in for it, in process.
FAKES: dict[str, Fake] = {
    "fake_claude.py": fake_claude.run,
    "fake_codex.py": fake_codex.run,
}


class InProcessRunner(LocalRunner):
    """A local runner whose harness is a Python function called right here."""

    name = "local"

    def launch(self, run: Run, worktree: Path, brief_path: Path, env: dict[str, str]) -> None:
        assert self.harness is not None
        d = run.path
        argv = self.harness_argv(run, worktree, d / "final.md")
        # What the shell wrapper records for a real run: the resolved command line.
        (d / "command.txt").write_text(" ".join(shlex.quote(c) for c in argv) + "\n")
        run.pid = os.getpid()
        run.harness = self.harness.name
        run.save()
        # The shell redirects create both files before the harness prints anything.
        (d / "stdout.json").write_text("")
        (d / "stderr.log").write_text("")
        fake = FAKES.get(Path(argv[0]).name)
        if fake is None:
            if not (Path(argv[0]).exists() or shutil.which(argv[0])):
                # What the shell says about a binary that is not there (a harness configured
                # wrong, or a trial contender meant to crash): exit 127, nothing on stdout.
                stdout, stderr, code = "", f"sh: 1: {argv[0]}: not found\n", 127
            else:
                raise RunnerError(f"in-process runner has no fake for harness binary {argv[0]!r}; "
                                  f"known: {', '.join(sorted(FAKES))}")
        else:
            stdout, stderr, code = fake(argv[1:], brief_path.read_text(), worktree, env)
        (d / "stdout.json").write_text(stdout)
        (d / "stderr.log").write_text(stderr)
        if code is not None:
            (d / "exit_code").write_text(f"{code}\n")  # the completion signal reap waits for

    def start_checks(self, run: Run, worktree: Path, payload: dict) -> None:
        """Run the same check job the local runner would launch, but synchronously in this
        process: by the time the tick that started it returns, `checks.json` and `exit_code`
        are written, so the next tick reaps it — exactly the two-tick shape reviews have."""
        from garden.checkrun import run_check_job

        d = run.path
        env = self.worker_env(run, dict(self.config.get("setup") or {}), worktree)
        if env.get("TMPDIR"):
            payload = {**payload, "temp_dir": env["TMPDIR"]}
        (d / "checks_input.json").write_text(json.dumps(payload))
        (d / "stdout.json").write_text("")
        (d / "stderr.log").write_text("")
        run.pid = os.getpid()
        run.save()
        results = run_check_job(payload)
        (d / "checks.json").write_text(json.dumps(results))
        (d / "exit_code").write_text("0\n")

    def _probe_launch(self, argv: list[str], stdin_text: str, cwd: Path, env: dict[str, str]) -> tuple[str, str]:
        fake = FAKES.get(Path(argv[0]).name)
        if fake is None:
            raise RunnerError(f"in-process runner has no fake for harness binary {argv[0]!r}; "
                              f"known: {', '.join(sorted(FAKES))}")
        stdout, stderr, _code = fake(argv[1:], stdin_text, cwd, env)
        return stdout, stderr

    def wake(self, run: Run) -> None:
        """Finish a `stall` run now, as if the silent worker woke up and completed a plain
        `done` round: the same launch, with the mode override dropped from its environment.
        For a test that needs a task to stay `running` across a tick and then move on."""
        env = self.worker_env(run, dict(self.config.get("setup") or {}))
        env.pop("FAKE_CLAUDE_MODE", None)
        self.launch(run, Path(run.worktree), run.path / "brief.md", env)
