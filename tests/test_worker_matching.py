from garden.hosts import (
    CapabilityGrant,
    MatchReason,
    ResourceCeilings,
    WorkerConfiguration,
    WorkerIdentityBinding,
    WorkerInstance,
    WorkerObservation,
    match_worker,
)
from garden.model import ExecutionRequirements, GpuReservation, ResourceReservation


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


def test_routing_explanation_does_not_export_private_match_detail():
    from garden.routing import SAFE_REASON_TEXT

    assert "capabil" not in SAFE_REASON_TEXT[MatchReason.NO_COMPATIBLE_PROFILE].lower()
    assert "identity" not in SAFE_REASON_TEXT[MatchReason.DENIED_ACCESS].lower()


def test_heterogeneous_worker_matrix_fences_capabilities_and_user_ownership():
    """Synthetic GPU/data labels prove routing policy, not access to either resource."""
    now = 200
    configurations = {
        "general": profile(),
        "restricted": WorkerConfiguration(
            "restricted", "1", 4, activities=("work",), projects=("demo",),
            resource_ceilings=ResourceCeilings(memory_mib=8192, vcpu=4),
            identity_references=("identity.analytics",),
            grants=(CapabilityGrant("data.analytics", "security", 1, 4),),
        ),
        "gpu": WorkerConfiguration(
            "gpu", "1", 2, activities=("work",), projects=("demo",),
            resource_ceilings=ResourceCeilings(
                memory_mib=32768, vcpu=8, gpu_count=1, gpu_device_memory_mib=24576,
            ),
            grants=(CapabilityGrant("tool.cuda", "operator", 1, 2),),
        ),
    }
    restricted_binding = WorkerIdentityBinding(
        "identity.analytics", "credential.alice", "alice", "install-restricted", 1,
    )
    workers = (
        instance("general-a"),
        WorkerInstance(
            "restricted-a", "restricted", "1", 4, "alice", "install-restricted",
            1, 100, 300, identity_bindings=(restricted_binding,),
        ),
        WorkerInstance("gpu-a", "gpu", "1", 2, "alice", "install-gpu", 1, 100, 300),
        WorkerInstance(
            "restricted-b", "restricted", "1", 4, "bob", "install-restricted-b",
            1, 100, 300,
            identity_bindings=(WorkerIdentityBinding(
                "identity.analytics", "credential.bob", "bob", "install-restricted-b", 1,
            ),),
        ),
    )
    requirements = {
        "general": REQ,
        "restricted": ExecutionRequirements(capabilities=("data.analytics",)),
        "gpu": ExecutionRequirements(
            capabilities=("tool.cuda",),
            resources=ResourceReservation(
                memory_mib=16000, vcpu=4,
                gpu=GpuReservation(count=1, min_device_memory_mib=16000),
            ),
        ),
    }

    for workload, expected in (
        ("general", "general-a"),
        ("restricted", "restricted-a"),
        ("gpu", "gpu-a"),
    ):
        match = match_worker(
            requirements[workload], activity="work", project="demo", owner="alice",
            configurations=configurations, instances=workers, now=now,
        )
        assert match.reason is MatchReason.MATCHED
        assert match.instance and match.instance.instance_id == expected

    busy = match_worker(
        requirements["gpu"], activity="work", project="demo", owner="alice",
        configurations=configurations, instances=workers, busy_instance_ids=("gpu-a",), now=now,
    )
    assert busy.reason is MatchReason.BUSY
    no_match = match_worker(
        ExecutionRequirements(capabilities=("tool.cuda", "data.analytics")),
        activity="work", project="demo", owner="alice", configurations=configurations,
        instances=workers, now=now,
    )
    assert no_match.reason is MatchReason.NO_COMPATIBLE_PROFILE
    cross_user = match_worker(
        requirements["restricted"], activity="work", project="demo", owner="carol",
        configurations=configurations, instances=workers, now=now,
    )
    assert cross_user.reason is MatchReason.DENIED_ACCESS


def test_self_asserted_stale_revoked_and_drifted_authority_never_routes_restricted_work():
    required = ExecutionRequirements(capabilities=("data.analytics",))
    base = WorkerConfiguration(
        "restricted", "1", 3, activities=("work",), projects=("demo",),
        resource_ceilings=ResourceCeilings(memory_mib=4096),
        grants=(CapabilityGrant("data.analytics", "security", 1, 3),),
    )
    observed_only = WorkerConfiguration(
        "observed", "1", 1, activities=("work",), projects=("demo",),
        resource_ceilings=ResourceCeilings(memory_mib=4096),
    )
    cases = (
        (base, WorkerInstance("stale", "restricted", "1", 3, "alice", "i-1", 1, 10, 100)),
        (base, WorkerInstance("drift", "restricted", "1", 2, "alice", "i-2", 1, 10, 300)),
        (WorkerConfiguration(
            "revoked", "1", 3, activities=("work",), projects=("demo",),
            resource_ceilings=ResourceCeilings(memory_mib=4096),
            grants=(CapabilityGrant("data.analytics", "security", 1, 3, revoked_at=150),),
        ), WorkerInstance("revoked", "revoked", "1", 3, "alice", "i-3", 1, 10, 300)),
        (observed_only, WorkerInstance(
            "asserted", "observed", "1", 1, "alice", "i-4", 1, 10, 300,
            observations=(WorkerObservation("data.analytics", 10, 300),),
        )),
    )

    for configuration, worker in cases:
        match = match_worker(
            required, activity="work", project="demo", owner="alice",
            configurations={configuration.name: configuration}, instances=(worker,), now=200,
        )
        assert match.instance is None
        assert match.reason in {MatchReason.NO_COMPATIBLE_PROFILE, MatchReason.OFFLINE_OR_STALE}
