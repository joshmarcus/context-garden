"""Retro questions use kickoff cards, including web/CLI resolution and verdict gating."""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.planner import plan_prompt
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app
from tests.test_retro import _garden_repo, _live_garden, _register_prs


@pytest.mark.parametrize("landed", [False, True])
def test_retro_questions_web_and_cli(tmp_path, fake_github, monkeypatch, landed):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_QUESTIONS", "1")
    root = _live_garden(tmp_path, repo=_garden_repo(tmp_path), work_dir=str(tmp_path / "work"))
    monkeypatch.setattr("garden.cli.state._store", lambda: Store(root))
    store = Store(root)
    sched = Scheduler(store, github=fake_github)
    _register_prs(fake_github)
    phase = store.phase("gdn", "p1")
    sched.start_retro(phase, ["designer"], skip_personas=True)
    report = sched.tick()
    assert not report.errors
    questions = sched.retro_questions(phase.key)
    assert len(questions) == 2
    assert all(q["kind"] == "question" and q["proposed_by"] == "retro:gdn/p1" for q in questions)
    wt = store.config.worktree_path("_retro-gdn-p1")
    if landed:
        shutil.copytree(wt / "gdn", root / "gdn", dirs_exist_ok=True)
    client = TestClient(create_app(Store(root), watch=False, github=fake_github, host="testserver"))
    inbox = client.get("/inbox").text
    assert all(q["question"] in inbox for q in questions)
    assert "retro is asking" in inbox
    assert questions[0]["question"] in client.get("/phases/gdn/p1/retro").text
    with pytest.raises(RuntimeError, match="blocking retro questions"):
        sched.retro_decide(phase, "reopen")
    assert client.post(f"/decisions/{questions[0]['id']}/dismiss").status_code == 409
    assert client.post(f"/decisions/{questions[0]['id']}/answer", data={"answer": "  "}).status_code == 409
    assert client.post(f"/decisions/{questions[0]['id']}/answer", data={"answer": "Support stable."},
                       follow_redirects=False).status_code == 303
    result = CliRunner().invoke(app, ["decide", questions[1]["id"], "--answer", "Prioritize observability."])
    assert result.exit_code == 0, result.output
    sched = Scheduler(Store(root), github=fake_github)
    resolved = sched.retro_questions(phase.key)
    assert [q["status"] for q in resolved] == ["answered", "answered"]
    assert [q["resolved_by"] for q in resolved] == ["web", "cli"]
    assert not [q for q in sched.pending_decisions() if q["kind"] == "question"]
    docs_root = root if landed else wt
    retro = (docs_root / "gdn/p1/docs/retro.md").read_text()
    goals = (docs_root / "gdn/p2/goals.md").read_text()
    assert "## Answers" in retro and "## Decisions" in goals
    for q in resolved:
        for doc in (retro, goals):
            assert q["answer"] in doc and q["resolved_at"] in doc and q["resolved_by"] in doc
    if not landed:
        # Model the retro PR landing: planning reads the committed goals, not side-state.
        shutil.copytree(wt / "gdn", root / "gdn", dirs_exist_ok=True)
    prompt = plan_prompt(Store(root), "gdn", "p2")
    assert "Support stable." in prompt and "Prioritize observability." in prompt
    page = client.get("/phases/gdn/p1/retro").text
    assert "Support stable." in page and "Prioritize observability." in page
    sched = Scheduler(Store(root), github=fake_github)
    assert sched.retro_decide(sched.store.phase("gdn", "p1"), "reopen")["status"] == "accepted"


def test_question_dismissal_and_failed_write_keep_state(tmp_path, fake_github, monkeypatch):
    from garden import gitops

    monkeypatch.setenv("FAKE_CLAUDE_RETRO_QUESTIONS", "1")
    root = _live_garden(tmp_path, repo=_garden_repo(tmp_path), work_dir=str(tmp_path / "work"))
    sched = Scheduler(Store(root), github=fake_github)
    _register_prs(fake_github)
    sched.start_retro(sched.store.phase("gdn", "p1"), ["designer"], skip_personas=True)
    assert not sched.tick().errors
    question = sched.retro_questions("gdn/p1")[1]
    push = gitops.push

    def fail(*args, **kwargs):
        raise RuntimeError("push unavailable")

    monkeypatch.setattr(gitops, "push", fail)
    with pytest.raises(RuntimeError, match="push unavailable"):
        sched.dismiss_question(question["id"])
    assert any(q["id"] == question["id"] for q in sched.pending_decisions())
    monkeypatch.setattr(gitops, "push", push)
    result = sched.dismiss_question(question["id"])
    assert result["status"] == "dismissed"
    wt = sched.cfg.worktree_path("_retro-gdn-p1")
    assert (wt / question["retro_path"]).read_text().count(f"<!-- decision:{question['id']} -->") == 1
