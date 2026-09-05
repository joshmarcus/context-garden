"""Suggestions on a task's own spec: capture, the edit run that folds them in, and the UI."""

from __future__ import annotations

from fastapi.testclient import TestClient

from garden.events import EventLog
from garden.model import Status
from garden.store import Store
from garden.suggestions import (
    add_suggestion,
    decompose,
    edit_brief,
    has_pending,
    mark_all_integrated,
    parse_edit,
    parse_suggestions,
    pending_suggestions,
    record_suggestion,
    set_spec_body,
    spec_body,
)
from garden.web.app import create_app


# ---- pure body helpers -----------------------------------------------------
def test_add_and_parse_keeps_log_last():
    body = "## Goal\n\nDo it.\n\n## Log\n\n- 2026-01-01 approved\n"
    body = add_suggestion(body, "tighten the goal", "josh", "2026-09-04", applies_to="goal")
    spec, sug, log = decompose(body)
    assert spec == "## Goal\n\nDo it."
    assert sug == ["- [ ] 2026-09-04 josh (applies to goal): tighten the goal"]
    assert "approved" in log
    # Log stays the last section so Task.log keeps appending under it
    assert body.rstrip().endswith("- 2026-01-01 approved")
    s = parse_suggestions(body)[0]
    assert not s.integrated and s.author == "josh" and s.applies_to == "goal" and s.text == "tighten the goal"
    assert has_pending(body)


def test_mark_integrated_and_spec_roundtrip():
    body = "## Goal\n\nOld goal.\n"
    body = add_suggestion(body, "add acceptance for the empty case", "cli", "2026-09-04")
    body = set_spec_body(body, "## Goal\n\nNew goal.\n\n## Acceptance\n\n- [ ] empty case")
    assert spec_body(body).startswith("## Goal\n\nNew goal.")
    assert has_pending(body)  # suggestion survives a spec rewrite
    body, n = mark_all_integrated(body)
    assert n == 1 and not has_pending(body)
    assert parse_suggestions(body)[0].integrated
    # the spec is preserved through integration
    assert "New goal." in body and "empty case" in body


def test_parse_edit_reads_marker():
    text = 'Edited.\nGARDEN_EDIT: {"body": "## Goal\\n\\nx", "priority": 2, "difficulty": "easy"}'
    obj = parse_edit(text)
    assert obj["body"].startswith("## Goal") and obj["priority"] == 2
    assert parse_edit("no marker here") == {}


def test_edit_brief_carries_body_and_suggestions(sched):
    t = sched.store.task("DM-001")
    record_suggestion(sched.store, t, "handle the empty input", author="josh", applies_to="acceptance")
    brief = edit_brief(sched.store, t, pending_suggestions(t.body))
    assert "GARDEN_EDIT:" in brief and "Do the first thing" in brief
    assert "handle the empty input" in brief and "priority:" in brief


# ---- capture ---------------------------------------------------------------
def test_suggest_lands_in_file_and_event(sched):
    t = sched.store.task("DM-001")
    record_suggestion(sched.store, t, "please add a test for the empty case", author="josh")
    sched.store.invalidate()
    reread = sched.store.task("DM-001")
    assert "## Suggestions" in reread.body
    assert "please add a test for the empty case" in reread.body
    events = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["suggestion"])
    assert events and events[-1]["author"] == "josh"


# ---- the edit run ----------------------------------------------------------
def test_edit_run_integrates_and_leaves_scheduler_fields(sched):
    t = sched.store.task("DM-001")
    record_suggestion(sched.store, t, "acceptance should cover the empty case", author="josh")

    rep = sched.tick()  # dispatch_edits runs before the work run
    assert "DM-001(edit)" in rep.dispatched
    assert sched.state.get("DM-001").get("edit_run")
    run = sched.runs.latest("DM-001")
    assert run.mode == "edit"

    sched.tick(dispatch=False)  # reap the edit; do not start a work run
    sched.store.invalidate()
    t = sched.store.task("DM-001")
    assert not has_pending(t.body)  # marked integrated
    assert parse_suggestions(t.body)[0].integrated
    assert "acceptance should cover the empty case" in t.body
    # scheduler-owned fields untouched
    assert t.status == Status.READY and t.branch == "" and t.pr == "" and t.attempts == 0
    assert not sched.state.get("DM-001").get("edit_run")
    # the old and new bodies are kept for the diff
    assert (run.path / "old_body.md").exists() and (run.path / "new_body.md").exists()
    assert (run.path / "old_body.md").read_text() != (run.path / "new_body.md").read_text()
    evs = EventLog(sched.cfg.garden_dir / "events.jsonl").read(task_id="DM-001", kinds=["integrated"])
    assert evs and evs[-1]["count"] == 1


