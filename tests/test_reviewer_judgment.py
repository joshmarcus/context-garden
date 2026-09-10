from __future__ import annotations

import json

from garden import gitops
from garden.model import Status
from garden.preflight import mechanical_results, preflight_section
from garden.review import (
    enforce_criteria_verdict,
    feedback_from_review,
    interaction_evidence_gaps,
    interaction_requirement,
    review_brief,
)
from garden.scheduler import TickReport
from garden.store import Store
from tests.reference_context import agent_context


def test_paths_keywords_and_legacy_metadata_do_not_prescribe_evidence():
    for changed, context in [
        (["src/garden/scheduler/review.py"], "lifecycle recovery"),
        (["src/garden/web/pages/task.py"], "material UI change"),
        (["src/garden/costs.py"], "p95 performance after cache expiry"),
        (["src/garden/run_supervisor.py"], "interaction_evidence: required"),
    ]:
        required, scalability, reason = interaction_requirement(changed, context)
        assert (required, scalability) == (False, False)
        assert "reviewer chooses" in reason


def test_briefs_invite_proportionate_attestation_without_a_required_schema(garden):
    store = Store(garden)
    text = review_brief(
        store, store.task("DM-001"), branch="b", base="main", pr_title="T", pr_body="",
        diff="+x", max_diff_chars=1000,
    )
    assert "clear honest attestation" in text
    assert '"attestation"' in text
    assert "Only `verdict` is mechanically required" in text
    assert "generic replay" in text
    assert "Running-application interaction required" not in text
    assert "Optional review pre-flight" in preflight_section()


def test_review_judgment_accepts_attestation_and_keeps_real_failures_blocking():
    attested = enforce_criteria_verdict({
        "verdict": "approve", "summary": "Source inspected", "attestation": "I ran parser tests.",
        "criteria": [{"criterion": "Parser handles empty input", "met": True}], "findings": [],
    })
    assert attested["verdict"] == "approve"

    unmet = enforce_criteria_verdict({
        "verdict": "approve", "criteria": [{"criterion": "Parser handles empty input", "met": False}],
        "findings": [],
    })
    assert unmet["verdict"] == "request_changes"
    assert unmet["findings"][-1]["severity"] == "blocking"

    defect = enforce_criteria_verdict({
        "verdict": "approve", "criteria": [],
        "findings": [{"severity": "blocking", "summary": "Empty input crashes"}],
    })
    assert defect["verdict"] == "request_changes"


def test_description_feedback_is_advisory_without_overriding_native_verdict():
    review = enforce_criteria_verdict({
        "verdict": "request_changes", "summary": "The parser still drops empty records.",
        "criteria": [], "findings": [],
        "description_ok": False, "description_feedback": "Prefer a shorter heading.",
    })
    assert review["verdict"] == "request_changes"
    assert review["description_ok"] is True
    assert review["description_advisory"] == "Prefer a shorter heading."
    assert review["findings"][-1]["severity"] == "nit"
    assert "parser still drops empty records" in feedback_from_review(review).lower()

    attestation_only = {
        "verdict": "request_changes",
        "attestation": "The parser drops empty rows during the focused exercise.",
    }
    assert "parser drops empty rows" in feedback_from_review(attestation_only).lower()
    assert "parser drops empty rows" not in feedback_from_review(
        attestation_only, actionable=False,
    ).lower()


def test_optional_evidence_shapes_do_not_block_but_contradictions_and_unmet_outcomes_do(tmp_path):
    warnings = []
    loose = {"interaction": {"command": "tested by inspection", "states": {}, "events": []}}
    assert interaction_evidence_gaps(
        loose, required=True, scalability=True, expected_head="head-a", metadata_warnings=warnings,
    ) == []
    assert warnings

    warnings = []
    assert interaction_evidence_gaps(
        {"interaction": {"head": "head-a"}}, required=False, scalability=False,
        expected_head="head-a", affected_flow="resume stopped task", metadata_warnings=warnings,
    ) == []
    assert "interaction affected flow was not recorded" in warnings

    assert interaction_evidence_gaps(
        {"interaction": {"head": "head-a", "affected_flow": "different task"}},
        required=False, scalability=False, expected_head="head-a",
        affected_flow="resume stopped task",
    ) == ["interaction evidence contradicts the declared affected flow: resume stopped task"]

    gaps = interaction_evidence_gaps(
        {"interaction": {"head": "head-a", "states": {
            "affected": {"status": "fail", "actions": ["open task"], "observed": "500 error"},
        }}},
        required=False, scalability=False, expected_head="head-a",
    )
    assert "affected interaction explicitly failed" in gaps

    manifest = tmp_path / "replay.json"
    manifest.write_text(json.dumps({
        "producer": "garden.scheduler.interaction-replay/v1", "head": "head-a",
        "nonce": "nonce-a", "environment": "disposable", "status": "pass",
    }))
    warnings = []
    assert interaction_evidence_gaps(
        {"interaction": {"head": "head-a"}}, required=True, scalability=False,
        expected_head="head-a", replay_manifest=manifest, replay_nonce="nonce-a",
        affected_flow="resume stopped task", metadata_warnings=warnings,
    ) == []
    assert "scheduler-produced replay affected flow was not recorded" in warnings

    failed_manifest = tmp_path / "failed-replay.json"
    failed_manifest.write_text(json.dumps({
        "producer": "garden.scheduler.interaction-replay/v1", "head": "head-a",
        "nonce": "nonce-b", "environment": "disposable", "status": "fail",
    }))
    gaps = interaction_evidence_gaps(
        {"interaction": {"head": "head-a"}}, required=True, scalability=False,
        expected_head="head-a", replay_manifest=failed_manifest, replay_nonce="nonce-b",
    )
    assert gaps == ["scheduler-produced interaction replay explicitly failed"]

    assert interaction_evidence_gaps(
        {"interaction": {"head": "wrong-head"}}, required=False, scalability=False,
        expected_head="head-a",
    ) == ["interaction evidence is stale or not tied to the reviewed head"]

    unmet = {"interaction": {"unverified": [{
        "scope": "required", "criterion": "Recovery works", "outcome": "recovery failed",
        "reason": "the process remained stuck",
    }]}}
    gaps = interaction_evidence_gaps(
        unmet, required=False, scalability=False, expected_head="head-a",
        expected_criteria=["Recovery works"],
    )
    assert any("required outcome remains unverified" in gap for gap in gaps)

    artifact = tmp_path / "evidence.json"
    artifact.write_text(json.dumps({"head": "wrong-head"}))
    gaps = interaction_evidence_gaps(
        {"interaction": {"artifacts": [str(artifact)]}}, required=False, scalability=False,
        expected_head="head-a",
    )
    assert any("contradicts" in gap for gap in gaps)


