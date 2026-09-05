"""Approve says which phase the task joins and offers another (CG-186): the Inbox draft
card and the task page show "Approve into <phase>" with a pulldown of the product's open
phases, defaulting to the task's own phase; picking another phase moves then approves in
one request, with a frozen target refused unless the task carries a freeze exception."""

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.model import Status
from garden.store import Store
from garden.web.app import create_app
from tests.conftest import complete_brief

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


def test_task_page_defaults_the_pulldown_to_the_current_phase(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "new-task", "demo/p1", "Late idea").exit_code == 0  # DM-003, draft

    page = client(garden).get("/tasks/DM-003").text
    assert "Approve into <span data-approve-label>p1</span>" in page
    assert '/tasks/DM-003/approve' in page
    assert 'value="demo/p1" data-name="p1" selected' in page
    assert 'value="demo/p2" data-name="p2">' in page  # the other open phase is offered, not selected


def test_inbox_card_defaults_the_pulldown_to_the_current_phase(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "new-task", "demo/p1", "Late idea").exit_code == 0  # DM-003, draft

    html = client(garden).get("/inbox").text
    assert "Approve into <span data-approve-label>p1</span>" in html
    assert 'value="demo/p1" data-name="p1" selected' in html
    assert 'value="demo/p2" data-name="p2">' in html


def test_approve_into_another_phase_moves_then_approves(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "new-task", "demo/p1", "Late idea").exit_code == 0  # DM-003, draft
    complete_brief(garden, "DM-003")

    c = client(garden)
    r = c.post("/tasks/DM-003/approve", data={"note": "demo/p2"}, follow_redirects=False)
    assert r.status_code == 303

    t = Store(garden).task("DM-003")
    assert t.phase == "p2" and t.status == Status.READY
    assert t.path.exists() and t.path.parent.parent.name == "p2"


def test_approve_into_a_closed_phase_is_refused_and_leaves_the_task_a_draft(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "close-phase", "demo/p2", "--force").exit_code == 0
    assert run(garden, "new-task", "demo/p1", "Late idea").exit_code == 0  # DM-003, draft

    c = client(garden)
    r = c.post("/tasks/DM-003/approve", data={"note": "demo/p2"}, follow_redirects=True)
    assert "closed" in r.text
    t = Store(garden).task("DM-003")
    assert t.phase == "p1" and t.status == Status.DRAFT


def test_approve_into_a_frozen_phase_is_refused_without_a_freeze_exception(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "freeze", "demo/p2").exit_code == 0
    assert run(garden, "new-task", "demo/p1", "Late idea").exit_code == 0  # DM-003, draft

    c = client(garden)
    r = c.post("/tasks/DM-003/approve", data={"note": "demo/p2"}, follow_redirects=True)
    assert "frozen" in r.text
    t = Store(garden).task("DM-003")
    # the move (drafts may enter a frozen phase) went through; the approve itself was refused
    assert t.phase == "p2" and t.status == Status.DRAFT

    store = Store(garden)
    tt = store.task("DM-003")
    tt.freeze_exception = True
    tt.freeze_exception_reason = "hotfix"
    store.save(tt)
    complete_brief(garden, "DM-003")

    r = c.post("/tasks/DM-003/approve", data={"note": "demo/p2"}, follow_redirects=False)
    assert r.status_code == 303
    assert Store(garden).task("DM-003").status == Status.READY


def test_approve_pulldown_marks_a_frozen_phase(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "freeze", "demo/p2").exit_code == 0
    assert run(garden, "new-task", "demo/p1", "Late idea").exit_code == 0  # DM-003, draft

    html = client(garden).get("/inbox").text
    assert "p2 (frozen)" in html
