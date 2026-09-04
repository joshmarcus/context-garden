from garden.review import feedback_from_review, parse_review, review_brief, review_to_markdown
from garden.store import Store


def test_review_brief_and_parse(garden):
    store = Store(garden)
    t = store.task("DM-001")
    text = review_brief(store, t, branch="b", base="main", pr_title="T", pr_body="B", diff="+++ x\n-a\n+b", max_diff_chars=1000)
    assert "GARDEN_REVIEW:" in text and "## Diff" in text and "```diff" in text and "Operating rules" not in text
    big = review_brief(store, t, branch="b", base="main", pr_title="T", pr_body="", diff="x" * 2000, max_diff_chars=100)
    assert "git diff main...HEAD" in big and "(empty)" in big
    rev = parse_review('junk\nGARDEN_REVIEW: {"verdict": "request_changes", "summary": "s", "description_ok": false, "description_feedback": "d", "findings": [{"severity": "blocking", "file": "a.py", "line": 2, "summary": "bug"}]}')
    assert rev["verdict"] == "request_changes"
    md = review_to_markdown(rev, "r1")
    assert "request changes" in md and "`a.py`:2" in md and "**PR description**" in md
    fb = feedback_from_review(rev)
    assert "blocking" in fb and "pr_body" in fb
    assert parse_review("nothing") == {}


def test_revise_with_pr_comment(sched, fake_github, monkeypatch):
    """Workers can include pr_comment in the result to explain revisions."""
    from tests.conftest import wait_for_runs

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "revise-with-comment")
    sched.tick()
    wait_for_runs(sched)
    sched.tick()  # reap work -> PR opened -> review dispatched
    wait_for_runs(sched)
    sched.tick()  # reap review -> request_changes -> revise dispatched
    wait_for_runs(sched)
    rep = sched.tick()  # reap revise -> PR body updated + pr_comment posted -> second review dispatched
    # Verify the pr_comment was posted as a separate comment
    assert any("I addressed the feedback by adding the missing test." in c for c in fake_github.comments)
    # Verify the standard revision comment was also posted
    assert any("Pushed a revision round:" in c for c in fake_github.comments)
    # Verify the pr_comment is not duplicated into the PR body/description
    assert not any("I addressed the feedback" in u.get("body", "") for u in fake_github.updated)
    # Verify the follow-up automated review can see the response, so it doesn't repeat the same finding
    assert "DM-001(review)" in rep.dispatched
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "I addressed the feedback by adding the missing test." in brief
    assert "not part of the description" in brief


def test_review_flow(sched, fake_github, monkeypatch):
    from tests.conftest import wait_for_runs

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    sched.tick()
    wait_for_runs(sched)
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    st = sched.state.get("DM-001")
    assert st["review_run"] and st["review_rounds"] == 1
    run = sched.runs.latest("DM-001")
    assert run.mode == "review" and "GARDEN_REVIEW" in (run.path / "brief.md").read_text()
    wait_for_runs(sched)
    rep = sched.tick()  # reap review -> request_changes -> revise dispatched
    assert "DM-001 -> changes_requested (review)" in rep.transitions and "DM-001(revise)" in rep.dispatched
    assert any("Automated review: request changes" in c for c in fake_github.comments)
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "missing test" in brief and "PR description" in brief
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-ok")
    wait_for_runs(sched)
    rep = sched.tick()  # reap revise -> PR body updated -> second review
    assert fake_github.updated and fake_github.updated[-1]["body"]
    assert "DM-001(review)" in rep.dispatched
    wait_for_runs(sched)
    rep = sched.tick()
    assert "DM-001 review: approve" in rep.transitions
    sched.store.invalidate()
    assert sched.store.task("DM-001").status.value == "in_review"
    assert sched.state.get("DM-001")["review_rounds"] == 2
    # cap reached: a further round would not start
    assert sched.state.get("DM-001")["last_review"]["verdict"] == "approve"
