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


def test_approve_and_dispatch_refuse_closed_phase(garden):
    """CG-148: a closed phase refuses approvals and dispatch too, the same way `new-task`
    and `plan` already did -- there is no exception for a closed phase (only a frozen one
    has one); reopen it first."""
    assert run(garden, "new-task", "demo/p1", "Late arrival").exit_code == 0  # DM-003, draft
    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0

    r = run(garden, "approve", "DM-003")
    assert r.exit_code == 1  # refused, like `dispatch` on the same phase (CG-205)
    assert "closed" in r.output and "reopen-phase" in r.output
    assert Store(garden).task("DM-003").status.value == "draft"

    r = run(garden, "dispatch", "DM-001")
    assert r.exit_code == 1 and "closed" in r.output and "reopen-phase" in r.output
    assert Store(garden).task("DM-001").status.value == "ready"  # never dispatched


def test_web_approve_and_dispatch_actions_refuse_closed_phase(garden):
    from fastapi.testclient import TestClient

    from garden.web.app import create_app

    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0
    c = TestClient(create_app(Store(garden), watch=False))
    r = c.post("/tasks/DM-001/dispatch", follow_redirects=True)
    assert "closed" in r.text and "reopen-phase" in r.text


def test_new_task_refuses_closed_phase_without_reopen(garden):
    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0
    r = run(garden, "new-task", "demo/p1", "Late arrival")
    assert r.exit_code == 1 and "--reopen" in r.output
    r = run(garden, "new-task", "demo/p1", "Late arrival", "--reopen")
    assert r.exit_code == 0 and "DM-003" in r.output
    assert not Store(garden).phase("demo", "p1").closed


def test_plan_refuses_closed_phase_without_reopen(garden, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "plan")
    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0

    r = run(garden, "plan", "demo/p1")
    assert r.exit_code == 1 and "--reopen" in r.output
    assert not Store(garden).tasks().get("DM-003")

    r = run(garden, "plan", "demo/p1", "--reopen")
    assert r.exit_code == 0, r.output
    assert not Store(garden).phase("demo", "p1").closed
    assert Store(garden).tasks().get("DM-003")


def test_plan_dry_run_allowed_on_closed_phase(garden):
    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0
    r = run(garden, "plan", "demo/p1", "--dry-run")
    assert r.exit_code == 0, r.output


def test_friction_report_records_but_skips_draft_task_on_closed_phase(garden):
    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0
    r = run(garden, "friction-report", "demo/p1", "the CLI is confusing")
    assert r.exit_code == 0, r.output
    assert "no draft task created" in r.output
    doc = (garden / "demo" / "p1" / "docs" / "friction.md").read_text()
    assert "the CLI is confusing" in doc
    assert not any(t.discovered_from == "" and t.title.startswith("the CLI") for t in Store(garden).tasks().values())


