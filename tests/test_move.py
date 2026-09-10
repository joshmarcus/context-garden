"""Move a task to another phase of the same product, keeping its id, run history, state.json
entry and dependencies (CG-162): the CLI `garden move`, the task-page and Inbox web actions,
and the later-phase dependency warning."""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.model import Status
from garden.runs import RunStore
from garden.scheduler import Scheduler, State
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


def client(garden):
    return TestClient(create_app(Store(garden), watch=False))


def events(garden):
    path = garden / ".garden" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_move_cli_moves_file_updates_phase_logs_and_emits_event(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    r = run(garden, "move", "DM-001", "demo/p2")
    assert r.exit_code == 0, r.output
    assert not (garden / "demo" / "p1" / "tasks" / "DM-001-first.md").exists()
    assert (garden / "demo" / "p2" / "tasks" / "DM-001-first.md").exists()

    t = Store(garden).task("DM-001")
    assert t.id == "DM-001" and t.phase == "p2" and t.key == "demo/p2"
    assert "moved from demo/p1 to demo/p2" in t.body
    text = t.path.read_text()
    assert text.startswith("---") and "phase: p2" in text

    assert any(e["kind"] == "moved" and e["from"] == "demo/p1" and e["to"] == "demo/p2"
               and e["task"] == "DM-001" for e in events(garden))


def test_move_cli_refuses_a_closed_phase(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "close-phase", "demo/p2", "--force").exit_code == 0
    r = run(garden, "move", "DM-001", "demo/p2")
    assert r.exit_code == 1 and "closed" in r.output
    assert Store(garden).task("DM-001").phase == "p1"  # never moved


def test_move_cli_refuses_a_run_in_flight(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "set-status", "DM-001", "running").exit_code == 0
    r = run(garden, "move", "DM-001", "demo/p2")
    assert r.exit_code == 1 and "in flight" in r.output
    assert Store(garden).task("DM-001").phase == "p1"


def test_move_cli_refuses_the_same_phase(garden):
    r = run(garden, "move", "DM-001", "demo/p1")
    assert r.exit_code == 1 and "already in demo/p1" in r.output


def test_move_ready_task_into_a_frozen_phase_preserves_status_and_refuses_dispatch(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "freeze", "demo/p2").exit_code == 0

    r = run(garden, "move", "DM-001", "demo/p2")
    assert r.exit_code == 0, r.output
    t = Store(garden).task("DM-001")
    assert t.phase == "p2" and t.status.value == "ready"
    assert Store(garden).phase("demo", "p2").frozen
    with pytest.raises(RuntimeError, match="frozen"):
        Scheduler(Store(garden)).dispatch(t)


def test_move_pr_task_keeps_history_and_frozen_review_hold(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "freeze", "demo/p2").exit_code == 0
    store = Store(garden)
    t = store.task("DM-002")
    t.status = Status.IN_REVIEW
    t.branch, t.pr = "garden/DM-002", "https://example.test/demo/pull/42"
    t.log("review approved before the move")
    store.save(t)
    state = State(garden / ".garden" / "state.json")
    st = state.get(t.id)
    st.update({
        "pr_number": 42,
        "revisions": 2,
        "last_review": {"verdict": "approve"},
        "review_rounds": 2,
        "needs_human": {"kind": "review_cap", "reason": "review cap reached"},
    })
    state.save()
    runs = RunStore(garden / ".garden")
    completed = runs.new_run(t.id, "local", mode="review")
    completed.status = "done"
    completed.recovery_artifacts = [{"name": "review-notes", "path": "artifacts/review.md"}]
    completed.save()

    c = client(garden)
    r = c.post("/tasks/DM-002/move", data={"note": "demo/p2"}, follow_redirects=False)
    assert r.status_code == 303
    moved = Store(garden).task("DM-002")
    assert (moved.id, moved.status.value, moved.branch, moved.pr, moved.depends_on) == (
        "DM-002", "in_review", "garden/DM-002", "https://example.test/demo/pull/42", ["DM-001"])
    assert "review approved before the move" in moved.body
    retained_state = State(garden / ".garden" / "state.json").get("DM-002")
    assert retained_state["pr_number"] == 42
    assert retained_state["revisions"] == 2 and retained_state["last_review"] == {"verdict": "approve"}
    retained = RunStore(garden / ".garden").runs_for("DM-002")
    assert retained[0].run_id == completed.run_id and retained[0].recovery_artifacts == completed.recovery_artifacts
    page = c.get("/tasks/DM-002").text
    assert "Held by frozen phase" in page and "in review" in page and "will not start, revise, review, rebase, or merge" in page
    assert "Persona review" not in page

    # CLI and web use review_again(), whose frozen gate must run before the cap bypass
    # changes either of these preserved review-lifecycle fields.
    refused = run(garden, "review", "DM-002")
    assert refused.exit_code == 1 and "frozen" in refused.output
    refused = c.post("/tasks/DM-002/review", follow_redirects=True)
    assert "frozen" in refused.text
    retained_state = State(garden / ".garden" / "state.json").get("DM-002")
    assert retained_state["review_rounds"] == 2
    assert retained_state["needs_human"] == {"kind": "review_cap", "reason": "review cap reached"}

    refused = c.post("/tasks/DM-002/persona", data={"note": "security"}, follow_redirects=True)
    assert "frozen" in refused.text


def test_move_draft_into_a_frozen_phase_still_succeeds(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "freeze", "demo/p2").exit_code == 0
    assert run(garden, "new-task", "demo/p1", "Late idea").exit_code == 0  # DM-003, draft
    r = run(garden, "move", "DM-003", "demo/p2")
    assert r.exit_code == 0, r.output
    assert Store(garden).task("DM-003").phase == "p2"


def test_move_keeps_state_history_and_dependencies(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    gd = garden / ".garden"
    # a state entry and a run record, both keyed by the (stable) id
    st = State(gd / "state.json")
    st.get("DM-002")["pr_number"] = 42
    st.save()
    rs = RunStore(gd)
    r = rs.new_run("DM-002", "local", mode="work")
    r.status = "done"  # a finished run: only a live run blocks a move
    r.save()
    run_id = r.run_id

    assert run(garden, "move", "DM-002", "demo/p2").exit_code == 0

    t = Store(garden).task("DM-002")
    assert t.phase == "p2" and t.depends_on == ["DM-001"]  # deps by id survive
    assert State(gd / "state.json").get("DM-002")["pr_number"] == 42
    assert [r.run_id for r in RunStore(gd).runs_for("DM-002")] == [run_id]


def test_move_web_action_and_closed_phase_refusal(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    c = client(garden)
    r = c.post("/tasks/DM-001/move", data={"note": "demo/p2"}, follow_redirects=False)
    assert r.status_code == 303
    assert Store(garden).task("DM-001").phase == "p2"

    # the pulldown is on the task page and applies on change with no Set button
    page = c.get("/tasks/DM-001").text
    assert '/tasks/DM-001/move' in page and 'value="demo/p1"' in page and 'value="demo/p2" selected' in page

    assert run(garden, "close-phase", "demo/p1", "--force").exit_code == 0
    r = c.post("/tasks/DM-001/move", data={"note": "demo/p1"}, follow_redirects=True)
    assert "closed" in r.text
    assert Store(garden).task("DM-001").phase == "p2"  # unchanged


def test_task_page_warns_when_a_dependency_sits_in_a_later_phase(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    # DM-002 depends on DM-001; move the dependency into the later phase
    assert run(garden, "move", "DM-001", "demo/p2").exit_code == 0
    page = client(garden).get("/tasks/DM-002").text
    assert "Dependency in a later phase" in page
    # a dependency in the same or an earlier phase raises no warning
    assert "Dependency in a later phase" not in client(garden).get("/tasks/DM-001").text


def test_inbox_offers_move_for_a_draft_in_a_frozen_phase(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "new-task", "demo/p1", "Homeless idea").exit_code == 0  # DM-003, draft
    assert run(garden, "freeze", "demo/p1").exit_code == 0

    html = client(garden).get("/inbox").text
    assert "Deferred work" in html
    assert "deferred by the phase freeze" in html
    assert "need you</div>" in html and '<div class="v">0</div><div class="l">need you' in html
    assert "Move to p2" in html
    assert '/tasks/DM-003/move' in html and 'value="demo/p2"' in html
    assert '/tasks/DM-003/troubled-reconsider' not in html
