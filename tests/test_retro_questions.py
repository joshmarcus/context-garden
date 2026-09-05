"""CG-225: the retro's "questions for the owner" become decision cards through the same
mechanism the kickoff uses (CG-224) — one question/decision path, not two."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.model import Phase
from garden.retro import (
    append_retro_answer,
    append_retro_decision_to_goals,
    questions_section,
    render_retro_doc,
)
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app

# reuse the end-to-end fixtures from the retro tests
from tests.test_retro import _garden_repo, _live_garden, _register_prs

runner = CliRunner()


def _cli(root, *args):
    cwd = os.getcwd()
    os.chdir(root)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)


def _merge_into_live(store: Store, root: Path, next_phase: str = "p2") -> None:
    """Simulate the retro PR merging: copy the retro document and the next-phase goals draft
    the retro wrote in its worktree into the live garden, where questions get resolved."""
    import shutil

    wt = store.config.worktree_path("_retro-gdn-p1")
    src, dst = wt / "gdn" / "p1" / "docs" / "retro.md", root / "gdn" / "p1" / "docs" / "retro.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    gsrc, gdst = wt / "gdn" / next_phase / "goals.md", root / "gdn" / next_phase / "goals.md"
    gdst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(gsrc, gdst)


# --------------------------------------------------------------------------- pure logic
def test_questions_section_marks_blocking_and_the_decision_card():
    items = [
        {"question": "Which db?", "context": "matters", "options": ["a", "b"], "blocking": True},
        {"question": "Rename it?", "context": "", "options": []},
    ]
    filed = [{"question": "Which db?", "decision_id": "run1-q0"}]
    text = questions_section(items, filed)
    assert "Which db?" in text and "run1-q0" in text and "_(blocking the verdict)_" in text
    assert "Rename it?" in text and "_(blocking the verdict)_" not in text.split("Rename it?")[1]
    assert questions_section([], []) == "_No open questions._"


def test_render_retro_doc_includes_questions_and_an_answers_placeholder():
    phase = Phase(product="p", name="ph1", path=Path("/x/p/ph1"), goals_path=None, specs=[], docs=[], tasks=[])
    rev = {"reconciliation": [], "summary": "s", "questions": [{"question": "Which db?", "blocking": True}]}
    filed_questions = [{"question": "Which db?", "decision_id": "run1-q0"}]
    doc = render_retro_doc(phase, rev, {}, None, filed_questions=filed_questions)
    assert "## Questions for the owner" in doc and "Which db?" in doc and "run1-q0" in doc
    assert "## Answers" in doc and "_Not yet answered._" in doc
    # no questions -> no Answers section at all
    doc2 = render_retro_doc(phase, {"reconciliation": [], "summary": "s"}, {}, None)
    assert "## Answers" not in doc2
    assert "_No open questions._" in doc2


def test_append_retro_answer_replaces_the_placeholder_and_creates_the_heading(tmp_path):
    path = tmp_path / "retro.md"
    path.write_text("# Retro\n\n## Answers\n\n_Not yet answered._\n\n## Findings\n\nx\n")
    append_retro_answer(path, "Which db?", "answered", "postgres", "cli", "2026-01-02T00:00:00+00:00")
    text = path.read_text()
    assert "_Not yet answered._" not in text
    assert "**Which db?** — answered by cli on 2026-01-02: postgres" in text
    assert "## Findings" in text  # the section after Answers survives

    # a second answer appends alongside the first, under the same heading
    append_retro_answer(path, "Rename it?", "dismissed", "", "web", "2026-01-03T00:00:00+00:00")
    text = path.read_text()
    assert "**Which db?**" in text and "**Rename it?** — dismissed by web on 2026-01-03" in text

    # a no-op when the document is not live yet
    append_retro_answer(tmp_path / "missing.md", "x", "answered", "y", "cli", "2026-01-01T00:00:00+00:00")


def test_append_retro_decision_to_goals_creates_the_heading(tmp_path):
    path = tmp_path / "goals.md"
    path.write_text("# p2 goals (draft)\n\nSome goals.\n")
    append_retro_decision_to_goals(path, "Which db?", "answered", "postgres", "cli", "2026-01-02T00:00:00+00:00")
    text = path.read_text()
    assert "## Decisions" in text
    assert "**Which db?** — answered by cli on 2026-01-02: postgres" in text
    # a no-op when the goals file isn't live yet
    append_retro_decision_to_goals(tmp_path / "missing.md", "x", "answered", "y", "cli", "2026-01-01T00:00:00+00:00")


# --------------------------------------------------------------------------- end to end
def test_retro_files_two_questions_answered_on_the_web_and_the_cli(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_QUESTIONS", "1")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)

    ph = store.phase("gdn", "p1")
    sched.start_retro(ph, ["designer"], skip_personas=True)
    rep = sched.tick()  # reap the reconcile run -> file the questions, render, commit, push, PR
    assert not rep.errors, rep.errors
    assert fake_github.created

    qs = [d for d in sched.pending_decisions() if d.get("kind") == "question" and d.get("source") == "retro"]
    assert len(qs) == 2
    assert all(d["phase"] == "gdn/p1" and d["next_phase"] == "gdn/p2" for d in qs)
    blocking_q = next(d for d in qs if d["blocking"])
    other_q = next(d for d in qs if not d["blocking"])

    # the retro document (still only in the worktree, not merged) already carries both
    # questions and their decision card ids
    retro_md = (store.config.worktree_path("_retro-gdn-p1") / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert blocking_q["question"] in retro_md and blocking_q["id"] in retro_md
    assert other_q["question"] in retro_md and other_q["id"] in retro_md
    assert "_(blocking the verdict)_" in retro_md

    # simulate the retro PR merging into the live garden before either question is answered
    _merge_into_live(store, root)

    # answer the blocking one through the CLI
    r = _cli(root, "decide", blocking_q["id"], "--answer", "two rounds")
    assert r.exit_code == 0, r.output

    # dismiss (a form of resolving) the other one through the web
    c = TestClient(create_app(Store(root), watch=False))
    r = c.post(f"/decisions/{other_q['id']}/answer", data={"answer": "3"}, follow_redirects=False)
    assert r.status_code == 303

    fresh = Scheduler(store, github=fake_github, log=print)
    assert fresh.pending_decisions() == []

    retro_md = (root / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "answered by cli" in retro_md and blocking_q["question"] in retro_md
    assert "answered by web" in retro_md and other_q["question"] in retro_md
    assert "_Not yet answered._" not in retro_md

    goals_md = (root / "gdn" / "p2" / "goals.md").read_text()
    assert "## Decisions" in goals_md
    assert blocking_q["question"] in goals_md and "answered by cli" in goals_md
    assert other_q["question"] in goals_md and "answered by web" in goals_md

    # garden plan for the next phase inlines goals.md verbatim, decisions included
    from garden.model import goals_text

    next_ph = Store(root).phase("gdn", "p2")
    assert "## Decisions" in goals_text(next_ph.goals_path)

    # the retro page renders both questions with their answers (it's the same document)
    html = c.get("/phases/gdn/p1/retro").text
    assert "Questions for the owner" in html and "Answers" in html
    assert blocking_q["question"] in html and "answered by cli" in html
    assert other_q["question"] in html and "answered by web" in html


def test_retro_decide_refuses_while_a_blocking_question_is_open(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_QUESTIONS", "1")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)

    ph = store.phase("gdn", "p1")
    sched.start_retro(ph, ["designer"], skip_personas=True)
    rep = sched.tick()
    assert not rep.errors, rep.errors

    blocking_q = next(d for d in sched.pending_decisions()
                      if d.get("kind") == "question" and d.get("blocking"))
    try:
        sched.retro_decide(store.phase("gdn", "p1"), "reopen")
        raise AssertionError("expected a refusal")
    except RuntimeError as e:
        assert blocking_q["id"] in str(e)

    sched.answer_kickoff_question(blocking_q["id"], "two rounds", by="cli")
    rec = sched.retro_decide(store.phase("gdn", "p1"), "reopen")
    assert rec["status"] == "accepted"
