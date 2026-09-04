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


def test_trial_with_one_contender_shows_a_message_not_a_500(garden):
    c = client(garden)
    r = c.post("/tasks/DM-001/trial", data={"note": "claude:sonnet"}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "a trial needs at least two contenders, e.g. claude:sonnet, claude:opus" in page


def test_scheduler_errors_flash_a_message_instead_of_500(garden):
    """CG-092: a task whose precondition changed underneath the person (here: DM-001 is
    'ready', not 'waiting_human') must say so on the page, not 500 or silently drop the
    submitted text."""
    c = client(garden)
    r = c.post("/tasks/DM-001/answer", data={"note": "SQLite, please"}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "no longer waiting for you" in page
    assert "SQLite, please" in page  # the typed answer is preserved, not lost

    r = c.post("/tasks/DM-001/reject", data={"note": "no"}, follow_redirects=False)
    assert r.status_code == 303
    assert "has no pending worker decision to reject" in c.get(r.headers["location"]).text


def test_unexpected_action_exception_shows_a_generic_message(garden, monkeypatch):
    from garden.scheduler import Scheduler

    def boom(self, task, note="cancelled"):
        raise ValueError("boom")

    monkeypatch.setattr(Scheduler, "cancel", boom)
    c = client(garden)
    r = c.post("/tasks/DM-001/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert "something failed; see the log" in c.get(r.headers["location"]).text


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


def test_stdout_partial(garden):
    c = client(garden)
    r = c.get("/partials/tasks/DM-001/stdout")
    assert r.status_code == 200
    assert "no output yet" in r.text

    # Write JSONL events into a fake run dir and verify the partial reflects them
    import json

    from garden.runs import RunStore
    from garden.store import Store
    store = Store(garden)
    rs = RunStore(store.config.garden_dir)
    run = rs.new_run("DM-001", "local", "work")
    (run.path / "stdout.json").write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}) + "\n" +
        json.dumps({"type": "result", "subtype": "success", "result": "Done."}) + "\n"
    )
    r = c.get("/partials/tasks/DM-001/stdout")
    assert r.status_code == 200
    assert "Bash" in r.text and "ls" in r.text


def test_stdout_partial_handles_string_and_list_tool_result_content(garden):
    """A stream-json run mixes tool_result.content shapes (string and list of blocks); the
    task page must render both instead of 500ing (CG-104)."""
    import yaml

    from garden.scheduler import Scheduler
    from garden.store import Store
    from tests.conftest import FakeGitHub, wait_for_runs

    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["harnesses"]["claude"]["output_format"] = "stream-json"
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(garden)
    sched = Scheduler(store, github=FakeGitHub())
    sched.tick()
    wait_for_runs(sched)

    c = client(garden)
    r = c.get("/tasks/DM-001")
    assert r.status_code == 200
    assert "abc1234 fake change" in r.text  # string tool_result.content
    assert "working" in r.text  # list tool_result.content, first text block

    r = c.get("/partials/tasks/DM-001/stdout")
    assert r.status_code == 200
    assert "abc1234 fake change" in r.text and "working" in r.text


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


def test_config_page_renders(garden):
    c = client(garden)
    r = c.get("/config")
    assert r.status_code == 200
    assert "Pause" in r.text
    assert "max_parallel" in r.text
    assert "auto_dispatch" in r.text
    assert "Config" in r.text


def test_pause_resume_web(garden):
    c = client(garden)
    # not paused by default
    assert "dispatch paused" not in c.get("/").text
    # pause via web
    r = c.post("/pause", data={"reason": "testing"}, follow_redirects=False)
    assert r.status_code == 303
    home = c.get("/").text
    assert "dispatch paused" in home
    config_page = c.get("/config").text
    assert "testing" in config_page
    assert "Resume dispatch" in config_page
    # resume via web
    r = c.post("/resume", follow_redirects=False)
    assert r.status_code == 303
    home = c.get("/").text
    assert "dispatch paused" not in home
    config_page = c.get("/config").text
    assert "Pause dispatch" in config_page


def test_priority_and_difficulty_from_the_task_page(garden):
    from garden.store import Store

    c = client(garden)
    r = c.post("/tasks/DM-001/difficulty", data={"note": "hard"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/tasks/DM-001/priority", data={"note": "0"}, follow_redirects=False)
    assert r.status_code == 303
    t = Store(garden).task("DM-001")
    assert t.difficulty == "hard" and t.priority == 0
    assert "difficulty medium -> hard (web)" in t.body and "priority" in t.body
    assert c.post("/tasks/DM-001/difficulty", data={"note": "extreme"}, follow_redirects=False).status_code == 400
    page = c.get("/tasks/DM-001").text
    assert 'name="note"' in page and 'value="hard" selected' in page
