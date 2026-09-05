"""Draft PRs and the human's triage step."""

from garden.github import Feedback
from garden.inbox import build_inbox, counts


def statuses(sched):
    sched.store.invalidate()
    return {tid: t.status.value for tid, t in sched.store.tasks().items()}


def test_draft_pr_waits_for_triage_then_ready(sched, fake_github):
    sched.cfg.data["github"] = {"draft_pr": True}
    sched.tick()
    rep = sched.tick()
    assert "DM-001 -> awaiting_triage" in rep.transitions[-1] or statuses(sched)["DM-001"] == "awaiting_triage"
    pr = fake_github.prs["garden/dm-001-first-task"]
    assert pr.is_draft is True and sched.state.get("DM-001")["pr_draft"] is True
    # dependents can still stack on a draft PR
    assert statuses(sched)["DM-002"] == "running"
    # it shows up in the inbox as a triage item
    items = build_inbox(sched.store, sched)
    assert counts(items).get("triage") == 1 and items[0]["task"] == "DM-001"
    assert any(a["command"].startswith("garden triage DM-001 --ready") for a in items[0]["actions"])
    # the human marks it ready
    sched.triage(sched.store.task("DM-001"), ready=True, note="looks right")
    assert statuses(sched)["DM-001"] == "in_review" and fake_github.readied == [pr.number]
    assert pr.is_draft is False
    sched.tick()
    assert statuses(sched)["DM-001"] == "in_review"


def test_triage_changes_go_to_revise(sched, fake_github):
    sched.cfg.data["github"] = {"draft_pr": True}
    sched.tick()
    sched.tick()
    rep_before = statuses(sched)["DM-001"]
    assert rep_before == "awaiting_triage"
    sched.triage(sched.store.task("DM-001"), changes="rename the flag to --dry-run")
    assert statuses(sched)["DM-001"] == "changes_requested"
    rep = sched.tick()
    assert "DM-001(revise)" in rep.dispatched
    assert "rename the flag" in (sched.runs.latest("DM-001").path / "brief.md").read_text()
    rep = sched.tick()
    assert statuses(sched)["DM-001"] == "awaiting_triage"  # still a draft after the revision


def test_ready_on_github_is_detected_by_poll(sched, fake_github):
    sched.cfg.data["github"] = {"draft_pr": True}
    sched.tick()
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.is_draft, pr.updated_at = False, "t2"
    rep = sched.tick()
    assert "DM-001 -> in_review (triaged)" in rep.transitions
    # human comments on the now-ready PR still drive revise rounds
    pr.updated_at = "t3"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "nit", "created": "2099-01-01T00:00:00Z"}])
    rep = sched.tick()
    assert "DM-001(revise)" in rep.dispatched


def test_triage_row_shows_diff_summary(sched, fake_github):
    sched.cfg.data["github"] = {"draft_pr": True}
    sched.tick()
    sched.tick()
    items = build_inbox(sched.store, sched)
    it = next(i for i in items if i["group"] == "triage")
    assert "files changed" in it["why"]
    assert "files changed" in it["diff_stat"]


def test_inbox_groups(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "needs_input")
    sched.tick()
    sched.tick()
    items = build_inbox(sched.store, sched)
    c = counts(items)
    assert c.get("question") == 1
    q = next(i for i in items if i["group"] == "question")
    assert q["question"] == "Postgres or SQLite?" and q["actions"][0]["kind"] == "answer"
    sched.cfg.data["budgets"] = {"demo/p1": 0.001}
    items = build_inbox(sched.store, sched)
    assert counts(items).get("budget") == 1
