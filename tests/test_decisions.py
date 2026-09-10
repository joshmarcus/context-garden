"""A worker's wont_do / no_change is a decision for the person, not a failure (CG-100)."""

import os
import threading
import time

import pytest

from garden.github import Feedback, PRInfo
from garden.inbox import build_inbox
from garden.model import Status
from garden.scheduler.report import TickReport
from garden.scheduler.state import State
from tests.conftest import FakeGitHub
from tests.reference_context import agent_context


def statuses(sched):
    sched.store.invalidate()
    return {tid: t.status.value for tid, t in sched.store.tasks().items()}


def _to_decision(sched, mode, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    sched.tick()
    sched.tick()


# ---- wont_do -----------------------------------------------------------------
def test_wont_do_pauses_as_a_decision(sched, fake_github, monkeypatch):
    _to_decision(sched, "wont_do", monkeypatch)
    assert statuses(sched)["DM-001"] == "waiting_human"
    dec = sched.state.get("DM-001")["decision"]
    assert dec["kind"] == "wont_do" and "duplicates DM-002" in dec["reason"]
    # the worker's full final message is kept for the card and the task page
    assert "I do not think this should be done" in dec["final"]
    # it shows as a decision card, not a question
    item = next(i for i in build_inbox(sched.store, sched) if i["task"] == "DM-001")
    assert item["group"] == "decision" and "duplicates DM-002" in item["why"]
    assert item["final"] and any(a["kind"] == "accept" for a in item["actions"])


def test_wont_do_accept_ends_the_task(sched, fake_github, monkeypatch):
    _to_decision(sched, "wont_do", monkeypatch)
    sched.accept_decision(sched.store.task("DM-001"), note="agreed")
    t = sched.store.task("DM-001")
    assert t.status == Status.WONT_DO and t.status.terminal
    assert "won't do" in t.body and "agreed" in t.body
    # counted in neither done, failed nor the inbox
    assert not any(i["task"] == "DM-001" for i in build_inbox(sched.store, sched))


def test_wont_do_closes_the_open_pr(sched, fake_github):
    # a task that already reached a PR, then a person rules it won't be done
    sched.tick()
    sched.tick()
    t = sched.store.task("DM-001")
    assert t.status == Status.IN_REVIEW and t.pr
    number = sched._pr_number(t)
    sched.mark_wont_do(t, reason="superseded by DM-002")
    t = sched.store.task("DM-001")
    assert t.status == Status.WONT_DO
    assert number in fake_github.closed
    assert any("superseded by DM-002" in c for c in fake_github.comments)


def test_wont_do_reject_carries_the_note_into_a_revise(sched, fake_github, monkeypatch):
    _to_decision(sched, "wont_do", monkeypatch)
    sched.reject_decision(sched.store.task("DM-001"), "No, this is still needed; please implement it.")
    assert statuses(sched)["DM-001"] == "changes_requested"
    fb = sched.state.get("DM-001")["pending_feedback"]
    assert "The person disagrees" in fb and "still needed" in fb
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(revise)"]
    brief = agent_context(sched.runs.latest("DM-001"))
    assert "The person disagrees" in brief and "still needed" in brief
    # the revise round finishes normally -> a PR
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"


# ---- no_change ---------------------------------------------------------------
def test_satisfied_no_change_reconciles_without_a_human_decision(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_change")
    sched.tick()
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "tweak", "created": "2099-01-01T00:00:00Z"}])
    rep = sched.tick()  # -> changes_requested + revise dispatched
    assert rep.dispatched == ["DM-001(revise)"]
    rep = sched.tick()  # reap no_change: unchanged head goes through checks/review itself
    assert statuses(sched)["DM-001"] == "in_review"
    assert "DM-001 no-change -> verification" in rep.transitions
    assert not sched.state.get("DM-001").get("decision")
    assert not sched.state.get("DM-001").get("needs_human")


