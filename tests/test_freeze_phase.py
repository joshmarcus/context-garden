"""Freezing a phase: `garden freeze`/`unfreeze`, the approve/dispatch guards (CLI, web action,
scheduler), the freeze exception escape hatch, and discovered work deferred by the freeze
(CG-148). Closed-phase parity for the same guards lives in test_close_phase.py."""

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.model import Status
from garden.store import Store
from garden.web.app import create_app

runner = CliRunner()


def run(garden, *args):
    import os

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)


def grant_exception(garden, task_id: str, reason: str = "hotfix while the phase is frozen") -> None:
    store = Store(garden)
    t = store.task(task_id)
    t.freeze_exception = True
    t.freeze_exception_reason = reason
    store.save(t)


def test_freeze_and_unfreeze_cli(garden):
    r = run(garden, "freeze", "demo/p1")
    assert r.exit_code == 0, r.output
    ph = Store(garden).phase("demo", "p1")
    assert ph.frozen
    goals = (garden / "demo" / "p1" / "goals.md").read_text()
    assert goals.startswith("---") and "frozen:" in goals
    events = [json.loads(line) for line in (garden / ".garden" / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "phase_frozen" and e["phase"] == "demo/p1" for e in events)
    # freezing again is a no-op, not an error
    assert run(garden, "freeze", "demo/p1").exit_code == 0

    r = run(garden, "unfreeze", "demo/p1")
    assert r.exit_code == 0, r.output
    assert not Store(garden).phase("demo", "p1").frozen
    assert "frozen:" not in (garden / "demo" / "p1" / "goals.md").read_text()
    events = [json.loads(line) for line in (garden / ".garden" / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "phase_unfrozen" for e in events)
    # unfreezing an open phase is an error
    assert run(garden, "unfreeze", "demo/p1").exit_code == 1


def test_freeze_refuses_a_closed_phase(garden):
    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0
    r = run(garden, "freeze", "demo/p1")
    assert r.exit_code == 1 and "closed" in r.output


def test_approve_cli_refuses_frozen_task_without_exception(garden):
    assert run(garden, "new-task", "demo/p1", "Late arrival").exit_code == 0
    assert run(garden, "freeze", "demo/p1").exit_code == 0

    r = run(garden, "approve", "DM-003")
    assert r.exit_code == 0  # skips with a message rather than hard-failing
    assert "frozen" in r.output
    assert Store(garden).task("DM-003").status == Status.DRAFT

    grant_exception(garden, "DM-003")
    r = run(garden, "approve", "DM-003")
    assert r.exit_code == 0, r.output
    assert Store(garden).task("DM-003").status == Status.READY


def test_dispatch_cli_refuses_frozen_task_without_exception(garden):
    assert run(garden, "freeze", "demo/p1").exit_code == 0
    r = run(garden, "dispatch", "DM-001")
    assert r.exit_code == 1
    assert "frozen" in r.output
    assert Store(garden).task("DM-001").status == Status.READY  # never dispatched


def test_scheduler_does_not_dispatch_into_frozen_phase(garden, sched):
    store = Store(garden)
    store.set_phase_frozen(store.phase("demo", "p1"), "2026-09-04")
    rep = sched.tick()
    assert not rep.dispatched
    assert Store(garden).task("DM-001").status.value == "ready"


def test_scheduler_dispatches_a_task_with_a_valid_freeze_exception(garden, sched):
    store = Store(garden)
    store.set_phase_frozen(store.phase("demo", "p1"), "2026-09-04")
    grant_exception(garden, "DM-001")
    sched.store.invalidate()
    rep = sched.tick()
    assert rep.dispatched == ["DM-001(work)"]  # DM-002 has no exception and stays put
    assert Store(garden).task("DM-002").status.value == "ready"


def test_discovered_task_in_frozen_phase_lands_as_deferred_draft(sched, fake_github, monkeypatch):
    """DM-001 dispatches while the phase is still open; it is frozen while the worker runs, so
    its discoveries -- including a `blocking: true` one that would otherwise auto-approve --
    land as drafts, logged as deferred."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "discover")
    sched.tick()  # dispatches and runs DM-001 synchronously
    sched.store.set_phase_frozen(sched.store.phase("demo", "p1"), "2026-09-04")
    sched.tick(dispatch=False)  # reap only
    sched.store.invalidate()

    filed = {t.title: t for t in sched.store.tasks().values() if t.discovered_from == "DM-001"}
    assert filed  # the fake worker's discoveries were still filed
    blocking_task = filed["Add the missing config schema"]  # reported with blocking: true
    assert blocking_task.status == Status.DRAFT
    assert "deferred by the freeze" in blocking_task.body


def test_web_approve_action_refuses_frozen_task(garden):
    assert run(garden, "new-task", "demo/p1", "Late arrival").exit_code == 0
    assert run(garden, "freeze", "demo/p1").exit_code == 0
    c = TestClient(create_app(Store(garden), watch=False))
    r = c.post("/tasks/DM-003/approve", follow_redirects=True)
    assert "frozen" in r.text
    assert Store(garden).task("DM-003").status == Status.DRAFT

    grant_exception(garden, "DM-003")
    r = c.post("/tasks/DM-003/approve", follow_redirects=False)
    assert r.status_code == 303
    assert Store(garden).task("DM-003").status == Status.READY


def test_web_dispatch_action_refuses_frozen_task(garden):
    assert run(garden, "freeze", "demo/p1").exit_code == 0
    c = TestClient(create_app(Store(garden), watch=False))
    r = c.post("/tasks/DM-001/dispatch", follow_redirects=True)
    assert "frozen" in r.text
    assert Store(garden).task("DM-001").status == Status.READY


def test_web_approve_all_skips_frozen_drafts(garden):
    assert run(garden, "new-task", "demo/p1", "Late arrival").exit_code == 0
    assert run(garden, "freeze", "demo/p1").exit_code == 0
    c = TestClient(create_app(Store(garden), watch=False))
    r = c.post("/phases/demo/p1/approve-all", follow_redirects=True)
    assert "frozen" in r.text
    assert Store(garden).task("DM-003").status == Status.DRAFT


def test_phase_page_and_rail_show_frozen(garden):
    assert run(garden, "freeze", "demo/p1").exit_code == 0
    c = TestClient(create_app(Store(garden), watch=False))
    home = c.get("/").text
    assert "frozen" in home  # the rail drawer flags it
    page = c.get("/phases/demo/p1").text
    assert "frozen" in page
