"""What a person does to a task: retry past the cap, retry a capped pre-PR round."""

import subprocess
import sys
from pathlib import Path

import pytest

from garden import gitops
from garden.github import GitHubError
from garden.model import Status, now_iso
from garden.preflight import PREFLIGHT_ITEMS
from garden.runner.manual import ManualRunner
from garden.scheduler import Scheduler, TickReport
from garden.store import Store
from tests.scheduler.conftest import statuses


def test_take_manual_refuses_a_stale_ready_task_with_an_active_manual_claim(sched):
    """A manual run owns its task even if an interrupted state write left it READY."""
    task = sched.store.task("DM-001")
    task.runner = "manual"
    sched.store.save(task)
    claimed = sched.runs.new_run(task.id, "manual", "work")

    with pytest.raises(RuntimeError, match="already claimed"):
        sched.take_manual(sched.store.task(task.id))

    assert sched.runs.runs_for(task.id) == [claimed]


def test_retry_grants_one_more_round_past_cap(sched, fake_github):
    """Resuming a capped task rolls the revision counter back one so a revise run runs."""
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    t.pr = "https://example.com/pull/101"
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["revisions"] = 2  # == max_revisions (2) in the test garden
    st["needs_human"] = "2 revision rounds already used"
    sched.retry(t)
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert int(st["revisions"]) == 1  # cap (2) minus one -> one more round dispatchable


def test_revision_policy_escalates_each_substantive_threshold_once(sched):
    sched.cfg.data["revision_policy"] = {"enabled": True, "every": 2, "decision_after": 6}
    task = sched.store.task("DM-001")
    task.difficulty = "easy"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["substantive_revisions"] = 2
    sched._apply_revision_policy(task, st)
    assert task.difficulty == "medium"
    assert st["difficulty_floor"] == "medium"
    assert st["difficulty_escalations"][0]["counter"] == 2
    sched._apply_revision_policy(task, st)
    assert len(st["difficulty_escalations"]) == 1
    st["substantive_revisions"] = 4
    sched._apply_revision_policy(task, st)
    assert task.difficulty == "hard"


def test_revision_policy_protects_explicit_model_and_stops(sched):
    sched.cfg.data["revision_policy"] = {"enabled": True, "every": 2, "decision_after": 6}
    task = sched.store.task("DM-001")
    task.difficulty = "easy"
    task.model = "owner-model"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["substantive_revisions"] = 2
    with pytest.raises(RuntimeError, match="explicit model owner-model is protected"):
        sched._apply_revision_policy(task, st)
    assert task.difficulty == "easy"
    assert st["needs_human"]["kind"] == "troubled_task"


def test_investigation_is_idempotent_preserves_work_and_report_waits_for_decision(sched):
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.com/pull/101"
    task.branch = "garden/preserved"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["pending_feedback"] = "- repeated finding"
    sched.pause_for_investigation(task, "find the root cause", owner="agent", budget="$2")
    first = dict(st["investigation"])
    sched.pause_for_investigation(task, "duplicate click", owner="operator")
    assert st["investigation"] == first
    assert task.pr.endswith("/101") and task.branch == "garden/preserved"
    assert st["pending_feedback"] == "- repeated finding"
    with pytest.raises(RuntimeError, match="paused for investigation"):
        sched.dispatch(task, mode="revise")
    report = {"likely_cause": "stale fixture", "confidence": "medium", "unknowns": [],
              "evidence": ["base comparison"], "attempted_checks": ["focused test"],
              "retain_work": True, "alternatives": ["repair verification"],
              "recommendation": "repair environment/verification", "links": []}
    sched.complete_investigation(task, report)
    assert st["investigation"]["status"] == "report_ready"
    assert task.status == Status.CHANGES_REQUESTED


def test_operator_investigation_report_rejects_partial_prose_and_unsupported_recommendation(sched):
    task = sched.store.task("DM-001")
    sched.pause_for_investigation(task, "diagnose", owner="operator")
    with pytest.raises(RuntimeError, match="missing"):
        sched.complete_investigation(task, {"likely_cause": "maybe stale"})
    assert sched.state.get(task.id)["investigation"]["status"] == "requested"
    report = {"likely_cause": "stale fixture", "confidence": "high", "unknowns": [],
              "evidence": ["base passes"], "attempted_checks": ["focused comparison"],
              "retain_work": True, "alternatives": ["replace fixture"],
              "recommendation": "merge anyway", "links": ["/runs/one"]}
    with pytest.raises(RuntimeError, match="unsupported recommendation"):
        sched.complete_investigation(task, report)
    report["recommendation"] = "repair environment/verification"
    sched.complete_investigation(task, report)
    assert sched.state.get(task.id)["investigation"]["report"] == report


def test_troubled_continue_preserves_lifetime_counter_and_rejects_double_action(sched):
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["revisions"] = 2
    st["substantive_revisions"] = 7
    st["pending_feedback"] = "- retain me"
    st["needs_human"] = {"kind": "troubled_task", "reason": "not converging"}
    sched.continue_troubled(task, allowance=1, difficulty="hard")
    assert st["substantive_revisions"] == 7
    assert st["pending_feedback"] == "- retain me"
    assert st["revision_allowance"] == 1
    with pytest.raises(RuntimeError, match="no troubled-task decision"):
        sched.continue_troubled(task)


def test_exhausted_troubled_allowance_stops_before_another_dispatch(sched):
    sched.cfg.data["revision_policy"] = {"enabled": True, "every": 2, "decision_after": 6}
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    sched.store.save(task)
    st = sched.state.get(task.id)
    st.update({"substantive_revisions": 7, "troubled_decisions": [{"allowance": 1}],
               "revision_allowance": 0})
    with pytest.raises(RuntimeError, match="allowance is exhausted"):
        sched._apply_revision_policy(task, st)
    assert st["needs_human"]["kind"] == "troubled_task"


