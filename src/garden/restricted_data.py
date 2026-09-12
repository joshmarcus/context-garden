"""Host-owned authorization for workloads that may handle restricted data.

The policy is deliberately resolved from trusted local configuration.  A task may ask for
less access, but its brief, labels, and execution requirements are never authority inputs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .workload_identity import AuthorityMetadata


class RestrictedDataError(RuntimeError):
    """The workload cannot be dispatched inside the requested data boundary."""


class DatasetPermission(StrEnum):
    READ = "read"
    READ_WRITE = "read-write"


class EvidenceState(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class DatasetGrant:
    dataset: str
    permission: DatasetPermission


@dataclass(frozen=True)
class RestrictedWorkloadAuthorization:
    """Public, secret-free authorization fixed before a workload is dispatched."""

    boundary: str
    automation_identity: str
    identity_reference: str
    project: str
    activity: str
    datasets: tuple[DatasetGrant, ...]
    allowed_models: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    artifact_boundary: str
    evidence_exports: tuple[str, ...]

    @property
    def digest(self) -> str:
        body = {
            "boundary": self.boundary,
            "automation_identity": self.automation_identity,
            "identity_reference": self.identity_reference,
            "project": self.project,
            "activity": self.activity,
            "datasets": [(grant.dataset, grant.permission.value) for grant in self.datasets],
            "allowed_models": self.allowed_models,
            "allowed_tools": self.allowed_tools,
            "artifact_boundary": self.artifact_boundary,
            "evidence_exports": self.evidence_exports,
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def authorize_operation(self, *, dataset: str, write: bool = False) -> None:
        grants = {grant.dataset: grant.permission for grant in self.datasets}
        permission = grants.get(dataset)
        if permission is None or (write and permission is not DatasetPermission.READ_WRITE):
            operation = "write" if write else "read"
            raise RestrictedDataError(f"restricted dataset {operation} is not authorized")

    def validate_egress(self, *, model: str = "", tool: str = "") -> None:
        if model and model not in self.allowed_models:
            raise RestrictedDataError(f"model {model!r} is outside the restricted-data boundary")
        if tool and tool not in self.allowed_tools:
            raise RestrictedDataError(f"tool {tool!r} is outside the restricted-data boundary")

    def export_evidence(
        self, kind: str, payload: str, *, synthetic_markers: tuple[str, ...] = (),
        state: EvidenceState = EvidenceState.SUFFICIENT,
    ) -> dict[str, str]:
        """Validate deliberately small evidence leaving the private artifact boundary."""
        if kind not in self.evidence_exports:
            raise RestrictedDataError(f"evidence export {kind!r} is not permitted")
        if any(marker and marker in payload for marker in synthetic_markers):
            raise RestrictedDataError("evidence contains a restricted synthetic marker")
        return {"kind": kind, "state": state.value, "summary": payload}


def authorize_restricted_workload(
    config: Mapping[str, Any], boundary: str, *, identity: AuthorityMetadata,
    identity_reference: str, project: str, activity: str,
    requested_datasets: Mapping[str, str] | None = None, model: str = "", tool: str = "",
) -> RestrictedWorkloadAuthorization:
    """Resolve trusted data policy and optionally narrow its dataset grants.

    ``identity_reference`` is compared with policy; it does not select policy.  Likewise,
    requested datasets can remove grants or downgrade write access, never add authority.
    """
    section = config.get("restricted_data") or {}
    boundaries = section.get("boundaries") if isinstance(section, Mapping) else None
    policy = boundaries.get(boundary) if isinstance(boundaries, Mapping) else None
    if not isinstance(policy, Mapping):
        raise RestrictedDataError(f"restricted-data boundary {boundary!r} is not configured")
    unknown = set(policy) - {
        "identity_reference", "projects", "activities", "datasets", "models", "tools",
        "artifact_boundary", "evidence_exports",
    }
    if unknown:
        raise RestrictedDataError(f"restricted-data boundary {boundary!r} has unsupported fields")
    expected_reference = str(policy.get("identity_reference") or "")
    if not expected_reference or identity_reference != expected_reference:
        raise RestrictedDataError("workload identity is not approved for the restricted-data boundary")
    if not identity.automation_identity.startswith("automation:"):
        raise RestrictedDataError("restricted data requires an automation workload identity")
    projects = {str(value) for value in policy.get("projects") or []}
    activities = {str(value) for value in policy.get("activities") or []}
    if project not in projects or activity not in activities:
        raise RestrictedDataError("project or activity is outside the restricted-data boundary")
    raw_datasets = policy.get("datasets") or {}
    if not isinstance(raw_datasets, Mapping) or not raw_datasets:
        raise RestrictedDataError("restricted-data boundary has no approved datasets")
    approved: dict[str, DatasetPermission] = {}
    try:
        approved = {str(name): DatasetPermission(str(value)) for name, value in raw_datasets.items()}
    except ValueError as exc:
        raise RestrictedDataError("restricted-data boundary has an invalid dataset permission") from exc
    requested = approved if requested_datasets is None else requested_datasets
    grants: list[DatasetGrant] = []
    for name, raw_permission in requested.items():
        try:
            permission = DatasetPermission(str(raw_permission))
        except ValueError as exc:
            raise RestrictedDataError("requested dataset permission is invalid") from exc
        maximum = approved.get(str(name))
        if maximum is None or (
            permission is DatasetPermission.READ_WRITE and maximum is not DatasetPermission.READ_WRITE
        ):
            raise RestrictedDataError("requested dataset scope exceeds approved authority")
        grants.append(DatasetGrant(str(name), permission))
    authorization = RestrictedWorkloadAuthorization(
        boundary, identity.automation_identity, expected_reference, project, activity,
        tuple(sorted(grants, key=lambda grant: grant.dataset)),
        tuple(sorted(str(value) for value in policy.get("models") or [])),
        tuple(sorted(str(value) for value in policy.get("tools") or [])),
        str(policy.get("artifact_boundary") or ""),
        tuple(sorted(str(value) for value in policy.get("evidence_exports") or [])),
    )
    if not authorization.artifact_boundary:
        raise RestrictedDataError("restricted-data artifact boundary is not configured")
    authorization.validate_egress(model=model, tool=tool)
    return authorization
