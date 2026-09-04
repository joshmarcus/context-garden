"""Closing a phase: close-phase/reopen-phase, the dispatch and new-task guards,
the herbarium and the closed phase page (CG-078)."""

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
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


def finish_all(garden):
    for tid in ("DM-001", "DM-002"):
        assert run(garden, "set-status", tid, "done").exit_code == 0


def test_close_phase_refuses_open_tasks_and_force_overrides(garden):
    r = run(garden, "close-phase", "demo/p1")
    assert r.exit_code == 1
    assert "DM-001" in r.output and "DM-002" in r.output and "--force" in r.output
    assert not Store(garden).phase("demo", "p1").closed

    r = run(garden, "close-phase", "demo/p1", "--force")
    assert r.exit_code == 0, r.output
    ph = Store(garden).phase("demo", "p1")
    assert ph.closed
    goals = (garden / "demo" / "p1" / "goals.md").read_text()
    assert goals.startswith("---") and "closed:" in goals
    events = [json.loads(line) for line in (garden / ".garden" / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "phase_closed" and e["phase"] == "demo/p1" for e in events)
    # closing again is a no-op, not an error
    assert run(garden, "close-phase", "demo/p1").exit_code == 0


def test_close_then_reopen(garden):
    finish_all(garden)
    assert run(garden, "close-phase", "demo/p1").exit_code == 0
    assert Store(garden).phase("demo", "p1").closed
    r = run(garden, "reopen-phase", "demo/p1")
    assert r.exit_code == 0, r.output
    assert not Store(garden).phase("demo", "p1").closed
    assert "closed:" not in (garden / "demo" / "p1" / "goals.md").read_text()
    events = [json.loads(line) for line in (garden / ".garden" / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "phase_reopened" for e in events)
    # reopening an open phase is an error
    assert run(garden, "reopen-phase", "demo/p1").exit_code == 1


def test_new_task_refuses_closed_phase_without_reopen(garden):
    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0
    r = run(garden, "new-task", "demo/p1", "Late arrival")
    assert r.exit_code == 1 and "--reopen" in r.output
    r = run(garden, "new-task", "demo/p1", "Late arrival", "--reopen")
    assert r.exit_code == 0 and "DM-003" in r.output
    assert not Store(garden).phase("demo", "p1").closed


def test_scheduler_does_not_dispatch_into_closed_phase(garden, sched):
    store = Store(garden)
    store.set_phase_closed(store.phase("demo", "p1"), "2026-09-04")
    rep = sched.tick()
    assert not rep.dispatched
    assert Store(garden).task("DM-001").status.value == "ready"


def test_status_summary_line_for_closed_phases(garden):
    finish_all(garden)
    assert run(garden, "close-phase", "demo/p1").exit_code == 0
    out = run(garden, "status").output
    assert "1 closed phase" in out and "demo/p1" in out
    out_all = run(garden, "status", "--all").output
    assert "closed phase" not in out_all  # the phase is back to one row (rich may truncate its key)


def test_closed_phase_leaves_the_rail_and_joins_the_herbarium(garden):
    finish_all(garden)
    (garden / "demo" / "p1" / "docs").mkdir(exist_ok=True)
    (garden / "demo" / "p1" / "docs" / "friction.md").write_text("# Friction\n\nnone\n")
    (garden / "demo" / "p1" / "docs" / "closing.md").write_text("# Closing\n\nDone well.\n")
    assert run(garden, "close-phase", "demo/p1").exit_code == 0

    c = TestClient(create_app(Store(garden), watch=False))
    home = c.get("/").text
    assert '/phases/demo/p1"' not in home  # no drawer for the closed phase
    assert "Herbarium" in home and "1 closed phase" in home

    page = c.get("/herbarium")
    assert page.status_code == 200
    html = page.text
    assert "pressed" in html and "p1" in html
    assert "2 of 2" in html  # tasks done
    assert "/phases/demo/p1/doc/docs/friction.md" in html
    assert "/phases/demo/p1/doc/docs/closing.md" in html

    # the linked docs render; anything outside the phase's docs/specs is a 404
    assert c.get("/phases/demo/p1/doc/docs/friction.md").status_code == 200
    assert "Done well." in c.get("/phases/demo/p1/doc/docs/closing.md").text
    assert c.get("/phases/demo/p1/doc/../../garden.yaml").status_code in (404, 422)
    assert c.get("/phases/demo/p1/doc/tasks/DM-001-first.md").status_code == 404


def test_closed_phase_page_shows_the_closing_header(garden):
    finish_all(garden)
    (garden / "demo" / "p1" / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    (garden / "demo" / "p1" / "docs" / "reviews" / "designer-2026-09-01.md").write_text(
        "# designer review of demo/p1\n\n**Persona:** designer · **Score:** 7/10 · now\n\nSolid overall.\n\n## High\n\n- **nav** — the rail is crowded\n"
    )
    assert run(garden, "close-phase", "demo/p1").exit_code == 0

    c = TestClient(create_app(Store(garden), watch=False))
    html = c.get("/phases/demo/p1").text
    assert "closed" in html and "pressed" in html
    assert "Persona reviews" in html and "designer" in html and "Solid overall." in html and "the rail is crowded" in html
    assert "Outcomes" in html and "Artifacts" in html and "Pull requests" in html
    # no working controls
    assert "Approve all drafts" not in html and "Plan phase" not in html and "Run personas" not in html


def test_board_and_trellis_default_to_open_phases(garden):
    finish_all(garden)
    assert run(garden, "close-phase", "demo/p1").exit_code == 0
    c = TestClient(create_app(Store(garden), watch=False))
    assert "DM-001" not in c.get("/board").text
    assert "DM-001" in c.get("/board?closed=1").text
    assert "DM-001" in c.get("/board?product=demo&phase=p1").text  # explicit selection wins
    assert "DM_001" not in c.get("/trellis").text
    assert "DM_001" in c.get("/trellis?closed=1").text
    assert "include closed" in c.get("/board").text and "include closed" in c.get("/trellis").text