def test_already_hard_threshold_stops_once_and_survives_restart(sched, fake_github):
    sched.cfg.data["revision_policy"] = {"enabled": True, "every": 2, "decision_after": 8}
    task = sched.store.task("DM-001")
    task.difficulty = "hard"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["substantive_revisions"] = 2

    with pytest.raises(RuntimeError, match="troubled"):
        sched._apply_revision_policy(task, st)
    first_thresholds = list(st["revision_thresholds"])

    fresh = Scheduler(Store(sched.store.root), github=fake_github, log=print)
    fresh_task = fresh.store.task(task.id)
    assert fresh.state.get(task.id)["revision_thresholds"] == first_thresholds == [2]
    fresh._apply_revision_policy(fresh_task, fresh.state.get(task.id))
    assert fresh.state.get(task.id)["revision_thresholds"] == [2]
    assert fresh_task.difficulty == "hard"


def test_troubled_change_approach_and_cancel_require_current_drained_decision(sched):
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.branch = "garden/preserved"
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st.update({"needs_human": {"kind": "troubled_task", "reason": "not converging"},
               "pending_feedback": "keep this finding", "substantive_revisions": 6})

    sched.change_troubled_approach(task, "replace the parser, keeping its public API")
    assert "replace the parser" in st["pending_feedback"]
    assert st["approach_changes"][-1]["approach"].startswith("replace")
    assert task.branch == "garden/preserved" and task.pr.endswith("/101")
    with pytest.raises(RuntimeError, match="no troubled-task decision"):
        sched.change_troubled_approach(task, "stale second click")

    st["needs_human"] = {"kind": "troubled_task", "reason": "still not converging"}
    active = sched.runs.new_run(task.id, "local", mode="revise", initial_status="running")
    with pytest.raises(RuntimeError, match="run in flight"):
        sched.cancel_troubled(task, "not worth further work")
    active.status = "done"
    active.save()
    with pytest.raises(RuntimeError, match="reason is required"):
        sched.cancel_troubled(task, "")
    sched.cancel_troubled(task, "not worth further work")
    assert task.status == Status.CANCELLED
    assert task.branch == "garden/preserved" and task.pr.endswith("/101")


