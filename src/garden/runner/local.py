"""Run a harness headlessly in the task worktree on this machine, detached from the
scheduler. Only the worker spends tokens, and it only sees the brief."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..config import no_live_garden_root
from ..runs import Run
from ..sandbox import SandboxPolicy
from ..validation import bounded_validation_timeout_seconds
from .base import Runner, RunnerError, run_temp_dir, scrubbed_env


class LocalRunner(Runner):
    name = "local"

    def harness_argv(self, run: Run, worktree: Path, final_path: Path | None) -> list[str]:
        """The harness argv for this run, with the binary resolved to its absolute path.

        The worktree and `run.fence_paths` are handed to the harness so it can scope the
        worker's writes to the worktree and deny edits to the live garden and the product
        clone (see Harness.fence_settings); the runner's fence, not the brief's."""
        assert self.harness is not None
        deny = list(run.fence_paths or [])
        policy = SandboxPolicy.from_config(self.config)
        if run.mode == "resume" and run.session_id:
            cmd = self.harness.resume_command(run.session_id, run.model, final_path, deny_paths=deny,
                                              worktree=worktree, sandbox_policy=policy)
        else:
            cmd = self.harness.command(run.model, final_path, deny_paths=deny, worktree=worktree,
                                       sandbox_policy=policy)
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
        # This private supervisor input belongs only to a detached check or nested
        # garden.validation invocation. Never let an enclosing process cap a model run.
        env.pop("GARDEN_EXECUTION_TIMEOUT_SECONDS", None)
        env["GARDEN_TASK_ID"] = run.task_id
        env["GARDEN_RUN_ID"] = run.run_id
        # Deep dives are controller diagnostics, not implementation workers. They are the
        # one run type intentionally given the real workspace root for read access; the
        # ordinary worktree fence still prevents writes there.
        env["GARDEN_ROOT"] = (str(run.path.parents[3]) if run.mode == "investigation"
                              else no_live_garden_root(run.path))
        work_dir = self.config.get("work_dir")
        if work_dir:
            temp_dir = run_temp_dir(work_dir, run)
            temp_dir.mkdir(parents=True, exist_ok=True)
            env["TMPDIR"] = str(temp_dir)
            env["PYTEST_DEBUG_TEMPROOT"] = str(temp_dir)
        env["GARDEN_HEAVY_TEST_PARALLEL"] = str(
            int(self.config.get("resources", {}).get("heavy_test_parallel", 1))
        )
        env["GARDEN_EXECUTION_CGROUP"] = str(
            self.config.get("resources", {}).get("execution_cgroup", "") or ""
        )
        env["GARDEN_VALIDATION_TIMEOUT_SECONDS"] = str(
            int(self.config.get("checks", {}).get("timeout_seconds", 900) or 900)
        )
        return env

    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        if self.harness is None:
            raise RunnerError("local runner needs a harness")
        d = run.path
        setup = dict(self.config.get("setup") or {})
        # The scrubbed environment (see worker_env): what the setup command and the worker
        # get, and nothing else of the scheduler's.
        env = self.worker_env(run, setup, worktree)
        policy = SandboxPolicy.from_config(self.config)
        mechanism = policy.native_harness(self.harness.name, str(self.harness.cfg.get("permission_mode") or ""))
        env.update(policy.report_env(mechanism))
        if mechanism:
            (d / "sandbox.json").write_text(policy.summary(mechanism) + "\n")
        # The supervisor runs setup only after it owns the heavy-execution lease and has
        # entered the execution cgroup.  Persisting the small payload also keeps the
        # detached launch recoverable/auditable.
        if str(setup.get("command") or "").strip():
            (d / "setup_input.json").write_text(json.dumps({"setup": setup, "config": self.config}))
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
        timeout_min = float(self.config.get("timeout_minutes", 90) or 0)
        env = dict(env)
        if timeout_min:
            # GNU ``timeout`` is not part of macOS. The Python supervisor owns the same
            # monotonic deadline on every supported POSIX host and can terminate the whole
            # process tree rather than only the shell leader.
            env["GARDEN_EXECUTION_TIMEOUT_SECONDS"] = f"{timeout_min * 60:g}"
            env["GARDEN_EXECUTION_TIMEOUT_KIND"] = "worker"
        script = (
            f"cd {shlex.quote(str(worktree))} && {inner} "
            f"< {shlex.quote(str(brief_path))} > {shlex.quote(str(d / 'stdout.json'))} "
            f"2> {shlex.quote(str(d / 'stderr.log'))}"
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "garden.run_supervisor", str(d), script], cwd=str(worktree), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        run.pid = proc.pid
        run.status = "running"
        run.harness = self.harness.name
        run.save()
        (d / "command.txt").write_text(script + "\n")

    def start_checks(self, run: Run, worktree: Path, payload: dict[str, Any]) -> None:
        """Launch a check run detached: a shell runs `garden.checkrun` on the payload in the
        worktree and writes `exit_code` when it ends, so the tick starts the checks and reaps
        them later instead of running the product's suite in-process (CG-182). Overridden by
        the in-process test runner to run the same job synchronously."""
        d = run.path
        env = self.worker_env(run, dict(self.config.get("setup") or {}), worktree)
        env["GARDEN_HEAVY_EXECUTION"] = "1"
        execution_timeout = bounded_validation_timeout_seconds(env.get("GARDEN_VALIDATION_TIMEOUT_SECONDS"))
        env["GARDEN_EXECUTION_TIMEOUT_SECONDS"] = f"{execution_timeout:g}"
        env["GARDEN_EXECUTION_TIMEOUT_KIND"] = "validation"
        if env.get("TMPDIR"):
            payload = {**payload, "temp_dir": env["TMPDIR"]}
        (d / "checks_input.json").write_text(json.dumps(payload))
        script = (
            f"{shlex.quote(sys.executable)} -m garden.checkrun {shlex.quote(str(d))} "
            f"> {shlex.quote(str(d / 'stdout.json'))} 2> {shlex.quote(str(d / 'stderr.log'))}"
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "garden.run_supervisor", str(d), script], cwd=str(worktree), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        run.pid = proc.pid
        run.status = "running"
        run.save()
        (d / "command.txt").write_text(script + "\n")

    def collect(self, run: Run) -> dict[str, Any]:
        assert self.harness is not None
        return self.harness.parse(run.stdout_text(), run.stderr_text(), run.path / "final.md", model=run.model)

    def probe(self, cwd: Path) -> dict[str, Any]:
        """Run the harness's `login_probe` — no permission flags, no allowed tools, no fence
        (see Harness.login_probe) — in a throwaway `cwd`, through the same scrubbed
        environment a worker gets. Unlike a real dispatch this never grants edit/Bash
        permissions: a paused harness's probe must not be able to touch anything."""
        assert self.harness is not None
        cwd.mkdir(parents=True, exist_ok=True)
        argv, stdin_text = self.harness.login_probe()
        resolved = shutil.which(self.harness.bin) or self.harness.bin
        if argv and argv[0] == self.harness.bin and resolved != self.harness.bin:
            argv = [resolved] + argv[1:]
        env = scrubbed_env(self.config, dict(self.config.get("setup") or {}), worktree=cwd)
        try:
            stdout, stderr = self._probe_launch(argv, stdin_text, cwd, env)
        except (subprocess.TimeoutExpired, OSError) as e:
            return {"final_text": "", "usage": {}, "cost_usd": None, "session_id": "", "result": {},
                    "error": str(e), "env_error": True, "env_kind": "probe_failed"}
        return self.harness.parse(stdout, stderr)

    def _probe_launch(self, argv: list[str], stdin_text: str, cwd: Path, env: dict[str, str]) -> tuple[str, str]:
        """The actual invocation, split out so the test suite's in-process runner can call the
        fake harness synchronously instead of spawning a real process (see tests/inprocess.py)."""
        with tempfile.TemporaryDirectory(prefix="garden-probe-", dir=cwd) as raw_dir:
            run_dir = Path(raw_dir)
            stdin_path = run_dir / "stdin"
            stdout_path = run_dir / "stdout"
            stderr_path = run_dir / "stderr"
            stdin_path.write_text(stdin_text)
            script = (
                f"{shlex.join(argv)} < {shlex.quote(str(stdin_path))} "
                f"> {shlex.quote(str(stdout_path))} 2> {shlex.quote(str(stderr_path))}"
            )
            supervised_env = {
                **env,
                "GARDEN_HEAVY_EXECUTION": "1",
                "GARDEN_HEAVY_TEST_PARALLEL": str(
                    int(self.config.get("resources", {}).get("heavy_test_parallel", 1))
                ),
                "GARDEN_EXECUTION_CGROUP": str(
                    self.config.get("resources", {}).get("execution_cgroup", "") or ""
                ),
                "GARDEN_EXECUTION_TIMEOUT_SECONDS": "90",
                "GARDEN_EXECUTION_TIMEOUT_KIND": "probe",
            }
            subprocess.run(
                [sys.executable, "-m", "garden.run_supervisor", str(run_dir), script],
                cwd=str(cwd), env=supervised_env, timeout=95, check=False,
            )
            return stdout_path.read_text(), stderr_path.read_text()

    def doctor(self) -> list[str]:
        if os.name == "nt":
            return ["local runner: Windows is not supported; run garden in WSL (Windows Subsystem for Linux) instead"]
        if self.harness and not shutil.which(self.harness.bin):
            return [f"harness {self.harness.name}: binary {self.harness.bin!r} not found on PATH"]
        return []
