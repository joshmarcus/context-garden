from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from garden.cli import app
from garden.events import EventLog
from garden.scheduler import Scheduler
from garden.stabilization import RECOVERY_EXERCISES, gate, intervene, record_outcome, sample, start
from garden.store import Store


def protected_phase(garden):
    path = garden / "demo" / "p1" / "specs" / "stabilization.md"
    path.write_text("# Stabilization gate\n")
    return Store(garden).phase("demo", "p1")


def record_passes(phase, sha="build-a", *, real_user=True):
    for name in ("independent_project", "recovery_exercises", "application_journey", "resources_and_cost"):
        record_outcome(phase, name, "PASS", command=f"run {name}", observed="worked",
                       artifacts=[f"artifacts/{name}.json"], evidence_type="interaction" if name != "resources_and_cost" else "automated",
                       build_sha=sha, real_user=real_user if name == "independent_project" else False,
                       exercises=sorted(RECOVERY_EXERCISES) if name == "recovery_exercises" else [],
                       fixture_isolated=name == "recovery_exercises")
    record_outcome(phase, "productive_unattended", "PASS", command="timer samples",
                   observed="ten useful tasks completed", artifacts=["artifacts/soak.json"],
                   evidence_type="automated", build_sha=sha)


def test_missing_evidence_is_unproven_and_force_cannot_bypass(garden, monkeypatch):
    phase = protected_phase(garden)
    monkeypatch.setattr("garden.stabilization.running_build_sha", lambda: "build-a")
    ok, missing = gate(phase)
    assert not ok and "independent_project: UNPROVEN" in missing
    with pytest.raises(RuntimeError, match="stabilization is UNPROVEN"):
        Scheduler(Store(garden)).close_phase(phase, force=True)


def test_unfreeze_next_phase_cannot_bypass_unproven_stabilization(garden):
    protected_phase(garden)
    goals = garden / "demo" / "p2" / "goals.md"
    goals.parent.mkdir(parents=True)
    goals.write_text("---\nfrozen: '2026-09-06'\n---\n\n# p2\n")
    import os
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        result = CliRunner().invoke(app, ["unfreeze", "demo/p2"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 1
    assert "stabilization is UNPROVEN; cannot release demo/p2" in result.output
    assert Store(garden).phase("demo", "p2").frozen


def test_stale_and_fixture_only_evidence_do_not_pass(garden):
    phase = protected_phase(garden)
    start(phase, "old-build")
    record_passes(phase, "old-build", real_user=False)
    ok, missing = gate(phase, build_sha="new-build")
    assert not ok
    assert "evidence is not tied to the current running build" in missing
    assert any("real-user evidence is absent" in item for item in missing)


def test_recorder_counts_repairs_resets_window_and_measures_resources(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    events = EventLog(garden / ".garden" / "events.jsonl")
    for i in range(10):
        events.emit("transition", f"DM-{i:03}", phase=phase.key, to="done")
    first = sample(phase, events, at="2026-09-06T04:00:00+00:00")
    assert first["completed_tasks"] == 10 and "memory_available_bytes" in first
    intervene(phase, "requeued a stuck worker", at="2026-09-06T05:00:00+00:00")
    data = json.loads((phase.path / "docs" / "stabilization-evidence.json").read_text())
    assert data["samples"] == [] and len(data["interventions"]) == 1
    assert data["started_at"] == "2026-09-06T05:00:00+00:00"


def test_sample_detects_operator_action_event_and_resets_automatically(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    events = EventLog(garden / ".garden" / "events.jsonl")
    repair = events.emit("retry", "DM-001", phase=phase.key, reason="unstick worker")
    sample(phase, events)
    data = json.loads((phase.path / "docs" / "stabilization-evidence.json").read_text())
    assert data["started_at"] == repair["at"]
    assert data["interventions"] == [{"at": repair["at"], "kind": "retry", "reason": "unstick worker"}]


def test_complete_current_build_report_passes_and_cites_evidence(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    data_path = phase.path / "docs" / "stabilization-evidence.json"
    data = json.loads(data_path.read_text())
    data["started_at"] = "2026-09-06T00:00:00+00:00"
    data["samples"] = [{"at": "2026-09-06T04:00:00+00:00", "completed_tasks": 10}]
    data_path.write_text(json.dumps(data))
    record_passes(phase)
    ok, missing = gate(phase, build_sha="build-a")
    assert ok, missing
    report = (phase.path / "docs" / "stabilization-evidence.md").read_text()
    assert "**Overall: PASS**" in report
    assert "Command: `run application_journey`" in report
    assert "Evidence type: interaction" in report
    assert "`artifacts/application_journey.json`" in report
    assert "## Unverified requirements\n\n- None." in report