def test_investigation_drains_then_uses_shared_admission_and_preserves_failed_run(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    sched.store.save(task)
    writer = sched.runs.new_run(task.id, "local", mode="revise", initial_status="running")
    sched.pause_for_investigation(task, "diagnose repeated review", owner="agent", budget="$2")
    inv = sched.state.get(task.id)["investigation"]
    assert inv["status"] == "draining"

    calls = []
    monkeypatch.setattr(sched, "slots_free", lambda: 0)
    monkeypatch.setattr(sched, "dispatch_investigation", lambda *args, **kwargs: calls.append(args))
    sched._dispatch_pending_investigations({task.id: task}, TickReport())
    assert calls == []

    writer.status = "done"
    writer.save()
    monkeypatch.setattr(sched, "slots_free", lambda: 1)
    monkeypatch.setattr(sched, "local_slots_free", lambda: 1)
    sched._dispatch_pending_investigations({task.id: task}, TickReport())
    assert calls and calls[0][0].id == task.id

    inv.update({"status": "failed", "run_id": "old", "transcript": "/tmp/old/final.md", "cost_usd": 1.25})
    sched.retry_investigation(task)
    assert inv is not sched.state.get(task.id)["investigation"]
    assert sched.state.get(task.id)["investigation_history"][-1]["cost_usd"] == 1.25
    with pytest.raises(RuntimeError, match="no failed investigation"):
        sched.retry_investigation(task)


def test_investigation_report_is_separate_from_revision_cost_and_waits_for_followup(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    sched.store.save(task)
    st = sched.state.get(task.id)
    st.update({"revisions": 4, "substantive_revisions": 4,
               "investigation": {"status": "active", "owner": "agent",
                                 "task_status": Status.CHANGES_REQUESTED.value,
                                 "feedback_markdown": "older inline feedback token=ghp_abcdefghijklmnopqrstuvwxyz1234"}})
    run = sched.runs.new_run(task.id, "local", mode="investigation")
    run.cost_usd = 0.75
    run.usage = {"input_tokens": 20}
    run.result = {"status": "done", "investigation_report": {
        "likely_cause": "stale verification fixture", "confidence": "high", "unknowns": [],
        "evidence": ["base comparison"], "attempted_checks": ["focused test"],
        "retain_work": True, "alternatives": ["repair fixture"],
        "recommendation": "repair environment/verification",
        "discovered": [{"title": "Second task", "body": "## Goal\n\nRepair the fixture."}],
    }}
    sched._finalize_investigation(task, run, TickReport(), {})

    assert task.status == Status.CHANGES_REQUESTED
    assert st["revisions"] == st["substantive_revisions"] == 4
    assert st["investigation"]["cost_usd"] == 0.75
    assert st["investigation"]["publication"]["status"] == "failed"
    assert Path(st["investigation"]["report_paths"]["markdown"]).is_file()
    related = sched.store.task("DM-002")
    assert "stale verification fixture" in related.body and "older inline feedback" in related.body
    assert "ghp_" not in related.body
    assert "/tasks/DM-002" in st["investigation"]["report"]["links"]
    assert st["needs_human"]["kind"] == "investigation_report"
    with pytest.raises(RuntimeError, match="paused for investigation"):
        sched.dispatch(task, mode="revise")
    monkeypatch.setattr("garden.deepdives.publish_report", lambda *args: {
        "status": "published", "commit": "abc123", "markdown": "reports/a.md", "html": "reports/a.html",
    })
    sched.retry_investigation_publication(task)
    assert st["investigation"]["publication"]["commit"] == "abc123"
    sched.continue_troubled(task)
    assert "stale verification fixture" in st["investigation_handoff"]["diagnosis"]
    assert "base comparison" in st["investigation_handoff"]["diagnosis"]


def test_investigation_agent_gets_read_only_dossier_and_isolated_fenced_checkout(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st.update({"pending_feedback": "same finding", "substantive_revisions": 4,
               "review_rounds": 3, "checks": "failure"})
    sched.github.complete_feedback_snapshots[101] = {
        "repository": "acme/widget", "pr": 101, "fetched_at": "now", "complete": True, "errors": [],
        "items": [
            {"kind": "review", "id": "old", "author": "reviewer", "created_at": "before-cursor",
             "body": "summary body", "state": "CHANGES_REQUESTED", "commit_id": "old-head",
             "trusted_instruction": True},
            {"kind": "line_comment", "id": "reply", "author": "reviewer", "created_at": "later",
             "body": "full inline reply", "thread_id": "thread-1", "resolved": False, "outdated": True,
             "commit_id": "old-head", "trusted_instruction": True},
            {"kind": "discussion_comment", "id": "discussion", "author": "visitor", "created_at": "later",
             "body": "discussion context", "trusted_instruction": False},
        ],
    }
    monkeypatch.setattr(sched, "slug_for", lambda task: "acme/widget")
    monkeypatch.setattr(sched, "_pr_number", lambda task: 101)
    sched.pause_for_investigation(task, "why does the same finding recur?", owner="agent",
                                  scope="read-only diagnosis; no fix", budget="$2 or 20 minutes")
    runner = sched.runner_for(task)
    captured = {}

    def start(run, cwd, prompt):
        captured.update({"cwd": cwd, "prompt": prompt})
        run.status = "running"
        run.save()

    monkeypatch.setattr(runner, "start", start)
    run = sched.dispatch_investigation(task, runner=runner)

    assert run.mode == "investigation" and run.worktree
    assert run.fence_paths, "investigation checkout must use the ordinary write fence"
    assert captured["cwd"] == sched.worktree_for(task)
    assert "diagnosing only. Do not edit files" in captured["prompt"]
    assert "same finding" in captured["prompt"] and "reviews=3" in captured["prompt"]
    assert "summary body" in captured["prompt"] and "full inline reply" in captured["prompt"]
    assert "unresolved, outdated" in captured["prompt"] and "diagnostic context only" in captured["prompt"]
    snapshot = Path(st["investigation"]["feedback_snapshot_path"])
    assert snapshot.is_file() and '"id": "old"' in snapshot.read_text()
    assert "$2 or 20 minutes" in captured["prompt"]
    assert run.env_snapshot["requires_preflight"] is False


def test_read_only_investigation_can_run_in_frozen_phase_but_fix_remains_held(sched, monkeypatch):
    task = sched.store.task("DM-001")
    phase = sched.store.phase(task.product, task.phase)
    sched.store.set_phase_frozen(phase, "owner hold")
    assert sched.store.phase(task.product, task.phase).frozen
    sched.pause_for_investigation(task, "explain held work", owner="agent")
    runner = sched.runner_for(task)
    monkeypatch.setattr(runner, "start", lambda run, cwd, prompt: None)
    run = sched.dispatch_investigation(task, runner=runner)
    assert run.mode == "investigation"


def test_investigation_after_drain_restores_the_safe_boundary_status(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.status = Status.RUNNING
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["investigation"] = {"status": "draining", "owner": "agent",
                           "task_status": Status.RUNNING.value, "scope": "read-only",
                           "budget": "$1", "reason": "diagnose"}
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    fake_run = sched.runs.new_run(task.id, "local", mode="investigation")
    fake_run.status = "done"
    fake_run.save()
    monkeypatch.setattr(sched, "dispatch", lambda *args, **kwargs: fake_run)

    sched.dispatch_investigation(task)

    assert st["investigation"]["task_status"] == Status.IN_REVIEW.value


def test_retry_of_changes_requested_without_pr_is_a_revise(sched, fake_github):
    """A pre-PR check that failed at the cap leaves the task in changes_requested with no
    PR. `garden retry` must continue the revise loop (keep the feedback, roll the cap back),
    not reset to a fresh work run that would drop both."""
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    t.pr = ""  # capped before any PR was opened
    sched.store.save(t)
    st = sched.state.get("DM-001")
    st["revisions"] = 2  # == max_revisions in the test garden
    st["pending_feedback"] = "- **pre-PR check** `unit` fail: exit 1"
    st["needs_human"] = "pre-PR checks failed and 2 revision rounds already used"
    sched.retry(sched.store.task("DM-001"))
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert int(st["revisions"]) == 1  # one more round dispatchable, counter not lost
    assert st["pending_feedback"]  # feedback preserved, not dropped for a fresh work run
    assert statuses(sched)["DM-001"] == "changes_requested"
    # the revise round is dispatchable now
    rep = sched.tick()
    assert "DM-001(revise)" in rep.dispatched


def test_delegated_recovery_queues_one_retained_feedback_revision(sched, fake_github):
    """A delegated operator repairs a routine cap without turning it into an owner card.

    The same feedback fingerprint may use exactly one extra round; a repeated unchanged
    failure remains stopped for a product owner instead of consuming another worker run.
    """
    from garden.inbox import build_inbox

    sched.cfg.data["recovery"] = {"delegated": True}
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["revisions"] = 2
    st["pending_feedback"] = "- **CI** is missing; preserve this request"
    st["needs_human"] = {"kind": "revision_cap", "reason": "2 revision rounds used",
                         "delegated_recovery": True}

    card = next(item for item in build_inbox(sched.store, sched) if item["task"] == task.id)
    assert card["group"] == "operator"
    assert any(action["kind"] == "recover" for action in card["actions"])
    assert sched.delegate_recovery(task) == "one retained-feedback revise round queued"
    assert st["pending_feedback"] == "- **CI** is missing; preserve this request"
    assert st["revisions"] == 1
    assert "DM-001(revise)" in sched.tick().dispatched

    task = sched.store.task(task.id)
    task.status = Status.CHANGES_REQUESTED
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["revisions"] = 2
    st["pending_feedback"] = "- **CI** is missing; preserve this request"
    st["needs_human"] = {"kind": "revision_cap", "reason": "2 revision rounds used",
                         "delegated_recovery": True}
    with pytest.raises(RuntimeError, match="unchanged recovery"):
        sched.delegate_recovery(sched.store.task(task.id))


def test_delegated_check_recovery_keeps_stop_when_resource_admission_defers(sched, monkeypatch):
    """A resource gate cannot consume the sole delegated check continuation."""
    from garden.scheduler.resources import ResourcePressureError

    sched.cfg.data["recovery"] = {"delegated": True}
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["needs_human"] = {"kind": "check_did_not_run", "reason": "timed out",
                         "delegated_recovery": True}
    st["recovery_check"] = {"stage": "ci", "specs": [{"name": "unit", "command": "true"}],
                            "cont": {"worktree": str(sched.worktree_for(task)), "branch": "garden/test", "base": "main"}}
    monkeypatch.setattr(sched, "_dispatch_check_run", lambda *_a, **_k: (_ for _ in ()).throw(ResourcePressureError("full")))

    with pytest.raises(ResourcePressureError, match="full"):
        sched.delegate_recovery(task)
    assert st["needs_human"]["kind"] == "check_did_not_run"
    assert not st.get("delegated_recovery_fingerprints")


def test_infrastructure_and_missing_ci_are_operator_actions_not_owner_cards(sched):
    """Routine prerequisites name their repair without masquerading as product decisions."""
    from garden.inbox import build_inbox, needs_you

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["infrastructure_hold"] = {"kind": "missing_libraries", "diagnostic": "install libnss3"}
    st["ci_missing"] = True

    cards = [item for item in build_inbox(sched.store, sched) if item["task"] == task.id]
    assert [card["kind"] for card in cards if card["group"] == "operator"] == ["ci_missing"]
    assert not any(needs_you(card) for card in cards if card["group"] == "operator")

    st["ci_missing"] = False
    cards = [item for item in build_inbox(sched.store, sched) if item["task"] == task.id]
    assert [card["kind"] for card in cards if card["group"] == "operator"] == ["infrastructure_hold"]


def test_mixed_checkout_and_live_config_work_waits_for_operator_evidence(sched, fake_github):
    """A live setting is an operator prerequisite, never a worker instruction or owner card."""
    from garden.brief import build_brief
    from garden.inbox import build_inbox, needs_you

    task = sched.store.task("DM-001")
    task.extra["deliverables"] = [
        {"path": "src/demo.py", "action": "add the product behavior"},
        {"path": "/etc/demo/live.yaml", "owner": "operator", "action": "enable the deployed feature"},
    ]
    sched.store.save(task)

    # Preflight parks the live change as an operator action before a worker/run exists.
    assert not sched.operator_scope_ready(task)
    cards = [item for item in build_inbox(sched.store, sched) if item["task"] == task.id]
    card = next(item for item in cards if item["kind"] == "operator_scope")
    assert card["group"] == "operator"
    assert not needs_you(card)
    assert "/etc/demo/live.yaml" in card["reason"]
    assert "enable the deployed feature" not in build_brief(sched.store, task).text
    assert not sched.runs.runs_for(task.id)

    sched.submit_operator_evidence(task, "deployed setting enabled in the disposable environment")
    assert sched.operator_scope_ready(sched.store.task(task.id))
    evidence = sched.state.get(task.id)["operator_evidence"]
    assert evidence["text"] == "deployed setting enabled in the disposable environment"

    # The product-only work now runs normally; the worker still receives no live config step.
    assert "DM-001(work)" in sched.tick().dispatched


# ---- CG-142: a done or cancelled task is terminal; no action reopens it -----
@pytest.mark.parametrize("action", ["triage", "retry", "cancel", "answer", "accept_decision", "reject_decision", "resume_task", "dispatch", "dispatch_review", "review_again", "dispatch_persona_pr", "integrate_now"])
def test_state_changing_actions_refuse_a_merged_done_task(sched, fake_github, action):
    """A task whose PR was merged (poll sets `done`) must not be moved back into the loop
    by any of the actions a stale page or a race could still fire."""
    sched.tick()
    sched.tick()  # DM-001 -> in_review with an open PR
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    task.log("PR merged: https://github.com/test/demo/pull/71")
    task.status = Status.DONE
    sched.store.save(task)

    kwargs = {}
    if action == "triage":
        kwargs = {"ready": True}
    elif action == "answer":
        kwargs = {"text": "hi"}
    elif action in ("accept_decision", "reject_decision"):
        kwargs = {"note": "ok"} if action == "accept_decision" else {"note": "no"}
    elif action == "dispatch_persona_pr":
        kwargs = {"name": "security"}

    with pytest.raises(RuntimeError, match=r"DM-001 is done: #71 was merged"):
        getattr(sched, action)(sched.store.task("DM-001"), **kwargs)

    assert statuses(sched)["DM-001"] == "done"  # untouched by the refused action


def test_cancel_clears_needs_human_and_automerge_blocked(sched, fake_github):
    """CG-175: cancelling a task with a stop recorded (a review cap, feedback waiting for a
    revise run, an automerge hold) must drop them so the cancelled task never shows as a
    decision on the Inbox."""
    task = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.cancel(task, "cancelled by hand")
    assert statuses(sched)["DM-001"] == "cancelled"
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


def test_wont_do_clears_needs_human_and_automerge_blocked(sched, fake_github):
    """CG-195: `wont_do` is terminal alongside done and cancelled, so it must clear the same
    stale stops on transition — it was missing from the CG-175 fix, which only checked
    Status.DONE/CANCELLED."""
    task = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.mark_wont_do(task, reason="not worth doing")
    assert statuses(sched)["DM-001"] == "wont_do"
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


def test_mark_done_requires_pr_commits_on_the_base_unless_forced(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    sched.store.save(task)
    monkeypatch.setattr(sched, "_pr_commits_on_base", lambda _: False)

    with pytest.raises(RuntimeError, match="commits are not on its base branch"):
        sched.mark_done(task)
    assert statuses(sched)["DM-001"] == "ready"

    sched.mark_done(task, force=True)
    assert statuses(sched)["DM-001"] == "done"
    done = sched.events.read(task_id=task.id, kinds=("transition",))[-1]
    assert done["base_merged"] is False


def test_forced_done_custom_note_is_not_a_merge(sched):
    task = sched.store.task("DM-001")
    sched.mark_done(task, note="obsolete", force=True)
    done = sched.events.read(task_id=task.id, kinds=("transition",))[-1]
    assert done["base_merged"] is False


def test_external_open_pr_uses_claimed_identity_and_review_without_managed_worktree(sched, fake_github):
    """An operator-owned directory is audit data, not a signal to push or test it."""
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/fix", "main", "external", "")
    coincidental_path = sched.worktree_for(task)
    coincidental_path.mkdir(parents=True)
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override="operator/fix", completion_mode="external",
                         external_pr=pr.url, worktree_override=coincidental_path)
    assert run.completion_mode == "external" and run.worktree == str(coincidental_path)

    sched.finish_manual(task, {"status": "done", "summary": "implemented", "pr": pr.url})

    task = sched.store.task("DM-001")
    assert task.status == Status.IN_REVIEW and task.branch == "operator/fix"
    assert sched.state.get(task.id)["pr_number"] == pr.number
    assert any(r.mode == "review" for r in sched.runs.runs_for(task.id))


def test_external_claim_persists_actual_identity_before_finish(sched, fake_github):
    """A restart after take retains the operator's branch and PR, not a generated default."""
    from garden.store import Store

    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/actual", "main", "external", "")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    reloaded = Store(sched.store.root).task(task.id)
    assert reloaded.branch == "operator/actual"
    assert reloaded.pr == pr.url
    assert sched.state.get(task.id)["pr_number"] == pr.number


def test_external_claim_stores_a_safe_provider_identity_without_a_browser_url(sched):
    """Provider identities are accepted after the CLI has verified their PR number."""
    task = sched.store.task("DM-001")
    provider_url = "https://provider.test/api/pull-requests/opaque-identity"

    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override="operator/actual", completion_mode="external",
                   external_pr=provider_url, external_pr_number=101)

    assert sched.store.task(task.id).pr == provider_url
    assert sched.state.get(task.id)["pr_number"] == 101


@pytest.mark.parametrize("url", [
    "https://operator:synthetic-password@provider.test/pull/101",
    "https://operator@provider.test/pull/101",
    "https://provider.test/pull/101?access=synthetic-token",
    "https://provider.test/pull/101#synthetic-fragment",
])
def test_external_claim_rejects_unsafe_provider_identity_before_persistence(sched, url):
    task = sched.store.task("DM-001")

    with pytest.raises(RuntimeError, match="unsupported components"):
        sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                       branch_override="operator/actual", completion_mode="external",
                       external_pr=url, external_pr_number=101)

    assert sched.store.task(task.id).pr == ""
    assert not sched.runs.all_runs()


def test_pushed_manual_completion_fetches_exact_head_and_enters_normal_review(sched, fake_github, tmp_path):
    """Work from another clone is materialised before the ordinary PR/review handoff."""
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    task = sched.store.task("DM-001")
    branch = "operator/pushed"
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=branch, completion_mode="pushed")
    clone = tmp_path / "authoring-clone"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(clone)], check=True)
    gitops.git("config", "user.email", "author@example.com", cwd=clone)
    gitops.git("config", "user.name", "Author", cwd=clone)
    gitops.git("checkout", "-q", "-b", branch, cwd=clone)
    (clone / "manual.txt").write_text("authored elsewhere\n")
    gitops.git("add", "manual.txt", cwd=clone)
    gitops.git("commit", "-q", "-m", "external work", cwd=clone)
    pushed_sha = gitops.git("rev-parse", "HEAD", cwd=clone).strip()
    gitops.git("push", "-q", "origin", branch, cwd=clone)
    result = {
        "status": "done", "summary": "authored elsewhere", "repository": "test/demo",
        "branch": branch, "pushed_sha": pushed_sha,
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked"}
                       for item in PREFLIGHT_ITEMS],
    }

    sched.finish_manual(task, result)

    worktree = sched.worktree_for(task)
    assert gitops.git("rev-parse", "HEAD", cwd=worktree).strip() == pushed_sha
    assert sched.store.task(task.id).status == Status.IN_REVIEW
    assert any(r.mode == "review" for r in sched.runs.runs_for(task.id))


