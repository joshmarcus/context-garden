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
from ..reference_snapshot import REFERENCE_DIR
from ..runs import Run
from ..sandbox import SandboxPolicy
from ..storage import StorageAdmissionError, require_storage
from ..validation import bounded_validation_timeout_seconds
from ..workload_identity import WorkloadIdentityError, subprocess_authority
from .base import (
    Runner,
    RunnerError,
    run_temp_dir,
    scrubbed_env,
    worker_credentials_dir,
    worker_home,
)


def _trusted_module_root() -> Path:
    """Return the controller-owned directory containing the loaded ``garden`` package.

    Controller-owned checks retain scheduler credentials for trusted Python analysers.  They
    must therefore never use a task worktree as an import root: a branch could add a
    ``garden`` package which shadows the supervisor or check runner before either can
    establish the scrubbed child-command boundary.
    """
    root = Path(__file__).resolve().parents[2]
    if not (root / "garden" / "run_supervisor.py").is_file():
        raise RunnerError(f"trusted garden package is unavailable under {root}")
    return root


def _trusted_module_environment(env: dict[str, str]) -> dict[str, str]:
    """Pin module imports for a privileged controller subprocess to loaded code."""
    result = dict(env)
    result["PYTHONPATH"] = str(_trusted_module_root())
    return result


