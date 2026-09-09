"""Attention cards distinguish owner decisions from bounded operator recovery."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from garden.github import PRInfo
from garden.inbox import attention_view, build_inbox, needs_human_info
from garden.model import Status
from garden.runs import RunStore
from garden.scheduler import Scheduler, State
from garden.store import Store
from garden.web.app import create_app
from tests.conftest import FakeGitHub


def _set_state(root: Path, task_id: str, **keys) -> None:
    state = State(root / ".garden" / "state.json")
    st = state.get(task_id)
    for k, v in keys.items():
        st[k] = v
    state.save()


def _set_task(store: Store, task_id: str, status: Status, pr: str = "", log: str = "") -> None:
    t = store.task(task_id)
    t.status = status
    if pr:
        t.pr = pr
    if log:
        t.log(log)
    store.save(t)


def _attention(garden: Path, task_id: str) -> dict:
    store = Store(garden)
    sched = Scheduler(store, github=FakeGitHub(), log=lambda m: None)
    items = [i for i in build_inbox(store, sched) if i["group"] == "attention" and i["task"] == task_id]
    assert items, f"{task_id} should have an attention card"
    return items[0]


STOP = {"kind": "stall", "reason": "revise run 20260904T170828Z-revise produced no change to the diff",
        "prior_status": "in_review", "at": "2026-09-04T17:08:28+00:00"}


# ---------------------------------------------------------------- card content per kind


def test_stall_card_names_kind_evidence_and_button_effects(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human=dict(STOP), revisions=2,
               last_review={"verdict": "approve", "summary": "clean and mergeable"},
               pr_state="OPEN", review_decision="APPROVED", checks="SUCCESS")
    it = _attention(garden, "DM-001")
    assert it["kind"] == "stall"
    assert it["why"].startswith("The loop stalled — ")
    assert "produced no change" in it["reason"]
    # evidence: review, PR state, revisions
    ev = "\n".join(it["evidence"])
    assert "last automated review: approve — clean and mergeable" in ev
    assert "PR: open · review approved · CI success" in ev
    assert "2 revision round(s) used" in ev
    # every button explains its effect
    assert all(a.get("detail") for a in it["actions"])
    labels = [a["label"] for a in it["actions"]]
    assert "Nothing to fix, resume" not in labels and "Continue the loop" not in labels
    assert "Send outstanding work to a worker" in labels
    assert "Discuss" in labels and "Cancel" in labels and "Open PR" in labels
    retry = next(a for a in it["actions"] if a["kind"] == "retry")
    assert "keeps the PR" in retry["detail"] and "revise" in retry["detail"]
    assert it["owner"] == "you" and it["user_decision"] is True


def test_revision_cap_card(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "revision_cap", "reason": "3 revision rounds used",
                                              "prior_status": "in_review", "at": "2026-09-04T00:00:00+00:00"})
    it = next(item for item in build_inbox(Store(garden), Scheduler(Store(garden), github=FakeGitHub()))
              if item["task"] == "DM-001" and item["group"] == "attention")
    assert it["kind"] == "revision_cap"
    assert it["kind_title"] == "Revision cap reached"
    assert "3 revision rounds used" in it["why"]
    assert it["owner"] == "you" and it["user_decision"] is True
    assert it["recommendation"] == "Authorize one more bounded revision"
    assert next(action for action in it["actions"] if action["kind"] == "retry")["label"] == "Authorize one more revision"
    html = TestClient(create_app(Store(garden), watch=False)).get("/").text
    assert "Needs your decision: Revision cap reached" in html
    assert "Your decision</dt><dd>Required" in html
    assert "Authorize one more revision" in html


def test_delegated_revision_cap_card_is_operator_owned(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "revision_cap", "reason": "3 revision rounds used",
                                              "delegated_recovery": True})

    it = next(item for item in build_inbox(Store(garden), Scheduler(Store(garden), github=FakeGitHub()))
              if item["task"] == "DM-001" and item["group"] == "operator")

    assert it["owner"] == "implementation worker" and it["user_decision"] is False
    assert next(action for action in it["actions"] if action["kind"] == "recover")["label"] == "Send failures to the worker"
    html = TestClient(create_app(Store(garden), watch=False)).get("/").text
    assert "Operator recovery: Revision cap reached" in html
    assert "Your decision</dt><dd>Not required" in html


def test_troubled_card_is_distinct_and_offers_bounded_decisions(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    runs = RunStore(garden / ".garden")
    run = runs.new_run("DM-001", "local", mode="revise")
    run.status = "done"
    run.pushed_head = "abcdef1234567890"
    run.diff_stat = " 3 files changed, 12 insertions(+), 2 deletions(-)"
    run.save()
    _set_state(garden, "DM-001", needs_human={"kind": "troubled_task", "reason": "6 substantive revisions did not converge"},
               substantive_revisions=6, revisions=6, review_rounds=4,
               review_feedback_history=["parser still drops rows", "parser still drops rows"],
               troubled={"recommendation": "investigate the parser boundary"},
               difficulty_escalations=[{"from": "easy", "to": "medium", "counter": 2, "model": "terra"}])
    it = _attention(garden, "DM-001")
    assert it["kind_title"] == "Troubled task"
    assert {a["kind"] for a in it["actions"]} >= {
        "troubled-continue", "investigate", "change-approach", "defer", "troubled-cancel",
    }
    evidence = "\n".join(it["evidence"])
    assert "6 revision" in evidence and "4 automated review" in evidence
    assert "easy → medium" in evidence and "current owner" in evidence
    assert "head abcdef123456" in evidence and "3 files changed" in evidence
    assert "repeated finding (2 reviews): parser still drops rows" in evidence
    assert "recommended next action: investigate the parser boundary" in evidence


def test_inbox_can_request_an_agent_investigation(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001",
               needs_human={"kind": "troubled_task", "reason": "repeated reviews did not converge"},
               substantive_revisions=4)
    client = TestClient(create_app(Store(garden), watch=False))

    page = client.get("/inbox")
    form = re.search(r'<form[^>]+action="/tasks/DM-001/investigate".*?</form>',
                     page.text, re.S)
    assert form is not None
    assert re.findall(r'<option value="([^"]+)">', form.group()) == ["operator", "agent"]
    assert "Request investigation agent" in form.group()

    response = client.post("/tasks/DM-001/investigate", data={
        "note": "diagnose the repeated review finding",
        "applies_to": "agent",
    }, follow_redirects=False)
    assert response.status_code == 303
    investigation = State(garden / ".garden" / "state.json").get("DM-001")["investigation"]
    assert investigation["owner"] == "agent"
    assert investigation["status"] == "requested"
    assert investigation["reason"] == "diagnose the repeated review finding"


def test_investigation_report_card_is_readable_and_actions_are_explicit(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    report = {"likely_cause": "stale verifier", "confidence": "high", "unknowns": ["remote image"],
              "evidence": ["base passes"], "attempted_checks": ["focused comparison"],
              "retain_work": True, "alternatives": ["repair verifier"],
              "recommendation": "repair environment/verification",
              "links": ["https://example.com/evidence/7"]}
    _set_state(garden, "DM-001",
               needs_human={"kind": "investigation_report", "reason": "report ready"},
               investigation={"status": "report_ready", "owner": "agent", "report": report})
    it = _attention(garden, "DM-001")
    evidence = "\n".join(it["evidence"])
    assert "likely cause (high confidence): stale verifier" in evidence
    assert "investigation recommendation: repair environment/verification" in evidence
    assert "unknowns: remote image" in evidence
    assert "evidence: base passes" in evidence
    assert "attempted checks: focused comparison" in evidence
    assert "retain earlier work: yes" in evidence
    assert "alternatives and tradeoffs: repair verifier" in evidence
    assert "evidence links: https://example.com/evidence/7" in evidence
    assert {a["kind"] for a in it["actions"]} >= {
        "troubled-continue", "change-approach", "investigate", "defer", "troubled-cancel",
    }
    client = TestClient(create_app(Store(garden), watch=False))
    response = client.post("/tasks/DM-001/investigate",
                           data={"note": "check the replacement verifier", "applies_to": "agent"},
                           follow_redirects=False)
    assert response.status_code == 303
    persisted = State(garden / ".garden" / "state.json").get("DM-001")
    assert persisted["investigation_history"][-1]["status"] == "report_ready"
    assert persisted["investigation"]["status"] == "requested"
    assert persisted["investigation"]["reason"] == "check the replacement verifier"


@pytest.mark.parametrize("current,selected,options", [
    ("easy", "medium", ["easy", "medium", "hard"]),
    ("medium", "hard", ["medium", "hard"]),
    ("hard", "hard", ["hard"]),
])
def test_web_troubled_continue_selects_same_or_higher_difficulty(garden, current, selected, options):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    task = store.task("DM-001")
    task.difficulty = current
    store.save(task)
    _set_state(garden, "DM-001", needs_human={"kind": "troubled_task", "reason": "choose next step"},
               substantive_revisions=6, pending_feedback="preserve the existing finding")
    client = TestClient(create_app(Store(garden), watch=False))

    for page in ("/inbox", "/tasks/DM-001"):
        response = client.get(page)
        assert response.status_code == 200
        form = re.search(r'<form[^>]+action="/tasks/DM-001/troubled-continue".*?</form>',
                         response.text, re.S)
        assert form is not None
        assert re.findall(r'<option value="([^"]+)"', form.group()) == options
        assert f"Continue at {current}" in form.group()
        if selected != current:
            assert f"Escalate to {selected}" in form.group()

    if current != "easy":
        refused = client.post("/tasks/DM-001/troubled-continue", data={"applies_to": "easy"},
                              follow_redirects=False)
        assert refused.status_code == 303 and "preserve+or+raise" in refused.headers["location"]
        assert Store(garden).task("DM-001").difficulty == current
        assert State(garden / ".garden" / "state.json").get("DM-001")["needs_human"]

    response = client.post("/tasks/DM-001/troubled-continue", data={"applies_to": selected},
                           follow_redirects=False)
    assert response.status_code == 303
    assert Store(garden).task("DM-001").difficulty == selected
    persisted = State(garden / ".garden" / "state.json").get("DM-001")
    assert persisted["revision_allowance"] == 1
    assert not persisted.get("needs_human")
    assert persisted["troubled_decisions"][-1]["difficulty"] == selected
    assert persisted["pending_feedback"] == "preserve the existing finding"


def test_served_operator_report_failure_recovery_and_explicit_followup(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    task = store.task("DM-001")
    task.branch = "garden/preserved"
    store.save(task)
    _set_state(garden, "DM-001", needs_human={"kind": "investigation", "reason": "diagnose"},
               investigation={"status": "active", "owner": "operator", "request_id": "served"},
               substantive_revisions=6, pending_feedback="retain this finding")
    client = TestClient(create_app(Store(garden), watch=False))

    failed = client.post("/tasks/DM-001/investigation-report",
                         data={"likely_cause": "stale fixture"}, follow_redirects=False)
    assert failed.status_code == 303 and "confidence+is+required" in failed.headers["location"]
    state = State(garden / ".garden" / "state.json").get("DM-001")
    assert state["investigation"]["status"] == "active"

    corrected = client.post("/tasks/DM-001/investigation-report", data={
        "likely_cause": "stale fixture", "confidence": "high", "unknowns": "remote image",
        "evidence": "base passes\n/runs/served", "attempted_checks": "focused comparison",
        "retain_work": "true", "alternatives": "repair fixture", "recommendation": "repair environment/verification",
        "links": "https://example.com/evidence/7",
    }, follow_redirects=False)
    assert corrected.status_code == 303
    page = client.get("/").text
    assert "Investigation report ready" in page and "stale fixture" in page
    task = Store(garden).task("DM-001")
    assert task.status == Status.CHANGES_REQUESTED and task.branch == "garden/preserved"
    assert not RunStore(garden / ".garden").active()

    followed = client.post("/tasks/DM-001/troubled-change-approach",
                           data={"note": "repair the fixture, then rerun the focused check"},
                           follow_redirects=False)
    assert followed.status_code == 303
    state = State(garden / ".garden" / "state.json").get("DM-001")
    assert "repair the fixture" in state["pending_feedback"]
    assert not state.get("needs_human") and not RunStore(garden / ".garden").active()


def test_web_troubled_actions_preserve_work_and_reject_stale_clicks(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    task = store.task("DM-001")
    task.branch = "garden/preserved"
    store.save(task)
    _set_state(garden, "DM-001",
               needs_human={"kind": "troubled_task", "reason": "not converging"},
               pending_feedback="keep finding", substantive_revisions=6)
    c = TestClient(create_app(Store(garden), watch=False))
    page = c.get("/").text
    assert "Troubled task" in page
    assert 'action="/tasks/DM-001/troubled-change-approach"' in page
    assert 'action="/tasks/DM-001/troubled-cancel"' in page
    assert "Required cancellation reason" in page

    response = c.post("/tasks/DM-001/troubled-change-approach",
                      data={"note": "replace parser without changing API"},
                      follow_redirects=False)
    assert response.status_code == 303
    state = State(garden / ".garden" / "state.json").get("DM-001")
    assert "replace parser" in state["pending_feedback"]
    assert Store(garden).task("DM-001").branch == "garden/preserved"

    stale = c.post("/tasks/DM-001/troubled-cancel", data={"note": "stale click"},
                   follow_redirects=False)
    assert stale.status_code == 303 and "no+troubled-task+decision" in stale.headers["location"]
    assert Store(garden).task("DM-001").status == Status.CHANGES_REQUESTED


def test_inbox_defer_requires_reason_and_uses_troubled_defer_action(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001",
               needs_human={"kind": "troubled_task", "reason": "not converging"},
               substantive_revisions=6)
    client = TestClient(create_app(Store(garden), watch=False))

    page = client.get("/inbox").text
    form = re.search(r'<form[^>]+action="/tasks/DM-001/troubled-defer".*?</form>', page, re.S)
    assert form is not None
    assert 'name="note"' in form.group() and "required" in form.group()

    response = client.post(
        "/tasks/DM-001/troubled-defer",
        data={"note": "wait for the upstream parser release"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    state = State(garden / ".garden" / "state.json").get("DM-001")
    assert state["troubled_deferred"]["reason"] == "wait for the upstream parser release"
    assert not state.get("investigation")


def test_parent_closed_card(garden):
    store = Store(garden)
    _set_task(store, "DM-002", Status.IN_REVIEW, pr="https://example.com/pull/8")
    _set_state(garden, "DM-002", needs_human={"kind": "parent_closed", "reason": "stack parent DM-001 was closed without merging",
                                              "prior_status": "in_review", "at": "2026-09-04T00:00:00+00:00"})
    it = next(item for item in build_inbox(Store(garden), Scheduler(Store(garden), github=FakeGitHub()))
              if item["task"] == "DM-002" and item["group"] == "operator")
    assert it["kind"] == "parent_closed"
    assert it["kind_title"] == "Stack parent closed"
    assert "DM-001" in it["reason"]


def test_worker_failed_card_no_resume(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.FAILED, log="attempt 2 failed: worker exited 1: error_max_turns; giving up")
    it = next(item for item in build_inbox(Store(garden), Scheduler(Store(garden), github=FakeGitHub()))
              if item["task"] == "DM-001" and item["group"] == "operator")
    assert it["kind"] == "worker_failed"
    assert "error_max_turns" in it["reason"]
    kinds = [a["kind"] for a in it["actions"]]
    assert "resume" not in kinds, "a real failure has something to fix; resume is not offered"
    assert all(a.get("detail") for a in it["actions"])
    retry = next(a for a in it["actions"] if a["kind"] == "retry")
    assert "fresh work run" in retry["detail"]


def test_env_error_card(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.FAILED, log="dispatch failed: runner not found")
    it = next(item for item in build_inbox(Store(garden), Scheduler(Store(garden), github=FakeGitHub()))
              if item["task"] == "DM-001" and item["group"] == "operator")
    assert it["kind"] == "env_error"
    assert it["kind_title"] == "The garden hit an environment error"


def test_deployment_prerequisite_is_an_operator_recovery_card(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.FAILED, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "deployment", "reason": "deploy the verified build to the staging host",
                                              "prior_status": "in_review", "at": "2026-09-04T00:00:00+00:00"})
    store = Store(garden)
    sched = Scheduler(store, github=FakeGitHub(), log=lambda m: None)
    it = next(item for item in build_inbox(store, sched)
              if item["group"] == "operator" and item["task"] == "DM-001")
    assert it["kind_title"] == "Deployment prerequisite"
    assert it["category"] == "Operational prerequisite"
    assert it["owner"] == "operator" and it["user_decision"] is False
    assert "deploy the verified build" in it["reason"]
    assert "operational work" in it["kind_blurb"]
    assert next(a for a in it["actions"] if a["kind"] == "resume")["label"] == "Deployment completed — continue"


def test_legacy_string_needs_human_normalizes():
    assert needs_human_info("3 revision rounds used")["kind"] == "revision_cap"
    assert needs_human_info("stack parent DM-001 was closed without merging")["kind"] == "parent_closed"
    assert needs_human_info("revise run x produced no change to the diff")["kind"] == "stall"
    assert needs_human_info(None) is None
    assert needs_human_info({"kind": "stall", "reason": "r", "prior_status": "in_review"})["prior_status"] == "in_review"


# ---------------------------------------------------------------- discuss


def test_discuss_prompt_has_task_reason_pr_and_run_ids(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    rs = RunStore(garden / ".garden")
    run = rs.new_run("DM-001", "local", mode="revise")
    run.status = "done"
    run.save()
    _set_state(garden, "DM-001", needs_human=dict(STOP))
    it = _attention(garden, "DM-001")
    prompt = it["discuss"]
    assert "DM-001" in prompt and "First task" in prompt
    assert STOP["reason"] in prompt
    assert "https://example.com/pull/7" in prompt
    assert run.run_id in prompt
    assert "garden retry DM-001" in prompt


# ---------------------------------------------------------------- nothing to fix, resume


def test_resume_returns_task_to_prior_state_without_a_run(sched):
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    t.pr = "https://example.com/pull/7"
    sched.store.save(t)
    st = sched.state.get(t.id)
    st["needs_human"] = dict(STOP)
    st["pending_feedback"] = "- old feedback that needs no action"
    sched.state.save()
    sched.resume_task(t)
    assert t.status == Status.IN_REVIEW
    st = State(sched.state.path).get(t.id)
    assert not st.get("needs_human")
    assert not st.get("pending_feedback")
    assert sched.runs.runs_for(t.id) == [], "resume must not start a run"
    rep = sched.tick()
    assert "DM-001(revise)" not in rep.dispatched


def test_resume_without_prior_status_uses_pr_state(sched):
    t = sched.store.task("DM-001")
    t.status = Status.CHANGES_REQUESTED
    t.pr = "https://example.com/pull/7"
    sched.store.save(t)
    st = sched.state.get(t.id)
    st["needs_human"] = "legacy string reason"
    st["pr_draft"] = True
    sched.state.save()
    sched.resume_task(t)
    assert t.status == Status.AWAITING_TRIAGE


def test_resume_parent_closed_keeps_status(sched):
    t = sched.store.task("DM-002")
    t.status = Status.IN_REVIEW
    t.pr = "https://example.com/pull/8"
    sched.store.save(t)
    st = sched.state.get(t.id)
    st["needs_human"] = {"kind": "parent_closed", "reason": "stack parent DM-001 was closed without merging",
                         "prior_status": "in_review", "at": "2026-09-04T00:00:00+00:00"}
    sched.state.save()
    sched.resume_task(t)
    assert t.status == Status.IN_REVIEW
    assert not State(sched.state.path).get(t.id).get("needs_human")


# ---------------------------------------------------------------- web


def test_web_task_page_and_inbox_show_attention_and_resume_works(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human=dict(STOP))
    c = TestClient(create_app(Store(garden), watch=False))
    page = c.get("/tasks/DM-001").text
    assert "Needs your decision: The loop stalled" in page
    assert "Send outstanding work to a worker" in page
    assert "Nothing to fix, resume" not in page
    assert "Discuss" in page and "Copy prompt" in page
    inbox = c.get("/").text
    assert "The loop stalled" in inbox and "Your decision</dt><dd>Required" in inbox


def test_evidence_includes_diff_summary(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED, pr="https://example.com/pull/7")
    rs = RunStore(garden / ".garden")
    run = rs.new_run("DM-001", "local", mode="work")
    run.status = "done"
    run.diff_stat = " worker-output.txt | 1 +\n 1 file changed, 1 insertion(+)\n"
    run.save()
    _set_state(garden, "DM-001", needs_human=dict(STOP))
    it = _attention(garden, "DM-001")
    ev = "\n".join(it["evidence"])
    assert "1 file changed, 1 insertion(+)" in ev


def test_attention_view_none_for_quiet_task(garden):
    store = Store(garden)
    t = store.task("DM-001")
    assert attention_view(t, {}, None) is None


def test_interrupted_check_keeps_source_and_review_blockers_separate(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.IN_REVIEW, pr="https://example.com/pull/7")
    runs = RunStore(garden / ".garden")
    run = runs.new_run("DM-001", "local", mode="check")
    run.status = "timeout"
    run.error = "validation supervisor timed out"
    run.save()
    _set_state(
        garden,
        "DM-001",
        needs_human={"kind": "check_did_not_run", "reason": "lint timed out twice", "run": run.run_id},
        pending_feedback="CI tests fail in src/example.py",
        checks="FAILURE",
        last_review={"verdict": "changes_requested", "summary": "Fix the source failure"},
        head_sha="abcdef123456",
        recovery_check={"stage": "ci", "specs": [{"name": "lint", "command": "ruff check"}], "cont": {}},
    )

    sched = Scheduler(Store(garden), github=FakeGitHub(), log=lambda _message: None)
    card = next(item for item in build_inbox(sched.store, sched) if item["task"] == "DM-001")
    assert card["group"] == "operator"
    assert card["owner"] == "operator" and card["user_decision"] is False
    assert [blocker["category"] for blocker in card["blockers"]] == [
        "Interrupted check", "Source or CI failure", "Review findings"]
    investigate = next(action for action in card["actions"] if action["kind"] == "investigate")
    assert investigate["label"] == "Investigate missing check provenance"
    assert not {"recover", "recover-check"} & {action["kind"] for action in card["actions"]}
    assert not {"resume", "retry"} & {action["kind"] for action in card["actions"]}
    html = TestClient(create_app(Store(garden), watch=False)).get("/").text
    assert "Your decision</dt><dd>Not required" in html
    assert f'/runs/DM-001/{run.run_id}' in html and "Current PR at abcdef12" in html


def test_plain_interrupted_check_renders_and_runs_only_preserved_retry(garden, monkeypatch):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED)
    _set_state(garden, "DM-001", needs_human={"kind": "check_did_not_run", "reason": "check timed out twice"},
               recovery_check={"stage": "pre_pr", "specs": [{"name": "unit", "command": "pytest"}], "cont": {}})
    sched = Scheduler(Store(garden), github=FakeGitHub(), log=lambda _message: None)
    card = next(item for item in build_inbox(sched.store, sched) if item["task"] == "DM-001")
    assert card["group"] == "operator" and len(card["blockers"]) == 1
    assert card["recommendation"] == "Retry the interrupted check"
    recovery_actions = [action["kind"] for action in card["actions"]
                        if action["kind"] in {"recover", "recover-check", "resume", "retry"}]
    assert recovery_actions == ["recover"]

    launches = []
    monkeypatch.setattr(
        Scheduler, "_dispatch_check_run",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    client = TestClient(create_app(Store(garden), watch=False))
    page = client.get("/").text
    assert "Retry the interrupted check" in page
    assert 'action="/tasks/DM-001/recover"' in page
    assert 'action="/tasks/DM-001/recover-check"' not in page

    response = client.post("/tasks/DM-001/recover")
    assert response.status_code == 200
    assert len(launches) == 1
    assert launches[0][1]["specs"] == [{"name": "unit", "command": "pytest"}]
    state = State(garden / ".garden" / "state.json").get("DM-001")
    assert not state.get("needs_human") and not state.get("recovery_check")


def test_stale_successful_check_stop_can_be_cleared_without_rerunning(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.IN_REVIEW, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "check_did_not_run", "reason": "older timeout"},
               checks="SUCCESS", head_sha="abcdef123456",
               recovery_check={"source_head": "abcdef123456"})
    sched = Scheduler(Store(garden), github=FakeGitHub(), log=lambda _message: None)
    sched.github.prs["garden/test"] = PRInfo(
        number=7, url="https://example.com/pull/7", state="OPEN", head_sha="abcdef123456"
    )
    card = next(item for item in build_inbox(sched.store, sched) if item["task"] == "DM-001")
    assert card["category"] == "Stale bookkeeping"
    recover = next(action for action in card["actions"] if action["kind"] == "recover-check")
    assert recover["label"] == "Recover check and resume pipeline"
    client = TestClient(create_app(Store(garden), watch=False))
    page = client.get("/").text
    assert "Recover check and resume pipeline" in page and "stale" in page.lower()

    assert sched.recover_waiting_check(sched.store.task("DM-001")) == (
        "stale check metadata cleared; restored in_review"
    )
    state = sched.state.get("DM-001")
    assert not state.get("needs_human")
    assert Store(garden).task("DM-001").status == Status.IN_REVIEW


def test_stale_successful_check_stop_refuses_a_changed_live_head(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.IN_REVIEW, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "check_did_not_run", "reason": "older timeout"},
               checks="SUCCESS", head_sha="old-head",
               recovery_check={"source_head": "old-head"})
    sched = Scheduler(Store(garden), github=FakeGitHub(), log=lambda _message: None)
    sched.github.prs["garden/test"] = PRInfo(
        number=7, url="https://example.com/pull/7", state="OPEN", head_sha="new-head"
    )

    with pytest.raises(RuntimeError, match="different PR head"):
        sched.recover_waiting_check(sched.store.task("DM-001"))

    state = sched.state.get("DM-001")
    assert state["needs_human"]["reason"] == "older timeout"
    assert state["checks"] == "SUCCESS"


def test_stale_successful_check_stop_refuses_mutable_head_without_check_provenance(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.IN_REVIEW, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "check_did_not_run", "reason": "older timeout"},
               checks="SUCCESS", head_sha="abcdef123456")
    sched = Scheduler(Store(garden), github=FakeGitHub(), log=lambda _message: None)
    sched.github.prs["garden/test"] = PRInfo(
        number=7, url="https://example.com/pull/7", state="OPEN", head_sha="abcdef123456"
    )

    with pytest.raises(RuntimeError, match="no recorded source head"):
        sched.recover_waiting_check(sched.store.task("DM-001"))

    state = sched.state.get("DM-001")
    assert state["needs_human"]["reason"] == "older timeout"
    assert state["checks"] == "SUCCESS"
    assert Store(garden).task("DM-001").status == Status.IN_REVIEW


def test_stale_successful_check_without_provenance_renders_investigation_not_recovery(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.IN_REVIEW, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001",
               needs_human={"kind": "check_did_not_run", "reason": "older timeout"},
               checks="SUCCESS", head_sha="abcdef123456")

    sched = Scheduler(Store(garden), github=FakeGitHub(), log=lambda _message: None)
    card = next(item for item in build_inbox(sched.store, sched)
                if item["task"] == "DM-001")
    assert card["group"] == "operator"
    assert card["category"] == "Interrupted check · provenance unavailable"
    assert card["owner"] == "operator" and card["user_decision"] is False
    assert card["recommendation"] == "Investigate and repair the stopped check record"
    assert card["evidence"][0].startswith("check provenance unavailable:")
    assert [action["kind"] for action in card["actions"] if action["kind"] != "link"] == [
        "investigate", "cancel"]

    client = TestClient(create_app(Store(garden), watch=False))
    page = client.get("/").text
    assert "Investigate missing check provenance" in page
    assert "no immutable source head was recorded" in page
    assert "Your decision</dt><dd>Not required" in page
    assert 'action="/tasks/DM-001/recover-check"' not in page
    assert 'action="/tasks/DM-001/recover"' not in page
    assert 'action="/tasks/DM-001/investigate"' in page

    response = client.post(
        "/tasks/DM-001/investigate",
        data={"note": "Recover immutable check provenance", "applies_to": "operator"},
    )
    assert response.status_code == 200
    state = State(Store(garden).config.garden_dir / "state.json").get("DM-001")
    assert state["investigation"]["status"] == "requested"
    assert state["investigation"]["owner"] == "operator"
    assert state["investigation"]["origins"] == {
        "stop_kind": "check_did_not_run",
        "stop_reason": "older timeout",
        "stop_run": "",
    }
    page = response.text
    assert "Operator recovery: Investigation requested" in page
    assert "investigation requested · owner operator" in page
    assert "Your decision</dt><dd>Not required" in page
    assert 'action="/tasks/DM-001/investigation-take"' in page
    assert 'action="/tasks/DM-001/troubled-continue"' not in page
    assert 'action="/tasks/DM-001/troubled-change-approach"' not in page
    assert 'action="/tasks/DM-001/troubled-defer"' not in page


def test_explicit_hold_and_real_question_name_the_correct_owner(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.IN_REVIEW, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "deployment", "reason": "owner hold: wait for launch approval"})
    _set_task(store, "DM-002", Status.WAITING_HUMAN)
    _set_state(garden, "DM-002", question="Should this public API remain compatible?")
    card = _attention(garden, "DM-001")
    assert card["kind"] == "explicit_hold"
    assert card["owner"] == "you" and card["user_decision"] is True
    action = next(action for action in card["actions"] if action["kind"] == "resume")
    assert action["label"] == "Authorize held step and continue"
    client = TestClient(create_app(Store(garden), watch=False))
    html = client.get("/").text
    assert "Needs your decision: Owner authorization required" in html
    assert "Authorize the held step, or leave it paused" in html
    assert "Your decision</dt><dd>Required" in html
    assert "Authorize held step and continue" in html
    assert "Should this public API remain compatible?" in html
    assert "Answer and resume" in html
    response = client.post("/tasks/DM-001/resume", follow_redirects=False)
    assert response.status_code == 303
    assert not State(garden / ".garden" / "state.json").get("DM-001").get("needs_human")
    assert Store(garden).task("DM-001").status == Status.IN_REVIEW
