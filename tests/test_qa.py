"""`garden qa` (CG-135): an agent drives the loop end to end through the web app on a
throwaway garden. The scripted agent is the flows as code; the harness path is exercised
with tests/fake_claude.py in its `qa` mode."""

import json
import os
import re
from pathlib import Path

from typer.testing import CliRunner

from garden.cli import app
from garden.qa import _normalise, qa_brief, run_qa
from garden.qa.flows import FLOWS, Flow, FlowFailed


def run(cwd, *args):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(old)


def test_scripted_agent_completes_every_flow(tmp_path):
    out = tmp_path / "qa"
    report = run_qa(out, scripted=True)
    assert report.ok, report.summary()
    assert [f["name"] for f in report.flows] == [f.name for f in FLOWS]
    assert report.findings == []
    assert "every flow completed (9 of 9)" in report.summary()
    # the run directory keeps the agent's result and every page the app served
    assert json.loads((out / "result.json").read_text())["flows"][0]["ok"] is True
    pages = sorted((out / "pages").glob("*.html"))
    assert pages and any("tasks-dm-003" in p.name for p in pages)
    assert "DM-003" in next(p for p in pages if "tasks-dm-003" in p.name).read_text()
    # the throwaway garden is gone without --keep
    assert not (out / "garden").exists() and report.garden is None


def test_a_broken_flow_names_the_step_and_exits_non_zero(tmp_path, monkeypatch):
    import garden.qa as qa_pkg
    import garden.qa.flows as flows_mod

    def missing_button(c):
        c.get("/inbox")
        raise FlowFailed("the Inbox shows no 'Ready for review' button")

    broken = [Flow(f.name, f.page, f.script, missing_button) if f.name == "triage" else f for f in FLOWS]
    monkeypatch.setattr(flows_mod, "FLOWS", broken)
    monkeypatch.setattr(qa_pkg, "FLOWS", broken)
    out = tmp_path / "qa"
    r = run(tmp_path, "qa", "--scripted", "--keep", "--out", str(out))  # no garden around: nothing is filed
    assert r.exit_code == 1, r.output
    assert "FAILED at 'triage' (/inbox): the Inbox shows no 'Ready for review' button" in r.output
    assert "ok   dispatch" in r.output and "FAIL triage" in r.output
    findings = json.loads((out / "findings.json").read_text())
    assert len(findings) == 1 and findings[0]["page"] == "/inbox"
    assert Path(findings[0]["html"]).exists() and "Inbox" in Path(findings[0]["html"]).read_text()
    # later flows were not attempted, and --keep left the throwaway garden in place
    assert not any(f["ok"] for f in json.loads((out / "result.json").read_text())["flows"][6:])
    assert (out / "garden" / "garden.yaml").exists()


def test_harness_agent_findings_become_friction_reports_with_the_page(garden, tmp_path):
    """The real path: the brief goes to the garden's harness (here tests/fake_claude.py, which
    reports every flow done and one finding on the Inbox); each finding is filed on the phase
    with the page it was seen on and that page's HTML kept in the run directory."""
    out = tmp_path / "qa"
    r = run(garden, "qa", "--phase", "demo/p1", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert "every flow completed" in r.output and "filed as friction reports: /inbox -> DM-003" in r.output
    brief = (out / "agent" / "brief.md").read_text()
    assert re.search(r"http://127\.0\.0\.1:\d+", brief) and "GARDEN_QA:" in brief
    for f in FLOWS:
        assert f"**{f.name}**" in brief
    text = (garden / "demo" / "p1" / "docs" / "friction.md").read_text()
    assert "garden qa · /inbox" in text and "no link back to the phase" in text
    html_path = re.search(r"Page as seen: `(.+?)`", text).group(1)
    assert Path(html_path).exists() and "Inbox" in Path(html_path).read_text()
    from garden.store import Store

    t = Store(garden).task("DM-003")
    assert t.status.value == "draft" and "garden qa · /inbox" in t.body
    assert json.loads((out / "agent" / "run.json").read_text())["cost_usd"] == 0.10


def test_harness_agent_that_fails_a_flow(garden, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_QA_FAIL", "merge")
    r = run(garden, "qa", "--phase", "demo/p1", "--no-task", "--out", str(tmp_path / "qa"))
    assert r.exit_code == 1, r.output
    assert "FAILED at 'merge' (/inbox): the button was missing" in r.output
    from garden.store import Store

    assert "DM-003" not in Store(garden).tasks()  # --no-task: the record only
    assert "garden qa · /inbox" in (garden / "demo" / "p1" / "docs" / "friction.md").read_text()


def test_qa_refuses_an_unknown_phase(garden, tmp_path):
    r = run(garden, "qa", "--scripted", "--phase", "demo/nope", "--out", str(tmp_path / "qa"))
    assert r.exit_code == 1 and "no phase" in r.output


def test_normalise_treats_an_unreported_flow_as_a_failure():
    flows, findings = _normalise({"flows": [{"name": "Add a task", "ok": True, "page": "/phases/demo/p1"}],
                                  "findings": ["a bare string finding", {"page": "/x", "text": "  spaced  "}, {"text": ""}]})
    assert flows[0] == {"name": "add a task", "ok": True, "page": "/phases/demo/p1", "note": ""}
    assert all(not f["ok"] and f["note"] == "not reported by the agent" for f in flows[1:])
    assert findings == [{"page": "", "text": "a bare string finding"}, {"page": "/x", "text": "spaced"}]
    assert "http://x" in qa_brief("http://x")