@pytest.mark.parametrize(
    ("repository", "branch", "sha", "message"),
    [
        ("other/repo", "operator/pushed", "a" * 40, "does not match configured repository"),
        ("test/demo", "operator/other", "a" * 40, "does not match claimed branch"),
        ("test/demo", "operator/pushed", "short", "exact full commit SHA"),
        ("test/demo", "operator/pushed", "a" * 40, "was not found"),
    ],
)
def test_pushed_manual_completion_refuses_untrusted_identity(
    sched, fake_github, repository, branch, sha, message,
):
    task = sched.store.task("DM-001")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override="operator/pushed", completion_mode="pushed")

    with pytest.raises(RuntimeError, match=message):
        sched.finish_manual(task, {"status": "done", "repository": repository,
                                   "branch": branch, "pushed_sha": sha})

    saved = sched.runs.latest(task.id)
    assert saved.status == "running"
    assert saved.completion_attempts[-1]["status"] == "refused"
    assert sched.store.task(task.id).status == Status.RUNNING


def test_pushed_manual_stale_sha_can_be_corrected_after_restart(sched, fake_github, tmp_path):
    task = sched.store.task("DM-001")
    branch = "operator/recover"
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=branch, completion_mode="pushed")
    clone = tmp_path / "recovery-clone"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(clone)], check=True)
    gitops.git("config", "user.email", "author@example.com", cwd=clone)
    gitops.git("config", "user.name", "Author", cwd=clone)
    gitops.git("checkout", "-q", "-b", branch, cwd=clone)
    (clone / "recovery.txt").write_text("recoverable\n")
    gitops.git("add", "recovery.txt", cwd=clone)
    gitops.git("commit", "-q", "-m", "recoverable work", cwd=clone)
    pushed_sha = gitops.git("rev-parse", "HEAD", cwd=clone).strip()
    gitops.git("push", "-q", "origin", branch, cwd=clone)

    with pytest.raises(RuntimeError, match="SHA is stale"):
        sched.finish_manual(task, {"status": "done", "repository": "test/demo",
                                   "branch": branch, "pushed_sha": "a" * 40})

    restarted = Scheduler(Store(sched.store.root), github=fake_github)
    restarted.finish_manual(restarted.store.task(task.id), {
        "status": "done", "repository": "test/demo", "branch": branch,
        "pushed_sha": pushed_sha, "summary": "recovered",
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked"}
                       for item in PREFLIGHT_ITEMS],
    })
    assert restarted.store.task(task.id).status == Status.IN_REVIEW
    saved = restarted.runs.latest(task.id)
    assert saved.pushed_head == pushed_sha
    assert saved.completion_attempts[-1]["status"] == "refused"


