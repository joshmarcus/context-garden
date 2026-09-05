"""The backlog view (CG-163): a product's open phases as stacked sections, drag (or the ↑/↓
buttons and the phase pulldown) to reorder a task within a phase or move it to another. Covers
the `order` action, the multi-phase render, the button/pulldown no-JS path, and the refusals."""

import os

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.model import dispatch_sort_key
from garden.store import Store
from garden.web.app import create_app

runner = CliRunner()


def run(garden, *args):
    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)


def client(garden):
    return TestClient(create_app(Store(garden), watch=False))


def p1_order(garden, priority=None):
    tasks = [t for t in Store(garden).tasks().values() if t.phase == "p1"]
    if priority is not None:
        tasks = [t for t in tasks if t.priority == priority]
    return [t.id for t in sorted(tasks, key=dispatch_sort_key)]


def test_backlog_shows_two_phases_as_sections_with_freeze_marker(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "freeze", "demo/p2").exit_code == 0
    c = client(garden)
    page = c.get("/board?product=demo&view=backlog")
    assert page.status_code == 200
    assert 'class="backlog"' in page.text
    # both open phases render as sections, in phase order, p1 before p2
    assert 'data-phase="demo/p1"' in page.text and 'data-phase="demo/p2"' in page.text
    assert page.text.index("demo/p1") < page.text.index("demo/p2")
    # the frozen phase carries the freeze marker
    assert "frozen" in page.text
    # the tasks show as draggable rows
    assert 'data-task="DM-001"' in page.text and 'draggable="true"' in page.text
    # reachable through the live-refresh partial too
    assert 'class="backlog"' in c.get("/partials/board?product=demo&view=backlog").text
    # with no product picked the backlog shows every product's open phases
    allp = c.get("/board?view=backlog")
    assert allp.status_code == 200 and 'data-phase="demo/p1"' in allp.text


def test_order_action_persists_order_and_crosses_a_band(garden):
    c = client(garden)
    # DM-001 is priority 1, DM-002 priority 2, both in demo/p1. Drop DM-002 at the top of the
    # section: it crosses into the priority-1 band and takes rank 0.
    r = c.post("/tasks/DM-002/order", data={"note": ""}, follow_redirects=False)
    assert r.status_code == 303
    t1, t2 = Store(garden).task("DM-001"), Store(garden).task("DM-002")
    assert t2.priority == 1 and t2.order == 0
    assert t1.order == 1
    assert "order: 0" in t2.path.read_text()  # the new frontmatter field is written
    # the shared sort key (ready set + dispatch) now ranks DM-002 ahead of DM-001
    assert p1_order(garden) == ["DM-002", "DM-001"]


def test_move_up_down_buttons_reorder_without_js(garden):
    # two independent ready tasks that share a priority band
    assert run(garden, "new-task", "demo/p1", "Alpha").exit_code == 0  # DM-003, draft, priority 3
    assert run(garden, "new-task", "demo/p1", "Beta").exit_code == 0   # DM-004, draft, priority 3
    for tid in ("DM-003", "DM-004"):
        assert run(garden, "approve", tid).exit_code == 0
    assert p1_order(garden, priority=3) == ["DM-003", "DM-004"]  # by id to start

    c = client(garden)
    # the ↑ button posts note=up to the order action; DM-004 rises above DM-003, staying in p3
    r = c.post("/tasks/DM-004/order", data={"note": "up"}, follow_redirects=False)
    assert r.status_code == 303
    assert p1_order(garden, priority=3) == ["DM-004", "DM-003"]
    assert Store(garden).task("DM-004").priority == 3  # a same-band reorder does not change the band

    # ↓ puts it back
    c.post("/tasks/DM-004/order", data={"note": "down"}, follow_redirects=False)
    assert p1_order(garden, priority=3) == ["DM-003", "DM-004"]


def test_drag_into_another_phase_moves_the_task(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    c = client(garden)
    # a within-phase drop is an order POST; a cross-phase drop is the CG-162 move POST
    r = c.post("/tasks/DM-001/move", data={"note": "demo/p2"}, follow_redirects=False)
    assert r.status_code == 303
    assert Store(garden).task("DM-001").phase == "p2"


def test_move_into_a_closed_phase_flashes_and_leaves_the_row(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "close-phase", "demo/p2", "--force").exit_code == 0
    c = client(garden)
    r = c.post("/tasks/DM-001/move", data={"note": "demo/p2"},
               headers={"referer": "http://testserver/board?product=demo&view=backlog"},
               follow_redirects=True)
    assert "closed" in r.text  # the refusal shows as a flash
    assert Store(garden).task("DM-001").phase == "p1"  # the row stays put


def test_running_task_can_reorder_but_not_move(garden):
    assert run(garden, "new-phase", "demo", "p2").exit_code == 0
    assert run(garden, "set-status", "DM-002", "running").exit_code == 0
    c = client(garden)
    # the backlog marks a running row as not movable, with the reason on hover
    page = c.get("/board?product=demo&view=backlog").text
    assert 'data-task="DM-002" data-movable="0"' in page
    # a move is refused server-side...
    r = c.post("/tasks/DM-002/move", data={"note": "demo/p2"},
               headers={"referer": "http://testserver/board?product=demo&view=backlog"},
               follow_redirects=True)
    assert "in flight" in r.text
    assert Store(garden).task("DM-002").phase == "p1"
    # ...but a reorder still works
    r = c.post("/tasks/DM-002/order", data={"note": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert Store(garden).task("DM-002").order == 0