def test_web_close_phase_refuses_open_tasks_then_closes(garden):
    c = TestClient(create_app(Store(garden), watch=False))
    assert "Close phase" in c.get("/phases/demo/p1").text
    r = c.post("/phases/demo/p1/close", follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "still has 2 open task(s)" in page and "DM-001 (ready)" in page
    assert not Store(garden).phase("demo", "p1").closed
    finish_all(garden)
    r = c.post("/phases/demo/p1/close", follow_redirects=False)
    assert r.status_code == 303 and "flash" not in r.headers["location"]
    assert Store(garden).phase("demo", "p1").closed
    events = [json.loads(line) for line in (garden / ".garden" / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "phase_closed" and e["phase"] == "demo/p1" for e in events)
    assert c.post("/phases/demo/nope/close").status_code == 404


def test_web_plan_refuses_closed_phase(garden):
    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0
    c = TestClient(create_app(Store(garden), watch=False))
    r = c.post("/phases/demo/p1/plan", data={"guidance": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert not Store(garden).tasks().get("DM-003")


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
    assert "demo has no open phase" in home  # the rail points at planning the next one

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


def test_closed_phase_with_no_run_records_omits_counts(garden):
    """CG-205: a phase finished by hand before the scheduler tracked runs has no dispatch
    events and no `pr:` on its tasks, so PRs-merged and cost would read as a real zero
    (`0 PR(s) merged · spent $0.00`) rather than as data the loop never recorded. Omit those
    counts and say so instead; `tasks done` is unaffected since it comes from task status,
    not the event log."""
    finish_all(garden)
    assert run(garden, "close-phase", "demo/p1").exit_code == 0

    c = TestClient(create_app(Store(garden), watch=False))
    page = c.get("/phases/demo/p1").text
    assert "2 of 2 tasks done" in page
    assert "no run records for this phase" in page
    assert "PR(s) merged" not in page
    assert "$0.00" not in page

    herb = c.get("/herbarium").text
    assert "2 of 2" in herb
    assert "no records" in herb
    assert "$0.00" not in herb


def test_closed_phase_with_run_records_shows_counts(garden):
    """The counterpart to the no-records case: once a dispatch event, a run's cost and a PR
    are recorded, the real figures show (not the "no run records" note)."""
    from garden.events import EventLog
    from garden.runs import RunStore

    log = EventLog(garden / ".garden" / "events.jsonl")
    log.emit("dispatch", "DM-001", mode="work")
    log.emit("transition", "DM-001", to="done")
    rs = RunStore(garden / ".garden")
    rn = rs.new_run("DM-001", "local", mode="work")
    rn.status = "done"
    rn.cost_usd = 1.23
    rn.save()
    t = Store(garden).task("DM-001")
    t.pr = "https://github.com/test/demo/pull/1"
    Store(garden).save(t)
    finish_all(garden)
    assert run(garden, "close-phase", "demo/p1").exit_code == 0

    c = TestClient(create_app(Store(garden), watch=False))
    page = c.get("/phases/demo/p1").text
    assert "no run records for this phase" not in page
    assert "1 PR(s) merged" in page
    assert "$1.23" in page


def test_herbarium_shows_persona_scores_and_links_to_the_retro(garden):
    """CG-146: a closed phase whose retro has run shows each persona's score on its Herbarium
    card and links to its retro page; the closed-phase header links there too."""
    finish_all(garden)
    docs = garden / "demo" / "p1" / "docs"
    (docs / "reviews").mkdir(parents=True, exist_ok=True)
    (docs / "reviews" / "designer-2026-09-01.md").write_text(
        "# designer review\n\n**Persona:** designer · **Score:** 8/10 · now\n\nSolid.\n")
    (docs / "retro.md").write_text("# Retrospective: demo/p1\n\nAll reconciled.\n")
    assert run(garden, "close-phase", "demo/p1").exit_code == 0

    c = TestClient(create_app(Store(garden), watch=False))
    herb = c.get("/herbarium").text
    assert "personas:" in herb and "designer" in herb and "8/10" in herb
    assert "/phases/demo/p1/retro" in herb
    # the closed-phase header links to the retro too
    assert "/phases/demo/p1/retro" in c.get("/phases/demo/p1").text


def test_phase_summary_figures():
    from garden.events import phase_summary
    from garden.model import Status, Task

    tasks = {
        "DM-001": Task(path=None, id="DM-001", title="A", status=Status.DONE, pr="https://x/pull/1", difficulty="easy"),
        "DM-002": Task(path=None, id="DM-002", title="B", status=Status.CANCELLED),
    }
    events = [
        {"at": "2026-09-01T10:00:00+00:00", "kind": "dispatch", "task": "DM-001", "mode": "work"},
        {"at": "2026-09-01T11:00:00+00:00", "kind": "review", "task": "DM-001", "verdict": "approve"},
        {"at": "2026-09-01T12:00:00+00:00", "kind": "run_finished", "task": "DM-001", "cost_usd": 2.5},
        {"at": "2026-09-02T10:00:00+00:00", "kind": "transition", "task": "DM-001", "to": "done"},
    ]
    s = phase_summary(events, tasks)
    assert s["first_dispatch"] == "2026-09-01"
    assert s["tasks_done"] == 1 and s["tasks_total"] == 2 and s["prs_merged"] == 1
    assert s["avg_lead_hours"] == 24.0
    assert s["first_pass_rate"] == 1.0
    assert s["revisions"] == 0
    assert s["cost_usd"] == 2.5
    assert s["done_at"]["DM-001"].startswith("2026-09-02")


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