def test_pushed_manual_submitted_result_resumes_finalization_after_restart(sched, fake_github, tmp_path):
    task = sched.store.task("DM-001")
    branch = "operator/interrupted"
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override=branch, completion_mode="pushed")
    clone = tmp_path / "interrupted-clone"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(clone)], check=True)
    gitops.git("config", "user.email", "author@example.com", cwd=clone)
    gitops.git("config", "user.name", "Author", cwd=clone)
    gitops.git("checkout", "-q", "-b", branch, cwd=clone)
    (clone / "interrupted.txt").write_text("durable\n")
    gitops.git("add", "interrupted.txt", cwd=clone)
    gitops.git("commit", "-q", "-m", "durable work", cwd=clone)
    pushed_sha = gitops.git("rev-parse", "HEAD", cwd=clone).strip()
    gitops.git("push", "-q", "origin", branch, cwd=clone)
    result = {
        "status": "done", "repository": "test/demo", "branch": branch,
        "pushed_sha": pushed_sha, "summary": "interrupted",
        "pre_flight": [{"item": item, "status": "pass", "evidence": "checked"}
                       for item in PREFLIGHT_ITEMS],
    }
    run.pushed_head = pushed_sha
    ManualRunner.finish(run, result)
    run.env_snapshot["pushed_completion_submitted"] = True
    run.finished_at = now_iso()
    run.status = "done"
    run.save()  # controller stops during finalize(), before the task transition

    restarted = Scheduler(Store(sched.store.root), github=fake_github)
    restarted.tick()

    assert restarted.store.task(task.id).status == Status.IN_REVIEW
    assert gitops.git("rev-parse", "HEAD", cwd=restarted.worktree_for(task)).strip() == pushed_sha


