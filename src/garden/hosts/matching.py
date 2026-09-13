"""Pure capability and capacity matching for every task execution activity."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from ..model import ExecutionRequirements, ResourceReservation
from .models import WorkerConfiguration, WorkerInstance, verify_worker_configuration


class MatchReason(str, Enum):
    MATCHED = "matched"
    BUSY = "busy"
    NO_COMPATIBLE_PROFILE = "no_compatible_profile"
    OFFLINE_OR_STALE = "offline_or_stale"
    DENIED_ACCESS = "denied_access"
    INVALID_REQUIREMENTS = "invalid_requirements"
    BUDGET_OR_DEADLINE_HOLD = "budget_or_deadline_hold"


@dataclass(frozen=True)
class WorkerMatch:
    instance: WorkerInstance | None
    reason: MatchReason
    detail: str = ""


def match_worker(
    requirements: ExecutionRequirements,
    *,
    activity: str,
    project: str,
    owner: str,
    configurations: Mapping[str, WorkerConfiguration],
    instances: Iterable[WorkerInstance],
    busy_instance_ids: Iterable[str] = (),
    allocations: Mapping[str, Iterable[ResourceReservation]] | None = None,
    selection_counts: Mapping[str, int] | None = None,
    pinned_instance_id: str = "",
    held: bool = False,
    now: float | None = None,
) -> WorkerMatch:
    """Select one authorized instance before considering soft preferences.

    Ordering is stable: configuration preferences come first, then the least-selected
    compatible instance and its id. The caller supplies committed allocations from its
    transactionally protected run store; a pin is a hard constraint, never a way around
    authorization.
    """
    if held:
        return WorkerMatch(None, MatchReason.BUDGET_OR_DEADLINE_HOLD, "activity is held")
    if not activity or not project or not owner:
        return WorkerMatch(None, MatchReason.INVALID_REQUIREMENTS,
                           "activity, project, and authenticated task owner are required")
    candidates = list(instances)
    if pinned_instance_id:
        candidates = [item for item in candidates if item.instance_id == pinned_instance_id]
        if not candidates:
            return WorkerMatch(None, MatchReason.NO_COMPATIBLE_PROFILE,
                               "pinned worker instance is not configured")
    preferred = {name: index for index, name in enumerate(
        requirements.preferred_worker_configurations
    )}
    counts = selection_counts or {}
    candidates.sort(key=lambda item: (
        preferred.get(item.configuration, len(preferred)),
        int(counts.get(item.instance_id, 0)), item.instance_id
    ))
    busy = set(busy_instance_ids)
    saw_compatible_other_owner = False
    saw_stale = False
    saw_busy = False
    for instance in candidates:
        profile = configurations.get(instance.configuration)
        if profile is None:
            continue
        admission = verify_worker_configuration(
            profile, instance, activity=activity, project=project,
            required_capabilities=requirements.capabilities,
            required_memory_mib=requirements.resources.memory_mib,
            required_vcpu=requirements.resources.vcpu,
            required_gpu_count=requirements.resources.gpu.count if requirements.resources.gpu else 0,
            required_gpu_device_memory_mib=(
                requirements.resources.gpu.min_device_memory_mib
                if requirements.resources.gpu else 0
            ),
            required_gpu_vendor=(requirements.resources.gpu.vendor
                                 if requirements.resources.gpu else ""),
            required_gpu_features=(requirements.resources.gpu.features
                                   if requirements.resources.gpu else ()),
            now=now,
        )
        if instance.operating_user != owner:
            saw_compatible_other_owner |= admission.eligible
            continue
        if not admission.eligible:
            saw_stale |= "stale" in admission.detail
            continue
        reserved = tuple((allocations or {}).get(instance.instance_id, ()))
        memory_used = sum(item.memory_mib for item in reserved)
        vcpu_used = sum(item.vcpu for item in reserved)
        gpu_used = sum(item.gpu.count for item in reserved if item.gpu)
        requested_gpu = requirements.resources.gpu.count if requirements.resources.gpu else 0
        ceilings = profile.resource_ceilings
        insufficient_capacity = (
            memory_used + requirements.resources.memory_mib > ceilings.memory_mib
            or vcpu_used + requirements.resources.vcpu > ceilings.vcpu
            or gpu_used + requested_gpu > ceilings.gpu_count
        )
        if instance.instance_id in busy or insufficient_capacity:
            saw_busy = True
            continue
        return WorkerMatch(instance, MatchReason.MATCHED)
    if saw_busy:
        return WorkerMatch(None, MatchReason.BUSY, "all compatible same-user workers are busy")
    if saw_stale:
        return WorkerMatch(None, MatchReason.OFFLINE_OR_STALE,
                           "compatible same-user worker readiness is stale")
    if saw_compatible_other_owner:
        return WorkerMatch(None, MatchReason.DENIED_ACCESS,
                           "compatible capacity belongs to another user")
    return WorkerMatch(None, MatchReason.NO_COMPATIBLE_PROFILE,
                       "no trusted same-user worker profile satisfies the activity")
