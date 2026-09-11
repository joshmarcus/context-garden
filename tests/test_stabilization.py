from __future__ import annotations

import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from garden import gitops
from garden.cli import app
from garden.events import EventLog
from garden.runner.manual import ManualRunner
from garden.scheduler import Scheduler, TickReport
from garden.stabilization import (
    RECOVERY_EXERCISES,
    accept_limitations,
    gate,
    intervene,
    record_outcome,
    render_report,
    sample,
    start,
)
from garden.store import Store


def protected_phase(garden):
    path = garden / "demo" / "p1" / "specs" / "stabilization.md"
    path.write_text("# Stabilization gate\n")
    return Store(garden).phase("demo", "p1")


def record_passes(phase, sha="build-a", *, real_user=False):
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


def test_stale_fixture_evidence_does_not_pass(garden):
    phase = protected_phase(garden)
    start(phase, "old-build")
    record_passes(phase, "old-build", real_user=False)
    ok, missing = gate(phase, build_sha="new-build")
    assert not ok
    assert "evidence is not tied to the current running build" in missing


def test_current_fixture_evidence_can_close_and_release_phase(garden, monkeypatch):
    phase = protected_phase(garden)
    monkeypatch.setattr("garden.stabilization.running_build_sha", lambda: "build-a")
    start(phase, "build-a")
    data_path = phase.path / "docs" / "stabilization-evidence.json"
    data = json.loads(data_path.read_text())
    data["started_at"] = "2026-09-06T00:00:00+00:00"
    data["samples"] = [{"at": "2026-09-06T04:00:00+00:00", "completed_tasks": 10}]
    data_path.write_text(json.dumps(data))
    record_passes(phase, real_user=False)

    assert gate(phase, build_sha="build-a") == (True, [])
    assert "fixture evidence remains valid" in (phase.path / "docs" / "stabilization-evidence.md").read_text()
    assert Scheduler(Store(garden)).close_phase(phase, force=True) == dt.date.today().isoformat()

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
    assert result.exit_code == 0
    assert "demo/p2 unfrozen" in result.output