class LocalRunner(Runner):
    name = "local"

    def harness_output_path(self, run: Run, worktree: Path) -> Path:
        """Return a model-writable result path outside protected scheduler state.

        Required sandboxes cannot grant the harness access to ``run.path/final.md``.  Give
        each run a narrow sibling output root instead; the trusted supervisor copies the
        completed result into the run record after the sandboxed command exits.
        """
        policy = SandboxPolicy.from_config(self.config)
        if not policy.required:
            return run.path / "final.md"
        output_dir = worktree.parent / f".garden-output-{worktree.name}-{run.run_id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / "final.md"

    def harness_argv(self, run: Run, worktree: Path, final_path: Path | None) -> list[str]:
        """The harness argv for this run, with the binary resolved to its absolute path.

        The worktree and `run.fence_paths` are handed to the harness so it can scope the
        worker's writes to the worktree and deny edits to the live garden and the product
        clone (see Harness.fence_settings); the runner's fence, not the brief's."""
        assert self.harness is not None
        deny = list(run.fence_paths or [])
        policy = SandboxPolicy.from_config(self.config)
        if policy.required:
            # Callers may still use the historical run-state path. Never turn that into a
            # writable sandbox grant; required isolation owns the result location.
            final_path = self.harness_output_path(run, worktree)
        if run.mode == "resume" and run.session_id:
            cmd = self.harness.resume_command(run.session_id, run.model, final_path,
                                              difficulty=run.difficulty, deny_paths=deny,
                                              worktree=worktree, sandbox_policy=policy)
        else:
            cmd = self.harness.command(run.model, final_path, difficulty=run.difficulty,
                                       deny_paths=deny, worktree=worktree,
                                       sandbox_policy=policy)
        resolved = shutil.which(self.harness.bin) or self.harness.bin
        if cmd and cmd[0] == self.harness.bin and resolved != self.harness.bin:
            cmd = [resolved] + cmd[1:]
        if policy.required:
            output_roots = [final_path.parent] if final_path is not None else []
            cmd, _ = policy.command_argv(
                shlex.join(cmd), worktree,
                additional_writable_roots=[Path(worker_home(worktree)), *output_roots],
                readable_roots=[worktree, Path(worker_home(worktree)), Path(worker_credentials_dir(worktree)),
                                *(([run.path / REFERENCE_DIR]) if (run.path / REFERENCE_DIR).is_dir() else [])],
                protected_roots=[Path(path) for path in run.fence_paths or []],
            )
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
        references = run.path / REFERENCE_DIR
        if references.is_dir():
            env["GARDEN_CONTEXT_DIR"] = str(references)
        # Deep dives are controller diagnostics, not implementation workers. They are the
        # one run type intentionally given the real workspace root for read access; the
        # ordinary worktree fence still prevents writes there.
        env["GARDEN_ROOT"] = (str(run.path.parents[3]) if run.mode == "investigation"
                              else no_live_garden_root(run.path))
        resources = self.config.get("resources", {})
        reserve = int(resources.get("disk_reserve_bytes", 0) or 0)
        required = int((run.env_snapshot or {}).get(
            "disk_recheck_required_bytes",
            (run.env_snapshot or {}).get("disk_required_bytes", 0),
        ) or 0)
        backing = str(resources.get("windows_backing_path", "") or "")
        work_dir = self.config.get("work_dir")
        if work_dir:
            temp_dir = run_temp_dir(work_dir, run)
            try:
                require_storage(tuple(path for path in (wt, temp_dir) if path is not None),
                                reserve_bytes=reserve, required_bytes=required,
                                windows_backing_path=backing, operation="local runtime scratch")
            except StorageAdmissionError as exc:
                raise RunnerError(str(exc)) from exc
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
        env["GARDEN_DISK_RESERVE_BYTES"] = str(reserve)
        env["GARDEN_DISK_REQUIRED_BYTES"] = str(required)
        env["GARDEN_WINDOWS_BACKING_PATH"] = backing
        return env

    def harness_environment(self, env: dict[str, str]) -> dict[str, str]:
        """Test-runner compatibility environment for an in-process harness fake."""
        result = dict(env)
        assert self.harness is not None
        key_name = self.harness.api_key_env
        if key_name and key_name in os.environ:
            result[key_name] = os.environ[key_name]
        return result

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
        try:
            with subprocess_authority(
                self.config, "worker", f"automation:{run.run_id}", env,
            ) as (execution_env, metadata, _redactor, _authority):
                if metadata is not None:
                    (d / "workload_identity.json").write_text(json.dumps(metadata.__dict__))
                    boundary = self.config["workload_identity"]["references"][
                        self.config["workload_identity"]["boundaries"]["worker"]["reference"]
                    ]
                    execution_env["GARDEN_WORKLOAD_IDENTITY_BINDINGS"] = ",".join(
                        str(name) for name in boundary["bindings"]
                    )
                    # The detached supervisor owns the operation after this scheduler tick
                    # exits. It resolves again from the same trusted, fenced local policy,
                    # removes this control input before launch, and enforces the authority
                    # for the complete lifetime of the child.
                    execution_env["GARDEN_WORKLOAD_IDENTITY_CONFIG"] = json.dumps({
                        "workload_identity": self.config["workload_identity"],
                    })
                    execution_env["GARDEN_WORKLOAD_IDENTITY_TARGET"] = "worker"
                    execution_env["GARDEN_WORKLOAD_IDENTITY_RUN"] = f"automation:{run.run_id}"
                self.launch(run, worktree, brief_path, execution_env)
        except WorkloadIdentityError as exc:
            # Complete through the ordinary reap path. It will restore the attempt/revision
            # snapshot and classify this as host environment trouble, not author failure.
            (d / "identity_error.json").write_text(json.dumps({"error": str(exc)}))
            (d / "exit_code").write_text("1\n")
            run.status = "running"
            run.save()

    def launch(self, run: Run, worktree: Path, brief_path: Path, env: dict[str, str]) -> None:
        """Start the harness detached: a shell runs it in the worktree with the brief on
        stdin, captures stdout/stderr beside the run record and writes `exit_code` when it
        ends (the completion signal reap waits for). The test suite's in-process runner
        overrides only this step."""
        assert self.harness is not None
        d = run.path
        raw_final = self.harness_output_path(run, worktree)
        inner = self.harness_shell(run, worktree, raw_final)
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
            f"< {shlex.quote(str(brief_path))}"
        )
        # Custom harness commands are allowed to ignore the optional final-output path.
        # Do not make their supervisors own a FIFO reader which can never have a writer.
        # When the harness does consume it, the supervisor installs the FIFO before launch.
        if str(raw_final) in inner:
            env["GARDEN_RAW_FINAL_PATH"] = str(raw_final)
            env["GARDEN_FINAL_PATH"] = str(d / "final.md")
        credential_fds: tuple[int, ...] = ()
        credential_read_fd = -1
        key_name = self.harness.api_key_env
        key_value = os.environ.get(key_name, "") if key_name else ""
        if key_name and key_value:
            # A pipe admits the provider key after setup has completed. It never enters
            # setup's environment, the worktree, argv, command.txt, or the run record.
            credential_read_fd, credential_write_fd = os.pipe()
            os.write(credential_write_fd, key_value.encode())
            os.close(credential_write_fd)
            env["GARDEN_HARNESS_API_KEY_FD"] = str(credential_read_fd)
            env["GARDEN_HARNESS_API_KEY_NAME"] = key_name
            credential_fds = (credential_read_fd,)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "garden.run_supervisor", str(d), script], cwd=str(worktree), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=credential_fds,
            )
        finally:
            if credential_read_fd >= 0:
                os.close(credential_read_fd)
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
        # Controller-owned Python analysers may use controller credentials. Their output is
        # redacted before revision dispatch; command checks and setup still create scrubbed
        # child environments in checkrun/checks.py.
        if payload.get("execution_owner") == "controller":
            env = dict(os.environ)
            env["GARDEN_TASK_ID"] = run.task_id
            env["GARDEN_RUN_ID"] = run.run_id
            env["GARDEN_ROOT"] = no_live_garden_root(run.path)
            # Both modules below execute with controller credentials.  Start from the
            # loaded package's parent rather than the untrusted worktree, and replace any
            # inherited PYTHONPATH which could also put branch code ahead of it.
            module_root = _trusted_module_root()
            env = _trusted_module_environment(env)
            launch_cwd = module_root
        else:
            env = self.worker_env(run, dict(self.config.get("setup") or {}), worktree)
            launch_cwd = worktree
        policy = SandboxPolicy.from_config(self.config)
        if policy.required:
            _, mechanism = policy.command_argv("true", worktree)
            env.update(policy.report_env(mechanism))
            (d / "sandbox.json").write_text(policy.summary(mechanism) + "\n")
        env["GARDEN_HEAVY_EXECUTION"] = "1"
        # A supported validation launched by this check inherits the check run's host
        # slot.  Without the owner scope it tries to acquire a second slot while this
        # supervisor still owns the first, making a short focused check wait behind
        # itself whenever shared admission is full.
        env["GARDEN_OWNER_SCOPED"] = "1"
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
            [sys.executable, "-m", "garden.run_supervisor", str(d), script], cwd=str(launch_cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        run.pid = proc.pid
        run.status = "running"
        run.save()
        (d / "command.txt").write_text(script + "\n")

    def collect(self, run: Run) -> dict[str, Any]:
        assert self.harness is not None
        identity_error = run.path / "identity_error.json"
        if identity_error.exists():
            detail = json.loads(identity_error.read_text())
            return {"final_text": "", "usage": {}, "cost_usd": None, "session_id": "",
                    "result": {}, "error": detail["error"], "env_error": True,
                    "env_kind": "workload_identity"}
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
        resources = self.config.get("resources", {})
        try:
            require_storage((cwd,),
                            reserve_bytes=int(resources.get("disk_reserve_bytes", 0) or 0),
                            required_bytes=int(resources.get("operation_required_bytes", 0) or 0),
                            windows_backing_path=str(resources.get("windows_backing_path", "") or ""),
                            operation="local harness probe scratch")
        except StorageAdmissionError as exc:
            raise OSError(str(exc)) from exc
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