def test_external_claim_refuses_pr_with_a_different_actual_branch(sched, fake_github):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/actual", "main", "external", "")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override="garden/DM-001-generated", completion_mode="external")

    with pytest.raises(RuntimeError, match="does not match claimed branch"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})
    assert sched.store.task(task.id).status == Status.RUNNING
    failed = sched.runs.latest(task.id)
    assert failed.status == "running"
    assert failed.completion_attempts[-1]["status"] == "refused"
    assert failed.completion_attempts[-1]["cost_usd"] is None
    assert failed.completion_attempts[-1]["pr_url"] == pr.url
    assert failed.completion_attempts[-1]["pr_number"] == pr.number
    event = next(e for e in reversed(sched.events.read()) if e["kind"] == "external_completion_refused")
    assert event["pr_url"] == pr.url and event["pr_number"] == pr.number
@pytest.mark.parametrize("error_type", [GitHubError, KeyError])
def test_external_completion_pr_lookup_failure_is_audited(sched, fake_github, monkeypatch, error_type):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/fix", "main", "external", "")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)
    def unavailable(*_):
        raise error_type("unavailable")

    monkeypatch.setattr(sched.github, "get_pr", unavailable)

    with pytest.raises(RuntimeError, match="could not read external PR: .*unavailable"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.store.task(task.id).status == Status.RUNNING
    assert sched.store.task(task.id).pr == pr.url
    refused = sched.runs.latest(task.id).completion_attempts[-1]
    assert refused["pr_url"] == pr.url and refused["pr_number"] == pr.number
    assert refused["cost_usd"] is None
    event = next(e for e in reversed(sched.events.read()) if e["kind"] == "external_completion_refused")
    assert event["pr_url"] == pr.url and event["pr_number"] == pr.number


def test_external_blocked_result_uses_ordinary_manual_completion(sched, fake_github):
    task = sched.store.task("DM-001")
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override="operator/blocked", completion_mode="external")

    rep = sched.finish_manual(task, {"status": "blocked", "summary": "waiting on access"})

    assert sched.store.task(task.id).status == Status.FAILED
    assert "failed" in rep.transitions[0]
    assert sched.runs.latest(task.id).result["status"] == "blocked"


