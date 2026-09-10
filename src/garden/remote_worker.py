"""Pull-based worker for a host that shares only HTTPS and git with the garden."""

from __future__ import annotations

import base64
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any, TextIO

from .brief import parse_result
from .harness import Harness
from .runner.base import _no_fsmonitor_env, install_config_files, scrubbed_env, setup_marker
from .validation import bounded_validation_timeout_seconds, validation_timeout_result


class WorkerRequestError(RuntimeError):
    """An HTTP response that retry policy can classify without parsing its text."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"garden returned HTTP {status}: {detail}")
        self.status = status
        self.retryable = status in {408, 425, 429} or status >= 500


class ClaimMaterializationError(RuntimeError):
    """A claim could not safely prepare source before author code launched."""

    def __init__(self, stage: str, detail: str, preserved: Path | None = None):
        super().__init__(detail)
        self.stage = stage
        self.preserved = preserved


_RECEIPT_SOURCE_PATTERN = re.compile(r'"source_sha"\s*:\s*"([^"\\]*)"')
_MAX_MALFORMED_RECEIPT_SOURCES = 8
_SOURCE_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")


def _validation_receipts(execution_dir: Path) -> list[dict[str, Any]]:
    """Collect ordered receipts without hiding a malformed newer attempt."""
    receipts = []
    receipt_paths = sorted(
        execution_dir.glob("validations/*/result.json"),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
    )
    for receipt_path in receipt_paths:
        try:
            raw = receipt_path.read_text()
            receipt = json.loads(raw)
        except OSError:
            continue
        except json.JSONDecodeError:
            # Do not transport arbitrary receipt contents: source identities are the
            # only data ingestion needs to distinguish an unrelated malformed attempt.
            sources = list(dict.fromkeys(
                source for source in _RECEIPT_SOURCE_PATTERN.findall(raw)
                if _SOURCE_SHA_PATTERN.fullmatch(source)
            ))
            receipts.append({
                "malformed_validation_receipt": True,
                "recoverable_source_shas": sources[:_MAX_MALFORMED_RECEIPT_SOURCES],
                "recoverable_source_shas_overflow": (
                    len(sources) > _MAX_MALFORMED_RECEIPT_SOURCES
                ),
            })
            continue
        if not isinstance(receipt, dict):
            receipts.append({
                "malformed_validation_receipt": True,
                "recoverable_source_shas": [],
                "recoverable_source_shas_overflow": False,
            })
            continue
        try:
            receipt["durable_execution"] = json.loads(
                (receipt_path.parent / "execution.json").read_text()
            )
            receipt["durable_exit_code"] = int(
                (receipt_path.parent / "exit_code").read_text().strip()
            )
            receipt["durable_stderr"] = (receipt_path.parent / "stderr.log").read_text()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        receipts.append(receipt)
    return receipts


def _claim_suffix(run: dict[str, Any]) -> str:
    token = str(run.get("lease_token") or "")
    digest = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{run['id']}-{digest}"


def _quarantine_materialization(repo: Path, root: Path, run: dict[str, Any],
                                heartbeat: _LeaseHeartbeat) -> Path | None:
    """Move a failed checkout and its setup stamp aside without deleting either.

    The setup lock inode remains in place. Taking that same lock before moving a sibling
    marker prevents racing a still-running setup shell from an earlier daemon generation.
    """
    heartbeat.ensure_current()
    destination = root / "preserved-materializations" / str(run["task_id"]) / _claim_suffix(run)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination
    marker = setup_marker(repo)
    lock_path = marker.with_suffix(marker.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as setup_lock:
        fcntl.flock(setup_lock, fcntl.LOCK_EX)
        heartbeat.ensure_current()
        if repo.exists():
            repo.rename(destination)
        for suffix in ("", ".tmp"):
            candidate = marker if not suffix else marker.with_suffix(marker.suffix + suffix)
            if candidate.exists():
                candidate.rename(destination.parent / f"{destination.name}.setup-marker{suffix}")
    return destination if destination.exists() else None


def _preserve_materialization_failure(repo: Path, root: Path, run: dict[str, Any],
                                      heartbeat: _LeaseHeartbeat, *, stage: str,
                                      error: BaseException) -> ClaimMaterializationError:
    """Preserve failed source and retain the claim failure if quarantine also fails."""
    try:
        preserved = _quarantine_materialization(repo, root, run, heartbeat)
    except OSError as preserve_error:
        destination = root / "preserved-materializations" / str(run["task_id"]) / _claim_suffix(run)
        try:
            source_location = destination if destination.exists() else repo
        except OSError:
            source_location = repo
        return ClaimMaterializationError(
            stage,
            f"{error}; preservation failed: {preserve_error}; source remains at {source_location}",
            source_location,
        )
    return ClaimMaterializationError(stage, str(error), preserved)


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

    def upload_transcript(self, offset: int, payload: bytes) -> int:
        """Upload one bounded canonical chunk and return its durable byte offset."""
        self.ensure_not_failed()
        response = self._transcript_post(
            f"/api/runs/{self.run['id']}/transcript", {
                "lease_token": self.run["lease_token"], "offset": offset,
                "data": base64.b64encode(payload).decode(),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )
        return int(response["offset"])

    def finish_transcript(self, *, byte_count: int, sha256: str, event_count: int,
                          redactions: int, capture_limits: list[str]) -> None:
        self._transcript_post(
            f"/api/runs/{self.run['id']}/transcript/finish", {
                "lease_token": self.run["lease_token"], "byte_count": byte_count,
                "sha256": sha256, "event_count": event_count, "redactions": redactions,
                "capture_limits": capture_limits, "harness_schema": "observable-events-v1",
            },
        )

    def _transcript_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        delay = 0.1
        while True:
            try:
                with self.post_lock:
                    status, response = self.client.post(path, payload)
                if status != 200:
                    raise WorkerRequestError(status, "transcript request rejected")
                self.recovery_deadline = time.monotonic() + self.recovery_window_seconds
                return response
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


class _TranscriptCapture:
    """Capture both process streams in observed order while retaining parser inputs."""

    def __init__(self, root: Path, environment: dict[str, str]):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.path = root / "transcript.jsonl"
        self.path.touch()
        self.stdout_path = root / "stdout.log"
        self.stderr_path = root / "stderr.log"
        self.checkpoint_path = root / "upload.json"
        self.lock = threading.Lock()
        self.sequence = 0
        self.redactions = 0
        with self.path.open(errors="replace") as existing:
            for line in existing:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self.sequence = max(self.sequence, int(event.get("sequence", -1)) + 1)
                    self.redactions += int(event.get("redactions", 0))
        secret_names = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "ACCESS_KEY")
        self.secrets = sorted({value for key, value in environment.items()
                               if len(value) >= 4
                               and any(marker in key.upper() for marker in secret_names)},
                              key=len, reverse=True)

    def _redacted_parts(self, stream: TextIO) -> Iterator[tuple[str, int]]:
        """Yield bounded text while retaining enough overlap to match split secrets."""
        pending = ""
        overlap = max((len(secret) for secret in self.secrets), default=1) - 1
        while data := stream.read(64 * 1024):
            pending += data
            safe = max(0, len(pending) - overlap)
            position = 0
            while position < safe:
                matches = [(pending.find(secret, position), secret) for secret in self.secrets]
                matches = [(start, secret) for start, secret in matches if start >= 0]
                if not matches:
                    yield pending[position:safe], 0
                    position = safe
                    break
                start, secret = min(matches, key=lambda item: item[0])
                if start >= safe:
                    yield pending[position:safe], 0
                    position = safe
                    break
                if start > position:
                    yield pending[position:start], 0
                yield "<redacted>", 1
                position = start + len(secret)
            pending = pending[position:]
        position = 0
        while position < len(pending):
            matches = [(pending.find(secret, position), secret) for secret in self.secrets]
            matches = [(start, secret) for start, secret in matches if start >= 0]
            if not matches:
                yield pending[position:], 0
                break
            start, secret = min(matches, key=lambda item: item[0])
            if start > position:
                yield pending[position:start], 0
            yield "<redacted>", 1
            position = start + len(secret)

    def reader(self, stream: TextIO, channel: str) -> None:
        raw_path = self.stdout_path if channel == "stdout" else self.stderr_path
        with raw_path.open("a") as raw, self.path.open("a") as transcript:
            # Fixed reads keep a harness that emits one enormous line from becoming an
            # unbounded worker-side allocation. Concatenating channel data is lossless.
            for data, redacted in self._redacted_parts(stream):
                raw.write(data)
                raw.flush()
                self._write_event(transcript, channel, data, redacted=redacted)

    def _write_event(self, transcript: TextIO, channel: str, data: str, *, redacted: int = 0,
                     payload: Any = None) -> None:
        with self.lock:
            event = {"schema_version": 1, "sequence": self.sequence,
                     "timestamp": dt.datetime.now(dt.UTC).isoformat(),
                     "channel": channel, "data": data}
            if payload is not None:
                event["payload"] = payload
            if redacted:
                event["redactions"] = redacted
            transcript.write(json.dumps(event, separators=(",", ":")) + "\n")
            transcript.flush()
            self.sequence += 1
            self.redactions += redacted

    def record(self, channel: str, data: str, *, payload: Any = None) -> None:
        clean_data, data_redactions = self._redact_value(data)
        clean_payload, payload_redactions = self._redact_value(payload)
        with self.path.open("a") as transcript:
            self._write_event(
                transcript, channel, clean_data, payload=clean_payload,
                redacted=data_redactions + payload_redactions,
            )

    def _redact_value(self, value: Any) -> tuple[Any, int]:
        if isinstance(value, str):
            count = 0
            for secret in self.secrets:
                occurrences = value.count(secret)
                value = value.replace(secret, "<redacted>")
                count += occurrences
            return value, count
        if isinstance(value, list):
            values, count = [], 0
            for item in value:
                clean, redactions = self._redact_value(item)
                values.append(clean)
                count += redactions
            return values, count
        if isinstance(value, dict):
            values, count = {}, 0
            for key, item in value.items():
                clean, redactions = self._redact_value(item)
                values[key] = clean
                count += redactions
            return values, count
        return value, 0

    def acknowledged_offset(self) -> int:
        try:
            value = json.loads(self.checkpoint_path.read_text()).get("offset", 0)
            return value if isinstance(value, int) and value >= 0 else 0
        except (OSError, json.JSONDecodeError, AttributeError):
            return 0

    def _checkpoint(self, offset: int) -> None:
        temporary = self.checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"offset": offset}))
        with temporary.open("rb") as source:
            os.fsync(source.fileno())
        temporary.replace(self.checkpoint_path)

    def upload_available(self, heartbeat: _LeaseHeartbeat, offset: int | None = None) -> int:
        if offset is None:
            offset = self.acknowledged_offset()
        if not self.path.exists():
            return offset
        with self.path.open("rb") as source:
            source.seek(offset)
            while chunk := source.read(1024 * 1024):
                offset = heartbeat.upload_transcript(offset, chunk)
                self._checkpoint(offset)
        return offset

    def capture_file(self, path: Path, channel: str) -> None:
        if path.exists():
            with path.open(errors="replace") as source:
                self.reader(source, channel)

    def parser_texts(self, max_bytes: int = 16 * 1024 * 1024) -> tuple[str, str, bool]:
        """Return bounded tail views for parsers; canonical capture remains complete."""
        values = []
        truncated = False
        for path in (self.stdout_path, self.stderr_path):
            if not path.exists():
                values.append("")
                continue
            size = path.stat().st_size
            with path.open("rb") as source:
                if size > max_bytes:
                    source.seek(size - max_bytes)
                    truncated = True
                values.append(source.read(max_bytes).decode(errors="replace"))
        return values[0], values[1], truncated

    def digest(self) -> str:
        digest = hashlib.sha256()
        with self.path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def upload_legacy_stdout(self, heartbeat: _LeaseHeartbeat) -> None:
        offset = 0
        if not self.stdout_path.exists():
            return
        with self.stdout_path.open() as source:
            while chunk := source.read(256 * 1024):
                offset = heartbeat.upload(offset, chunk)


def _transcript_spool(root: Path, run: dict[str, Any]) -> Path:
    """Stable, lease-scoped capture path used again after a worker process restart."""
    return root / "transcript-spool" / _claim_suffix(run)


def _env(names: list[str], worktree: Path, run: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if any(fnmatch.fnmatchcase(k, p) for p in names)}
    home = worktree.parent / f".garden-home-{run['task_id']}"
    home.mkdir(parents=True, exist_ok=True)
    install_config_files({"worker_env": {"config_files": run.get("config_files") or {}}}, home)
    env.setdefault("HOME", str(home))
    env.update(GARDEN_TASK_ID=run["task_id"], GARDEN_RUN_ID=run["id"],
               GARDEN_ROOT=str(worktree / ".garden-no-live-garden"))
    # A host worker's harness can run Git just like a local harness. Keep optional
    # filesystem-monitor and maintenance daemons out of its supervised process group so
    # the claim can finish after the harness exits.
    env.update(_no_fsmonitor_env())
    env.pop("GARDEN_EXECUTION_TIMEOUT_SECONDS", None)
    env["GARDEN_VALIDATION_TIMEOUT_SECONDS"] = str(
        int(run.get("validation_timeout_seconds") or 900)
    )
    env.pop("CLAUDECODE", None)
    from .validation import enforce_validation_policy_env

    enforce_validation_policy_env(env)
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


def _finish_materialization_failure(run: dict[str, Any], heartbeat: _LeaseHeartbeat,
                                    failure: ClaimMaterializationError) -> None:
    preserved = str(failure.preserved) if failure.preserved else ""
    error = f"worker materialization failed during {failure.stage}: {failure}"
    if preserved:
        error += f"; preserved at {preserved}"
    heartbeat.ensure_current()
    heartbeat.finish({
        "lease_token": run["lease_token"], "exit_code": 1, "final_text": "", "result": {},
        "usage": {}, "cost_usd": 0.0, "error": error, "pushed_head": "",
        "env_error": True, "env_kind": "materialization",
    })


def _acquire_repo_lock(run: dict[str, Any], root: Path) -> TextIO:
    """Acquire exclusive checkout ownership or report a claim-scoped failure."""
    lock_path = root / "repo-locks" / f"{run['task_id']}.lock"
    repo_lock = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        repo_lock = lock_path.open("a")
        fcntl.flock(repo_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return repo_lock
    except BlockingIOError as exc:
        if repo_lock is not None:
            repo_lock.close()
        raise ClaimMaterializationError(
            "checkout ownership", "checkout is still owned by a live claim supervisor"
        ) from exc
    except OSError as exc:
        if repo_lock is not None:
            repo_lock.close()
        raise ClaimMaterializationError(
            "checkout ownership", f"cannot acquire repository lock {lock_path}: {exc}"
        ) from exc


def _prepare_claim_repo(run: dict[str, Any], root: Path, heartbeat: _LeaseHeartbeat,
                        *, setup_command: str, lock_fd: int) -> tuple[Path, dict[str, str]]:
    """Prepare one warm checkout, quarantining unsafe state for the next generation."""
    repo = root / "repos" / run["task_id"]
    stage = "clone"
    try:
        heartbeat.ensure_current()
        if (repo / ".git").exists():
            stage = "checkout preflight"
            dirty = subprocess.run(
                ["git", "--no-optional-locks", "status", "--porcelain"],
                cwd=repo, capture_output=True, text=True,
                check=True, pass_fds=(lock_fd,),
            ).stdout.strip()
            unmerged = subprocess.run(
                ["git", "ls-files", "-u"], cwd=repo, capture_output=True, text=True,
                check=True, pass_fds=(lock_fd,),
            ).stdout.strip()
            if dirty or unmerged:
                condition = "unresolved index" if unmerged else "dirty worktree"
                raise _preserve_materialization_failure(
                    repo, root, run, heartbeat, stage=stage,
                    error=RuntimeError(f"warm checkout has {condition}"),
                )
        if not (repo / ".git").exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", str(run["repo"]), str(repo)], check=True,
                           pass_fds=(lock_fd,))
        stage = "fetch"
        subprocess.run(["git", "fetch", "--prune", "origin"], cwd=repo, check=True,
                       pass_fds=(lock_fd,))
        branch, base = str(run["branch"]), str(run["base"])
        source_head = str(run.get("source_head") or "")
        stage = "checkout"
        if source_head:
            subprocess.run(["git", "checkout", "--detach", source_head], cwd=repo, check=True,
                           pass_fds=(lock_fd,))
            actual_source = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                check=True, pass_fds=(lock_fd,),
            ).stdout.strip()
            if actual_source != source_head:
                raise ClaimMaterializationError(
                    stage, f"advertised source {source_head} materialised as {actual_source}"
                )
        else:
            remote_branch = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
                cwd=repo, pass_fds=(lock_fd,),
            ).returncode == 0
            subprocess.run(
                ["git", "checkout", "-B", branch, f"origin/{branch if remote_branch else base}"],
                cwd=repo, check=True, pass_fds=(lock_fd,),
            )
        stage = "configuration"
        try:
            env = _env(list(run.get("env_allowlist") or []), repo, run)
        except Exception as exc:
            # Claim config is host-owned input and can fail validation or installation in
            # several supported ways. It is still pre-author materialization, not a daemon
            # failure. No heartbeat operation occurs inside _env, so lease fencing remains
            # outside this conversion boundary.
            raise ClaimMaterializationError(stage, str(exc)) from exc
        runtime_dir = root / "runtime"
        runtime_dir.mkdir(mode=0o700, exist_ok=True)
        env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        execution_dir = repo.parent / f"{run['id']}-execution"
        execution_dir.mkdir(parents=True, exist_ok=True)
        env.update(GARDEN_EXECUTION_OWNER=f"remote:{run['id']}",
                   GARDEN_EXECUTION_RUN_DIR=str(execution_dir),
                   GARDEN_VALIDATION_RUNNER=sys.executable)
        setup = dict(run.get("setup") or {})
        if setup_command:
            stage = "setup"
            subprocess.run(setup_command, shell=True, cwd=repo, env=env,
                           timeout=int(setup.get("timeout_seconds") or 600), check=True,
                           pass_fds=(lock_fd,))
        return repo, env
    except (ClaimMaterializationError, WorkerRequestError):
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise _preserve_materialization_failure(
            repo, root, run, heartbeat, stage=stage, error=exc,
        ) from exc


def execute_claim(run: dict[str, Any], root: Path, client: WorkerClient, *, setup_command: str = "") -> None:
    """Materialise one claim, run it, push it, and post its auditable outcome."""
    heartbeat = _LeaseHeartbeat(run, client)
    heartbeat.start()
    repo_lock = None
    try:
        try:
            repo_lock = _acquire_repo_lock(run, root)
        except ClaimMaterializationError as failure:
            _finish_materialization_failure(run, heartbeat, failure)
            return
        try:
            repo, env = _prepare_claim_repo(
                run, root, heartbeat, setup_command=setup_command, lock_fd=repo_lock.fileno()
            )
        except ClaimMaterializationError as failure:
            _finish_materialization_failure(run, heartbeat, failure)
            return
        setup = dict(run.get("setup") or {})
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
            execution_env["GARDEN_PRESERVE_FDS"] = str(repo_lock.fileno())
            execution_timeout = bounded_validation_timeout_seconds(run.get("validation_timeout_seconds"))
            execution_env["GARDEN_EXECUTION_TIMEOUT_SECONDS"] = f"{execution_timeout:g}"
            check_command = (
                f"{shlex.quote(sys.executable)} -m garden.checkrun {shlex.quote(str(execution_dir))} "
                f"> {shlex.quote(str(execution_dir / 'stdout.json'))} "
                f"2> {shlex.quote(str(execution_dir / 'stderr.log'))}"
            )
            proc = subprocess.Popen(
                [sys.executable, "-m", "garden.run_supervisor", str(execution_dir), check_command],
                cwd=repo, env=execution_env, pass_fds=(repo_lock.fileno(),),
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
            with nullcontext(str(_transcript_spool(root, run))) as capture_dir:
                capture = _TranscriptCapture(Path(capture_dir), execution_env)
                capture.capture_file(execution_dir / "stdout.json", "stdout")
                capture.capture_file(execution_dir / "stderr.log", "stderr")
                capture.record("worker", "check result", payload=parsed)
                transcript_upload_offset = capture.upload_available(heartbeat)
                heartbeat.finish_transcript(
                    byte_count=transcript_upload_offset, sha256=capture.digest(),
                    event_count=capture.sequence, redactions=capture.redactions,
                    capture_limits=[
                        "check stdout/stderr file ordering unavailable after redirected execution",
                        "private model reasoning and unexposed harness events unavailable",
                    ],
                )
                capture.upload_legacy_stdout(heartbeat)
                shutil.rmtree(capture.root)
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
                        "GARDEN_VALIDATION_RUNNER", "GARDEN_HEAVY_EXECUTION", "GARDEN_OWNER_SCOPED",
                        "GARDEN_PRESERVE_FDS"):
                execution_env.pop(key, None)
            execution_env["GARDEN_PRESERVE_FDS"] = str(repo_lock.fileno())
            supervised = [sys.executable, "-m", "garden.run_supervisor",
                          str(execution_dir), shlex.join(argv)]
            with nullcontext(str(_transcript_spool(root, run))) as capture_dir:
                capture = _TranscriptCapture(Path(capture_dir), execution_env)
                proc = subprocess.Popen(supervised, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, cwd=repo,
                                        env=execution_env, pass_fds=(repo_lock.fileno(),))
                assert proc.stdin is not None
                assert proc.stdout is not None and proc.stderr is not None
                readers = [threading.Thread(target=capture.reader, args=(proc.stdout, "stdout")),
                           threading.Thread(target=capture.reader, args=(proc.stderr, "stderr"))]
                for reader in readers:
                    reader.start()
                proc.stdin.write(str(run.get("brief") or ""))
                proc.stdin.close()
                transcript_upload_offset = capture.acknowledged_offset()
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
                        break
                    time.sleep(0.1)
                    transcript_upload_offset = capture.upload_available(
                        heartbeat, transcript_upload_offset
                    )
                for reader in readers:
                    reader.join()
                transcript_upload_offset = capture.upload_available(heartbeat, transcript_upload_offset)
                stdout, stderr, parser_truncated = capture.parser_texts()
                capture_limits = ["private model reasoning and unexposed harness events unavailable"]
                if parser_truncated:
                    capture_limits.append(
                        "legacy harness parser inputs truncated to the final 16 MiB per channel"
                    )
                heartbeat.finish_transcript(
                    byte_count=transcript_upload_offset, sha256=capture.digest(),
                    event_count=capture.sequence, redactions=capture.redactions,
                    capture_limits=capture_limits,
                )
                # Keep the legacy stdout renderer populated while canonical delivery is
                # independently finalized and acknowledged.
                capture.upload_legacy_stdout(heartbeat)
                shutil.rmtree(capture.root)
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
        # PID directory names do not describe completion order. Preserve the host's
        # observed write order so the controller can make a later rerun authoritative.
        receipts = _validation_receipts(execution_dir)
        heartbeat.ensure_current()
        heartbeat.finish({"lease_token": run["lease_token"], "exit_code": rc,
                          "final_text": final, "result": parsed, "usage": usage,
                          "cost_usd": cost, "error": error, "pushed_head": head,
                          "validation_receipts": receipts})
    finally:
        if repo_lock is not None:
            repo_lock.close()
        heartbeat.stop()


def run_worker(url: str, host: str, token: str, root: Path, harnesses: list[str], tiers: list[str],
               capacity: int = 1, once: bool = False, poll_seconds: float = 5,
               setup_command: str = "") -> None:
    client = WorkerClient(url, token)
    claim_state = root / "claim-requests" / f"{hashlib.sha256(host.encode()).hexdigest()[:16]}.json"
    while True:
        claim_state.parent.mkdir(parents=True, exist_ok=True)
        try:
            claim_request_id = str(json.loads(claim_state.read_text())["claim_request_id"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            claim_request_id = secrets.token_urlsafe(24)
            temporary = claim_state.with_suffix(".tmp")
            temporary.write_text(json.dumps({"claim_request_id": claim_request_id}))
            with temporary.open("rb") as source:
                os.fsync(source.fileno())
            temporary.replace(claim_state)
        try:
            status, claim = client.post("/api/runs/claim", {
                "host": host, "harnesses": harnesses, "tiers": tiers,
                "capacity": capacity, "claim_request_id": claim_request_id,
            })
        except WorkerRequestError as exc:
            if exc.status != 409:
                raise
            claim_state.unlink(missing_ok=True)
            continue
        if status == 204 or not claim:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        execute_claim(claim, root, client, setup_command=setup_command)
        claim_state.unlink(missing_ok=True)
        if once:
            return