def test_mechanical_gate_keeps_source_failures_and_makes_presentation_advisory(garden, monkeypatch):
    worktree = garden / "candidate"
    worktree.mkdir()
    (worktree / "bad.py").write_text("def broken(:\n")
    monkeypatch.setattr(gitops, "base_ref", lambda *_: "main")
    monkeypatch.setattr(gitops, "git", lambda *args, **_kwargs: (
        "+<<<<<<< ours\n+>>>>>>> theirs\n" if "--name-only" not in args
        else "bad.py\nsrc/garden/web/page.html\n"
    ))
    results = mechanical_results(
        worktree, "main", "", require_description=True, ui_changed=True, captures=[],
    )
    by_name = {row["name"]: row["status"] for row in results}
    assert by_name == {
        "conflict markers": "fail", "syntax": "fail",
        "UI captures": "advisory", "PR description": "advisory",
    }


def test_review_admission_never_injects_a_generic_replay(sched, monkeypatch):
    task = sched.store.task("DM-001")
    monkeypatch.setattr(
        "garden.scheduler.review.gitops.diff_names",
        lambda *_: ["src/garden/scheduler/review.py"],
    )
    run = sched.dispatch_review(task)
    assert run.mode == "review"
    assert run.env_snapshot["interaction_required"] is False
    assert run.env_snapshot["scalability_required"] is False
    assert run.env_snapshot["validation_plan"]["evidence_policy"] == "reviewer_judgment"
    assert not sched.state.get(task.id).get("check_run")


def test_visual_context_does_not_inject_a_capture_job(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Task layout changed", "pages": ["task"]}
    submitted = []
    runner_type = type(sched.runner_for(task, "local"))
    monkeypatch.setattr(runner_type, "start_checks", lambda self, run, wt, payload: submitted.append(payload))
    monkeypatch.setattr("garden.scheduler.checkruns.gitops.diff_names", lambda *_: ["src/garden/web/pages/task.py"])
    monkeypatch.setattr("garden.scheduler.checkruns.gitops.head_sha", lambda *_: "head-a")

    run = sched._dispatch_check_run(
        task, worktree=sched.store.root, branch=task.default_branch(), base="main",
        specs=[{"name": "focused", "command": "true"}], stage="pre_pr", cont={}, rep=TickReport(),
    )
    assert [spec["name"] for spec in submitted[0]["specs"]] == ["focused"]
    assert run.env_snapshot["generated_ui_check_indices"] == []
    assert run.env_snapshot["validation_plan"]["pages"] == ["task"]


def test_missing_worker_preflight_is_recorded_without_forcing_a_revision(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "omit-preflight")
    sched.cfg.data["stack"] = False
    sched.tick()
    sched.tick()
    task = sched.store.task("DM-001")
    assert task.status == Status.IN_REVIEW
    assert task.pr
    advisories = sched.state.get(task.id)["verification_advisories"]
    assert any(item["kind"] == "pre_flight" for item in advisories)
    assert not any(run.mode == "revise" for run in sched.runs.runs_for(task.id))


def test_attestation_only_review_progresses_without_optional_fields(sched, fake_github, monkeypatch):
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-attestation")
    monkeypatch.setattr(
        "garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/criteria.py"],
    )
    sched.tick()
    assert "DM-001(review)" in sched.tick().dispatched
    report = sched.tick()
    assert "DM-001 review: approve" in report.transitions
    stored = sched.state.get("DM-001")["last_review"]
    assert stored["verdict"] == "approve"
    assert stored["attestation"].startswith("I inspected")
    assert not any(run.mode == "revise" for run in sched.runs.runs_for("DM-001"))


def test_summary_only_request_changes_routes_actionable_feedback(sched, monkeypatch):
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-summary-block")
    sched.tick()
    assert "DM-001(review)" in sched.tick().dispatched
    report = sched.tick()
    assert "DM-001 -> changes_requested (review)" in report.transitions
    assert "DM-001(revise)" in report.dispatched
    assert "parser still drops empty records" in agent_context(sched.runs.latest("DM-001")).lower()
