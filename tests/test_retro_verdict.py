"""CG-178: a retro ends in a verdict — close, close with follow-ups, or reopen. The verdict
files the tasks it names, closes or holds the phase, and `close-phase` follows it."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app
from garden.model import Phase
from garden.retro import (
    normalize_verdict,
    render_retro_doc,
    resolve_retro_tasks,
    verdict_section,
)
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app

# reuse the end-to-end fixtures from the retro tests
from tests.test_retro import _garden_repo, _live_garden, _register_prs

runner = CliRunner()


def _merge_retro_doc_into_live(store: Store, root: Path) -> None:
    """Simulate the retro PR merging: copy the retro document the retro wrote in its worktree
    into the live garden, where the retro page reads it from."""
    import shutil

    wt = store.config.worktree_path("_retro-gdn-p1")
    src, dst = wt / "gdn" / "p1" / "docs" / "retro.md", root / "gdn" / "p1" / "docs" / "retro.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)


def _cli(root, *args):
    cwd = os.getcwd()
    os.chdir(root)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)


# --------------------------------------------------------------------------- pure logic
def test_normalize_verdict_canonicalises_and_rejects():
    assert normalize_verdict("close") == "close"
    assert normalize_verdict("REOPEN") == "reopen"
    assert normalize_verdict("close_with_followups") == "close_with_followups"
    assert normalize_verdict("followups") == "close_with_followups"
    assert normalize_verdict("close-with-followups") == "close_with_followups"
    assert normalize_verdict("") == ""
    assert normalize_verdict("nonsense") == ""


def test_resolve_retro_tasks_flags_duplicate_titles_and_keeps_reason():
    items = [
        {"title": "New blocker", "body": "b", "difficulty": "hard", "priority": 1, "reason": "base is red"},
        {"title": "First task", "body": "b2", "difficulty": "easy"},
        {"title": "   "},
        "not a dict",
    ]
    out = resolve_retro_tasks(items, {"first task": "GD-001"})
    assert len(out) == 2
    assert out[0]["skip"] is False and out[0]["reason"] == "base is red"
    assert out[1]["skip"] is True and "GD-001" in out[1]["dup_reason"]


def test_verdict_section_reads_each_choice():
    phase = Phase(product="p", name="ph1", path=Path("/x/p/ph1"), goals_path=None, specs=[], docs=[], tasks=[])
    close = verdict_section(phase, "close", [], [], "ph2")
    assert "**Close.**" in close and "join the herbarium" in close

    fu = [{"task_id": "GD-9", "title": "Do the next thing"}]
    with_fu = verdict_section(phase, "close_with_followups", fu, [], "ph2")
    assert "**Close with follow-ups.**" in with_fu and "GD-9: Do the next thing" in with_fu and "ph2" in with_fu

    bl = [{"task_id": "GD-8", "title": "Land the fix", "reason": "base is red"}]
    reopen = verdict_section(phase, "reopen", [], bl, "ph2")
    assert "**Reopen.**" in reopen and "GD-8: Land the fix — base is red" in reopen

    assert "no verdict" in verdict_section(phase, "", [], [], "ph2")


def test_render_retro_doc_has_a_verdict_section():
    phase = Phase(product="p", name="ph1", path=Path("/x/p/ph1"), goals_path=None, specs=[], docs=[], tasks=[])
    rev = {"reconciliation": [], "summary": "s", "verdict": "reopen"}
    doc = render_retro_doc(phase, rev, {}, None,
                           blocking=[{"task_id": "GD-8", "title": "Land the fix", "reason": "red"}], next_phase="ph2")
    assert "## Verdict" in doc and "**Reopen.**" in doc and "GD-8: Land the fix" in doc


# --------------------------------------------------------------------------- end to end
def _run_retro(root, store, fake_github):
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    ph = store.phase("gdn", "p1")
    sched.start_retro(ph, ["designer"], skip_personas=True)
    rep = sched.tick()  # reap the reconcile run -> file tasks, render, PR, apply verdict
    assert not rep.errors, rep.errors
    assert fake_github.created
    return sched


def test_close_verdict_closes_the_phase_at_once(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "close")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)

    assert Store(root).phase("gdn", "p1").closed  # closed without waiting for approval
    rec = sched.retro_verdict("gdn/p1")
    assert rec["verdict"] == "close" and rec["status"] == "accepted" and rec["accepted_by"] == "retro"
    retro_md = (store.config.worktree_path("_retro-gdn-p1") / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "## Verdict" in retro_md and "**Close.**" in retro_md

    # the closed phase page records the verdict and who accepted it
    c = TestClient(create_app(Store(root), watch=False))
    html = c.get("/phases/gdn/p1").text
    assert "Retro verdict" in html and "Close" in html and "accepted by retro" in html


def test_close_with_followups_files_a_draft_in_the_next_phase(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "close_with_followups")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)

    assert Store(root).phase("gdn", "p1").closed
    rec = sched.retro_verdict("gdn/p1")
    assert rec["verdict"] == "close_with_followups" and rec["status"] == "accepted"
    assert len(rec["followup_ids"]) == 1
    fid = rec["followup_ids"][0]

    # the follow-up landed in the worktree's next phase as a draft with retro provenance
    from garden.model import Task as TaskModel

    tasks_dir = store.config.worktree_path("_retro-gdn-p1") / "gdn" / "p2" / "tasks"
    followup = next(p for p in tasks_dir.glob("*.md") if TaskModel.parse(p, p.read_text()).id == fid)
    t = TaskModel.parse(followup, followup.read_text())
    assert t.status.value == "draft" and t.discovered_from == "retro:gdn/p1"
    assert not t.retro_blocking
    retro_md = (store.config.worktree_path("_retro-gdn-p1") / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "**Close with follow-ups.**" in retro_md and fid in retro_md


def test_reopen_verdict_holds_the_phase_and_files_a_blocking_task(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)

    # the phase stays open and a blocking task was filed live in the current phase
    assert not Store(root).phase("gdn", "p1").closed
    rec = sched.retro_verdict("gdn/p1")
    assert rec["verdict"] == "reopen" and rec["status"] == "pending"
    assert len(rec["blocking_ids"]) == 1
    bid = rec["blocking_ids"][0]
    blk = Store(root).task(bid)
    assert blk.retro_blocking and blk.freeze_exception and blk.freeze_exception_reason
    assert blk.status.value == "draft" and blk.discovered_from == "retro:gdn/p1"
    assert blk.phase == "p1"


def test_close_phase_refuses_open_blocking_task_and_force_overrides(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)
    bid = sched.retro_verdict("gdn/p1")["blocking_ids"][0]

    r = _cli(root, "close-phase", "gdn/p1")
    assert r.exit_code == 1
    assert bid in r.output and "--force" in r.output and "retro-blocking" in r.output
    assert not Store(root).phase("gdn", "p1").closed

    r = _cli(root, "close-phase", "gdn/p1", "--force")
    assert r.exit_code == 0, r.output
    assert Store(root).phase("gdn", "p1").closed


def test_retro_decide_reopen_approves_the_blocking_task_then_close_follows(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)
    bid = sched.retro_verdict("gdn/p1")["blocking_ids"][0]

    # accepting the reopen verdict approves the blocking task (draft -> ready)
    r = _cli(root, "retro-decide", "gdn/p1", "reopen", "--note", "yes, block on it")
    assert r.exit_code == 0, r.output
    assert Store(root).task(bid).status.value == "ready"
    rec = Scheduler(store, github=fake_github, log=print).retro_verdict("gdn/p1")
    assert rec["status"] == "accepted" and rec["accepted_by"] == "cli" and rec["note"] == "yes, block on it"

    # close-phase still refuses while the (now ready) blocking task is open
    assert _cli(root, "close-phase", "gdn/p1").exit_code == 1

    # finish it, then close-phase follows through
    assert _cli(root, "set-status", bid, "done").exit_code == 0
    r = _cli(root, "close-phase", "gdn/p1")
    assert r.exit_code == 0, r.output
    assert Store(root).phase("gdn", "p1").closed


def test_close_phase_warns_when_no_verdict_exists(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    # no retro has run for this phase
    r = _cli(root, "close-phase", "gdn/p1")
    assert r.exit_code == 0, r.output
    assert "no retro verdict" in r.output
    assert Store(root).phase("gdn", "p1").closed


def test_phase_page_shows_the_reopen_verdict_and_its_task(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)
    bid = sched.retro_verdict("gdn/p1")["blocking_ids"][0]

    c = TestClient(create_app(Store(root), watch=False))
    html = c.get("/phases/gdn/p1").text
    assert "Retro verdict" in html and "Reopen" in html and bid in html
    assert "needs a decision" in html  # the pending decision card

    # accepting the reopen through the web approves the blocking task
    r = c.post("/phases/gdn/p1/retro-decide", data={"choice": "reopen"}, follow_redirects=False)
    assert r.status_code == 303 and "flash" not in r.headers["location"]
    assert Store(root).task(bid).status.value == "ready"


def test_retro_page_also_shows_the_reopen_verdict_and_its_task(tmp_path, fake_github, monkeypatch):
    """The retro page (not just the phase page) shows the verdict and offers the same
    accept-reopen / change-to-close actions."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RETRO_VERDICT", "reopen")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = _run_retro(root, store, fake_github)
    bid = sched.retro_verdict("gdn/p1")["blocking_ids"][0]
    _merge_retro_doc_into_live(store, root)  # simulate the retro PR merging into the live garden

    c = TestClient(create_app(Store(root), watch=False))
    html = c.get("/phases/gdn/p1/retro").text
    assert "Retro verdict" in html and "Reopen" in html and bid in html
    assert "needs a decision" in html
    assert 'action="/phases/gdn/p1/retro-decide"' in html

    # accepting the reopen through the retro page approves the blocking task
    r = c.post("/phases/gdn/p1/retro-decide", data={"choice": "reopen"}, follow_redirects=False)
    assert r.status_code == 303 and "flash" not in r.headers["location"]
    assert Store(root).task(bid).status.value == "ready"
