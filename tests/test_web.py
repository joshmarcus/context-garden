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


def test_drawings_render_unescaped(garden, tmp_path):
    """Plant and stage drawings are inline SVG, not escaped text (a Jinja autoescape regression)."""
    c = TestClient(create_app(Store(garden), watch=False, plates_dir=tmp_path / "plates"))
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


def test_scanned_plates_replace_the_drawing_when_present(garden, tmp_path):
    plates = tmp_path / "plates"
    c = TestClient(create_app(Store(garden), watch=False, plates_dir=plates))
    html = c.get("/phases/demo/p1").text
    assert '<use href="#pea"/>' in html and 'class="plate"' not in html  # nothing fetched yet: the drawing
    (plates / "pea.webp").write_bytes(b"RIFF....WEBP")
    html = c.get("/phases/demo/p1").text
    assert '<img class="plate" src="/static/plates/pea.webp"' in html
    assert "plate: Thomé, Flora von Deutschland, 1885" in html
    assert c.get("/static/plates/pea.webp").status_code == 200
    assert c.get("/static/plates/bramble.webp").status_code == 404
    # the rail thumbnail uses the thumb file, or the plate itself until a thumb exists
    assert 'src="/static/plates/pea.webp" alt="" width="38"' in html
    (plates / "pea-thumb.webp").write_bytes(b"RIFF....WEBP")
    assert 'src="/static/plates/pea-thumb.webp"' in c.get("/").text


def test_friction_report_web(garden):
    c = client(garden)
    r = c.post(
        "/friction-report",
        data={"product": "demo", "phase": "p1", "text": "The form is confusing.", "page": "/inbox"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    assert doc.exists()
    text = doc.read_text()
    assert "## Reported" in text
    assert "The form is confusing." in text
    # A draft task was created
    from garden.store import Store
    tasks = Store(garden).tasks()
    friction_tasks = [t for t in tasks.values() if "confusing" in t.title]
    assert friction_tasks, "expected a draft task for the friction report"
    assert friction_tasks[0].status.value == "draft"


def test_friction_report_web_with_task_id(garden):
    c = client(garden)
    r = c.post(
        "/friction-report",
        data={"product": "demo", "phase": "p1", "text": "Brief is too long.", "page": "/tasks/DM-001", "task_id": "DM-001"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    text = doc.read_text()
    assert "DM-001" in text
    assert "Brief is too long." in text


def test_friction_form_in_inbox_and_task(garden):
    c = client(garden)
    assert "Report friction" in c.get("/").text
    assert "Report friction" in c.get("/tasks/DM-001").text
