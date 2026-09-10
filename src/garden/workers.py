"""Bounded, provider-neutral worker presence and occupancy snapshots.

Worker contact is durable independently of task leases: an idle pull worker still polls the
claim endpoint, and that poll is evidence that the agent is online.  Page and API reads only
combine this small ledger, configuration, and the indexed active-run set; they never contact
a host or provider.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
from typing import Any

from .runs import Run, RunStore

ACTIVE = {"requested", "preparing", "running"}
TERMINAL_HOST_STATES = {"disabled", "terminated"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except (TypeError, ValueError):
        return None


class WorkerContactStore:
    """Atomic last-contact ledger keyed by the configured worker name."""

    def __init__(self, garden_dir: Path):
        self.path = garden_dir / "workers.json"

    def read(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        workers = value.get("workers") if isinstance(value, dict) else None
        return {str(k): dict(v) for k, v in workers.items() if isinstance(v, dict)} \
            if isinstance(workers, dict) else {}

    def record(self, name: str, *, capacity: int | None, harnesses: list[str], tiers: list[str],
               facts: dict[str, Any] | None = None, outcome: str = "contact") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            workers = self.read()
            previous = workers.get(name, {})
            identities = list(previous.get("prior_provider_ids") or [])
            old_provider = str((previous.get("facts") or {}).get("provider_id") or "")
            new_provider = str((facts or {}).get("provider_id") or "")
            if old_provider and new_provider and old_provider != new_provider and old_provider not in identities:
                identities.append(old_provider)
            workers[name] = {
                "last_contact": _now().isoformat(),
                "capacity": (max(1, int(capacity)) if capacity is not None
                             else max(1, int(previous.get("capacity") or 1))),
                "harnesses": sorted(set(harnesses)) if harnesses else list(previous.get("harnesses") or []),
                "tiers": sorted(set(tiers)) if tiers else list(previous.get("tiers") or []),
                "outcome": outcome,
                "facts": dict(facts or previous.get("facts") or {}),
                "prior_provider_ids": identities[-8:],
            }
            temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"workers": workers}, indent=2, sort_keys=True) + "\n")
            temporary.replace(self.path)


def _job(run: Run, now: dt.datetime) -> dict[str, Any]:
    lease = _parse(run.lease_expires_at)
    recovery = _parse(run.recovery_expires_at)
    if run.runner == "remote" and run.host:
        if lease and lease <= now:
            lease_state = "recovering" if recovery and recovery > now else "expired"
        else:
            lease_state = "current" if lease else "unknown"
    else:
        lease_state = "not applicable"
    return {
        "task_id": run.task_id,
        "run_id": run.run_id,
        "mode": run.mode,
        "status": run.status,
        "task_url": f"/tasks/{run.task_id}",
        "run_url": f"/runs/{run.task_id}/{run.run_id}",
        "lease_state": lease_state,
        "lease_expires_at": run.lease_expires_at or None,
        "recovery_expires_at": run.recovery_expires_at or None,
    }


def snapshot(config: Any, runs: RunStore, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Return one secret-free fleet reading without network or subprocess work."""
    now = now or _now()
    active = [run for run in runs.active() if run.status in ACTIVE and run.runner != "manual"
              and not run.process_finished()]
    queued_remote = [run for run in active if run.runner == "remote" and not run.host]
    contacts = WorkerContactStore(config.garden_dir).read()
    remote_cfg = {str(row.get("name") or ""): dict(row)
                  for row in (config.get("workers.hosts") or []) if row.get("name")}
    ssh_cfg = {str(row.get("name") or ""): dict(row)
               for row in (config.get("ssh.hosts") or []) if row.get("name")}
    rows: list[dict[str, Any]] = []

    def add(identity: str, placement: str, cfg: dict[str, Any], contact: dict[str, Any] | None,
            jobs: list[Run], capacity: int) -> None:
        explicit = str(cfg.get("state") or cfg.get("status") or "").lower()
        last_contact = str((contact or {}).get("last_contact") or "")
        contact_at = _parse(last_contact)
        poll_stale_after = max(1, int(config.get("workers.poll_seconds", 5) or 5) * 4)
        # Busy pull workers contact the controller on the lease heartbeat cadence rather
        # than the idle poll cadence. A lease remains current only while those heartbeats
        # arrive, so its duration is the natural upper bound for current worker contact.
        lease_stale_after = max(1, int(config.get("workers.lease_seconds", 120) or 120))
        stale_after = lease_stale_after if placement == "remote" and jobs else poll_stale_after
        stale = bool(contact_at and (now - contact_at).total_seconds() > stale_after)
        job_rows = [_job(run, now) for run in jobs]
        reconnecting = any(job["lease_state"] == "recovering" for job in job_rows)
        used = sum(int((run.env_snapshot or {}).get("resource_weight") or 1) for run in jobs)
        if explicit in TERMINAL_HOST_STATES | {"stopped"}:
            status = "disabled" if explicit == "stopped" else explicit
        elif explicit in {"draining", "restarting"}:
            status = explicit
        elif explicit in {"provisioning", "bootstrapping"}:
            status = "restarting"
        elif explicit == "interrupted":
            status = "reconnecting"
        elif explicit == "failed":
            status = "unreachable"
        elif placement == "remote" and not contact_at:
            status = "unknown"
        elif placement == "remote" and stale:
            status = "unreachable"
        elif placement == "ssh" and not contact_at:
            status = "unknown"
        elif reconnecting:
            status = "reconnecting"
        elif jobs:
            status = "executing"
        else:
            status = "available"
        available = (max(0, capacity - used)
                     if status in {"available", "executing"} else 0)
        reason = ""
        if status == "available":
            reason = "polling for work" if placement == "remote" else "no current job"
        if placement == "remote" and status in {"available", "executing"} \
                and available > 0 and queued_remote:
            offered = set((contact or {}).get("harnesses") or [])
            tiers = set((contact or {}).get("tiers") or [])
            compatible = [run for run in queued_remote
                          if (run.mode == "check" or run.harness in offered)
                          and (not tiers or not run.difficulty or run.difficulty in tiers)]
            if not compatible:
                reason = "queued work requires a harness or tier this worker did not offer"
            elif all(int((run.env_snapshot or {}).get("resource_weight") or 1) > available
                     for run in compatible):
                reason = "queued work requires more capacity than this worker has available"
        elif status in {"unknown", "unreachable"}:
            reason = "no recent worker-agent contact"
        elif status in TERMINAL_HOST_STATES | {"draining", "restarting", "reconnecting"}:
            reason = f"worker is {status}"
        elif used >= capacity:
            reason = "all capacity is occupied"
        facts = dict((contact or {}).get("facts") or {})
        observed = facts.get("observed_at")
        evidence_at = (dt.datetime.fromtimestamp(float(observed), dt.UTC).isoformat()
                       if isinstance(observed, (int, float)) else last_contact or None)
        evidence_time = _parse(evidence_at or "")
        evidence_stale = not evidence_time or (now - evidence_time).total_seconds() > stale_after
        rows.append({
            "id": identity, "placement": placement, "status": status,
            "last_contact": last_contact or None, "evidence_at": evidence_at,
            "evidence_stale": evidence_stale,
            "capacity": capacity, "available_capacity": available,
            "current_jobs": job_rows, "unavailable_reason": reason,
            "provider_id": facts.get("provider_id"),
            "prior_provider_ids": list((contact or {}).get("prior_provider_ids") or []),
        })

    remote_names = set(remote_cfg) | set(contacts)
    seen_providers: set[str] = set()
    ordered_names = sorted(
        remote_names,
        key=lambda name: (str((contacts.get(name) or {}).get("last_contact") or ""),
                          name in remote_cfg, name),
        reverse=True,
    )
    for name in ordered_names:
        cfg, contact = remote_cfg.get(name, {}), contacts.get(name)
        provider_id = str(((contact or {}).get("facts") or {}).get("provider_id") or "")
        if provider_id and provider_id in seen_providers:
            continue
        if provider_id:
            seen_providers.add(provider_id)
        aliases = {name}
        if provider_id:
            aliases |= {
                candidate for candidate in remote_names
                if str(((contacts.get(candidate) or {}).get("facts") or {}).get("provider_id") or "")
                == provider_id
            }
        jobs = [run for run in active if run.runner == "remote" and run.host in aliases]
        capacity = int((contact or {}).get("capacity") or cfg.get("max_parallel") or 1)
        add(name, "remote", cfg, contact, jobs, capacity)
    for name, cfg in sorted(ssh_cfg.items()):
        add(name, "ssh", cfg, None, [run for run in active if run.runner == "ssh" and run.host == name],
            int(cfg.get("max_parallel") or 1))
    local_jobs = [run for run in active if run.is_local_execution]
    if local_jobs:
        # Local workers are processes created for a run, not an idle daemon fleet. They
        # acquire an identity only while present, unlike configured pull and SSH hosts.
        add("local", "local", {}, {"last_contact": now.isoformat()}, local_jobs,
            int(config.get("max_parallel", 1) or 1))

    counts = {key: sum(row["status"] == key for row in rows) for key in (
        "available", "executing", "draining", "restarting", "reconnecting", "unreachable",
        "unknown", "terminated", "disabled")}
    return {
        "observed_at": now.isoformat(), "workers": rows,
        "totals": {"workers": len(rows), "jobs": sum(len(row["current_jobs"]) for row in rows),
                   "capacity": sum(row["capacity"] for row in rows),
                   "available_capacity": sum(row["available_capacity"] for row in rows), **counts},
        "explanation": ("Available pull workers poll for work and receive a task lease only when a claim succeeds. "
                        "Worker contact remains visible while idle; a task lease describes job ownership, not liveness."),
    }
