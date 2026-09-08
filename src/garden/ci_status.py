"""Exact-head CI status providers.

Status answers are deliberately smaller than CI analysers: a provider says whether one
immutable revision passed; analysers may still turn a known failure into useful feedback.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CIStatus:
    state: str
    queried_sha: str
    stale: bool = False
    exists_for_sha: bool = False
    evidence_url: str = ""
    failures: list[str] = field(default_factory=list)
    provider: str = "github"

    @property
    def green(self) -> bool:
        return self.state == "success" and self.exists_for_sha and not self.stale

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "green": self.green}


def github_status(pr: Any, required: bool) -> CIStatus:
    """Normalize the exact commit rollup returned with a GitHub PR."""
    sha = str(pr.head_sha or "")
    rollup = str(pr.checks or "").upper()
    if not required and not rollup:
        return CIStatus("not_required", sha, evidence_url=str(pr.url or ""))
    states = {"SUCCESS": "success", "FAILURE": "failure", "PENDING": "pending"}
    return CIStatus(states.get(rollup, "missing" if not rollup else "unknown"), sha,
                    exists_for_sha=bool(sha and rollup), evidence_url=str(pr.url or ""),
                    failures=list(pr.failed_checks or []))


def worker_check_status(garden_dir: Path, task_id: str, sha: str,
                        policy: dict[str, Any]) -> CIStatus:
    """Read the newest Garden-authored supervised-validation receipt for ``sha``.

    Receipt files live below immutable run directories and are written by the validation
    supervisor, not parsed from an author's result prose.
    """
    required_command = str(policy.get("command") or "").strip()
    candidates = sorted((garden_dir / "runs" / task_id).glob("*/validations/*/result.json"), reverse=True)
    mismatched = False
    malformed = False
    for path in candidates:
        try:
            row = json.loads(path.read_text())
            receipt_sha = str(row["source_sha"])
            command = str(row["command"])
            exit_code = int(row["exit_code"])
            log = str(row["log_location"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            malformed = True
            continue
        if receipt_sha != sha:
            mismatched = True
            continue
        if required_command and command != required_command:
            continue
        failures = [] if exit_code == 0 else [f"validation exited {exit_code}"]
        run_id = path.parents[2].name
        evidence_url = f"/runs/{task_id}/{run_id}" if run_id else log
        return CIStatus("success" if exit_code == 0 else "failure", sha,
                        exists_for_sha=True, evidence_url=evidence_url, failures=failures,
                        provider="worker_check")
    state = "mismatched" if mismatched else "malformed" if malformed else "missing"
    return CIStatus(state, sha, stale=mismatched, provider="worker_check")


def status_reason(status: CIStatus) -> str:
    if status.green:
        return ""
    detail = f" ({', '.join(status.failures)})" if status.failures else ""
    head = status.queried_sha[:12] or "unknown head"
    if status.provider == "github" and status.state == "missing":
        return f"GitHub CI has no CI result for {head} (checks are missing)"
    noun = "CI checks" if status.provider == "github" else "CI"
    return f"{status.provider} {noun} for {head} are {status.state}{detail}"


StatusProvider = Callable[[Path, str, Any, dict[str, Any]], CIStatus]


def _github_provider(_garden_dir: Path, _task_id: str, pr: Any, policy: dict[str, Any]) -> CIStatus:
    return github_status(pr, bool(policy.get("required")))


def _worker_provider(garden_dir: Path, task_id: str, pr: Any, policy: dict[str, Any]) -> CIStatus:
    return worker_check_status(garden_dir, task_id, str(pr.head_sha or ""),
                               dict(policy.get("worker_check") or {}))


STATUS_PROVIDERS: dict[str, StatusProvider] = {
    "github": _github_provider,
    "worker_check": _worker_provider,
}


def register_status_provider(name: str, provider: StatusProvider) -> None:
    """Register a trusted controller-side provider extension."""
    if not name or name in STATUS_PROVIDERS:
        raise ValueError(f"CI status provider already registered or invalid: {name!r}")
    STATUS_PROVIDERS[name] = provider


def resolve_status(name: str, garden_dir: Path, task_id: str, pr: Any,
                   policy: dict[str, Any]) -> CIStatus:
    provider = STATUS_PROVIDERS.get(name)
    if provider is None:
        return CIStatus("unknown", str(pr.head_sha or ""), provider=name)
    try:
        status = provider(garden_dir, task_id, pr, policy)
    except TimeoutError:
        return CIStatus("timeout", str(pr.head_sha or ""), provider=name)
    except (KeyError, TypeError, ValueError):
        return CIStatus("malformed", str(pr.head_sha or ""), provider=name)
    if not isinstance(status, CIStatus) or status.queried_sha != str(pr.head_sha or ""):
        return CIStatus("malformed", str(pr.head_sha or ""), provider=name)
    return status
