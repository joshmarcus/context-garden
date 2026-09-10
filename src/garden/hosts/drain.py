"""Durable bridge from provider interruption notices to pull-worker admission."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from ..runs import RunStore
from .models import HostFacts


class WorkerDrainStore:
    """Fence new claims and report when a managed host has uploaded its active run."""

    def __init__(self, garden_dir: Path):
        self.path = garden_dir / "hosts" / "worker-drains.json"
        self.garden_dir = garden_dir

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError, TypeError):
            return {}
        drains = value.get("drains") if isinstance(value, dict) else None
        return {str(key): dict(row) for key, row in drains.items() if isinstance(row, dict)} \
            if isinstance(drains, dict) else {}

    def request(self, host: HostFacts, *, deadline: str, detail: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        drains = self._read()
        previous = drains.get(host.operation_id, {})
        deadline = str(previous.get("deadline") or deadline)
        drains[host.operation_id] = {
            "host_id": host.host_id, "provider_id": host.provider_id,
            "deadline": deadline, "detail": detail,
        }
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"drains": drains}, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)
        active = [run for run in RunStore(self.garden_dir).active()
                  if run.runner == "remote" and run.host == host.host_id
                  and not run.process_finished()]
        if not active:
            return True
        try:
            expires = dt.datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        return expires <= dt.datetime.now(dt.UTC)

    def __call__(self, host: HostFacts, deadline: str, detail: str) -> bool:
        return self.request(host, deadline=deadline, detail=detail)

    def clear(self, operation_id: str) -> None:
        drains = self._read()
        if drains.pop(operation_id, None) is None:
            return
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"drains": drains}, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)

    def contains(self, operation_id: str) -> bool:
        return bool(operation_id and operation_id in self._read())
