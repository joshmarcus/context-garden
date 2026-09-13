import pytest

from garden.restricted_data import (
    EvidenceState,
    RestrictedDataError,
    authorize_restricted_workload,
)
from garden.workload_identity import AuthorityMetadata


def identity(run="automation:run-1"):
    return AuthorityMetadata(
        issuer="synthetic", expires_at=10_000, audience="warehouse",
        scopes=("dataset:clinical",), principal_kind="automation",
        automation_identity=run, provider="synthetic",
    )


def policy():
    return {"restricted_data": {"boundaries": {"analytics": {
        "identity_reference": "warehouse-reader",
        "projects": ["approved-project"],
        "activities": ["check", "work"],
        "datasets": {"clinical": "read", "derived": "read-write"},
        "models": ["private-model"],
        "tools": ["sql"],
        "artifact_boundary": "private-artifacts",
        "evidence_exports": ["aggregate", "validation-state"],
        "synthetic_markers": ["RESTRICTED-ROW-42"],
    }}}}


def authorize(**changes):
    kwargs = {
        "identity": identity(), "identity_reference": "warehouse-reader",
        "project": "approved-project", "activity": "work", "model": "private-model",
        "tool": "sql",
    }
    kwargs.update(changes)
    return authorize_restricted_workload(policy(), "analytics", **kwargs)


def test_policy_binds_dataset_permissions_to_identity_project_activity_and_run():
    authorization = authorize(requested_datasets={"clinical": "read", "derived": "read"})
    authorization.authorize_operation(dataset="clinical")
    with pytest.raises(RestrictedDataError, match="write is not authorized"):
        authorization.authorize_operation(dataset="clinical", write=True)
    assert authorization.automation_identity == "automation:run-1"
    assert authorization.digest == authorization.digest

    for changes in (
        {"identity_reference": "worker-label"},
        {"project": "unapproved-project"},
        {"activity": "review"},
        {"identity": identity("alice")},
        {"requested_datasets": {"new": "read"}},
        {"requested_datasets": {"clinical": "read-write"}},
    ):
        with pytest.raises(RestrictedDataError):
            authorize(**changes)


def test_egress_and_private_artifact_policy_fail_before_use():
    authorization = authorize()
    assert authorization.artifact_boundary == "private-artifacts"
    with pytest.raises(RestrictedDataError, match="model"):
        authorize(model="general-model")
    with pytest.raises(RestrictedDataError, match="tool"):
        authorize(tool="internet")


def test_evidence_export_allows_insufficient_state_but_blocks_private_markers():
    authorization = authorize()
    exported = authorization.export_evidence(
        "validation-state", "Synthetic cohort was too small",
        synthetic_markers=("RESTRICTED-ROW-42",), state=EvidenceState.INSUFFICIENT,
    )
    assert exported == {
        "kind": "validation-state", "state": "insufficient",
        "summary": "Synthetic cohort was too small",
    }
    with pytest.raises(RestrictedDataError, match="synthetic marker"):
        authorization.export_evidence(
            "aggregate", "count=2; RESTRICTED-ROW-42", synthetic_markers=("RESTRICTED-ROW-42",)
        )
    with pytest.raises(RestrictedDataError, match="not permitted"):
        authorization.export_evidence("raw-data", "row contents")
    with pytest.raises(RestrictedDataError, match="synthetic marker"):
        authorization.validate_export_payload({"result": {"summary": "RESTRICTED-ROW-42"}})


def test_general_worker_receives_no_restricted_policy_or_payload():
    with pytest.raises(RestrictedDataError, match="not configured"):
        authorize_restricted_workload(
            policy(), "general", identity=identity(), identity_reference="warehouse-reader",
            project="approved-project", activity="work",
        )
