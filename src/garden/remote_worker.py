"""Pull-based worker for a host that shares only HTTPS and git with the garden."""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .brief import parse_result
from .harness import Harness
from .runner.base import install_config_files, scrubbed_env
from .validation import bounded_validation_timeout_seconds, validation_timeout_result


class WorkerRequestError(RuntimeError):
    """An HTTP response that retry policy can classify without parsing its text."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"garden returned HTTP {status}: {detail}")
        self.status = status
        self.retryable = status in {408, 425, 429} or status >= 500


def doctor_worker(token: str, repo: str, harnesses: list[str],
                  config: dict[str, Any] | None = None, scratch_home: Path | None = None) -> list[str]:
    problems: list[str] = []
    if not token:
        problems.append("worker bearer token is missing")
    if not shutil.which("git"):
        problems.append("git is not on PATH")
    elif repo and subprocess.run(["git", "ls-remote", repo], capture_output=True).returncode != 0:
        problems.append(f"git cannot read {repo!r}")
    for name in harnesses:
        if not shutil.which(name):
            problems.append(f"harness {name!r} is not on PATH")
    try:
        with tempfile.TemporaryDirectory(prefix="garden-doctor-") as raw_home:
            probe_root = scratch_home or Path(raw_home)
            environment = scrubbed_env(config or {}, worktree=probe_root / "probe")
            for name in harnesses:
                if shutil.which(name) and not Harness(name, {}).check_login(environment)[0]:
                    problems.append(f"harness {name!r} authentication failed in scrubbed environment")
    except Exception as exc:  # a policy/configuration failure is a doctor finding
        # Mapping errors contain only operator-chosen entry names, never source paths/content.
        problems.append(str(exc))
    return problems


class WorkerClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(self.url + path, json.dumps(payload).encode(), self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as response:  # noqa: S310 - operator supplied garden URL
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return 204, {}
            raise WorkerRequestError(exc.code, exc.read().decode(errors="replace")) from exc


class _LeaseHeartbeat:
    """Renew a claim while any host-side stage is running."""

    def __init__(self, run: dict[str, Any], client: WorkerClient):
        self.run = run
        self.client = client
        self.stop_event = threading.Event()
        self.failure: BaseException | None = None
        # New controllers send the whole interval for which this generation remains
        # authoritative: the ordinary lease plus its recovery grace.  Keep the older
        # recovery_seconds fallback so a newly deployed worker remains compatible with
        # the previous claim shape.
        self.recovery_window_seconds = max(
            0.0,
            float(run.get("recovery_window_seconds") or run.get("recovery_seconds") or 300),
        )
        self.recovery_deadline = time.monotonic() + self.recovery_window_seconds
        self.post_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"garden-heartbeat-{run['id']}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _post(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = min(1.0, max(0.05, float(self.run.get("heartbeat_seconds") or 30) / 4))
        while True:
            try:
                with self.post_lock:
                    status, response = self.client.post(
                        f"/api/runs/{self.run['id']}/heartbeat",
                        {"lease_token": self.run["lease_token"], **(payload or {})},
                    )
                if status != 200:
                    raise WorkerRequestError(status, "heartbeat rejected")
                self.recovery_deadline = time.monotonic() + self.recovery_window_seconds
                return response
            except BaseException as exc:
                if isinstance(exc, WorkerRequestError) and not exc.retryable:
                    raise
                if time.monotonic() >= self.recovery_deadline:
                    raise
                if self.stop_event.wait(delay):
                    raise RuntimeError("remote run stopped during controller recovery") from None
                delay = min(delay * 2, 5.0)

    def _run(self) -> None:
        interval = max(0.05, float(self.run.get("heartbeat_seconds") or 30))
        while not self.stop_event.wait(interval):
            try:
                self._post()
            except BaseException as exc:  # retained for the foreground lease fence
                self.failure = exc
                return

    def ensure_current(self) -> None:
        self.ensure_not_failed()
        self._post()

    def ensure_not_failed(self) -> None:
        """Fence local execution as soon as background renewal becomes terminal."""
        if self.failure is not None:
            raise RuntimeError(f"remote run lease renewal failed: {self.failure}") from self.failure

    def upload(self, offset: int, chunk: str) -> int:
        self.ensure_not_failed()
        response = self._post({"transcript_offset": offset, "transcript": chunk})
        return int(response.get("transcript_offset", offset + len(chunk.encode())))

    def finish(self, payload: dict[str, Any]) -> None:
        delay = 0.1
        while True:
            try:
                with self.post_lock:
                    status, _ = self.client.post(f"/api/runs/{self.run['id']}/finish", payload)
                if status != 200:
                    raise WorkerRequestError(status, "finish rejected")
                return
            except BaseException as exc:
                if isinstance(exc, WorkerRequestError) and not exc.retryable:
                    raise
                if time.monotonic() >= self.recovery_deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 5.0)

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


def _stop_obsolete_process(proc: subprocess.Popen[Any]) -> None:
    """Stop a supervised process tree after its remote authority is lost."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_for_process(proc: subprocess.Popen[Any], heartbeat: _LeaseHeartbeat,
                      *, interval: float = 0.1) -> int:
    """Wait while fencing an active child against terminal lease loss."""
    while (returncode := proc.poll()) is None:
        try:
            heartbeat.ensure_not_failed()
        except BaseException:
            _stop_obsolete_process(proc)
            raise
        time.sleep(interval)
    return returncode