def test_accept_revise_no_change_queues_review_without_waiting_question(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_change_decision")
    sched.cfg.data["review"] = {"enabled": False, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    sched.tick()  # initial work opens the PR and queues review
    task = sched.store.task("DM-001")
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh",
                                                        "body": "tweak", "created": "2099-01-01T00:00:00Z"}])
    sched.tick()  # poll feedback -> changes_requested
    sched.tick()  # dispatch revise
    sched.tick()  # reap revise no_change -> waiting_human decision
    assert sched.store.task("DM-001").status == Status.WAITING_HUMAN
    sched.cfg.data["review"]["enabled"] = True
    sched.accept_decision(sched.store.task("DM-001"), note="the existing code is correct")
    task = sched.store.task("DM-001")
    assert task.status == Status.IN_REVIEW
    assert sched.state.get(task.id).get("review_run")
    assert not sched.state.get(task.id).get("question")
    assert not any(item["task"] == task.id and item["group"] == "question"
                   for item in build_inbox(sched.store, sched))


def test_accept_no_change_without_pr_keeps_detached_check_out_of_inbox(sched, fake_github, monkeypatch):
    """An accepted no-change on a pre-PR revise round is pipeline work while its check runs,
    not an unanswered human stop."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_change_decision")
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "unit", "command": "true"}], "ci": []}
    fake_github.available = False
    sched.tick()
    sched.tick()  # reap initial work and start its detached pre-PR check
    sched.tick()  # initial check passes; branch has no PR
    task = sched.store.task("DM-001")
    assert task.status == Status.IN_REVIEW and not task.pr
    st = sched.state.get(task.id)
    st["pending_feedback"] = "- revise the branch"
    sched._transition(task, Status.CHANGES_REQUESTED, "test: queue a revise round")
    assert sched.state.get(task.id)["pending_feedback"]
    assert [(candidate.id, mode) for candidate, mode, _ in sched.dispatch_queue()] == [(task.id, "revise")]
    sched.dispatch(task, mode="revise", runner=sched.runner_for(task))
    sched.tick()  # reap revise no_change -> waiting_human decision
    assert sched.store.task(task.id).status == Status.WAITING_HUMAN

    sched.accept_decision(sched.store.task(task.id), note="the branch is already correct")

    task = sched.store.task(task.id)
    assert task.status == Status.CHANGES_REQUESTED
    assert sched.state.get(task.id).get("check_run")
    inbox = build_inbox(sched.store, sched)
    assert not any(item["task"] == task.id and "no question" in item["why"].lower()
                   for item in inbox)
    assert not any(item["task"] == task.id and item.get("group") == "question" for item in inbox)

    # The check runner is detached in production, so allow the continuation to be
    # collected before asserting the final status.  In-process tests usually finish
    # in one tick, but the lifecycle contract is eventual and must not depend on that
    # scheduling detail.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not sched.state.get(task.id).get("check_run"):
            break
        sched.tick()
        time.sleep(0.01)
    assert sched.store.task(task.id).status == Status.IN_REVIEW


def test_real_scope_disagreement_is_a_product_decision(sched):
    result = {"status": "no_change", "verified": [
        {"criterion": "Keep the old API", "not_done": True, "reason": "That would break compatibility"}
    ]}
    assert sched._no_change_changes_outcome(result)
    assert sched._no_change_changes_outcome({"improvements_declined": [{"suggestion": "remove API", "reason": "public"}]})
    assert not sched._no_change_changes_outcome({"verified": [{"criterion": "works", "evidence": "green"}]})


def test_no_change_does_not_erase_an_internal_review_finding(sched, monkeypatch):
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 4, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_change")
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    seen: list[str] = []
    dispatched: list[str] = []
    original_keys: list[str] = []
    for _ in range(16):
        rep = sched.tick()
        seen.extend(rep.transitions)
        dispatched.extend(rep.dispatched)
        keys = list(sched.state.get("DM-001").get("last_findings") or [])
        if keys and not original_keys:
            original_keys = keys
        if sched.state.get("DM-001").get("needs_human", {}).get("kind") == "stall":
            break
    assert "DM-001 no-change -> verification" in seen
    assert original_keys
    assert set(original_keys) <= set(sched.state.get("DM-001").get("last_findings") or [])
    assert sched.state.get("DM-001")["needs_human"]["kind"] == "stall"
    assert sched.store.task("DM-001").status == Status.CHANGES_REQUESTED


def test_waiting_state_recovery_retains_a_live_check_and_its_continuation(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.status = Status.WAITING_HUMAN
    sched.store.save(task)
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "running"
    run.save()
    sched.state.get(task.id)["check_run"] = {
        "run_id": run.run_id, "stage": "pre_pr", "cont": {"task_status": "in_review"}
    }
    monkeypatch.setattr(sched, "_finished_or_timed_out", lambda run, runner: False)
    monkeypatch.setattr(run, "kill", lambda: (_ for _ in ()).throw(AssertionError("live check killed")))

    result = sched.recover_waiting_check(task)

    assert "retained" in result
    assert sched.store.task(task.id).status == Status.IN_REVIEW
    assert sched.state.get(task.id)["check_run"]["cont"]["task_status"] == "in_review"


@pytest.mark.parametrize("run_status", ["requested", "preparing"])
def test_terminal_check_recovery_retains_a_preparing_check_and_its_stop(sched, run_status):
    """A reserved check launch has no terminal result for recovery to reconcile yet."""
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    run = sched.runs.new_run(task.id, "local", mode="check", initial_status=run_status)
    run.save()
    st = sched.state.get(task.id)
    st["check_run"] = {"run_id": run.run_id, "stage": "ci", "cont": {"task_status": "in_review"}}
    st["recovery_check"] = {"run": run.run_id, "stage": "ci", "cont": {}, "specs": []}
    st["needs_human"] = {"kind": "check_did_not_run", "run": run.run_id,
                         "reason": "check did not run"}
    sched.state.save()

    outcome = sched.recover_waiting_check(task)

    assert outcome == f"live check {run.run_id} retained; still {run_status}"
    state = sched.state.get(task.id)
    assert state["check_run"]["run_id"] == run.run_id
    assert state["needs_human"]["run"] == run.run_id
    assert state["recovery_check"]["run"] == run.run_id
    assert sched.store.task(task.id).status == Status.IN_REVIEW


def test_waiting_state_recovery_reaps_a_finished_check_normally(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.status = Status.WAITING_HUMAN
    sched.store.save(task)
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "running"
    run.save()
    info = {"run_id": run.run_id, "stage": "pre_pr", "cont": {"task_status": "running"}}
    sched.state.get(task.id)["check_run"] = info
    monkeypatch.setattr(sched, "_finished_or_timed_out", lambda run, runner: True)
    reaped: list[dict] = []
    monkeypatch.setattr(sched, "reap_check", lambda task, rep: reaped.append(dict(sched.state.get(task.id)["check_run"])) or True)

    result = sched.recover_waiting_check(task)

    assert "reaped" in result
    assert reaped == [info]


def test_waiting_state_recovery_clears_only_missing_check_metadata(sched):
    task = sched.store.task("DM-001")
    task.status = Status.WAITING_HUMAN
    sched.store.save(task)
    st = sched.state.get(task.id)
    st["check_run"] = {"run_id": "missing", "stage": "pre_pr", "cont": {"task_status": "running"}}
    st["pending_feedback"] = "still actionable"

    result = sched.recover_waiting_check(task)

    assert "stale check metadata cleared" in result
    assert not sched.state.get(task.id).get("check_run")
    assert sched.state.get(task.id)["pending_feedback"] == "still actionable"
    assert sched.store.task(task.id).status == Status.CHANGES_REQUESTED


def _terminal_check_stop(sched, task, *, checks="SUCCESS", feedback="",
                         source_head="current-head"):
    """Record the stale-pointer shape left by a terminal check recovery interruption."""
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.result = {"checks": []}
    run.error = "original check diagnostic"
    run.source_head = source_head
    run.save()
    st = sched.state.get(task.id)
    st["check_run"] = {"run_id": run.run_id, "stage": "ci", "cont": {}}
    st["recovery_check"] = {"run": run.run_id, "stage": "ci", "cont": {}, "specs": [],
                            "source_head": source_head}
    st["needs_human"] = {"kind": "check_did_not_run", "run": run.run_id,
                          "reason": "check did not run"}
    st["checks"] = checks
    if feedback:
        st["pending_feedback"] = feedback
    sched.state.save()
    return run


def _parked_terminal_check_stop(sched, task, *, checks="SUCCESS", feedback="",
                                source_head="current-head"):
    """Create the ordinary exhausted-check stop through its production parking path."""
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.result = {"checks": []}
    run.error = "original check diagnostic"
    run.source_head = source_head
    run.save()
    st = sched.state.get(task.id)
    st["check_run"] = {"run_id": run.run_id, "stage": "ci", "cont": {}}
    st["checks"] = checks
    if feedback:
        st["pending_feedback"] = feedback
    sched.state.save()
    sched._retry_or_park_check(task, run, "ci", {}, [], 1, TickReport())
    assert not sched.state.get(task.id).get("check_run")
    return run


def test_terminal_check_recovery_resumes_green_pr_without_a_revision(sched):
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    sched.github.prs["garden/test"] = PRInfo(
        number=101, url=task.pr, state="OPEN", head_sha="current-head"
    )
    run = _terminal_check_stop(sched, task)
    sched.state.get(task.id)["head_sha"] = "current-head"

    outcome = sched.recover_waiting_check(task)

    assert outcome == "terminal check pointer cleared; pipeline progression resumed"
    st = sched.state.get(task.id)
    assert not st.get("check_run") and not st.get("needs_human") and not st.get("recovery_check")
    assert sched.store.task(task.id).status == Status.IN_REVIEW
    preserved = sched._run_by_id(task, run.run_id)
    assert preserved.error == "original check diagnostic" and preserved.result == {"checks": []}
    assert [item.run_id for item in sched.runs.runs_for(task.id)] == [run.run_id]
    assert sched.recover_waiting_check(task) == "no check recovery is needed"
    sched.tick(dispatch=False)
    assert not sched.state.get(task.id).get("needs_human")


def test_terminal_check_recovery_clears_an_ordinary_parked_stop(sched):
    """The normal exhausted-check path removes check_run before presenting recovery."""
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    sched.github.prs["garden/test"] = PRInfo(
        number=101, url=task.pr, state="OPEN", head_sha="current-head"
    )
    run = _parked_terminal_check_stop(sched, task)
    sched.state.get(task.id)["head_sha"] = "current-head"

    outcome = sched.recover_waiting_check(task)

    assert outcome == "terminal check pointer cleared; pipeline progression resumed"
    st = sched.state.get(task.id)
    assert not st.get("check_run") and not st.get("needs_human") and not st.get("recovery_check")
    assert sched._run_by_id(task, run.run_id).error == "original check diagnostic"
    sched.tick(dispatch=False)
    assert not sched.state.get(task.id).get("needs_human")


def test_terminal_check_recovery_refuses_success_from_an_older_pr_head(sched):
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    sched.github.prs["garden/test"] = PRInfo(
        number=101, url=task.pr, state="OPEN", head_sha="new-head"
    )
    run = _parked_terminal_check_stop(sched, task, source_head="old-head")
    st = sched.state.get(task.id)
    st["head_sha"] = "new-head"

    with pytest.raises(RuntimeError, match="different PR head"):
        sched.recover_waiting_check(task)

    state = sched.state.get(task.id)
    assert state["needs_human"]["run"] == run.run_id
    assert state["recovery_check"]["run"] == run.run_id
    assert state["checks"] == "SUCCESS"
    assert sched.store.task(task.id).status == Status.IN_REVIEW


def test_terminal_check_recovery_refuses_success_without_immutable_source_head(sched):
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    sched.github.prs["garden/test"] = PRInfo(
        number=101, url=task.pr, state="OPEN", head_sha="new-head"
    )
    run = _terminal_check_stop(sched, task, source_head="")
    st = sched.state.get(task.id)
    st["head_sha"] = "new-head"

    with pytest.raises(RuntimeError, match="no recorded source head"):
        sched.recover_waiting_check(task)

    state = sched.state.get(task.id)
    assert state["needs_human"]["run"] == run.run_id
    assert state["recovery_check"]["run"] == run.run_id
    assert state["checks"] == "SUCCESS"
    assert sched._run_by_id(task, run.run_id).result == {"checks": []}
    assert sched.store.task(task.id).status == Status.IN_REVIEW


def test_terminal_check_recovery_retains_current_failure_for_existing_revision(sched):
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    _parked_terminal_check_stop(sched, task, checks="FAILURE", feedback="- substantive review finding")

    outcome = sched.recover_waiting_check(task)

    assert outcome == "terminal check pointer cleared; existing revision will continue"
    st = sched.state.get(task.id)
    assert st["pending_feedback"] == "- substantive review finding"
    assert not st.get("check_run") and not st.get("needs_human") and not st.get("recovery_check")
    assert sched.store.task(task.id).status == Status.CHANGES_REQUESTED


def test_plain_resume_refuses_a_terminal_check_stop(sched):
    task = sched.store.task("DM-001")
    _terminal_check_stop(sched, task)

    with pytest.raises(RuntimeError, match="use garden recover-check DM-001"):
        sched.resume_task(task)


def test_terminal_check_recovery_does_not_touch_a_newer_live_pointer(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    stopped = _terminal_check_stop(sched, task)
    newer = sched.runs.new_run(task.id, "local", mode="check")
    newer.status = "running"
    newer.save()
    st = sched.state.get(task.id)
    st["check_run"] = {"run_id": newer.run_id, "stage": "ci", "cont": {"task_status": "in_review"}}
    sched.state.save()
    monkeypatch.setattr(newer, "kill", lambda: (_ for _ in ()).throw(AssertionError("live check killed")))

    outcome = sched.recover_waiting_check(task)

    assert f"current check {newer.run_id} does not match stopped check {stopped.run_id}" in outcome
    assert sched.state.get(task.id)["check_run"]["run_id"] == newer.run_id
    assert sched.state.get(task.id)["needs_human"]["run"] == stopped.run_id


def test_terminal_check_recovery_does_not_touch_a_newer_parked_pointer(sched):
    """A newer parked continuation owns recovery_check even without check_run."""
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    stopped = _terminal_check_stop(sched, task)
    newer = sched.runs.new_run(task.id, "local", mode="check")
    newer.status = "done"
    newer.save()
    st = sched.state.get(task.id)
    st.pop("check_run")
    st["recovery_check"] = {"run": newer.run_id, "stage": "ci", "cont": {}, "specs": []}
    sched.state.save()

    outcome = sched.recover_waiting_check(task)

    assert f"recovery check {newer.run_id} does not match stopped check {stopped.run_id}" in outcome
    state = sched.state.get(task.id)
    assert not state.get("check_run")
    assert state["needs_human"]["run"] == stopped.run_id
    assert state["recovery_check"]["run"] == newer.run_id


def test_terminal_check_recovery_waits_for_tick_lock(sched):
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    sched.github.prs["garden/test"] = PRInfo(
        number=101, url=task.pr, state="OPEN", head_sha="current-head"
    )
    _terminal_check_stop(sched, task)
    sched.state.get(task.id)["head_sha"] = "current-head"
    entered = threading.Event()
    finished = threading.Event()

    def recover() -> None:
        entered.set()
        sched.recover_waiting_check(task)
        finished.set()

    with sched.tick_lock():
        thread = threading.Thread(target=recover)
        thread.start()
        assert entered.wait(timeout=1)
        assert not finished.wait(timeout=0.05)
    thread.join(timeout=1)
    assert finished.is_set()
    assert not sched.state.get(task.id).get("check_run")


# ---- CLI and web agree -------------------------------------------------------
def _cli(garden, *args):
    from typer.testing import CliRunner

    from garden.cli import app

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(cwd)


def test_cli_shows_and_accepts_a_decision(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from garden.store import Store

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "wont_do")
    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()
    r = _cli(garden, "show", "DM-001")
    assert "worker decision" in r.output and "duplicates DM-002" in r.output
    r = _cli(garden, "accept", "DM-001")
    assert r.exit_code == 0 and "wont_do" in r.output
    assert Store(garden).task("DM-001").status.value == "wont_do"


def test_cli_reject_and_set_status_wont_do(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from garden.store import Store

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "wont_do")
    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()
    # garden answer on a decision task rejects with the note (per the protocol)
    r = _cli(garden, "answer", "DM-001", "please do it after all")
    assert r.exit_code == 0 and "rejected" in r.output
    assert Store(garden).task("DM-001").status.value == "changes_requested"
    # set-status wont_do is the direct route to the terminal status
    r = _cli(garden, "set-status", "DM-002", "wont_do", "--reason", "not needed")
    assert r.exit_code == 0
    t = Store(garden).task("DM-002")
    assert t.status.value == "wont_do" and "not needed" in t.body


def test_web_decision_flow(garden, monkeypatch):
    from fastapi.testclient import TestClient

    from garden.scheduler import Scheduler
    from garden.store import Store
    from garden.web.app import create_app

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "wont_do")
    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    sched.tick()
    c = TestClient(create_app(Store(garden), watch=False))
    page = c.get("/tasks/DM-001").text
    assert "Decide whether to cancel this work" in page and "duplicates DM-002" in page
    assert "Cancel this task" in page and "Keep this task" in page
    assert "I do not think this should be done" in page  # the full message
    assert "Choose the product outcome" in c.get("/").text
    assert "s-wont_do" in c.get("/partials/board").text
    r = c.post("/tasks/DM-001/accept", follow_redirects=False)
    assert r.status_code == 303
    api = {t["id"]: t for t in c.get("/api/tasks").json()}
    assert api["DM-001"]["status"] == "wont_do"


@pytest.mark.parametrize("mode", ["wont_do", "no_change_decision"])
def test_decision_is_readable_when_waiting_status_is_published(sched, fake_github, monkeypatch, mode):
    """A separate web reader must see the decision as soon as it sees the stop."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    if mode == "no_change_decision":
        sched.tick()
        sched.tick()
        task = sched.store.task("DM-001")
        sched.state.get(task.id)["pending_feedback"] = "Reconsider the promised outcome."
        sched._transition(task, Status.CHANGES_REQUESTED, "request revision")
        sched.dispatch(task, mode="revise", runner=sched.runner_for(task))
    else:
        sched.tick()

    observed = []
    save = sched.store.save

    def read_after_publication(task, *args, **kwargs):
        result = save(task, *args, **kwargs)
        if task.id == "DM-001" and task.status == Status.WAITING_HUMAN:
            observed.append(State(sched.state.path).get(task.id).get("decision"))
        return result

    monkeypatch.setattr(sched.store, "save", read_after_publication)
    sched.tick()

    assert observed, "the worker decision must publish a waiting status"
    kind = "no_change" if mode == "no_change_decision" else mode
    assert all(decision and decision["kind"] == kind and decision["reason"] for decision in observed)


def test_question_resume_identity_is_readable_when_waiting_status_is_published(sched, monkeypatch):
    """An immediate answer must resume the worker that asked the question."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched.tick()
    run = sched.runs.latest("DM-001")
    observed = []
    save = sched.store.save

    def read_after_publication(task, *args, **kwargs):
        result = save(task, *args, **kwargs)
        if task.id == "DM-001" and task.status == Status.WAITING_HUMAN:
            observed.append(dict(State(sched.state.path).get(task.id)))
        return result

    monkeypatch.setattr(sched.store, "save", read_after_publication)
    sched.tick()

    assert observed, "the worker question must publish a waiting status"
    for state in observed:
        assert state.get("question") == "Postgres or SQLite?"
        assert state.get("session_id") == "sess-42"
        assert state.get("session_host") == run.host
        assert state.get("session_harness") == run.harness
        assert state.get("question_run") == run.run_id
