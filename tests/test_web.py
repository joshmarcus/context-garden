from fastapi.testclient import TestClient

from garden.store import Store
from garden.web.app import create_app


def client(garden):
    return TestClient(create_app(Store(garden), watch=False))


def test_pages_render(garden):
    c = client(garden)
    for url in ["/", "/graph", "/runs", "/phases/demo/p1", "/tasks/DM-001", "/tasks/DM-001/brief", "/partials/board", "/api/tasks"]:
        r = c.get(url)
        assert r.status_code == 200, url
    assert "DM-002" in c.get("/").text
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
