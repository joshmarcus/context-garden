"""Single-slot managed host consumer of the portable worker protocol.

One process owns the machine lock across clone, setup, checks, harness and publication.
The EC2 lifecycle remains independent of this consumer. Credentials are local files,
never launch parameters or resource evidence.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import time
import urllib.error
import uuid
from contextlib import contextmanager
from pathlib import Path

from .remote_worker import WorkerClient, WorkerRequestError, execute_claim
from .system_resources import memory_bytes


def claim_with_retry(client: WorkerClient, payload: dict, *, sleep=time.sleep) -> tuple[int, dict]:
    """Repeat one logical idle claim with bounded backoff and a stable identity."""
    request = {**payload, "claim_request_id": uuid.uuid4().hex}
    delay = 0.25
    while True:
        try:
            return client.post("/api/runs/claim", request)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            sleep(delay)
        except WorkerRequestError as exc:
            if not exc.retryable:
                raise
            sleep(delay)
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
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class AttributedClient(WorkerClient):
    def __init__(self, config: dict, root: Path):
        super().__init__(config["endpoint"], config["worker_token"])
        self.config, self.root = config, root
        self.authenticated_registration = False

    def post(self, path: str, payload: dict):
        result = super().post(path, {**payload, "host_facts": {
            "profile_version": self.config["profile_version"],
            "bootstrap_version": self.config["bootstrap_version"],
            "source_head": self.config["source_head"],
            "provider_id": self.config["provider_id"],
            "readiness_attestations": {
                **self.config.get("readiness_attestations", {}),
                "authenticated_registration": self.authenticated_registration,
            },
            **resources(self.root),
        }})
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
    client = AttributedClient(config, root)
    with host_slot(root):
        while True:
            facts = resources(root)
            if (facts["memory_available_bytes"] < config.get("memory_reserve_mib", 512) * 1024**2
                    or facts["disk_free_bytes"] < config.get("disk_reserve_mib", 1024) * 1024**2):
                if once:
                    return
                time.sleep(5)
                continue
            status, claim = claim_with_retry(client, {
                "host": config["host"], "harnesses": config["harnesses"], "capacity": 1,
            })
            if status == 204 or not claim:
                if once:
                    return
                time.sleep(3)
                continue
            # These are explicitly host-owned credential/configuration paths, not controller values.
            claim["env_allowlist"] = [*claim.get("env_allowlist", []), *config.get("env_pass", [])]
            setup = dict(claim.get("setup") or {})
            execute_claim(claim, root, client, setup_command=str(setup.get("command") or ""))
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
