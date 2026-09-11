"""Mechanical stabilization evidence and productive-soak recording.

The recorder is deliberately token-free: an operator can call ``sample`` from cron or a
service timer while the normal scheduler runs.  The phase report is the durable, readable
artifact; its JSON sidecar retains the observations used to render it.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .events import EventLog
from .model import Phase, now_iso
from .system_resources import memory_bytes, swap_used_bytes
from .upgrade import installed_commit

OUTCOMES = (
    "independent_project",
    "productive_unattended",
    "recovery_exercises",
    "application_journey",
    "resources_and_cost",
)
RECOVERY_EXERCISES = frozenset({
    "worker_interruption", "failed_checks", "no_change_justified",
    "no_change_ignoring_feedback", "quota_environment", "changed_pr_head", "restart",
})
INTERVENTION_KINDS = frozenset({
    "operator_repair", "requeue", "retry", "set_status", "redispatch", "mark_done",
})
NON_OPERATIVE_KINDS = frozenset({"status_question", "conversation"})
ACTION_KINDS = INTERVENTION_KINDS | NON_OPERATIVE_KINDS
ACTORS = frozenset({"human_owner", "delegated_operator", "automated_scheduler", "unknown"})
ACCEPTANCE_ACTORS = frozenset({"human_owner", "delegated_operator"})

# ``automerged`` is emitted only after Scheduler has completed the merge.  It is therefore
# authoritative scheduler provenance even in an older event row that predates ``actor``.
_EVENT_ACTION_KINDS = {**{kind: kind for kind in ACTION_KINDS}, "automerged": "mark_done"}


def running_build_sha() -> str:
    if sha := installed_commit():
        return sha
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def evidence_paths(phase: Phase) -> tuple[Path, Path]:
    return phase.path / "docs" / "stabilization-evidence.json", phase.path / "docs" / "stabilization-evidence.md"


def acceptance_path(phase: Phase) -> Path:
    """Return the append-only operator decision ledger, separate from measured evidence."""
    return phase.path / "docs" / "stabilization-acceptance.json"


def load_acceptances(phase: Phase) -> list[dict[str, Any]]:
    path = acceptance_path(phase)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def accept_limitations(
    phase: Phase,
    build_sha: str,
    *,
    actor_type: str,
    actor: str,
    authority: str,
    rationale: str,
    sources: list[str],
    at: str | None = None,
) -> dict[str, Any]:
    """Append an accountable acceptance without modifying the measurements it assesses."""
    if actor_type not in ACCEPTANCE_ACTORS:
        raise ValueError("acceptance actor type must be human_owner or delegated_operator")
    text_fields = {"build SHA": build_sha, "actor": actor, "authority": authority, "rationale": rationale}
    missing = [name for name, value in text_fields.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise ValueError("acceptance requires nonempty " + ", ".join(missing))
    if not sources or not all(isinstance(source, str) and source.strip() for source in sources):
        raise ValueError("acceptance requires one or more nonempty evidence/source references")
    recorded_at = at or now_iso()
    try:
        timestamp = dt.datetime.fromisoformat(recorded_at)
    except (TypeError, ValueError):
        raise ValueError("acceptance timestamp must be ISO 8601") from None
    if timestamp.tzinfo is None:
        raise ValueError("acceptance timestamp must include a timezone")
    row = {
        "phase": phase.key,
        "build_sha": build_sha.strip(),
        "disposition": "accepted_with_limitations",
        "actor_type": actor_type,
        "actor": actor.strip(),
        "authority": authority.strip(),
        "rationale": rationale.strip(),
        "sources": [source.strip() for source in sources],
        "at": recorded_at,
    }
    path = acceptance_path(phase)
    decisions = load_acceptances(phase)
    if path.exists() and not decisions:
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing != []:
            raise ValueError("existing stabilization acceptance ledger is malformed; refusing to overwrite it")
    decisions.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decisions, indent=2, sort_keys=True) + "\n")
    return row


def matching_acceptance(phase: Phase, build_sha: str) -> dict[str, Any] | None:
    """Return the newest structurally valid acceptance for this exact phase and build."""
    for row in reversed(load_acceptances(phase)):
        if _valid_acceptance(row, phase.key, build_sha):
            return row
    return None


def _valid_acceptance(row: Any, phase_key: str, build_sha: str) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("phase") != phase_key or row.get("build_sha") != build_sha:
        return False
    if row.get("disposition") != "accepted_with_limitations" or row.get("actor_type") not in ACCEPTANCE_ACTORS:
        return False
    if not all(isinstance(row.get(field), str) and row[field].strip()
               for field in ("actor", "authority", "rationale", "at")):
        return False
    sources = row.get("sources")
    if not isinstance(sources, list) or not sources or not all(isinstance(item, str) and item.strip() for item in sources):
        return False
    try:
        return dt.datetime.fromisoformat(row["at"]).tzinfo is not None
    except (TypeError, ValueError):
        return False


def load_evidence(phase: Phase) -> dict[str, Any]:
    path, _ = evidence_paths(phase)
    if not path.exists():
        return {"phase": phase.key, "build_sha": "", "started_at": "", "samples": [], "interventions": [], "outcomes": {}}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"phase": phase.key, "invalid": True, "samples": [], "interventions": [], "outcomes": {}}
    return data if isinstance(data, dict) else {"phase": phase.key, "invalid": True, "samples": [], "interventions": [], "outcomes": {}}


def start(phase: Phase, build_sha: str | None = None) -> dict[str, Any]:
    sha = build_sha or running_build_sha()
    if not sha:
        raise RuntimeError("cannot identify the running build; supply --build-sha")
    data = {"phase": phase.key, "build_sha": sha, "started_at": now_iso(), "samples": [], "interventions": [], "outcomes": {}}
    _save(phase, data)
    return data


def sample(phase: Phase, events: EventLog, at: str | None = None) -> dict[str, Any]:
    data = load_evidence(phase)
    if not data.get("started_at"):
        raise RuntimeError(f"no stabilization recording for {phase.key}; start it first")
    at = at or now_iso()
    since = str(data["started_at"])
    phase_events = [e for e in events.read(since=since, with_line_number=True) if e.get("phase") == phase.key or
                    any(t.id == e.get("task") for t in phase.tasks)]
    phase_task_ids = {task.id for task in phase.tasks}
    actions = [action for event in phase_events if (action := _action_from_event(event))]
    known = {_action_identity(i) for i in data.get("interventions", [])}
    new_actions = [a for a in actions if _action_identity(a) not in known]
    owner_actions = [a for a in new_actions if a["operative"] and a["actor"] == "human_owner"]
    if new_actions:
        data.setdefault("interventions", []).extend(new_actions)
    if owner_actions:
        data["started_at"] = str(owner_actions[-1]["at"] or at)
        data["samples"] = []
        since = str(data["started_at"])
        phase_events = [e for e in phase_events if str(e.get("at") or "") >= since]
    completion_events = [
        event for event in events.read(kinds=["run_finished"])
        if event.get("task") in phase_task_ids
    ]
    completed = _unattended_completed_tasks(phase_events, completion_events)
    mem = _memory()
    disk = shutil.disk_usage(phase.path)
    row = {"at": at, "completed_tasks": completed, "memory_available_bytes": mem[0],
           "swap_used_bytes": mem[1], "temp_free_bytes": disk.free,
           "workers": sum(e.get("kind") == "dispatch" for e in phase_events),
           "checks": sum(e.get("mode") == "check" and e.get("kind") == "dispatch" for e in phase_events),
           "reviews": sum(e.get("kind") == "review" for e in phase_events)}
    data.setdefault("samples", []).append(row)
    _save(phase, data)
    return row


def intervene(phase: Phase, reason: str, *, kind: str = "operator_repair", actor: str = "unknown",
              at: str | None = None) -> None:
    if kind not in ACTION_KINDS:
        raise ValueError("unknown stabilization action kind")
    if actor not in ACTORS:
        raise ValueError("unknown stabilization actor")
    data = load_evidence(phase)
    recorded_at = at or now_iso()
    data.setdefault("interventions", []).append(_action_row(
        at=recorded_at, kind=kind, actor=actor, reason=reason,
    ))
    if kind in INTERVENTION_KINDS and actor == "human_owner":
        data["started_at"] = recorded_at
        data["samples"] = []
    _save(phase, data)


def record_outcome(phase: Phase, name: str, status: str, *, command: str, observed: str,
                   artifacts: list[str], evidence_type: str, build_sha: str | None = None,
                   real_user: bool = False, exercises: list[str] | None = None,
                   fixture_isolated: bool = False) -> None:
    if name not in OUTCOMES or status not in {"PASS", "FAIL", "UNPROVEN"}:
        raise ValueError("unknown outcome or status")
    if evidence_type not in {"automated", "interaction"}:
        raise ValueError("evidence_type must be automated or interaction")
    data = load_evidence(phase)
    data.setdefault("outcomes", {})[name] = {
        "status": status, "command": command, "observed": observed, "artifacts": artifacts,
        "evidence_type": evidence_type, "build_sha": build_sha or data.get("build_sha", ""),
        "real_user": real_user, "exercises": exercises or [], "at": now_iso(),
        "fixture_isolated": fixture_isolated,
    }
    _save(phase, data)


def gate(phase: Phase, *, build_sha: str | None = None) -> tuple[bool, list[str]]:
    if not (phase.path / "specs" / "stabilization.md").exists():
        return True, []
    data = load_evidence(phase)
    current = build_sha or running_build_sha()
    missing = _measured_missing(data, current)
    if not missing:
        return True, []
    if current and matching_acceptance(phase, current):
        return True, []
    return False, missing


def _measured_missing(data: dict[str, Any], current: str) -> list[str]:
    missing: list[str] = []
    if data.get("invalid"):
        return ["evidence JSON is invalid"]
    if not current or data.get("build_sha") != current:
        missing.append("evidence is not tied to the current running build")
    outcomes = data.get("outcomes") or {}
    for name in OUTCOMES:
        row = outcomes.get(name) or {}
        if row.get("status") != "PASS":
            missing.append(f"{name}: {row.get('status') or 'UNPROVEN'}")
        if not row.get("command") or not row.get("observed") or not row.get("artifacts"):
            missing.append(f"{name}: commands, observed result and artifact paths are required")
        if row.get("build_sha") != current:
            missing.append(f"{name}: stale or unknown build evidence")
    recovery = outcomes.get("recovery_exercises") or {}
    absent = sorted(RECOVERY_EXERCISES - set(recovery.get("exercises") or []))
    if absent:
        missing.append("recovery_exercises: missing " + ", ".join(absent))
    if not recovery.get("fixture_isolated"):
        missing.append("recovery_exercises: disposable fixture isolation is not established")
    journey = outcomes.get("application_journey") or {}
    if journey.get("evidence_type") != "interaction":
        missing.append("application_journey: actual interaction evidence is required")
    _check_soak(data, outcomes.get("productive_unattended") or {}, missing)
    return missing


def render_report(phase: Phase, *, build_sha: str | None = None) -> str:
    data = load_evidence(phase)
    current = build_sha or running_build_sha()
    acceptance = matching_acceptance(phase, current) if current else None
    ok, _ = gate(phase, build_sha=current)
    measured_missing = _measured_missing(data, current)
    overall = "ACCEPTED WITH LIMITATIONS" if acceptance else ("PASS" if ok else "UNPROVEN")
    lines = [f"# Stabilization evidence — {phase.key}", "", f"**Overall: {overall}**",
             "", f"Build: `{data.get('build_sha') or 'unknown'}`", "", "## Outcomes", ""]
    for name in OUTCOMES:
        row = (data.get("outcomes") or {}).get(name) or {}
        lines += [f"### {name.replace('_', ' ').title()} — {row.get('status') or 'UNPROVEN'}", "",
                  f"Evidence type: {row.get('evidence_type') or 'unverified'}", f"Command: `{row.get('command') or 'not recorded'}`",
                  f"Observed: {row.get('observed') or 'not recorded'}", "Artifacts: " + (", ".join(f"`{p}`" for p in row.get("artifacts", [])) or "none"), ""]
        if name == "independent_project":
            lines[-1:-1] = [
                "Real-user provenance: " + ("recorded" if row.get("real_user") else "not recorded (fixture evidence remains valid)"),
                "",
            ]
    if acceptance:
        lines += ["## Operator acceptance", "",
                  f"Disposition: accepted with limitations by {acceptance['actor_type']} `{acceptance['actor']}`",
                  f"Authority: {acceptance['authority']}", f"Rationale: {acceptance['rationale']}",
                  "Sources: " + ", ".join(f"`{source}`" for source in acceptance["sources"]),
                  f"Accepted at: {acceptance['at']}", f"Accepted build: `{acceptance['build_sha']}`", ""]
    lines += ["## Unverified requirements", ""] + ([f"- {item}" for item in measured_missing] if measured_missing else ["- None."])
    actions = data.get("interventions") or []
    counts = {actor: sum(a.get("actor", "unknown") == actor for a in actions) for actor in ACTORS}
    lines += ["", "## No-owner-action window", "",
              "Only required human-owner unblock or repair actions restart this window. Delegated operator and automated scheduler actions remain recorded for reliability and cost accounting. Unknown actor provenance leaves the window unproven.",
              "", f"Recorder samples: {len(data.get('samples') or [])}; actions: {len(actions)} (human owner {counts['human_owner']}, delegated operator {counts['delegated_operator']}, automated scheduler {counts['automated_scheduler']}, unknown {counts['unknown']})", ""]
    return "\n".join(lines)


def _check_soak(data: dict[str, Any], row: dict[str, Any], missing: list[str]) -> None:
    samples = data.get("samples") or []
    try:
        start_at = dt.datetime.fromisoformat(str(data.get("started_at")))
        end_at = dt.datetime.fromisoformat(str(samples[-1]["at"]))
        hours = (end_at - start_at).total_seconds() / 3600
    except (ValueError, TypeError, KeyError, IndexError):
        hours = 0
    completed = max((int(s.get("completed_tasks", 0)) for s in samples), default=0)
    if hours < 4 or completed < 10 or row.get("status") != "PASS":
        missing.append(f"productive_unattended: need 4 consecutive hours and 10 completed tasks without a required owner action (recorded {hours:.2f}h, {completed} tasks; {len(data.get('interventions') or [])} actions recorded)")
    start_at = str(data.get("started_at") or "")
    unknown = [a for a in data.get("interventions") or []
               if a.get("operative", a.get("kind") in INTERVENTION_KINDS)
               and a.get("actor", "unknown") == "unknown" and str(a.get("at") or "") >= start_at]
    if unknown:
        missing.append("productive_unattended: unknown actor provenance in the candidate window")


def _action_row(*, at: str, kind: str, actor: str, reason: str,
                source_identity: str = "") -> dict[str, str | bool]:
    row: dict[str, str | bool] = {
        "at": at, "kind": kind, "actor": actor, "reason": reason,
        "operative": kind in INTERVENTION_KINDS,
    }
    if source_identity:
        row["source_identity"] = source_identity
    return row


def _action_identity(action: dict[str, Any]) -> tuple[object, ...]:
    """Return the durable source identity, retaining a safe fallback for old evidence."""
    if source_identity := action.get("source_identity"):
        return ("event", source_identity)
    return ("legacy", action.get("at"), action.get("kind"), action.get("actor") or "unknown")


def _unattended_completed_tasks(
    events: list[dict[str, Any]], completion_events: list[dict[str, Any]],
) -> int:
    """Count completed tasks whose accepted run was not supervised external work.

    ``transition`` records the task outcome, while ``run_finished`` records who performed
    the accepted work.  Both are needed: an external merge follows the normal transition
    path, but its linked successful run explicitly says it was supervised.
    """
    supervised = {
        str(event.get("task"))
        for event in completion_events
        if event.get("kind") == "run_finished"
        and event.get("status") == "done"
        and event.get("external")
        and event.get("supervised")
    }
    completed = {
        str(event.get("task"))
        for event in events
        if event.get("kind") == "transition"
        and event.get("to") == "done"
        and str(event.get("task")) not in supervised
    }
    return len(completed)


def _action_from_event(event: dict[str, Any]) -> dict[str, str | bool] | None:
    """Normalize a relevant event-log row without inventing human provenance."""
    source_kind = str(event.get("kind") or "")
    kind = _EVENT_ACTION_KINDS.get(source_kind)
    if kind is None:
        return None
    actor = str(event.get("actor") or "")
    if not actor and source_kind == "automerged":
        actor = "automated_scheduler"
    if actor not in ACTORS:
        actor = "unknown"
    return _action_row(
        at=str(event.get("at") or ""), kind=kind, actor=actor,
        reason=str(event.get("reason") or event.get("note") or "recorded action"),
        source_identity=f"event-log-line:{event.get('_line_number')}" if event.get("_line_number") else "",
    )


def _memory() -> tuple[int, int]:
    available, _total = memory_bytes()
    return available or 0, swap_used_bytes() or 0


def _save(phase: Phase, data: dict[str, Any]) -> None:
    path, report = evidence_paths(phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    report.write_text(render_report(phase, build_sha=str(data.get("build_sha") or "")))
