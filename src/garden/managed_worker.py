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
from contextlib import contextmanager
from pathlib import Path

from .remote_worker import WorkerClient, execute_claim


def resources(root: Path) -> dict:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.split()[0]) * 1024
    return {"memory_available_bytes": memory["MemAvailable"],
            "memory_total_bytes": memory["MemTotal"],
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

    def post(self, path: str, payload: dict):
        return super().post(path, {**payload, "host_facts": {
            "profile_version": self.config["profile_version"],
            "bootstrap_version": self.config["bootstrap_version"],
            "source_head": self.config["source_head"],
            "provider_id": self.config["provider_id"],
            **resources(self.root),
        }})


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
            status, claim = client.post("/api/runs/claim", {
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
