"""CG-225: the retro's "questions for the human" become decision cards through the
kickoff's mechanism (CG-224) -- one question/decision mechanism, shared by kickoff and
retro."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app
from tests.test_retro import _garden_repo, _live_garden, _register_prs

runner = CliRunner()


def _cli(root: Path, *args: str):
    cwd = os.getcwd()
    os.chdir(root)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)


def _merge_retro_into_live(store: Store, root: Path) -> None:
    """Simulate the retro's own PR merging: copy the retro document and the next phase's
    goals draft out of the retro worktree into the live garden, where the retro page and a
    later `garden decide` read them from."""
    wt = store.config.worktree_path("_retro-gdn-p1")
    src_doc = wt / "gdn" / "p1" / "docs" / "retro.md"
    dst_doc = root / "gdn" / "p1" / "docs" / "retro.md"
    dst_doc.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src_doc, dst_doc)
    src_goals = wt / "gdn" / "p2" / "goals.md"
    dst_goals = root / "gdn" / "p2" / "goals.md"
    dst_goals.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src_goals, dst_goals)


def _run_retro_with_two_questions(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_QUESTIONS", "two")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    ph = store.phase("gdn", "p1")
    sched.start_retro(ph, ["designer"], skip_personas=True)
    rep = sched.tick()  # reap the reconcile run -> file questions, render, PR
    assert not rep.errors, rep.errors
    assert fake_github.created
    return root, store, sched


# --------------------------------------------------------------------------- filing
def test_retro_files_questions_as_decision_cards_sourced_from_the_retro(tmp_path, fake_github, monkeypatch):
    root, store, sched = _run_retro_with_two_questions(tmp_path, fake_github, monkeypatch)

    qs = [d for d in sched.pending_decisions() if d["kind"] == "question"]
    assert len(qs) == 2
    assert all(d["source"] == "retro" and d["phase"] == "gdn/p1" and d["next_phase"] == "p2" for d in qs)
    assert sum(1 for d in qs if d["blocking"]) == 1
    assert sum(1 for d in qs if not d["blocking"]) == 1

    retro_md = (store.config.worktree_path("_retro-gdn-p1") / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "## Questions for the owner" in retro_md
    assert "Should the merge queue require two rounds or one?" in retro_md
    assert "Keep the old dashboard or replace it?" in retro_md
    assert "blocks the verdict until answered" in retro_md
    for d in qs:
        assert d["id"] in retro_md
    assert "## Answers" in retro_md and "_No answers recorded yet._" in retro_md

    pr = fake_github.created[-1]
    assert "2 question(s) filed for the owner" in pr["body"]


def test_kickoff_panel_does_not_pick_up_retro_questions(tmp_path, fake_github, monkeypatch):
    """The phase's Kickoff panel (CG-224) reads pending question cards by phase; a retro
    question for the same phase key must not leak into it -- it belongs on the retro page."""
    root, store, sched = _run_retro_with_two_questions(tmp_path, fake_github, monkeypatch)
    from garden.web.pages.phase import _kickoff_panel

    ph = Store(root).phase("gdn", "p1")
    panel = _kickoff_panel(Store(root), sched, ph)
    assert panel["questions"] == []


# --------------------------------------------------------------------------- answering
def test_answer_and_dismiss_retro_questions_on_web_and_cli(tmp_path, fake_github, monkeypatch):
    root, store, sched = _run_retro_with_two_questions(tmp_path, fake_github, monkeypatch)
    _merge_retro_into_live(store, root)  # simulate the retro's PR merging into main

    qs = sorted(sched.pending_decisions(), key=lambda d: d["id"])
    non_blocking = next(d for d in qs if not d["blocking"])
    blocking = next(d for d in qs if d["blocking"])

    # answered on the web
    r = TestClient(create_app(Store(root), watch=False)).post(
        f"/decisions/{non_blocking['id']}/answer", data={"answer": "two rounds"}, follow_redirects=False)
    assert r.status_code == 303

    # dismissed on the CLI
    r = _cli(root, "decide", blocking["id"], "--dismiss")
    assert r.exit_code == 0, r.output

    fresh = Scheduler(Store(root), github=fake_github, log=print)
    assert [d for d in fresh.pending_decisions() if d["kind"] == "question"] == []

    retro_md = (root / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "## Answers" in retro_md
    assert "_No answers recorded yet._" not in retro_md
    assert "**Should the merge queue require two rounds or one?** — answered: two rounds (by web," in retro_md
    assert "**Keep the old dashboard or replace it?** — dismissed (by cli," in retro_md

    goals_md = (root / "gdn" / "p2" / "goals.md").read_text()
    assert "## Decisions" in goals_md
    assert "answered: two rounds (by web," in goals_md
    assert "dismissed (by cli," in goals_md

    # `garden plan` for the next phase inlines the whole goals body, decisions included
    from garden.planner import plan_prompt

    prompt = plan_prompt(Store(root), "gdn", "p2")
    assert "## Decisions" in prompt and "answered: two rounds" in prompt


def test_answering_a_retro_question_before_the_pr_merges_is_a_no_op(tmp_path, fake_github, monkeypatch):
    """Before the retro's own PR has merged, docs/retro.md and the next phase's goals.md do
    not exist live yet; answering must not create or crash on them (mirrors the kickoff's own
    `append_question_resolution`)."""
    root, store, sched = _run_retro_with_two_questions(tmp_path, fake_github, monkeypatch)
    qs = [d for d in sched.pending_decisions() if d["kind"] == "question"]
    did = qs[0]["id"]

    sched.answer_kickoff_question(did, "an answer", by="cli")
    assert not (root / "gdn" / "p1" / "docs" / "retro.md").exists()
    assert not (root / "gdn" / "p2").exists()
    assert [d for d in sched.pending_decisions() if d["id"] == did] == []


# --------------------------------------------------------------------------- verdict gate
def test_retro_decide_refuses_while_a_blocking_question_is_unanswered(tmp_path, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    root, store, sched = _run_retro_with_two_questions(tmp_path, fake_github, monkeypatch)
    ph = Store(root).phase("gdn", "p1")

    blocking = next(d for d in sched.pending_decisions() if d.get("kind") == "question" and d["blocking"])
    try:
        sched.retro_decide(ph, "reopen")
        raise AssertionError("expected a refusal")
    except RuntimeError as e:
        assert "unanswered blocking question" in str(e)
        assert blocking["question"] in str(e)

    # once it is answered, the verdict can be accepted
    sched.answer_kickoff_question(blocking["id"], "replace it", by="cli")
    rec = sched.retro_decide(Store(root).phase("gdn", "p1"), "reopen")
    assert rec["status"] == "accepted"
