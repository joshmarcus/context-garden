from fastapi.testclient import TestClient

from garden.store import Store
from garden.web.app import create_app


def client(garden):
    return TestClient(create_app(Store(garden), watch=False))


def test_pages_render(garden):
    c = client(garden)
    for url in ["/", "/board", "/trellis", "/runs", "/phases/demo/p1", "/tasks/DM-001", "/tasks/DM-001/brief", "/partials/board", "/api/tasks", "/events", "/trials"]:
        r = c.get(url)
        assert r.status_code == 200, url
    assert "DM-002" in c.get("/board").text
    assert "Inbox zero" in c.get("/").text
    assert c.get("/tasks/NOPE").status_code == 404


def test_actions(garden):
    c = client(garden)
    r = c.post("/tasks/DM-002/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert "cancelled" in c.get("/tasks/DM-002").text
    c.post("/tasks/DM-002/retry")
    assert "blocked" in c.get("/tasks/DM-002").text
    c.post("/tasks/DM-001/unapprove")
    assert "draft" in c.get("/api/tasks").json()[0]["status"]
    c.post("/phases/demo/p1/approve-all")
    assert c.get("/api/tasks").json()[0]["status"] == "ready"


def test_events_page_and_answer_flow(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub, wait_for_runs

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched = Scheduler(Store(garden), github=FakeGitHub())
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    c = client(garden)
    page = c.get("/tasks/DM-001").text
    assert "waiting for you" in page and "Postgres or SQLite?" in page
    assert c.get("/events").status_code == 200 and "waiting_human" in c.get("/events").text
    assert "Q: Postgres" in c.get("/partials/board").text
    r = c.post("/tasks/DM-001/answer", data={"note": "SQLite"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/api/tasks").json()[0]["status"] == "running"
    page = c.get("/tasks/DM-001").text
    assert "Questions and answers" in page and "SQLite" in page and "Timeline" in page


def test_trials_page_and_persona_form(garden):
    c = client(garden)
    r = c.get("/trials")
    assert r.status_code == 200 and "No trials yet" in r.text
    assert "Persona review of the body of work" in c.get("/phases/demo/p1").text
    assert c.get("/trellis").status_code == 200 and c.get("/graph").status_code == 200


def test_inbox_triage_flow(garden, monkeypatch):
    import yaml

    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub, wait_for_runs

    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["github"] = {"draft_pr": True}
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(garden)
    gh = FakeGitHub()
    sched = Scheduler(store, github=gh)
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    c = TestClient(create_app(store, watch=False))
    home = c.get("/").text
    assert "Triage a draft PR" in home and "DM-001" in home and "Ready for review" in home
    r = c.post("/tasks/DM-001/triage-changes", data={"note": "tighten the tests"}, headers={"referer": "http://t/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/")
    assert next(t for t in c.get("/api/tasks").json() if t["id"] == "DM-001")["status"] == "changes_requested"
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    assert "awaiting_triage" in next(t for t in c.get("/api/tasks").json() if t["id"] == "DM-001")["status"]
    c.post("/tasks/DM-001/triage-ready", follow_redirects=False)
    assert next(t for t in c.get("/api/tasks").json() if t["id"] == "DM-001")["status"] == "in_review"
    assert "Review and merge" in c.get("/").text


def test_drawings_render_unescaped(garden):
    """Plant and stage drawings are inline SVG, not escaped text (a Jinja autoescape regression)."""
    c = client(garden)
    for url in ["/", "/board", "/phases/demo/p1", "/tasks/DM-001", "/trellis"]:
        html = c.get(url).text
        assert "&lt;svg" not in html, url
        assert '<use href="#pea"/>' in html, url  # the rail shows every phase's plant
        if url != "/":  # the fixture inbox is empty, so it shows no stage glyphs
            assert '<use href="#st-' in html, url
    phase = c.get("/phases/demo/p1").text
    assert '<use href="#pea"/>' in phase
    assert "Plate I" in phase
    assert phase.count('class="bg-vine"') == 1  # the background vine, once per page
