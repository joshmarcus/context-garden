from __future__ import annotations

import yaml

from garden.config import Config, executable_diff
from garden.hosts import (
    WORKER_CONFIGURATION_CONTRACT_VERSION,
    CapabilityGrant,
    ProviderLifecycleCapabilities,
    ResourceCeilings,
    WorkerConfiguration,
    WorkerIdentityBinding,
    WorkerInstance,
    WorkerObservation,
    verify_worker_configuration,
)


def configuration(**changes):
    values = {
        "name": "restricted-gpu",
        "version": "2026-09-12",
        "generation": 3,
        "activities": ("work", "check"),
        "projects": ("analytics",),
        "resource_ceilings": ResourceCeilings(32768, 8, 1, 24576),
        "identity_references": ("identity.analytics-reader",),
        "grants": (
            CapabilityGrant("data.analytics", "security@example.test", 100, 3),
            CapabilityGrant("tool.cuda", "operator@example.test", 100, 3),
        ),
        "provider_capabilities": ProviderLifecycleCapabilities(True, True, True),
    }
    values.update(changes)
    return WorkerConfiguration(**values)


def instance(**changes):
    values = {
        "instance_id": "worker-a",
        "configuration": "restricted-gpu",
        "configuration_version": "2026-09-12",
        "profile_generation": 3,
        "operating_user": "alice",
        "installation_id": "install-a",
        "authenticated_at": 100,
        "readiness_checked_at": 190,
        "readiness_expires_at": 250,
        "observations": (WorkerObservation("tool.untrusted-label", 190, 250),),
        "identity_bindings": (
            WorkerIdentityBinding(
                "identity.analytics-reader", "credential.enrollment-a", "alice", "install-a", 100
            ),
        ),
    }
    values.update(changes)
    return WorkerInstance(**values)


def test_verified_grants_are_distinct_from_observations_and_provider_lifecycle():
    admitted = verify_worker_configuration(
        configuration(), instance(), activity="work", project="analytics",
        required_capabilities=("data.analytics",), required_memory_mib=16000,
        required_gpu_count=1, now=200,
    )
    assert admitted.eligible
    assert admitted.authoritative_capabilities == ("data.analytics", "tool.cuda")
    assert admitted.observed_capabilities == ("tool.untrusted-label",)

    denied = verify_worker_configuration(
        configuration(), instance(), activity="work", project="analytics",
        required_capabilities=("tool.untrusted-label",), now=200,
    )
    assert not denied.eligible
    assert "operator grants" in denied.detail


def test_revocation_generation_scope_resources_and_freshness_fail_closed():
    revoked = configuration(grants=(
        CapabilityGrant("data.analytics", "security@example.test", 100, 3, revoked_at=180),
    ))
    assert not verify_worker_configuration(
        revoked, instance(), activity="work", project="analytics",
        required_capabilities=("data.analytics",), now=200,
    ).eligible
    assert "generation" in verify_worker_configuration(
        configuration(), instance(profile_generation=2), activity="work", project="analytics", now=200,
    ).detail
    assert "stale" in verify_worker_configuration(
        configuration(), instance(), activity="work", project="analytics", now=251,
    ).detail
    assert "ceiling" in verify_worker_configuration(
        configuration(), instance(), activity="work", project="analytics",
        required_memory_mib=40000, now=200,
    ).detail
    transferred = instance(identity_bindings=(
        WorkerIdentityBinding(
            "identity.analytics-reader", "credential.enrollment-b", "bob", "install-b", 100
        ),
    ))
    assert "owned by another" in verify_worker_configuration(
        configuration(), transferred, activity="work", project="analytics", now=200,
    ).detail


def test_legacy_negotiation_preserves_unconstrained_work_but_rejects_new_constraints():
    unrestricted = configuration(activities=(), projects=(), grants=(), identity_references=())
    legacy = instance(protocol_version=0)
    assert verify_worker_configuration(
        unrestricted, legacy, activity="work", project="public", now=200
    ).eligible
    denied = verify_worker_configuration(
        unrestricted, legacy, activity="work", project="public",
        required_memory_mib=1, now=200,
    )
    assert not denied.eligible
    assert "legacy worker" in denied.detail


def test_config_supports_multiple_user_owned_instances_and_redacts_identity_refs(tmp_path):
    data = {
        "capability_definitions": {
            "data.analytics": {"type": "data", "description": "analytics reader",
                               "issuer": "security", "privileged": True},
        },
        "worker_configurations": {
            "restricted": {
                "contract_version": WORKER_CONFIGURATION_CONTRACT_VERSION,
                "version": "1", "generation": 2,
                "activities": ["work"], "projects": ["demo"],
                "identity_references": ["identity.reader"],
                "resource_ceilings": {"memory_mib": 8192, "vcpu": 4},
                "grants": [{"capability": "data.analytics", "approved_by": "operator",
                            "approved_at": 10, "profile_generation": 2}],
            },
        },
        "worker_instances": [
            {"instance_id": name, "configuration": "restricted", "configuration_version": "1",
             "profile_generation": 2, "operating_user": user, "installation_id": name,
             "authenticated_at": 10, "readiness_checked_at": 20, "readiness_expires_at": 30,
             "identity_bindings": [{"identity_reference": "identity.reader",
                                    "credential_reference": f"credential.{name}",
                                    "operating_user": user, "installation_id": name,
                                    "enrolled_at": 10}]}
            for name, user in (("one", "alice"), ("two", "bob"))
        ],
    }
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump(data))
    config = Config.load(tmp_path)
    assert len(config.worker_instances()) == 2
    public = config.worker_configurations()["restricted"].public_dict()
    assert public["identity_references"] == ["<redacted>"]
    assert "identity.reader" not in str(public)
    assert executable_diff({}, data) == ["worker_configurations", "worker_instances"]
