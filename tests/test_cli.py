import json

from typer.testing import CliRunner

from garden.cli import app

runner = CliRunner()


def run(garden, *args):
    import os

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)


def test_status_ls_graph_validate(garden):
    r = run(garden, "status")
    assert r.exit_code == 0, r.output
    r = run(garden, "ls", "--json")
    data = json.loads(r.output)
    assert {d["id"] for d in data} == {"DM-001", "DM-002"}
    assert next(d for d in data if d["id"] == "DM-002")["effective_status"] == "blocked"
    r = run(garden, "graph", "--format", "mermaid")
    assert "DM_001 --> DM_002" in r.output
    assert run(garden, "validate").exit_code == 0


def test_new_task_and_approve(garden):
    r = run(garden, "new-task", "demo/p1", "Third: thing", "--dep", "DM-001", "--read", "demo/p1/specs/spec.md")
    assert r.exit_code == 0 and "DM-003" in r.output
    r = run(garden, "approve", "DM-003")
    assert "DM-003 -> ready" in r.output
    r = run(garden, "show", "DM-003")
    assert "Third: thing" in r.output and "ready" in r.output


def test_brief_stats(garden):
    r = run(garden, "brief", "DM-001", "--stats")
    assert r.exit_code == 0 and "tokens" in r.output


def test_plan_import(garden, tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps([{"title": "Imported", "body": "## Goal\n\nx"}]))
    r = run(garden, "plan", "demo/p1", "--import", str(f))
    assert r.exit_code == 0 and "Imported" in r.output


def test_friction(garden):
    from garden.runs import RunStore

    rs = RunStore(garden / ".garden")
    r0 = rs.new_run("DM-001", "manual", "work")
    r0.status = "done"
    r0.result = {
        "status": "done",
        "pr_body": "## Summary\n\nDid the thing.\n\n## Friction\n\nNo docs for the X module.\n\n## Notes\n\nAll good.",
    }
    r0.save()

    r = run(garden, "friction", "demo/p1")
    assert r.exit_code == 0, r.output
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    assert doc.exists()
    text = doc.read_text()
    assert "DM-001" in text
    assert "No docs for the X module." in text

    # Running again produces identical output (idempotent)
    r2 = run(garden, "friction", "demo/p1")
    assert r2.exit_code == 0
    assert doc.read_text() == text


def test_friction_no_github_fallback_needed(garden):
    """Tasks without runs produce an empty friction file."""
    r = run(garden, "friction", "demo/p1")
    assert r.exit_code == 0, r.output
    doc = garden / "demo" / "p1" / "docs" / "friction.md"
    assert doc.exists()
    assert "No friction reported yet." in doc.read_text()


def test_init_scaffold(tmp_path):
    r = runner.invoke(app, ["init", str(tmp_path / "g"), "--name", "x"])
    assert r.exit_code == 0 and (tmp_path / "g" / "garden.yaml").exists()
    import os

    cwd = os.getcwd()
    os.chdir(tmp_path / "g")
    try:
        r = runner.invoke(app, ["new-product", "widget", "--repo", "../widget"])
        assert r.exit_code == 0, r.output
        r = runner.invoke(app, ["new-phase", "widget", "phase-01"])
        assert r.exit_code == 0 and (tmp_path / "g" / "widget" / "phase-01" / "goals.md").exists()
    finally:
        os.chdir(cwd)