def _env(names: list[str], worktree: Path, run: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if any(fnmatch.fnmatchcase(k, p) for p in names)}
    home = worktree.parent / f".garden-home-{run['task_id']}"
    home.mkdir(parents=True, exist_ok=True)
    install_config_files({"worker_env": {"config_files": run.get("config_files") or {}}}, home)
    env.setdefault("HOME", str(home))
    env.update(GARDEN_TASK_ID=run["task_id"], GARDEN_RUN_ID=run["id"],
               GARDEN_ROOT=str(worktree / ".garden-no-live-garden"))
    env.pop("GARDEN_EXECUTION_TIMEOUT_SECONDS", None)
    env["GARDEN_VALIDATION_TIMEOUT_SECONDS"] = str(
        int(run.get("validation_timeout_seconds") or 900)
    )
    env.pop("CLAUDECODE", None)
    return env


def _host_check_data(run: dict[str, Any], repo: Path) -> dict[str, Any]:
    """Replace controller-local paths in a portable check payload.

    Python checks may carry their worktree and output directory in the individual spec,
    in addition to the shared context.  Neither controller path exists on an independent
    host, so give every such check a lease-local artifact directory beside the clone.
    """
    check_data = dict(run.get("checks") or {})
    artifact_root = repo.parent / f"{run['id']}-check-artifacts"
    specs = []
    for index, original in enumerate(check_data.get("specs") or []):
        spec = dict(original)
        if "worktree" in spec:
            spec["worktree"] = str(repo)
        if "out_dir" in spec:
            spec["out_dir"] = str(artifact_root / f"{index}-{spec.get('name') or 'check'}")
        specs.append(spec)
    check_data["specs"] = specs
    check_data["ctx"] = {
        **dict(check_data.get("ctx") or {}), "exec_root": str(repo), "worktree": str(repo),
    }
    check_data["cwd"] = str(repo)
    return check_data