def test_external_merged_pr_completes_without_rechecks_after_final_base_verification(sched, fake_github, monkeypatch):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/merged", "main", "external", "")
    pr.state, pr.head_sha = "MERGED", "verified-head"
    monkeypatch.setattr(gitops, "fetch", lambda _: None)
    monkeypatch.setattr(gitops, "is_ancestor", lambda *_: True)
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    rep = sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.store.task(task.id).status == Status.DONE
    assert "external merged PR" in rep.transitions[0]
    assert sched.runs.latest(task.id).run_id == run.run_id
    assert not any(r.mode == "review" for r in sched.runs.runs_for(task.id))
    with pytest.raises(RuntimeError, match="no active run to finish"):
        sched.finish_manual(sched.store.task(task.id), {"status": "done", "pr": pr.url})
def test_external_merged_pr_restacks_its_child(sched, fake_github, monkeypatch):
    """An external parent merge shares the normal stacked-child lifecycle."""
    parent = sched.store.task("DM-001")
    child = sched.store.task("DM-002")
    pr = fake_github.create_pr("test/demo", "operator/merged", "main", "external", "")
    pr.state, pr.head_sha = "MERGED", "verified-head"
    sched.state.get(child.id)["stack_parent"] = parent.id
    restacked: list[str] = []
    monkeypatch.setattr(gitops, "fetch", lambda _: None)
    monkeypatch.setattr(gitops, "is_ancestor", lambda *_: True)
    monkeypatch.setattr(sched, "_restack", lambda task, _: restacked.append(task.id))
    sched.dispatch(parent, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    sched.finish_manual(parent, {"status": "done", "pr": pr.url})

    assert restacked == [child.id]


def test_external_stacked_merged_pr_is_not_completed_until_it_reaches_final_base(sched, fake_github, monkeypatch):
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/stacked", "parent-branch", "external", "")
    pr.state, pr.head_sha = "MERGED", "stacked-head"
    monkeypatch.setattr(gitops, "fetch", lambda _: None)
    monkeypatch.setattr(gitops, "is_ancestor", lambda *_: False)
    run = sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                         branch_override=pr.head, completion_mode="external", external_pr=pr.url)

    with pytest.raises(RuntimeError, match="not included in final base"):
        sched.finish_manual(task, {"status": "done", "pr": pr.url})
    assert sched.store.task(task.id).status == Status.RUNNING
    failed = sched.runs.latest(task.id)
    assert failed.run_id == run.run_id and failed.status == "running"
    assert "not included in final base" in failed.completion_attempts[-1]["reason"]
    assert failed.completion_attempts[-1]["pr_url"] == pr.url
    assert failed.completion_attempts[-1]["pr_number"] == pr.number
    event = next(e for e in reversed(sched.events.read()) if e["kind"] == "external_completion_refused")
    assert event["pr_url"] == pr.url and event["pr_number"] == pr.number


def test_external_completion_git_guard_violation_is_refused_and_failed(sched, fake_github):
    """External completion must not skip the metadata guard captured at dispatch."""
    task = sched.store.task("DM-001")
    pr = fake_github.create_pr("test/demo", "operator/fix", "main", "external", "")
    created_before = len(fake_github.created)
    sched.dispatch(task, runner=ManualRunner({}), worktree=False,
                   branch_override=pr.head, completion_mode="external", external_pr=pr.url)
    clone = sched.repo_for(task)
    config = clone / ".git" / "config"
    config.write_text(config.read_text() + "\n[core]\n\thooksPath = /tmp/garden-test-evil-hooks\n")

    rep = sched.finish_manual(task, {"status": "done", "pr": pr.url})

    assert sched.store.task(task.id).status == Status.FAILED
    assert "git guard" in rep.transitions[0]
    refused = sched.runs.latest(task.id).completion_attempts[-1]
    assert refused["status"] == "refused"
    assert refused["pr_url"] == pr.url and refused["pr_number"] == pr.number
    event = next(e for e in reversed(sched.events.read()) if e["kind"] == "external_completion_refused")
    assert event["pr_url"] == pr.url and event["pr_number"] == pr.number
    assert len(fake_github.created) == created_before
    with pytest.raises(gitops.GitError):
        gitops.git("status", cwd=clone)


