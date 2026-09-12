from garden.hosts import (
    CapabilityGrant,
    MatchReason,
    ResourceCeilings,
    WorkerConfiguration,
    WorkerInstance,
    match_worker,
)
from garden.model import ExecutionRequirements, ResourceReservation


def profile(name="general"):
    return WorkerConfiguration(
        name, "1", 1, activities=("work",), projects=("demo",),
        resource_ceilings=ResourceCeilings(memory_mib=4096, vcpu=2),
        grants=(CapabilityGrant("tool.build", "operator", 1, 1),),
    )


def instance(name, user="alice", configuration="general", *, expires=300):
    return WorkerInstance(
        name, configuration, "1", 1, user, f"install-{name}", 1, 100, expires
    )


REQ = ExecutionRequirements(
    capabilities=("tool.build",), resources=ResourceReservation(memory_mib=1024, vcpu=1)
)


def test_match_is_same_user_deterministic_and_fair_across_busy_capacity():
    configs = {"general": profile()}
    workers = [instance("worker-b"), instance("worker-a"), instance("worker-x", "bob")]
    first = match_worker(
        REQ, activity="work", project="demo", owner="alice",
        configurations=configs, instances=workers, now=200,
    )
    assert first.instance and first.instance.instance_id == "worker-a"
    second = match_worker(
        REQ, activity="work", project="demo", owner="alice",
        configurations=configs, instances=workers, busy_instance_ids=("worker-a",), now=200,
    )
    assert second.instance and second.instance.instance_id == "worker-b"
    fair = match_worker(
        REQ, activity="work", project="demo", owner="alice",
        configurations=configs, instances=workers,
        selection_counts={"worker-a": 2, "worker-b": 1}, now=200,
    )
    assert fair.instance and fair.instance.instance_id == "worker-b"
    busy = match_worker(
        REQ, activity="work", project="demo", owner="alice",
        configurations=configs, instances=workers,
        busy_instance_ids=("worker-a", "worker-b"), now=200,
    )
    assert busy.reason is MatchReason.BUSY


def test_other_user_stale_and_pin_fail_with_distinct_reasons():
    configs = {"general": profile()}
    denied = match_worker(
        REQ, activity="work", project="demo", owner="alice",
        configurations=configs, instances=(instance("worker-x", "bob"),), now=200,
    )
    assert denied.reason is MatchReason.DENIED_ACCESS
    stale = match_worker(
        REQ, activity="work", project="demo", owner="alice",
        configurations=configs, instances=(instance("worker-a", expires=150),), now=200,
    )
    assert stale.reason is MatchReason.OFFLINE_OR_STALE
    pinned = match_worker(
        REQ, activity="work", project="demo", owner="alice",
        configurations=configs, instances=(instance("worker-a"),),
        pinned_instance_id="missing", now=200,
    )
    assert pinned.reason is MatchReason.NO_COMPATIBLE_PROFILE


def test_preferences_rank_only_after_hard_requirements():
    preferred = profile("preferred")
    fallback = profile("fallback")
    req = ExecutionRequirements(
        capabilities=REQ.capabilities, resources=REQ.resources,
        preferred_worker_configurations=("preferred",),
    )
    match = match_worker(
        req, activity="work", project="demo", owner="alice",
        configurations={"preferred": preferred, "fallback": fallback},
        instances=(instance("fallback", configuration="fallback"),
                   instance("preferred", configuration="preferred")), now=200,
    )
    assert match.instance and match.instance.instance_id == "preferred"
