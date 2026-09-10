"""Single-slot managed host consumer of the portable worker protocol.

One process owns the machine lock across clone, setup, checks, harness and publication.
The EC2 lifecycle remains independent of this consumer. Credentials are local files,
never launch parameters or resource evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
import urllib.error
import uuid
from contextlib import contextmanager
from pathlib import Path

from .remote_worker import WorkerClient, WorkerRequestError, deliver_pending_results, execute_claim
from .system_resources import memory_bytes
from .worker_diagnostics import WorkerEventLog, durable_worker_identity

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
    import msvcrt


def claim_with_retry(client: WorkerClient, payload: dict, *, sleep=time.sleep,
                     max_elapsed_seconds: float = 300) -> tuple[int, dict]:
    """Repeat one logical idle claim with bounded backoff and a stable identity."""
    request = {**payload, "claim_request_id": uuid.uuid4().hex}
    delay = 0.25
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            result = client.post("/api/runs/claim", request)
            if getattr(client, "events", None) and attempts > 1:
                client.events.emit("transport_recovered", request_id=request["claim_request_id"],
                                   operation="claim", reconnect_attempts=attempts - 1,
                                   recovery_outcome="recovered")
            return result
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            cause = type(exc).__name__
        except WorkerRequestError as exc:
            if not exc.retryable:
                if getattr(client, "events", None):
                    client.events.emit("worker_exit", exit_reason="authentication_or_permanent_failure",
                                       cause="authentication" if exc.status in {401, 403} else "permanent_response",
                                       operator_action="verify worker enrollment and controller compatibility")
                raise
            cause = f"http_{exc.status}"
        elapsed = time.monotonic() - started
        if elapsed >= max_elapsed_seconds:
            if getattr(client, "events", None):
                client.events.emit("worker_exit", exit_reason="controller_unavailable",
                                   cause=cause, reconnect_attempts=attempts,
                                   recovery_outcome="retry_window_exhausted",
                                   operator_action="check controller and proxy health, then restart the worker")
            raise RuntimeError(f"claim recovery window exhausted after {attempts} attempts")
        jittered = delay * random.uniform(0.8, 1.2)
        if getattr(client, "events", None):
            client.events.emit("transport_retry", request_id=request["claim_request_id"], operation="claim",
                               reconnect_attempt=attempts, backoff_seconds=round(jittered, 3), cause=cause)
        sleep(min(jittered, max_elapsed_seconds - elapsed))
        delay = min(delay * 2, 5.0)


def resources(root: Path) -> dict:
    available, total = memory_bytes()
    if available is None or total is None:
        raise RuntimeError("host memory statistics are unavailable")
    return {"memory_available_bytes": available,
            "memory_total_bytes": total,
            "disk_free_bytes": shutil.disk_usage(root).free,
            "cpu_count": os.cpu_count(), "observed_at": time.time()}


@contextmanager
def host_slot(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "host.lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        else:  # pragma: no cover - exercised on Windows
            lock.seek(0)
            lock.write("0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


class AttributedClient(WorkerClient):
    def __init__(self, config: dict, root: Path):
        super().__init__(config["endpoint"], config["worker_token"])
        self.config, self.root = config, root
        self.authenticated_registration = False

    def post(self, path: str, payload: dict):
        attestations = dict(self.config.get("readiness_attestations", {}))
        versioned = "operation_id" in self.config or "source_bootstrap" in self.config
        if versioned and not all(self.config.get(name)
                                 for name in ("operation_id", "source_bootstrap")):
            raise ValueError("versioned worker config requires operation_id and source_bootstrap")
        if versioned:
            attestations["authenticated_registration"] = {
                "ok": self.authenticated_registration,
                "method": "scoped-worker-token",
            }
        else:
            # Compatibility for already deployed workers. These legacy booleans remain
            # useful attribution but cannot satisfy durable production readiness.
            attestations["authenticated_registration"] = self.authenticated_registration
        facts = {
            "profile_version": self.config["profile_version"],
            "bootstrap_version": self.config["bootstrap_version"],
            "source_head": self.config["source_head"],
            "provider_id": self.config["provider_id"],
            "readiness_attestations": attestations,
            **resources(self.root),
        }
        if versioned:
            facts.update({
                "schema_version": 1,
                "operation_id": self.config["operation_id"],
                "source_bootstrap": self.config["source_bootstrap"],
            })
        result = super().post(path, {**payload, "host_facts": facts})
        if path == "/api/runs/claim" and result[0] in {200, 204}:
            # The controller accepted this host's scoped bearer token. Subsequent
            # heartbeat/finish facts bind that authenticated registration to the run.
            self.authenticated_registration = True
        return result


def run(config: dict, *, once: bool = False):
    root = Path(config["work_dir"])
    root.mkdir(parents=True, exist_ok=True)
    temp = root / "tmp"
    temp.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(temp)
    # tempfile may have been imported before the disk-backed location was installed.
    import tempfile
    tempfile.tempdir = str(temp)
    worker_id = durable_worker_identity(root, str(config.get("worker_id") or ""))
    generation = uuid.uuid4().hex
    events = WorkerEventLog(root / "worker-events.jsonl", worker_id=worker_id, generation=generation)
    restart_path = root / "restart-count"
    restart_count = int(restart_path.read_text().strip() or 0) + 1 if restart_path.exists() else 1
    restart_path.write_text(f"{restart_count}\n")
    events.emit("worker_start", restart_count=restart_count, exit_reason="process_start")
    client = AttributedClient(config, root)
    client.events = events
    client.worker_id = worker_id
    client.process_generation = generation
    with host_slot(root):
        while True:
            deliver_pending_results(
                root, client,
                max_attempts=int(config.get("result_recovery_attempts", 5)),
                max_elapsed_seconds=float(config.get("result_recovery_seconds", 30)),
            )
            facts = resources(root)
            if (facts["memory_available_bytes"] < config.get("memory_reserve_mib", 512) * 1024**2
                    or facts["disk_free_bytes"] < config.get("disk_reserve_mib", 1024) * 1024**2):
                if once:
                    return
                time.sleep(5)
                continue
            status, claim = claim_with_retry(client, {
                "host": config["host"], "harnesses": config["harnesses"], "capacity": 1,
            }, max_elapsed_seconds=float(config.get("claim_recovery_seconds", 300)))
            if status == 204 or not claim:
                if once:
                    return
                time.sleep(3)
                continue
            # These are explicitly host-owned credential/configuration paths, not controller values.
            claim["env_allowlist"] = [*claim.get("env_allowlist", []), *config.get("env_pass", [])]
            setup = dict(claim.get("setup") or {})
            execute_claim(claim, root, client, setup_command=str(setup.get("command") or ""),
                          host_config=config)
            if once:
                return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(json.loads(args.config.read_text()), once=args.once)


if __name__ == "__main__":
    main()
