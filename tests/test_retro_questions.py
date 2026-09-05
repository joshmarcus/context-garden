"""CG-189: the retro's questions for the human are decision cards. The owner answers each on
the Inbox (or the CLI); the answer lands under `## Answers` in the retro document and under
`## Decisions` in the next phase's goals, where the planner reads it as settled."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.inbox import build_inbox, decisions
from garden.retro import append_under_heading, questions_section, retro_questions
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


# --------------------------------------------------------------------------- pure logic
def test_retro_questions_normalises_keys_and_fields():
    rev = {"questions": [
        {"key": "Hard Tier!", "question": "May the queue merge hard-tier PRs?",
         "context": "twelve by hand", "options": ["yes", "no"], "default": "yes", "blocking": True},
        {"question": "What is the budget?"},  # no key -> q2; no options
        {"question": "  "},                    # blank question dropped
        "not a dict",
    ]}
    qs = retro_questions(rev)
    assert len(qs) == 2
    assert qs[0]["key"] == "hard-tier" and qs[0]["blocking"] is True
    assert qs[0]["options"] == ["yes", "no"] and qs[0]["default"] == "yes"
    assert qs[1]["key"] == "q2" and qs[1]["options"] == [] and qs[1]["blocking"] is False


def test_retro_questions_keys_are_unique():
    rev = {"questions": [{"key": "budget", "question": "a?"}, {"key": "budget", "question": "b?"}]}
    keys = [q["key"] for q in retro_questions(rev)]
    assert len(set(keys)) == 2


def test_questions_section_renders_context_options_and_blocking():
    qs = retro_questions({"questions": [
        {"key": "k", "question": "Merge hard PRs?", "context": "why", "options": ["yes", "no"],
         "default": "yes", "blocking": True}]})
    section = questions_section(qs)
    assert "Merge hard PRs?" in section and "blocks closing" in section
    assert "why" in section and "options: yes, no" in section and "suggested: yes" in section
    assert questions_section([]) == "_The retro put no questions to the owner._"


def test_append_under_heading_creates_and_appends(tmp_path):
    p = tmp_path / "retro.md"
    assert append_under_heading(p, "Answers", "- a") is False  # missing file: nothing written
    p.write_text("# Retro\n\n## What changed\n\nstuff\n")
    assert append_under_heading(p, "Answers", "- one") is True
    assert append_under_heading(p, "Answers", "- two") is True
    text = p.read_text()
    assert "## Answers" in text
    # both answers land under the one section, in order, and the earlier section is untouched
    assert text.index("- one") < text.index("- two")
    assert "## What changed" in text and "stuff" in text
    assert text.count("## Answers") == 1


# --------------------------------------------------------------------------- end to end
def _run_retro(root, store, fake_github):
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    ph = store.phase("gdn", "p1")
    sched.start_retro(ph, ["designer"], skip_personas=True)
    rep = sched.tick()
    assert not rep.errors, rep.errors
    assert fake_github.created
    return sched


def _merge_retro_into_live(store: Store, root: Path) -> None:
    """Simulate the retro PR merging: copy the retro document and the next-phase goals draft
    the retro wrote in its worktree into the live garden, where the answer edits them."""
    wt = store.config.worktree_path("_retro-gdn-p1")
    for rel in ("gdn/p1/docs/retro.md", "gdn/p2/goals.md"):
        src, dst = wt / rel, root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)


def test_two_questions_answered_on_web_and_cli_land_in_retro_and_goals(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)

    # both questions were filed as decision cards on the Inbox, and count in the badge
    items = build_inbox(Store(root), sched)
    cards = [i for i in items if i["group"] == "retro_question"]
    assert len(cards) == 2
    assert all(c in decisions(items) for c in cards)
    keys = {c["retro_key"] for c in cards}
    assert keys == {"hard-tier-merges", "phase-budget"}
    hard = next(c for c in cards if c["retro_key"] == "hard-tier-merges")
    assert hard["options"] == ["yes", "no"] and hard["product"] == "gdn" and hard["phase_name"] == "p1"

    # the retro document the retro wrote lists the questions
    retro_src = (store.config.worktree_path("_retro-gdn-p1") / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "## Questions for the human" in retro_src and "May the queue merge hard-tier PRs?" in retro_src

    _merge_retro_into_live(store, root)

    # the Inbox page renders both question cards with their answer forms
    c = TestClient(create_app(Store(root), watch=False))
    inbox_html = c.get("/inbox").text
    assert "May the queue merge hard-tier PRs?" in inbox_html
    assert 'action="/phases/gdn/p1/retro-answer"' in inbox_html
    assert 'value="hard-tier-merges"' in inbox_html

    # one answered on the web (a selected option), the other on the CLI (free text)
    r = c.post("/phases/gdn/p1/retro-answer",
               data={"key": "hard-tier-merges", "choice": "yes", "answer": ""}, follow_redirects=False)
    assert r.status_code == 303 and "flash" not in r.headers["location"]

    r = _cli(root, "retro-answer", "gdn/p1", "phase-budget", "$40 for the phase")
    assert r.exit_code == 0, r.output

    # both answers, with who and when, are in the live retro document and next-phase goals
    retro_md = (root / "gdn" / "p1" / "docs" / "retro.md").read_text()
    goals = (root / "gdn" / "p2" / "goals.md").read_text()
    for doc in (retro_md, goals):
        assert "May the queue merge hard-tier PRs?" in doc and "yes" in doc and "(web," in doc
        assert "What is the phase budget?" in doc and "$40 for the phase" in doc and "(cli," in doc
    assert "## Answers" in retro_md and "## Decisions" in goals

    # nothing waits on the Inbox now, and the retro page shows both answers
    fresh = Scheduler(Store(root), github=fake_github, log=print)  # re-reads state.json off disk
    items = build_inbox(Store(root), fresh)
    assert not [i for i in items if i["group"] == "retro_question"]
    html = c.get("/phases/gdn/p1/retro").text
    assert "May the queue merge hard-tier PRs?" in html and "$40 for the phase" in html
    assert "web" in html and "cli" in html

    # the planner brief for the next phase carries the answered decisions
    from garden.planner import plan_prompt

    prompt = plan_prompt(Store(root), "gdn", "p2")
    assert "## Decisions" in prompt
    assert "May the queue merge hard-tier PRs?" in prompt and "$40 for the phase" in prompt


def test_a_blocking_question_holds_the_verdict_card_until_answered(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)

    # mark the first question blocking, as a reopen retro would
    qs = sched.state.get("_retro_questions")
    rec = qs.get("gdn/p1")
    rec["questions"][0]["blocking"] = True
    qs["gdn/p1"] = rec
    sched.state.save()

    ph = Store(root).phase("gdn", "p1")
    # the verdict card refuses while the blocking question is unanswered
    try:
        sched.retro_decide(ph, "reopen")
        raise AssertionError("expected a refusal on the unanswered blocking question")
    except RuntimeError as e:
        assert "blocking question" in str(e)

    # answering it (the file need not exist for the guard to clear) lets the verdict through
    _merge_retro_into_live(store, root)
    sched.retro_answer(ph, rec["questions"][0]["key"], "yes", by="cli")
    rec2 = sched.retro_decide(ph, "reopen")
    assert rec2["status"] == "accepted"
