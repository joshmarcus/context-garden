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


def running_build_sha() -> str:
    if sha := installed_commit():
        return sha
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def evidence_paths(phase: Phase) -> tuple[Path, Path]:
    return phase.path / "docs" / "stabilization-evidence.json", phase.path / "docs" / "stabilization-evidence.md"


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
    phase_events = [e for e in events.read(since=since) if e.get("phase") == phase.key or
                    any(t.id == e.get("task") for t in phase.tasks)]
    repairs = [e for e in phase_events if e.get("kind") in INTERVENTION_KINDS]
    known = {(i.get("at"), i.get("kind")) for i in data.get("interventions", [])}
    new_repairs = [e for e in repairs if (e.get("at"), e.get("kind")) not in known]
    if new_repairs:
        for event in new_repairs:
            data.setdefault("interventions", []).append({"at": event.get("at"), "kind": event.get("kind"),
                                                          "reason": event.get("reason") or event.get("note") or "recorded operator action"})
        data["started_at"] = str(new_repairs[-1].get("at") or at)
        data["samples"] = []
        since = str(data["started_at"])
        phase_events = [e for e in phase_events if str(e.get("at") or "") >= since]
    completed = len({e.get("task") for e in phase_events if e.get("kind") == "transition" and e.get("to") == "done"})
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


def intervene(phase: Phase, reason: str, *, kind: str = "operator_repair", at: str | None = None) -> None:
    data = load_evidence(phase)
    data.setdefault("interventions", []).append({"at": at or now_iso(), "kind": kind, "reason": reason})
    # An operator repair starts a new candidate unattended window, while preserving its count.
    data["started_at"] = at or now_iso()
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
    missing: list[str] = []
    if data.get("invalid"):
        return False, ["evidence JSON is invalid"]
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
    return not missing, missing


def render_report(phase: Phase, *, build_sha: str | None = None) -> str:
    data = load_evidence(phase)
    ok, missing = gate(phase, build_sha=build_sha)
    lines = [f"# Stabilization evidence — {phase.key}", "", f"**Overall: {'PASS' if ok else 'UNPROVEN'}**",
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
    lines += ["## Unverified requirements", ""] + ([f"- {item}" for item in missing] if missing else ["- None."])
    lines += ["", f"Recorder samples: {len(data.get('samples') or [])}; operator interventions: {len(data.get('interventions') or [])}", ""]
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
        missing.append(f"productive_unattended: need 4 consecutive hours and 10 completed tasks since the last repair (recorded {hours:.2f}h, {completed} tasks; {len(data.get('interventions') or [])} total interventions counted)")


def _memory() -> tuple[int, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return values.get("MemAvailable", 0), values.get("SwapTotal", 0) - values.get("SwapFree", 0)


def _save(phase: Phase, data: dict[str, Any]) -> None:
    path, report = evidence_paths(phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    report.write_text(render_report(phase, build_sha=str(data.get("build_sha") or "")))