def test_edit_holds_the_work_run_until_integrated(sched):
    t = sched.store.task("DM-001")
    record_suggestion(sched.store, t, "cover the empty case", author="josh")
    sched.tick()
    # the work run is held back while the edit run integrates the spec
    assert sched.store.task("DM-001").status == Status.READY
    sched.tick()  # reap edit, then dispatch the work run with the integrated spec
    assert sched.store.task("DM-001").status == Status.RUNNING


def test_failed_edit_does_not_loop_forever(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_EDIT", "noresult")
    t = sched.store.task("DM-001")
    record_suggestion(sched.store, t, "cover the empty case", author="josh")
    for _ in range(3):
        sched.tick()
    edits = [r for r in sched.runs.runs_for("DM-001") if r.mode == "edit"]
    assert len(edits) == sched.EDIT_MAX_ATTEMPTS  # capped, not endless
    # once the cap is hit the work run is allowed through despite the pending suggestion
    assert sched.store.task("DM-001").status == Status.RUNNING


def test_suggestion_on_running_task_waits_and_rides_revise(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")  # a worker still busy at the next tick
    sched.tick()  # DM-001 -> running (no suggestion yet)
    assert sched.store.task("DM-001").status == Status.RUNNING
    t = sched.store.task("DM-001")
    record_suggestion(sched.store, t, "also handle the empty input list", author="josh")
    sched.tick()  # still running; suggestion waits, no edit run dispatched
    assert sched.store.task("DM-001").status == Status.RUNNING
    assert not sched.state.get("DM-001").get("edit_run")
    assert not any(r.mode == "edit" for r in sched.runs.runs_for("DM-001"))

    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    sched.runner_for(t).wake(sched.runs.latest("DM-001"))  # the worker finishes its round
    sched.tick()  # reap -> in_review
    sched.triage(sched.store.task("DM-001"), changes="please revisit")
    sched.tick()  # dispatch the revise run
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"
    brief = (run.path / "brief.md").read_text()
    assert "Suggestions on this task" in brief and "also handle the empty input list" in brief


# ---- web -------------------------------------------------------------------
def test_web_suggest_and_page(garden):
    c = TestClient(create_app(Store(garden), watch=False))
    page = c.get("/tasks/DM-001").text
    assert "Suggest a change" in page
    r = c.post("/tasks/DM-001/suggest", data={"note": "clarify the goal wording", "applies_to": "goal"},
               follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/tasks/DM-001").text
    assert "clarify the goal wording" in page and "pending" in page
    # the inbox digest counts the pending suggestion
    assert "suggestions to integrate" in c.get("/").text


def test_web_integrate_now(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from tests.conftest import FakeGitHub

    store = Store(garden)
    record_suggestion(store, store.task("DM-001"), "add the empty-case acceptance", author="josh")
    c = TestClient(create_app(store, watch=False))
    r = c.post("/tasks/DM-001/integrate", follow_redirects=False)
    assert r.status_code == 303
    sched = Scheduler(Store(garden), github=FakeGitHub())
    assert sched.state.get("DM-001").get("edit_run")
    sched.tick(dispatch=False)
    assert not has_pending(sched.store.task("DM-001").body)
    assert "Integrated suggestions" in sched.store.task("DM-001").body
    # the task page shows the diff of what integration changed
    page = c.get("/tasks/DM-001").text
    assert "What integration changed" in page and "integrated" in page
