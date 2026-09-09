"""Attention cards distinguish owner decisions from bounded operator recovery."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

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
              if item["task"] == "DM-001" and item["group"] == "operator")
    assert it["kind"] == "revision_cap"
    assert it["kind_title"] == "Revision cap reached"
    assert "3 revision rounds used" in it["why"]


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
    assert next(action for action in card["actions"] if action["kind"] == "recover")["label"] == "Retry the interrupted check"
    assert not {"resume", "retry"} & {action["kind"] for action in card["actions"]}
    html = TestClient(create_app(Store(garden), watch=False)).get("/").text
    assert "Your decision</dt><dd>Not required" in html
    assert f'/runs/DM-001/{run.run_id}' in html and "Current PR at abcdef12" in html


def test_plain_interrupted_check_is_operator_owned(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.CHANGES_REQUESTED)
    _set_state(garden, "DM-001", needs_human={"kind": "check_did_not_run", "reason": "check timed out twice"},
               recovery_check={"stage": "pre_pr", "specs": [{"name": "unit", "command": "pytest"}], "cont": {}})
    sched = Scheduler(Store(garden), github=FakeGitHub(), log=lambda _message: None)
    card = next(item for item in build_inbox(sched.store, sched) if item["task"] == "DM-001")
    assert card["group"] == "operator" and len(card["blockers"]) == 1
    assert card["recommendation"] == "Retry the interrupted check"


def test_stale_successful_check_stop_can_be_cleared_without_rerunning(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.IN_REVIEW, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "check_did_not_run", "reason": "older timeout"},
               checks="SUCCESS", head_sha="abcdef123456")
    sched = Scheduler(Store(garden), github=FakeGitHub(), log=lambda _message: None)
    card = next(item for item in build_inbox(sched.store, sched) if item["task"] == "DM-001")
    assert card["category"] == "Stale bookkeeping"
    assert next(action for action in card["actions"] if action["kind"] == "recover-check")["label"] == "Clear resolved stop"
    client = TestClient(create_app(Store(garden), watch=False))
    page = client.get("/").text
    assert "Clear resolved stop" in page and "stale" in page.lower()

    response = client.post("/tasks/DM-001/recover-check", follow_redirects=False)

    assert response.status_code == 303
    state = State(garden / ".garden" / "state.json").get("DM-001")
    assert not state.get("needs_human")
    assert Store(garden).task("DM-001").status == Status.IN_REVIEW


def test_explicit_hold_and_real_question_name_the_correct_owner(garden):
    store = Store(garden)
    _set_task(store, "DM-001", Status.IN_REVIEW, pr="https://example.com/pull/7")
    _set_state(garden, "DM-001", needs_human={"kind": "deployment", "reason": "owner hold: wait for launch approval"})
    _set_task(store, "DM-002", Status.WAITING_HUMAN)
    _set_state(garden, "DM-002", question="Should this public API remain compatible?")
    html = TestClient(create_app(Store(garden), watch=False)).get("/").text
    assert "Complete the named deployment step" in html
    assert "Your decision</dt><dd>Not required" in html
    assert "Should this public API remain compatible?" in html
    assert "Answer and resume" in html