def execute_claim(run: dict[str, Any], root: Path, client: WorkerClient, *, setup_command: str = "") -> None:
    """Materialise one claim, run it, push it, and post its auditable outcome."""
    heartbeat = _LeaseHeartbeat(run, client)
    heartbeat.start()
    try:
        repo = root / "repos" / run["task_id"]
        if not (repo / ".git").exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", str(run["repo"]), str(repo)], check=True)
        subprocess.run(["git", "fetch", "--prune", "origin"], cwd=repo, check=True)
        branch, base = str(run["branch"]), str(run["base"])
        source_head = str(run.get("source_head") or "")
        if source_head:
            # Base probes must run on the immutable source advertised by the controller.
            # A detached checkout also ensures a bad source cannot alter the author branch.
            subprocess.run(["git", "checkout", "--detach", source_head], cwd=repo, check=True)
            actual_source = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                           capture_output=True, text=True, check=True).stdout.strip()
            if actual_source != source_head:
                raise RuntimeError(f"advertised source {source_head} materialised as {actual_source}")
        else:
            remote_branch = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=repo).returncode == 0
            subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch if remote_branch else base}"], cwd=repo, check=True)
        env = _env(list(run.get("env_allowlist") or []), repo, run)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir(mode=0o700, exist_ok=True)
        env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        setup = dict(run.get("setup") or {})
        if setup_command:
            subprocess.run(setup_command, shell=True, cwd=repo, env=env,
                           timeout=int(setup.get("timeout_seconds") or 600), check=True)
        if run.get("mode") == "check":
            check_data = _host_check_data(run, repo)
            # A managed consumer passes the product command above so admission covers it.
            # Do not repeat it inside the check job. A standalone worker may instead
            # supply its own setup override; without one the check job prepares the product.
            check_setup = {**setup, "command": ""} if setup_command else setup
            runs_dir = root / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            execution_dir = Path(tempfile.mkdtemp(prefix="check-", dir=runs_dir))
            (execution_dir / "checks_input.json").write_text(json.dumps({
                **check_data, "setup": check_setup,
            }))
            execution_env = dict(env)
            for key in ("GARDEN_EXECUTION_OWNER", "GARDEN_EXECUTION_RUN_DIR",
                        "GARDEN_VALIDATION_RUNNER", "GARDEN_OWNER_SCOPED"):
                execution_env.pop(key, None)
            execution_env["GARDEN_HEAVY_EXECUTION"] = "1"
            execution_timeout = bounded_validation_timeout_seconds(run.get("validation_timeout_seconds"))
            execution_env["GARDEN_EXECUTION_TIMEOUT_SECONDS"] = f"{execution_timeout:g}"
            check_command = (
                f"{shlex.quote(sys.executable)} -m garden.checkrun {shlex.quote(str(execution_dir))} "
                f"> {shlex.quote(str(execution_dir / 'stdout.json'))} "
                f"2> {shlex.quote(str(execution_dir / 'stderr.log'))}"
            )
            proc = subprocess.Popen(
                [sys.executable, "-m", "garden.run_supervisor", str(execution_dir), check_command],
                cwd=repo, env=execution_env,
            )
            check_returncode = _wait_for_process(proc, heartbeat)
            result_path = execution_dir / "checks.json"
            if result_path.exists():
                results = json.loads(result_path.read_text())
                error = ""
            else:
                timeout_result = validation_timeout_result(execution_dir, check_returncode)
                if timeout_result is not None:
                    results = [timeout_result]
                    error = timeout_result["details"]
                else:
                    error = f"remote check supervisor exited {check_returncode} without results"
                    results = [{
                        "name": "checks", "status": "error",
                        "summary": "check execution did not complete", "details": error,
                    }]
            final, parsed, usage, cost, rc = "", {"checks": results}, {}, 0.0, check_returncode
        else:
            harness = Harness(str(run["harness"]), dict(run.get("harness_config") or {}))
            final_path = repo.parent / f"{run['id']}-final.md"
            argv = harness.command(str(run.get("model") or ""), final_path,
                                   difficulty=str(run.get("difficulty") or "medium"), worktree=repo)
            # The same supervisor used by local workers supplies a usable validation
            # interpreter plus host-local run ownership. Merely exporting the interpreter
            # would leave garden.validation without the ownership fence it requires.
            runs_dir = root / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            execution_dir = Path(tempfile.mkdtemp(prefix="claim-", dir=runs_dir))
            execution_env = dict(env)
            for key in ("GARDEN_EXECUTION_OWNER", "GARDEN_EXECUTION_RUN_DIR",
                        "GARDEN_VALIDATION_RUNNER", "GARDEN_HEAVY_EXECUTION", "GARDEN_OWNER_SCOPED"):
                execution_env.pop(key, None)
            supervised = [sys.executable, "-m", "garden.run_supervisor",
                          str(execution_dir), shlex.join(argv)]
            with tempfile.NamedTemporaryFile(mode="w+") as stdout_file, tempfile.TemporaryFile(mode="w+") as stderr_file:
                proc = subprocess.Popen(supervised, stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
                                        text=True, cwd=repo, env=execution_env)
                assert proc.stdin is not None
                proc.stdin.write(str(run.get("brief") or ""))
                proc.stdin.close()
                transcript_read_offset = 0
                transcript_upload_offset = 0
                timeout_minutes = float(run.get("execution_timeout_minutes") or 0)
                deadline = time.monotonic() + timeout_minutes * 60 if timeout_minutes else None
                while proc.poll() is None:
                    try:
                        heartbeat.ensure_not_failed()
                    except BaseException:
                        _stop_obsolete_process(proc)
                        raise
                    if deadline is not None and time.monotonic() >= deadline:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        stderr_file.write(f"\nworker timed out after {timeout_minutes:g} minutes\n")
                        break
                    time.sleep(0.1)
                    stdout_file.flush()
                    with open(stdout_file.name) as transcript_file:
                        transcript_file.seek(transcript_read_offset)
                        chunk = transcript_file.read()
                        transcript_read_offset = transcript_file.tell()
                    if chunk:
                        transcript_upload_offset = heartbeat.upload(transcript_upload_offset, chunk)
                stdout_file.flush()
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout, stderr = stdout_file.read(), stderr_file.read()
                stdout_file.seek(transcript_read_offset)
                tail = stdout_file.read()
                if tail:
                    transcript_upload_offset = heartbeat.upload(transcript_upload_offset, tail)
            collected = harness.parse(stdout, stderr, final_path, model=str(run.get("model") or ""))
            final = str(collected.get("final_text") or "")
            parsed = collected.get("result") or parse_result(final) or {}
            usage, cost, error, rc = collected.get("usage") or {}, collected.get("cost_usd"), str(collected.get("error") or ""), proc.returncode
        if subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip():
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=garden", "-c", "user.email=garden@localhost", "commit", "-m", f"{run['task_id']}: remote worker changes"], cwd=repo, check=False)
        # Confirm this lease immediately before publishing to its staging ref. The garden
        # alone promotes that ref after accepting finish.
        heartbeat.ensure_current()
        push_ref = str(run["push_ref"])
        subprocess.run(["git", "push", "--force", "origin", f"HEAD:{push_ref}"], cwd=repo, check=rc == 0)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
        heartbeat.ensure_current()
        heartbeat.finish({"lease_token": run["lease_token"], "exit_code": rc,
                          "final_text": final, "result": parsed, "usage": usage,
                          "cost_usd": cost, "error": error, "pushed_head": head})
    finally:
        heartbeat.stop()


def run_worker(url: str, host: str, token: str, root: Path, harnesses: list[str], tiers: list[str],
               capacity: int = 1, once: bool = False, poll_seconds: float = 5,
               setup_command: str = "") -> None:
    client = WorkerClient(url, token)
    while True:
        status, claim = client.post("/api/runs/claim", {"host": host, "harnesses": harnesses,
                                                        "tiers": tiers, "capacity": capacity})
        if status == 204 or not claim:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        execute_claim(claim, root, client, setup_command=setup_command)
        if once:
            return
