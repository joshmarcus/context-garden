"""A worker's wont_do / no_change is a decision for the person, not a failure (CG-100)."""

import os

from garden.github import Feedback
from garden.inbox import build_inbox
from garden.model import Status
from tests.conftest import FakeGitHub, wait_for_runs


def statuses(sched):
    sched.store.invalidate()
    return {tid: t.status.value for tid, t in sched.store.tasks().items()}


def _to_decision(sched, mode, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    sched.tick()
    wait_for_runs(sched)
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
    wait_for_runs(sched)
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
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "The person disagrees" in brief and "still needed" in brief
    # the revise round finishes normally -> a PR
    wait_for_runs(sched)
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"


# ---- no_change ---------------------------------------------------------------
def test_no_change_pauses_then_accept_resumes_the_round(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_change")
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "tweak", "created": "2099-01-01T00:00:00Z"}])
    rep = sched.tick()  # -> changes_requested + revise dispatched
    assert rep.dispatched == ["DM-001(revise)"]
    wait_for_runs(sched)
    sched.tick()  # reap the revise run: it reports no_change
    assert statuses(sched)["DM-001"] == "waiting_human"
    assert sched.state.get("DM-001")["decision"]["kind"] == "no_change"

    n_runs = len(sched.runs.runs_for("DM-001"))
    n_comments = len(fake_github.comments)
    sched.accept_decision(sched.store.task("DM-001"))
    # resumed as if the round had pushed: no new run, back to the PR, not stalled
    assert statuses(sched)["DM-001"] == "in_review"
    assert len(sched.runs.runs_for("DM-001")) == n_runs
    assert len(fake_github.comments) > n_comments
    assert not sched.state.get("DM-001").get("needs_human")


def test_no_change_reject_revises(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_change")
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "tweak", "created": "2099-01-01T00:00:00Z"}])
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    assert statuses(sched)["DM-001"] == "waiting_human"
    sched.reject_decision(sched.store.task("DM-001"), "there is a real change to make here")
    assert statuses(sched)["DM-001"] == "changes_requested"
    assert "The person disagrees" in sched.state.get("DM-001")["pending_feedback"]


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
    wait_for_runs(sched)
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
    wait_for_runs(sched)
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
    wait_for_runs(sched)
    sched.tick()
    c = TestClient(create_app(Store(garden), watch=False))
    page = c.get("/tasks/DM-001").text
    assert "asks you to decide" in page and "duplicates DM-002" in page
    assert "I do not think this should be done" in page  # the full message
    assert "Accept or reject a worker" in c.get("/").text
    assert "s-wont_do" in c.get("/partials/board").text
    r = c.post("/tasks/DM-001/accept", follow_redirects=False)
    assert r.status_code == 303
    api = {t["id"]: t for t in c.get("/api/tasks").json()}
    assert api["DM-001"]["status"] == "wont_do"
