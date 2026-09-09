from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from garden.deepdives import publish_report, report_html, save_report
from garden.runs import RunStore
from garden.store import Store
from garden.web.app import create_app


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def test_report_redacts_secrets_and_renders_safe_clickable_html(tmp_path: Path) -> None:
    report = {
        "evidence": ["log: <script>alert('secret')</script>", "token=ghp_abcdefghijklmnopqrstuvwxyz1234",
                     "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"],
        "source_identities": ["run-1"],
        "observed_behavior": "failed", "intended_behavior": "pass", "likely_cause": "bad input",
        "confidence": "high", "unknowns": [], "impact": "blocked", "attempted_checks": ["pytest"],
        "corrective_action": "filed task", "links": ["https://example.test/task/1"],
        "recommendation": "change scope/approach",
    }
    md, rendered = save_report(tmp_path, "deep-1", "why?", report)
    assert "ghp_" not in md.read_text() and "eyJhbGci" not in md.read_text() and "[REDACTED]" in md.read_text()
    html = rendered.read_text()
    assert "<script>alert" not in html
    assert "<h2>Timeline and evidence</h2>" in html
    assert '<a href="https://example.test/task/1">' in html
    assert "ghp_" not in html
    assert report_html(md.read_text(), "deep-1") == html


def test_publication_uses_configured_workspace_remote_and_is_retry_safe(tmp_path: Path) -> None:
    remote, workspace = tmp_path / "remote.git", tmp_path / "workspace"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    _git("clone", str(remote), str(workspace), cwd=tmp_path)
    _git("switch", "-c", "main", cwd=workspace)
    (workspace / "garden.yaml").write_text("name: fixture\n")
    _git("add", "garden.yaml", cwd=workspace)
    _git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "seed", cwd=workspace)
    _git("push", "-u", "origin", "main", cwd=workspace)
    md, html_path = tmp_path / "report.md", tmp_path / "report.html"
    md.write_text("# Report\n")
    html_path.write_text("<html>report</html>")

    first = publish_report(workspace, "deep-1", md, html_path)
    assert first["branch"] == "main" and first["remote"] == str(remote)
    retry = publish_report(workspace, "deep-1", md, html_path)
    assert retry["commit"] == first["commit"]

    checkout = tmp_path / "check"
    _git("clone", "--branch", "main", str(remote), str(checkout), cwd=tmp_path)
    assert (checkout / first["markdown"]).read_text() == "# Report\n"
    assert (checkout / first["html"]).read_text() == "<html>report</html>"


def test_task_deep_dive_action_and_durable_report_routes(garden: Path) -> None:
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    page = client.get("/tasks/DM-001")
    assert "Deep dive…" in page.text
    response = client.post("/tasks/DM-001/investigate", data={"note": "why did it stop?", "applies_to": "agent"})
    assert response.status_code == 200
    state = client.app.state.hub.reader().state.get("DM-001")["investigation"]
    assert state["reason"] == "why did it stop?" and state["owner"] == "agent"

    run = RunStore(garden / ".garden").new_run("DM-001", "local", mode="investigation")
    (run.path / "report.md").write_text("# safe source\n")
    (run.path / "report.html").write_text("<html><body>&lt;script&gt;bad&lt;/script&gt;</body></html>")
    assert client.get(f"/investigations/DM-001/{run.run_id}/report.md").text == "# safe source\n"
    rendered = client.get(f"/investigations/DM-001/{run.run_id}/report.html")
    assert rendered.status_code == 200 and "default-src 'none'" in rendered.headers["content-security-policy"]


def test_task_independent_incident_creates_durable_investigation_anchor(garden: Path) -> None:
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))
    page = client.get("/inbox")
    assert "Garden incident deep dive" in page.text
    response = client.post("/investigations", data={
        "scope": "demo/p1", "question": "Why did several scheduler events disappear?",
        "references": "event 42; run external-7",
    }, follow_redirects=False)
    assert response.status_code == 303
    task_id = response.headers["location"].removeprefix("/tasks/")
    store = Store(garden)
    task = store.task(task_id)
    assert task.kind == "investigation" and "event 42" in task.body
    inv = client.app.state.hub.reader().state.get(task_id)["investigation"]
    assert inv["owner"] == "agent" and inv["origins"]["references"] == "event 42; run external-7"
    repeated = client.post("/investigations", data={
        "scope": "demo/p1", "question": "Why did several scheduler events disappear?",
        "references": "event 42; run external-7",
    }, follow_redirects=False)
    assert repeated.headers["location"] == response.headers["location"]
