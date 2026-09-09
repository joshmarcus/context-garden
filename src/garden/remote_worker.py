"""Pull-based worker for a host that shares only HTTPS and git with the garden."""

from __future__ import annotations

import base64
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
            raise RuntimeError(f"garden returned HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc


class _LeaseHeartbeat:
    """Renew a claim while any host-side stage is running."""

    def __init__(self, run: dict[str, Any], client: WorkerClient):
        self.run = run
        self.client = client
        self.stop_event = threading.Event()
        self.failure: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name=f"garden-heartbeat-{run['id']}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _post(self) -> None:
        status, _ = self.client.post(
            f"/api/runs/{self.run['id']}/heartbeat",
            {"lease_token": self.run["lease_token"]},
        )
        if status != 200:
            raise RuntimeError(f"garden heartbeat returned HTTP {status}")

    def _run(self) -> None:
        interval = max(0.05, float(self.run.get("heartbeat_seconds") or 30))
        while not self.stop_event.wait(interval):
            try:
                self._post()
            except BaseException as exc:  # retained for the foreground lease fence
                self.failure = exc
                return

    def ensure_current(self) -> None:
        if self.failure is not None:
            raise RuntimeError(f"remote run lease renewal failed: {self.failure}") from self.failure
        self._post()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


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
        remote_branch = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=repo).returncode == 0
        subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch if remote_branch else base}"], cwd=repo, check=True)
        env = _env(list(run.get("env_allowlist") or []), repo, run)
        execution_dir = repo.parent / f"{run['id']}-execution"
        execution_dir.mkdir(parents=True, exist_ok=True)
        env.update(GARDEN_EXECUTION_OWNER=f"remote:{run['id']}",
                   GARDEN_EXECUTION_RUN_DIR=str(execution_dir),
                   GARDEN_VALIDATION_RUNNER=sys.executable)
        setup = dict(run.get("setup") or {})
        if setup_command:
            subprocess.run(setup_command, shell=True, cwd=repo, env=env,
                           timeout=int(setup.get("timeout_seconds") or 600), check=True)
        if run.get("mode") == "check":
            check_data = dict(run.get("checks") or {})
            ctx = {**dict(check_data.get("ctx") or {}), "exec_root": str(repo), "worktree": str(repo)}
            # A managed consumer passes the product command above so admission covers it.
            # Do not repeat it inside the check job. A standalone worker may instead
            # supply its own setup override; without one the check job prepares the product.
            check_setup = {**setup, "command": ""} if setup_command else setup
            runs_dir = root / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            execution_dir = Path(tempfile.mkdtemp(prefix="check-", dir=runs_dir))
            (execution_dir / "checks_input.json").write_text(json.dumps({
                **check_data, "ctx": ctx, "cwd": str(repo), "setup": check_setup,
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
            proc = subprocess.run(
                [sys.executable, "-m", "garden.run_supervisor", str(execution_dir), check_command],
                cwd=repo, env=execution_env, check=False,
            )
            result_path = execution_dir / "checks.json"
            if result_path.exists():
                results = json.loads(result_path.read_text())
                error = ""
            else:
                timeout_result = validation_timeout_result(execution_dir, proc.returncode)
                if timeout_result is not None:
                    results = [timeout_result]
                    error = timeout_result["details"]
                else:
                    error = f"remote check supervisor exited {proc.returncode} without results"
                    results = [{
                        "name": "checks", "status": "error",
                        "summary": "check execution did not complete", "details": error,
                    }]
            final, parsed, usage, cost, rc = "", {"checks": results}, {}, 0.0, proc.returncode
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
                transcript_offset = 0
                while proc.poll() is None:
                    time.sleep(1)
                    stdout_file.flush()
                    with open(stdout_file.name) as transcript_file:
                        transcript_file.seek(transcript_offset)
                        chunk = transcript_file.read()
                        transcript_offset = transcript_file.tell()
                    if chunk:
                        client.post(f"/api/runs/{run['id']}/heartbeat",
                                    {"lease_token": run["lease_token"], "transcript": chunk})
                stdout_file.flush()
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout, stderr = stdout_file.read(), stderr_file.read()
                tail = stdout[transcript_offset:]
                if tail:
                    client.post(f"/api/runs/{run['id']}/heartbeat",
                                {"lease_token": run["lease_token"], "transcript": tail})
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
        receipts = []
        for receipt_path in sorted(execution_dir.glob("validations/*/result.json")):
            try:
                receipt = json.loads(receipt_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(receipt, dict):
                artifacts: dict[str, str] = {}
                total = 0
                for artifact in sorted(receipt_path.parent.iterdir()):
                    if not artifact.is_file() or artifact.name == "result.json":
                        continue
                    data = artifact.read_bytes()
                    if len(data) > 2_000_000 or total + len(data) > 5_000_000:
                        continue
                    artifacts[artifact.name] = base64.b64encode(data).decode("ascii")
                    total += len(data)
                receipt["artifacts"] = artifacts
                receipts.append(receipt)
        heartbeat.ensure_current()
        client.post(f"/api/runs/{run['id']}/finish", {"lease_token": run["lease_token"],
                    "exit_code": rc, "final_text": final, "result": parsed,
                    "usage": usage, "cost_usd": cost, "error": error, "pushed_head": head,
                    "validation_receipts": receipts})
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