def test_tick_sweeps_stale_state_off_a_task_already_terminal(sched, fake_github):
    """CG-195: a task that reached done/cancelled/wont_do before `_transition` cleared these
    fields (or through a path that bypassed it, e.g. a hand-edited state.json) must not keep
    showing a decision forever — a plain tick sweeps every terminal task's stale needs_human,
    pending feedback and automerge stop, not just the moment of transition."""
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    sched.store.save(task)
    st = sched.state.get("DM-001")
    st["needs_human"] = {"kind": "stall", "reason": "revise round changed nothing", "at": "t"}
    st["pending_feedback"] = "- please fix the thing"
    st["automerge_blocked"] = "the PR checks rollup is pending"
    sched.state.save()
    sched.tick()
    st = sched.state.get("DM-001")
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert not st.get("automerge_blocked")


@pytest.mark.parametrize("terminal", [Status.DONE, Status.CANCELLED, Status.WONT_DO])
def test_terminal_task_retires_collected_check_without_losing_evidence(sched, terminal):
    """CG-386: a stale collected continuation cannot reopen a merged or otherwise closed task."""
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.cost_usd = 1.25
    run.result = {"checks": []}
    run.save()
    sched.state.get(task.id)["check_run"] = {
        "run_id": run.run_id, "stage": "base_probe", "cont": {}, "specs": [],
        "collected": True,
    }
    sched.state.save()

    sched._transition(task, terminal, "terminal lifecycle regression")
    assert not sched.state.get(task.id).get("check_run")
    assert sched.reap_check(sched.store.task(task.id), type("Report", (), {})()) is False
    assert sched.store.task(task.id).status == terminal
    preserved = sched._run_by_id(task, run.run_id)
    assert preserved is not None
    assert preserved.result == {"checks": []}
    assert preserved.cost_usd == 1.25


def test_check_collection_discards_legacy_continuation_for_terminal_task(sched):
    """A task made terminal outside `_transition` is still protected at collection time."""
    task = sched.store.task("DM-001")
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.result = {"checks": []}
    run.save()
    sched.state.get(task.id)["check_run"] = {
        "run_id": run.run_id, "stage": "base_probe", "cont": {}, "specs": [],
        "collected": True,
    }
    task.status = Status.DONE
    sched.store.save(task)
    sched.state.save()

    assert sched.reap_check(sched.store.task(task.id), type("Report", (), {})()) is True
    assert not sched.state.get(task.id).get("check_run")
    assert sched.store.task(task.id).status == Status.DONE
    assert not sched.state.get(task.id).get("needs_human")


def test_terminal_task_keeps_check_ownership_when_run_record_is_missing(sched):
    """Missing metadata cannot prove that the detached process behind it has stopped."""
    task = sched.store.task("DM-001")
    pointer = {"run_id": "missing-check-run", "stage": "base_probe", "collected": True}
    sched.state.get(task.id)["check_run"] = pointer
    sched.state.save()

    sched._transition(task, Status.DONE, "terminal with incomplete run history")

    assert sched.store.task(task.id).status == Status.DONE
    assert sched.state.get(task.id)["check_run"] == pointer


def test_terminal_task_stops_live_check_before_releasing_ownership(sched):
    """A real detached child is confirmed dead before its continuation is retired."""
    task = sched.store.task("DM-001")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        run = sched.runs.new_run(task.id, "local", mode="check")
        run.pid = child.pid
        run.save()
        sched.state.get(task.id)["check_run"] = {
            "run_id": run.run_id, "stage": "base_probe", "cont": {}, "specs": [],
        }
        sched.state.save()

        sched._transition(task, Status.CANCELLED, "terminal while check is live")

        child.wait(timeout=10)
        assert child.poll() is not None
        assert not sched.state.get(task.id).get("check_run")
        retired = sched._run_by_id(task, run.run_id)
        assert retired is not None
        assert retired.status == "cancelled"
        assert retired.error == "task reached terminal status"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_cancel_refuses_an_already_cancelled_task(sched):
    task = sched.store.task("DM-001")
    sched.cancel(task, "cancelled by hand")
    assert statuses(sched)["DM-001"] == "cancelled"
    with pytest.raises(RuntimeError, match=r"DM-001 is cancelled: cancelled by hand"):
        sched.retry(sched.store.task("DM-001"))
    assert statuses(sched)["DM-001"] == "cancelled"  # still cancelled, not reopened


# ---- approve refuses an incomplete brief (CG-193) ----


def _draft(sched, body, reading=None):
    t = sched.store.create_task("demo", "p1", "Needs a real brief", body,
                                reading=reading or [], status="draft")
    return sched.store.task(t.id)


def test_approve_refuses_placeholder_criteria(sched, fake_github):
    t = _draft(sched, "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] ...\n")
    ph = sched.store.phase("demo", "p1")
    with pytest.raises(RuntimeError, match="incomplete brief"):
        sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.DRAFT


def test_approve_refuses_unresolved_reading_path(sched, fake_github):
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _draft(sched, body, reading=["demo/p1/specs/nope.md"])
    ph = sched.store.phase("demo", "p1")
    with pytest.raises(RuntimeError, match="reading-list path not found"):
        sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.DRAFT


def test_approve_accepts_a_complete_brief(sched, fake_github):
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _draft(sched, body, reading=["demo/p1/specs/spec.md"])
    ph = sched.store.phase("demo", "p1")
    sched.approve(t, by="cli", phase=ph)
    assert sched.store.task(t.id).status == Status.READY


def test_inbox_approve_card_shows_the_gap(sched, fake_github):
    from garden.inbox import build_inbox

    _draft(sched, "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] ...\n")
    items = build_inbox(sched.store, sched)
    card = next(i for i in items if i["group"] == "approve" and i["title"] == "Needs a real brief")
    assert card["gaps"]
    assert "brief incomplete" in card["why"]
