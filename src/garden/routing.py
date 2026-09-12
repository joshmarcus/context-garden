"""Read-only, redacted views of task-to-worker routing decisions."""

from __future__ import annotations

import time
from typing import Any

from .hosts import MatchReason, match_worker, verify_worker_configuration
from .model import Task, effective_owner, parse_execution_requirements
from .runs import RunStore

SAFE_REASON_TEXT = {
    MatchReason.MATCHED: "An authorized compatible worker is ready.",
    MatchReason.BUSY: "Compatible worker capacity is currently reserved.",
    MatchReason.NO_COMPATIBLE_PROFILE: "No authorized worker profile satisfies the request.",
    MatchReason.OFFLINE_OR_STALE: "Compatible worker readiness evidence is unavailable or stale.",
    MatchReason.DENIED_ACCESS: "Compatible capacity is outside this task owner's boundary.",
    MatchReason.INVALID_REQUIREMENTS: "The effective requirements are invalid.",
    MatchReason.BUDGET_OR_DEADLINE_HOLD: "Policy currently holds this activity.",
}


def task_routing_view(store: Any, task: Task, *, activity: str = "work", now: float | None = None) -> dict[str, Any]:
    """Explain current routing without claiming or contacting a worker."""
    checked_at = time.time() if now is None else now
    phase = store.phase(task.product, task.phase)
    owner, owner_source = effective_owner(task, phase)
    product_raw = store.config.product(task.product).get("execution_requirements")
    phase_raw = phase.meta.get("execution_requirements")
    layers = [
        {"source": f"product:{task.product}", "requirements": parse_execution_requirements(product_raw).to_dict()},
        {"source": f"phase:{phase.key}", "requirements": parse_execution_requirements(phase_raw).to_dict()},
        {"source": f"task:{task.id}", "requirements": task.execution_requirements.to_dict()},
    ]
    try:
        requirements = store.config.execution_requirements(task, phase)
    except ValueError:
        return {"task_id": task.id, "activity": activity, "owner": owner,
                "owner_source": owner_source, "layers": layers, "effective_requirements": {},
                "match": {"reason": MatchReason.INVALID_REQUIREMENTS.value,
                          "explanation": SAFE_REASON_TEXT[MatchReason.INVALID_REQUIREMENTS]},
                "dry_run": True, "selected_worker": None, "runs": _run_provenance(store, task.id)}

    configurations = store.config.worker_configurations()
    instances = store.config.worker_instances()
    runs = RunStore(store.config.garden_dir)
    active = runs.active()
    busy = {run.host for run in active if run.host}
    match = match_worker(
        requirements, activity=activity, project=task.product, owner=owner,
        configurations=configurations, instances=instances, busy_instance_ids=busy,
        pinned_instance_id=str(task.extra.get("worker_instance") or ""), now=checked_at,
    ) if not requirements.empty else None
    selected = None
    if match and match.instance:
        profile = configurations[match.instance.configuration]
        admission = verify_worker_configuration(
            profile, match.instance, activity=activity, project=task.product,
            required_capabilities=requirements.capabilities,
            required_memory_mib=requirements.resources.memory_mib,
            required_vcpu=requirements.resources.vcpu,
            required_gpu_count=requirements.resources.gpu.count if requirements.resources.gpu else 0,
            required_gpu_device_memory_mib=(requirements.resources.gpu.min_device_memory_mib
                                            if requirements.resources.gpu else 0), now=checked_at,
        )
        selected = {"profile": profile.name, "version": profile.version,
                    "generation": profile.generation, "readiness": "verified" if admission.eligible else "unverified",
                    "readiness_checked_at": match.instance.readiness_checked_at,
                    "readiness_expires_at": match.instance.readiness_expires_at}
    reason = match.reason if match else MatchReason.MATCHED
    explanation = (SAFE_REASON_TEXT[reason] if match else
                   "This activity has no capability or hardware constraints.")
    return {"task_id": task.id, "activity": activity, "owner": owner,
            "owner_source": owner_source, "layers": layers,
            "effective_requirements": requirements.to_dict(),
            "match": {"reason": reason.value, "explanation": explanation},
            "dry_run": True, "selected_worker": selected, "runs": _run_provenance(store, task.id)}


def worker_configuration_views(store: Any, *, now: float | None = None) -> list[dict[str, Any]]:
    """Authorized configuration/readiness/capacity view with credential identities omitted."""
    checked_at = time.time() if now is None else now
    configs = store.config.worker_configurations()
    instances = store.config.worker_instances()
    active = RunStore(store.config.garden_dir).active()
    rows = []
    for name, profile in sorted(configs.items()):
        profile_instances = [item for item in instances if item.configuration == name]
        reservations = [run for run in active if any(run.host == item.instance_id for item in profile_instances)]
        ready_instances = [item for item in profile_instances
                           if item.readiness_checked_at <= checked_at < item.readiness_expires_at
                           and not (item.revoked_at and item.revoked_at <= checked_at)]
        public = profile.public_dict()
        public.pop("identity_references", None)
        public["instances"] = [{
            "readiness": ("verified" if item.readiness_checked_at <= checked_at < item.readiness_expires_at
                          and not (item.revoked_at and item.revoked_at <= checked_at) else "stale_or_revoked"),
            "readiness_checked_at": item.readiness_checked_at,
            "readiness_expires_at": item.readiness_expires_at,
        } for item in profile_instances]
        public["capacity"] = {"instances": len(profile_instances), "verified": len(ready_instances),
                              "reserved": len(reservations),
                              "available": max(0, len(ready_instances) - len(reservations))}
        rows.append(public)
    return rows


def _run_provenance(store: Any, task_id: str) -> list[dict[str, Any]]:
    rows = []
    for run in RunStore(store.config.garden_dir).runs_for(task_id):
        envelope = dict((run.env_snapshot or {}).get("execution_envelope") or {})
        if not envelope:
            continue
        rows.append({"run_id": run.run_id, "mode": run.mode, "status": run.status,
                     "profile": str((run.env_snapshot or {}).get("worker_configuration") or ""),
                     "profile_version": str((run.env_snapshot or {}).get("worker_configuration_version") or ""),
                     "requirements": dict((run.env_snapshot or {}).get("execution_requirements") or {}),
                     "envelope": envelope})
    return rows