def test_failed_independent_project_evidence_still_blocks(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    record_passes(phase)
    record_outcome(phase, "independent_project", "FAIL", command="onboard fixture",
                   observed="accepted change failed", artifacts=["artifacts/onboarding.json"],
                   evidence_type="interaction", build_sha="build-a")

    ok, missing = gate(phase, build_sha="build-a")
    assert not ok
    assert "independent_project: FAIL" in missing


def test_fixture_evidence_does_not_bypass_recovery_gate(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    record_passes(phase)
    record_outcome(phase, "recovery_exercises", "FAIL", command="recover fixture",
                   observed="restart exercise failed", artifacts=["artifacts/recovery.json"],
                   evidence_type="interaction", build_sha="build-a")

    ok, missing = gate(phase, build_sha="build-a")
    assert not ok
    assert "recovery_exercises: FAIL" in missing


def test_delegated_repairs_do_not_reset_window_and_remain_visible(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    data_path = phase.path / "docs" / "stabilization-evidence.json"
    data = json.loads(data_path.read_text())
    data["started_at"] = "2026-09-06T00:00:00+00:00"
    data_path.write_text(json.dumps(data))
    events = EventLog(garden / ".garden" / "events.jsonl")
    for i in range(10):
        events.emit("transition", f"DM-{i:03}", phase=phase.key, to="done")
    first = sample(phase, events, at="2026-09-06T04:00:00+00:00")
    assert first["completed_tasks"] == 10 and "memory_available_bytes" in first
    intervene(phase, "repaired a stalled worker", actor="delegated_operator",
              at="2026-09-06T04:30:00+00:00")
    intervene(phase, "requeued a stuck worker", kind="requeue", actor="delegated_operator",
              at="2026-09-06T05:00:00+00:00")
    intervene(phase, "retried failed check", kind="retry", actor="delegated_operator",
              at="2026-09-06T05:30:00+00:00")
    intervene(phase, "merged verified task", kind="mark_done", actor="automated_scheduler",
              at="2026-09-06T06:00:00+00:00")
    data = json.loads((phase.path / "docs" / "stabilization-evidence.json").read_text())
    assert len(data["samples"]) == 1 and len(data["interventions"]) == 4
    assert data["started_at"] != "2026-09-06T05:00:00+00:00"
    record_passes(phase)
    assert gate(phase, build_sha="build-a") == (True, [])
    report = (phase.path / "docs" / "stabilization-evidence.md").read_text()
    assert "delegated operator 3, automated scheduler 1" in report


def test_required_owner_action_resets_window_but_a_status_question_does_not(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    intervene(phase, "asked for status", kind="status_question", actor="human_owner",
              at="2026-09-06T04:00:00+00:00")
    data = json.loads((phase.path / "docs" / "stabilization-evidence.json").read_text())
    assert data["started_at"] != "2026-09-06T04:00:00+00:00"
    intervene(phase, "approved the required repair", actor="human_owner", at="2026-09-06T05:00:00+00:00")
    data = json.loads((phase.path / "docs" / "stabilization-evidence.json").read_text())
    assert data["started_at"] == "2026-09-06T05:00:00+00:00"
    assert data["samples"] == []


def test_unknown_actor_provenance_cannot_manufacture_a_passing_window(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    data_path = phase.path / "docs" / "stabilization-evidence.json"
    data = json.loads(data_path.read_text())
    data["started_at"] = "2026-09-06T00:00:00+00:00"
    data["samples"] = [{"at": "2026-09-06T04:00:00+00:00", "completed_tasks": 10}]
    data_path.write_text(json.dumps(data))
    intervene(phase, "historical retry lacks provenance", kind="retry", at="2026-09-06T02:00:00+00:00")
    record_passes(phase)
    ok, missing = gate(phase, build_sha="build-a")
    assert not ok
    assert "productive_unattended: unknown actor provenance in the candidate window" in missing


def test_event_log_delegated_actions_and_automated_merge_preserve_passing_window(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    data_path = phase.path / "docs" / "stabilization-evidence.json"
    data = json.loads(data_path.read_text())
    data["started_at"] = "2026-09-06T00:00:00+00:00"
    data_path.write_text(json.dumps(data))
    events = EventLog(garden / ".garden" / "events.jsonl")
    for i in range(10):
        events.emit("transition", f"DM-{i:03}", phase=phase.key, to="done", at="2026-09-06T01:00:00+00:00")
    events.emit("retry", "DM-001", phase=phase.key, actor="delegated_operator", reason="retry check", at="2026-09-06T02:00:00+00:00")
    events.emit("operator_repair", "DM-002", phase=phase.key, actor="delegated_operator", reason="repair worker", at="2026-09-06T03:00:00+00:00")
    events.emit("automerged", "DM-003", phase=phase.key, at="2026-09-06T03:30:00+00:00")

    sample(phase, events, at="2026-09-06T04:00:00+00:00")
    record_passes(phase)

    assert gate(phase, build_sha="build-a") == (True, [])
    actions = json.loads(data_path.read_text())["interventions"]
    assert [(a["kind"], a["actor"]) for a in actions] == [
        ("retry", "delegated_operator"),
        ("operator_repair", "delegated_operator"),
        ("mark_done", "automated_scheduler"),
    ]


def test_distinct_same_second_owner_events_are_each_retained_and_reset_once(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    data_path = phase.path / "docs" / "stabilization-evidence.json"
    data = json.loads(data_path.read_text())
    data["started_at"] = "2026-09-06T00:00:00+00:00"
    data["samples"] = [{"at": "2026-09-06T01:00:00+00:00", "completed_tasks": 4}]
    data_path.write_text(json.dumps(data))
    events = EventLog(garden / ".garden" / "events.jsonl")
    at = "2026-09-06T02:00:00+00:00"
    events.emit("retry", "DM-001", phase=phase.key, actor="human_owner", reason="repair first failure", at=at)
    events.emit("retry", "DM-002", phase=phase.key, actor="human_owner", reason="repair second failure", at=at)

    sample(phase, events, at="2026-09-06T02:01:00+00:00")
    data = json.loads(data_path.read_text())
    assert data["started_at"] == at
    assert len(data["samples"]) == 1
    assert data["samples"][0]["at"] == "2026-09-06T02:01:00+00:00"
    assert [(a["reason"], a["source_identity"]) for a in data["interventions"]] == [
        ("repair first failure", "event-log-line:1"),
        ("repair second failure", "event-log-line:2"),
    ]

    sample(phase, events, at="2026-09-06T02:02:00+00:00")
    data = json.loads(data_path.read_text())
    assert len(data["interventions"]) == 2
    assert [row["at"] for row in data["samples"]] == [
        "2026-09-06T02:01:00+00:00", "2026-09-06T02:02:00+00:00",
    ]


def test_served_app_replay_covers_delegated_retry_empty_failure_and_recovery(garden):
    import httpx

    phase = protected_phase(garden)
    start(phase, "build-a")
    data_path = phase.path / "docs" / "stabilization-evidence.json"
    data = json.loads(data_path.read_text())
    data["started_at"] = "2026-09-06T00:00:00+00:00"
    data_path.write_text(json.dumps(data))
    events = EventLog(garden / ".garden" / "events.jsonl")
    for i in range(10):
        events.emit("transition", f"DM-{i:03}", phase=phase.key, to="done", at="2026-09-06T01:00:00+00:00")

    gate_dir = garden.parent / "served-gates"
    gate_dir.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    command = [
        sys.executable, str(Path(__file__).with_name("served_incident_app.py")),
        str(garden), str(port), str(gate_dir),
    ]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/healthz", timeout=0.1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.02)
        else:
            raise AssertionError("disposable served app did not start")
        affected = httpx.post(
            f"{base}/tasks/DM-001/retry", data={"actor": "delegated_operator"},
            headers={"Origin": base}, follow_redirects=False, timeout=5,
        )
        empty = httpx.get(f"{base}/inbox", timeout=5)
        failed = httpx.post(
            f"{base}/tasks/DM-404/retry", data={"actor": "delegated_operator"},
            headers={"Origin": base}, follow_redirects=False, timeout=5,
        )
        recovery = httpx.post(
            f"{base}/tasks/DM-001/retry", data={"actor": "delegated_operator"},
            headers={"Origin": base}, follow_redirects=False, timeout=5,
        )
        assert affected.status_code == 303
        assert empty.status_code == 200 and "Inbox zero" in empty.text
        assert failed.status_code == 404
        assert recovery.status_code == 303
    finally:
        process.terminate()
        process.wait(timeout=5)
    sample(phase, events, at="2026-09-06T04:00:00+00:00")
    record_passes(phase)

    assert gate(phase, build_sha="build-a") == (True, [])
    actions = json.loads(data_path.read_text())["interventions"]
    assert [(a["kind"], a["actor"]) for a in actions] == [
        ("retry", "delegated_operator"),
        ("retry", "delegated_operator"),
    ]
    # This test executed the current checkout above. The committed replay artifact is a
    # historical record bound to its own source_commit and component hashes; comparing those
    # hashes with a later checkout makes unrelated changes invalidate this behavior regression.


def test_unknown_nonoperative_event_log_entries_do_not_block_a_passing_window(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    data_path = phase.path / "docs" / "stabilization-evidence.json"
    data = json.loads(data_path.read_text())
    data["started_at"] = "2026-09-06T00:00:00+00:00"
    data_path.write_text(json.dumps(data))
    events = EventLog(garden / ".garden" / "events.jsonl")
    for i in range(10):
        events.emit("transition", f"DM-{i:03}", phase=phase.key, to="done", at="2026-09-06T01:00:00+00:00")
    events.emit("status_question", "DM-001", phase=phase.key, at="2026-09-06T02:00:00+00:00")
    events.emit("conversation", "DM-001", phase=phase.key, at="2026-09-06T03:00:00+00:00")

    sample(phase, events, at="2026-09-06T04:00:00+00:00")
    record_passes(phase)

    assert gate(phase, build_sha="build-a") == (True, [])


def test_intervention_cli_explains_no_owner_action_semantics():
    result = CliRunner().invoke(app, ["stabilization", "intervene", "--help"])
    assert result.exit_code == 0
    assert "human-owner repair resets" in result.output
    assert "delegated_operator" in result.output


def test_sample_excludes_a_completed_merged_external_pr_from_unattended_work(sched, fake_github, monkeypatch):
    """An operator's accepted PR must not make a supervised soak look productive."""
    phase = protected_phase(sched.store.root)
    start(phase, "build-a")
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/fix", "main", "external", "")
    pr.state, pr.head_sha, pr.head_repo, pr.merge_commit_sha = (
        "MERGED", "verified-head", "test/demo", "verified-merge",
    )
    monkeypatch.setattr(gitops, "fetch", lambda _: True)
    monkeypatch.setattr(gitops, "is_ancestor", lambda *_: True)
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    sched.finish_manual(task, {"status": "done", "summary": "implemented", "pr": pr.url})

    row = sample(phase, sched.events, at="2026-09-06T04:00:00+00:00")
    assert task.status.value == "done"
    assert row["completed_tasks"] == 0


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


def test_acceptance_is_append_only_and_does_not_rewrite_measured_evidence(garden):
    phase = protected_phase(garden)
    start(phase, "build-a")
    evidence_path = phase.path / "docs" / "stabilization-evidence.json"
    report_path = phase.path / "docs" / "stabilization-evidence.md"
    original_evidence = evidence_path.read_bytes()
    original_report = report_path.read_bytes()

    first = accept_limitations(
        phase, "build-a", actor_type="delegated_operator", actor="Alex Operator",
        authority="owner delegated phase-closing decisions", rationale="accepted known gaps",
        sources=["question:phase-close-q0", "https://example.test/review/5"],
        at="2026-09-11T10:11:15+00:00",
    )
    accept_limitations(
        phase, "build-a", actor_type="human_owner", actor="Product Owner",
        authority="product owner", rationale="confirmed the scoped acceptance",
        sources=["owner-message:2026-09-11"], at="2026-09-11T10:12:00+00:00",
    )

    assert evidence_path.read_bytes() == original_evidence
    assert report_path.read_bytes() == original_report
    decisions = json.loads((phase.path / "docs" / "stabilization-acceptance.json").read_text())
    assert decisions[0] == first and len(decisions) == 2
    assert gate(phase, build_sha="build-a") == (True, [])
    report = render_report(phase, build_sha="build-a")
    assert "**Overall: ACCEPTED WITH LIMITATIONS**" in report
    assert "Disposition: accepted with limitations by human_owner `Product Owner`" in report
    assert "independent_project: UNPROVEN" in report
    assert "### Independent Project — UNPROVEN" in report


def test_same_second_acceptance_change_requeues_closing_review(garden, monkeypatch):
    phase = protected_phase(garden)
    monkeypatch.setattr("garden.stabilization.running_build_sha", lambda: "build-a")
    scheduler = Scheduler(Store(garden))
    scheduler.cfg.data["retro"]["auto_start"] = True
    for task in phase.tasks:
        task.status = task.status.CANCELLED
        scheduler.store.save(task)
    scheduler.store.invalidate_tasks()
    phase = scheduler.store.phase("demo", "p1")
    decision_at = "2026-09-11T10:11:15+00:00"
    accept_limitations(
        phase, "build-a", actor_type="delegated_operator", actor="Alex",
        authority="owner delegation", rationale="first accepted scope", sources=["decision:q0"],
        at=decision_at,
    )
    first_identity = scheduler._closing_review_policy(phase)["evidence"]
    entry = {
        "phase": phase.key, "product": phase.product, "phase_name": phase.name,
        "personas": ["designer"], "skip_personas": False, "next_phase": "p2",
        "self_product": "demo", "stage": "queued", "persona_runs": {},
        "automatic": True, "request_id": "same-second-acceptance",
        "evidence": first_identity, "source": "", "no_file": False,
    }
    scheduler._retro_list().append(entry)
    scheduler.state.save()

    accept_limitations(
        phase, "build-a", actor_type="delegated_operator", actor="Alex",
        authority="owner delegation", rationale="superseding accepted scope",
        sources=["decision:q1"], at=decision_at,
    )
    second_identity = scheduler._closing_review_policy(phase)["evidence"]
    assert second_identity != first_identity

    scheduler.dispatch_queued_closing_reviews(TickReport())
    scheduler.prepare_claimed_closing_reviews(TickReport())

    assert entry["stage"] == "queued"
    assert entry["evidence"] == second_identity
    assert "re-preparing" in entry["waiting_reason"]


@pytest.mark.parametrize("actor_type", ["automated_scheduler", "unknown", "operator"])
def test_nonhuman_actor_types_cannot_record_acceptance(garden, actor_type):
    phase = protected_phase(garden)
    with pytest.raises(ValueError, match="human_owner or delegated_operator"):
        accept_limitations(
            phase, "build-a", actor_type=actor_type, actor="worker", authority="task result",
            rationale="claimed pass", sources=["worker-output"],
        )
    assert not (phase.path / "docs" / "stabilization-acceptance.json").exists()


def test_only_valid_exact_phase_and_build_acceptance_opens_gate(garden):
    phase = protected_phase(garden)
    accept_limitations(
        phase, "build-a", actor_type="delegated_operator", actor="Alex",
        authority="delegation record", rationale="known limitations accepted", sources=["decision:q0"],
        at="2026-09-11T10:11:15+00:00",
    )
    assert gate(phase, build_sha="build-a") == (True, [])
    assert gate(phase, build_sha="build-b")[0] is False

    path = phase.path / "docs" / "stabilization-acceptance.json"
    rows = json.loads(path.read_text())
    rows[0]["phase"] = "demo/p2"
    path.write_text(json.dumps(rows))
    assert gate(phase, build_sha="build-a")[0] is False
    rows[0]["phase"] = phase.key
    rows[0]["authority"] = ""
    path.write_text(json.dumps(rows))
    assert gate(phase, build_sha="build-a")[0] is False


def test_acceptance_cli_requires_explicit_provenance_and_native_close_keeps_task_gate(garden, monkeypatch):
    phase = protected_phase(garden)
    monkeypatch.setattr("garden.stabilization.running_build_sha", lambda: "build-a")
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        result = CliRunner().invoke(app, [
            "stabilization", "accept-limitations", "demo/p1", "--build-sha", "build-a",
            "--actor-type", "delegated_operator", "--actor", "Alex Operator",
            "--authority", "owner delegation", "--rationale", "reviewed limitations",
            "--source", "question:q0",
        ])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output
    scheduler = Scheduler(Store(garden))
    with pytest.raises(RuntimeError, match="open task"):
        scheduler.close_phase(phase)

    for task in phase.tasks:
        scheduler.mark_done(task)
    scheduler.store.invalidate_tasks()
    phase = scheduler.store.phase("demo", "p1")
    scheduler.state.get("_retro_verdicts")[phase.key] = {
        "phase": phase.key, "verdict": "reopen", "status": "pending", "blocking_ids": [],
    }
    scheduler.state.save()
    rec = scheduler.retro_decide(phase, "close_with_followups", by="cli")
    assert rec["status"] == "accepted"
    assert scheduler.store.phase("demo", "p1").closed
