"""Exact-head CI status providers.

Status answers are deliberately smaller than CI analysers: a provider says whether one
immutable revision passed; analysers may still turn a known failure into useful feedback.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import re
import shlex
import subprocess
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .validation import receipt_has_current_policy


def _malformed_receipt_sources(raw: str) -> set[str]:
    """Recover source identities that were fully written before a JSON truncation."""
    return set(re.findall(r'"source_sha"\s*:\s*"([^"\\]*)"', raw))


def _completed_supervisor_execution(execution: object) -> bool:
    """Validate the bounded admission record emitted by ``run_supervisor``."""
    if not isinstance(execution, dict) or execution.get("state") != "finished":
        return False
    owner = execution.get("owner")
    pid = execution.get("pid")
    timeout = execution.get("timeout_seconds")
    if (not isinstance(owner, str) or not owner
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        return False
    try:
        started = dt.datetime.fromisoformat(execution["execution_started_at"])
        deadline = dt.datetime.fromisoformat(execution["deadline_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if started.tzinfo is None or deadline.tzinfo is None:
        return False
    if abs((deadline - started).total_seconds() - timeout) > 1e-6:
        return False
    if execution.get("inherited_lease") is True:
        return True
    slot = execution.get("slot")
    limit = execution.get("limit")
    requested_limit = execution.get("requested_limit")
    return (
        execution.get("owner_scoped") is True
        and isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0
        and isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        and slot < limit
        and isinstance(requested_limit, int) and not isinstance(requested_limit, bool)
        and requested_limit > 0
    )


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
                    # A PR rollup is already scoped to the PR's current head. Some API
                    # fakes and legacy providers omit the redundant head field.
                    exists_for_sha=bool(rollup), evidence_url=str(pr.url or ""),
                    failures=list(pr.failed_checks or []))


def worker_check_status(garden_dir: Path, task_id: str, sha: str,
                        policy: dict[str, Any]) -> CIStatus:
    """Read the newest Garden-authored supervised-validation receipt for ``sha``.

    Receipt files live below immutable run directories and are written by the validation
    supervisor, not parsed from an author's result prose.
    """
    required_command = str(policy.get("command") or "").strip()
    # Validation directory names are PIDs locally and remote sequence numbers after
    # ingestion; neither is chronological. result.json is written only on completion,
    # and remote results are recreated by the controller in host-observed write order.
    candidates = sorted(
        (garden_dir / "runs" / task_id).glob("*/validations/*/result.json"),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
        reverse=True,
    )
    mismatched = False
    malformed = False
    for path in candidates:
        try:
            raw = path.read_text()
            row = json.loads(raw)
        except OSError:
            malformed = True
            continue
        except json.JSONDecodeError:
            malformed_shas = _malformed_receipt_sources(raw)
            if sha in malformed_shas:
                return CIStatus("malformed", sha, exists_for_sha=True, provider="worker_check")
            if not malformed_shas:
                # This is the newest completion candidate, but truncation happened
                # before its source identity became durable.  It therefore cannot be
                # proven unrelated to the queried head, so an older success must not
                # become authoritative.
                return CIStatus("malformed", sha, provider="worker_check")
            mismatched = True
            continue
        if not isinstance(row, dict):
            # The newest completion has no trustworthy source identity, so an older
            # success cannot safely stand in for it.
            return CIStatus("malformed", sha, provider="worker_check")
        try:
            receipt_sha = str(row["source_sha"])
        except (TypeError, KeyError):
            if row.get("malformed_validation_receipt") is True:
                sources = row.get("recoverable_source_shas")
                overflow = row.get("recoverable_source_shas_overflow", False)
                if not isinstance(sources, list) or not all(
                    isinstance(item, str) for item in sources
                ) or not isinstance(overflow, bool):
                    return CIStatus("malformed", sha, provider="worker_check")
                if sha in sources:
                    return CIStatus(
                        "malformed", sha, exists_for_sha=True, provider="worker_check"
                    )
                if overflow or not sources:
                    return CIStatus("malformed", sha, provider="worker_check")
                mismatched = True
                continue
            return CIStatus("malformed", sha, provider="worker_check")
        if receipt_sha != sha:
            mismatched = True
            continue
        try:
            command = str(row["command"])
            exit_code = int(row["exit_code"])
            log = str(row["log_location"])
        except (ValueError, TypeError, KeyError):
            return CIStatus("malformed", sha, exists_for_sha=True, provider="worker_check")
        if required_command and command != required_command:
            continue
        try:
            execution = json.loads((path.parent / "execution.json").read_text())
            durable_exit_code = int((path.parent / "exit_code").read_text().strip())
            (path.parent / "stderr.log").read_text()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return CIStatus("malformed", sha, exists_for_sha=True, provider="worker_check")
        selection = row.get("selection")
        if (not receipt_has_current_policy(row)
                or row.get("source_dirty") or row.get("source_changed")
                or not isinstance(selection, list) or not selection
                or not all(isinstance(item, str) and item for item in selection)
                or str(Path(log).resolve()) != str(path.parent.resolve())
                or not _completed_supervisor_execution(execution)
                or ("exit_code" in execution and execution.get("exit_code") != exit_code)
                or durable_exit_code != exit_code):
            return CIStatus("malformed", sha, exists_for_sha=True, provider="worker_check")
        failures = [] if exit_code == 0 else [f"validation exited {exit_code}"]
        run_id = path.parents[2].name
        evidence_url = f"/runs/{task_id}/{run_id}" if run_id else log
        return CIStatus("success" if exit_code == 0 else "failure", sha,
                        exists_for_sha=True, evidence_url=evidence_url, failures=failures,
                        provider="worker_check")
    state = "mismatched" if mismatched else "malformed" if malformed else "missing"
    return CIStatus(state, sha, stale=mismatched, provider="worker_check")


COMMAND_STATES = ("success", "failure", "pending", "missing", "unavailable")
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0
_MAX_COMMAND_OUTPUT_BYTES = 64_000
_COMMIT_ID = re.compile(r"[0-9a-f]{7,64}")

_COMMAND_QUERIES: ContextVar[dict[tuple[str, ...], CIStatus] | None] = ContextVar(
    "garden_ci_command_queries", default=None,
)


def command_timeout(value: Any) -> float:
    """The bounded budget for one query: the default when unset, otherwise exactly what was
    configured. An unusable value becomes 0, which no query accepts, so a mistyped budget
    fails closed instead of silently borrowing the default."""
    if value is None:
        return DEFAULT_COMMAND_TIMEOUT_SECONDS
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@contextlib.contextmanager
def tick_query_cache():
    """Ask each configured validation command at most once per commit per scheduler tick.

    A tick is one coherent observation: polling, the review gate and the merge gate all ask
    about the same head, and a build does not become more current by being queried three
    times in one pass. The context boundary keeps the answers out of CLI operations and out
    of the next tick, so the next tick still sees a build that finished meanwhile.
    """
    token = _COMMAND_QUERIES.set({})
    try:
        yield
    finally:
        _COMMAND_QUERIES.reset(token)


def command_status(sha: str, policy: dict[str, Any]) -> CIStatus:
    """Ask a configured controller-side command whether one exact commit passed CI.

    The command is the whole provider boundary. It receives the candidate commit as its
    final argument -- never a branch name -- and prints one JSON object on stdout:

        {"sha": "<the commit it was asked about>",
         "state": "success" | "failure" | "pending" | "missing" | "unavailable",
         "exists_for_sha": true, "stale": false,
         "evidence_url": "<optional>", "failures": ["<optional short reasons>"]}

    A pass must echo the queried commit and state ``exists_for_sha`` and ``stale``
    explicitly, so an answer about another commit, a superseded result or a build that
    never ran for this head can never become this head's evidence. Nonzero exit,
    unparsable or oversized output, an unknown state, a timeout and an unexecutable
    command all fail closed. CI vocabulary, hosts and credentials stay inside the
    operator's own wrapper; only this contract is public.
    """
    argv = shlex.split(str(policy.get("command") or ""))
    timeout = command_timeout(policy.get("timeout_seconds"))
    if not argv or not 0 < timeout <= 3600 or not _COMMIT_ID.fullmatch(sha):
        # A policy that cannot be executed, or a head that is not a commit id, is not an
        # observation: nothing may pass on it.
        return CIStatus("malformed", sha, provider="command")
    key = (sha, *argv)
    cache = _COMMAND_QUERIES.get()
    if cache is not None and key in cache:
        return cache[key]
    status = _command_answer(argv, sha, timeout)
    if cache is not None:
        cache[key] = status
    return status


def _command_answer(argv: list[str], sha: str, timeout: float) -> CIStatus:
    try:
        completed = subprocess.run(
            [*argv, sha], capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return CIStatus("timeout", sha, provider="command")
    except (OSError, UnicodeDecodeError):
        return CIStatus("unavailable", sha, provider="command",
                        failures=["validation command could not be run"])
    if completed.returncode != 0:
        # The command's own diagnostics can carry internal detail; keep the shape only.
        return CIStatus("unavailable", sha, provider="command",
                        failures=[f"validation command exited {completed.returncode}"])
    return _command_result(completed.stdout, sha)


def _command_result(stdout: str, sha: str) -> CIStatus:
    from .deepdives import redact_secrets

    malformed = CIStatus("malformed", sha, provider="command")
    if len(stdout.encode("utf-8", "replace")) > _MAX_COMMAND_OUTPUT_BYTES:
        return malformed
    try:
        row = json.loads(stdout)
    except json.JSONDecodeError:
        return malformed
    if not isinstance(row, dict) or str(row.get("sha") or "") != sha:
        return malformed
    if row.get("state") not in COMMAND_STATES:
        return malformed
    state = str(row["state"])
    facts: dict[str, bool] = {}
    for name in ("stale", "exists_for_sha"):
        value = row.get(name, False)
        if not isinstance(value, bool):
            return malformed
        facts[name] = value
    if state == "success" and not {"stale", "exists_for_sha"} <= set(row):
        # A pass has to bind itself to this commit and claim a current result out loud.
        return malformed
    failures = row.get("failures", [])
    url = row.get("evidence_url", "")
    if (not isinstance(failures, list) or not isinstance(url, str)
            or not all(isinstance(item, str) for item in failures)):
        return malformed
    return CIStatus(state, sha, stale=facts["stale"], exists_for_sha=facts["exists_for_sha"],
                    evidence_url=redact_secrets(url[:500]),
                    failures=[redact_secrets(item[:200]) for item in failures[:5]],
                    provider="command")


_UNAVAILABLE_STATES = frozenset({"unavailable", "malformed", "timeout", "unknown"})
_PROVIDER_LABELS = {"github": "github checks", "worker_check": "worker validation",
                    "command": "the configured validation command"}


def status_disposition(status: CIStatus) -> str:
    """Classify one exact-head answer by the action it calls for.

    ``green`` and ``failure`` are decisions. ``waiting`` is a result still being produced,
    ``absent`` means no result is bound to this head (never ran, superseded or stale), and
    ``unavailable`` means the provider could not answer at all. Only the last two need a
    person: waiting resolves itself, and a failure routes into the ordinary revision path.
    """
    if status.green or status.state == "not_required":
        return "green"
    if status.state in _UNAVAILABLE_STATES:
        return "unavailable"
    if status.stale or not status.exists_for_sha:
        return "absent"
    if status.state == "failure":
        return "failure"
    return "waiting" if status.state == "pending" else "absent"


def status_diagnostic(status: CIStatus) -> str:
    """One actionable sentence for an exact-head answer nobody can act on yet."""
    label = _PROVIDER_LABELS.get(status.provider, f"{status.provider} CI")
    head = status.queried_sha[:12] or "this PR head"
    if status_disposition(status) == "unavailable":
        return (f"{label} could not report a status for {head} ({status.state}); check the "
                "command, its access to CI and its timeout")
    return (f"{label} reports no current result for {head} ({status.state}); validation is "
            "bound to the exact commit, so an earlier or superseded result is not evidence")


def status_reason(status: CIStatus) -> str:
    if status.green:
        return ""
    detail = f" ({', '.join(status.failures)})" if status.failures else ""
    label = "github checks" if status.provider == "github" else f"{status.provider} CI"
    return f"{label} for {status.queried_sha[:12] or 'unknown head'} is {status.state}{detail}"


StatusProvider = Callable[[Path, str, Any, dict[str, Any]], CIStatus]


def _github_provider(_garden_dir: Path, _task_id: str, pr: Any, policy: dict[str, Any]) -> CIStatus:
    return github_status(pr, bool(policy.get("required")))


def _worker_provider(garden_dir: Path, task_id: str, pr: Any, policy: dict[str, Any]) -> CIStatus:
    return worker_check_status(garden_dir, task_id, str(pr.head_sha or ""),
                               dict(policy.get("worker_check") or {}))


def _command_provider(_garden_dir: Path, _task_id: str, pr: Any, policy: dict[str, Any]) -> CIStatus:
    return command_status(str(pr.head_sha or ""), dict(policy.get("command") or {}))


STATUS_PROVIDERS: dict[str, StatusProvider] = {
    "github": _github_provider,
    "worker_check": _worker_provider,
    "command": _command_provider,
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
