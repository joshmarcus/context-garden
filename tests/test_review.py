from garden.model import Status
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


def test_second_review_dispatch_supersedes_the_first(sched, fake_github):
    """CG-144: dispatching a second review while the first is still `running` (a person
    pressed "one more review", or the poll re-reviewed a fresh push) closes the first as
    `superseded` with its cost recorded, rather than leaving it running forever with
    nothing left pointing at it."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    sched.tick()  # reap work -> PR opened -> first review dispatched
    t = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    run1_id = st["review_run"]
    assert run1_id
    run1 = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run1_id)
    assert run1.status == "running" and run1.process_finished()  # finished, not yet reaped

    run2 = sched.dispatch_review(t)

    superseded = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run1_id)
    assert superseded.status == "superseded"
    assert superseded.finished_at
    assert superseded.cost_usd == 0.02  # the finished run's cost is still recorded
    assert st["review_run"] == run2.run_id != run1_id
    # the superseded run no longer counts as active
    assert run1_id not in {r.run_id for r in sched.runs.active()}


def test_revise_with_pr_comment(sched, fake_github, monkeypatch):
    """Workers can include pr_comment in the result to explain revisions."""

    # Focus on DM-001's review cycle: without this, DM-002 stacks on DM-001's open PR
    # and runs its own review rounds concurrently, so the fixed per-tick assertions
    # below become order-dependent on a loaded machine.
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "revise-with-comment")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched
    sched.tick()  # reap review -> request_changes -> revise dispatched
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

    # Focus on DM-001's review cycle: without this, DM-002 stacks on DM-001's open PR
    # and runs its own review rounds concurrently, so the fixed per-tick assertions
    # below become order-dependent on a loaded machine.
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    st = sched.state.get("DM-001")
    assert st["review_run"] and st["review_rounds"] == 1
    run = sched.runs.latest("DM-001")
    assert run.mode == "review" and "GARDEN_REVIEW" in (run.path / "brief.md").read_text()
    rep = sched.tick()  # reap review -> request_changes -> revise dispatched
    assert "DM-001 -> changes_requested (review)" in rep.transitions and "DM-001(revise)" in rep.dispatched
    assert any("Automated review: request changes" in c for c in fake_github.comments)
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "missing test" in brief and "PR description" in brief
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-ok")
    rep = sched.tick()  # reap revise -> PR body updated -> second review
    assert fake_github.updated and fake_github.updated[-1]["body"]
    assert "DM-001(review)" in rep.dispatched
    rep = sched.tick()
    assert "DM-001 review: approve" in rep.transitions
    sched.store.invalidate()
    assert sched.store.task("DM-001").status.value == "in_review"
    assert sched.state.get("DM-001")["review_rounds"] == 2
    # cap reached: a further round would not start
    assert sched.state.get("DM-001")["last_review"]["verdict"] == "approve"


def test_review_parses_description_rewrite():
    rev = parse_review('GARDEN_REVIEW: {"verdict": "request_changes", "summary": "s", "description_ok": false, '
                       '"description_feedback": "d", "description_rewrite": "## What\\n\\nBetter.", "findings": []}')
    assert rev["description_rewrite"] == "## What\n\nBetter."


def test_review_brief_advertises_description_rewrite(garden):
    store = Store(garden)
    text = review_brief(store, store.task("DM-001"), branch="b", base="main", pr_title="T", pr_body="B",
                        diff="+a", max_diff_chars=1000)
    assert "description_rewrite" in text
    assert "rewrite the description yourself" in text


def test_review_description_only_rewrite_applied_without_a_round(sched, fake_github, monkeypatch):
    """description_ok false, no blocking finding, rewrite supplied: the scheduler updates the PR
    body through the API and starts no revise round."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-rewrite")
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched (as review-rewrite)
    assert "DM-001(review)" in rep.dispatched
    rep = sched.tick()  # reap review -> apply the rewrite, no round
    assert any("description rewritten by the reviewer" in t for t in rep.transitions)
    assert "DM-001(revise)" not in rep.dispatched
    assert not any("changes_requested" in t for t in rep.transitions)
    # the corrected body reached GitHub
    assert fake_github.updated and fake_github.updated[-1]["body"] == "## What\n\nThe corrected description."
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert task.status.value == "in_review"
    assert "description rewritten by the reviewer" in task.body


def test_description_only_revise_dispatches_on_easy_tier(sched, fake_github, monkeypatch):
    """CG-109: a review with no code findings, only a description fix, is a paragraph
    rewrite, so the revise round should not cost a code-review-tier model."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-desc")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched
    rep = sched.tick()  # reap review -> request_changes -> revise dispatched
    assert "DM-001 -> changes_requested (review)" in rep.transitions and "DM-001(revise)" in rep.dispatched
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"
    assert run.model == "haiku"  # the easy tier, not the task's (medium) tier
    assert run.difficulty == "easy"
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert task.difficulty == "medium"  # the task's own tier is unchanged
    assert "description only; easy tier" in task.body


def test_revise_with_code_finding_keeps_task_tier(sched, fake_github, monkeypatch):
    """A revise round with a blocking code finding is a real review round, so it keeps
    the task's own tier rather than dropping to easy."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched
    rep = sched.tick()  # reap review -> request_changes -> revise dispatched
    assert "DM-001(revise)" in rep.dispatched
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"
    assert run.model == "sonnet"  # the task's own (medium) tier
    assert run.difficulty == "medium"
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert "description only; easy tier" not in task.body


def test_approve_with_description_rewrite_applies_directly(sched, fake_github, monkeypatch):
    """CG-140: an approve verdict with description_ok false and a rewrite applies the
    rewrite through the GitHub API and stores no pending feedback; the task stays
    in_review with no revise round."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-approve-rewrite")
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    rep = sched.tick()  # reap review -> approve, apply the rewrite, no round
    assert any("description rewritten by the reviewer" in t for t in rep.transitions)
    assert "DM-001(revise)" not in rep.dispatched
    assert not any("changes_requested" in t for t in rep.transitions)
    assert fake_github.updated and fake_github.updated[-1]["body"] == "## What\n\nThe corrected description."
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert task.status.value == "in_review"
    assert not str(sched.state.get("DM-001").get("pending_feedback") or "").strip()


def test_approve_with_empty_rewrite_dispatches_description_round(sched, fake_github, monkeypatch):
    """CG-140: an approve verdict with description_ok false and no rewrite dispatches a
    description-only revise round instead of leaving feedback parked on an in_review task."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-approve-desc")
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    rep = sched.tick()  # reap review -> approve but description flagged -> revise dispatched
    assert "DM-001 -> changes_requested (description round)" in rep.transitions
    assert "DM-001(revise)" in rep.dispatched
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"
    assert run.difficulty == "easy"  # description-only: the easy tier, not the task's own


def test_orphaned_review_run_is_closed_not_left_running(sched, fake_github):

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    review_run_id = sched.state.get("DM-001")["review_run"]
    assert review_run_id

    # the task moves on (e.g. the PR is merged by a human) before the tick that
    # would have read the review's verdict; the reap gate on t.status.pr_open now
    # fails, and the run would otherwise be stuck "running" forever.
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    sched.store.save(task)

    rep = sched.tick()

    assert not any(r.task_id == "DM-001" and r.run_id == review_run_id for r in sched.runs.active())
    run = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == review_run_id)
    assert run.status in ("done", "failed")
    assert run.cost_usd == 0.02  # usage/cost still recorded from the fake worker's output
    assert any(f"{review_run_id} closed (orphaned)" in t for t in rep.transitions)
    # no verdict posted and the task's own status is left alone
    assert sched.store.task("DM-001").status == Status.DONE
    assert not any("request_changes" in c or "approve" in c for c in fake_github.comments)


def test_review_cap_reached_flags_needs_human_and_one_more_review_grants_a_round(sched, fake_github, monkeypatch):
    """CG-117: once the cap stops the automated reviewer, the task says so instead of sitting
    silently in review, and the Inbox offers one more round without a human editing state.json."""
    from garden.inbox import build_inbox

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-desc")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched (round 1)
    sched.tick()  # reap review round 1: request_changes (description only) -> revise dispatched
    rep = sched.tick()  # reap revise -> pushed -> review dispatched (round 2)
    assert "DM-001(review)" in rep.dispatched
    assert sched.state.get("DM-001")["review_rounds"] == 2
    sched.tick()  # reap review round 2: request_changes again -> revise dispatched
    rep = sched.tick()  # reap revise -> pushed -> cap already used; no third review dispatched
    assert "DM-001(review)" not in rep.dispatched
    assert "DM-001 review cap reached" in rep.transitions

    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert "2 automated review round(s) used" in task.body
    assert task.status == Status.IN_REVIEW  # still in review; the loop did not stall it

    st = sched.state.get("DM-001")
    assert st["needs_human"]["kind"] == "review_cap"
    assert st["review_rounds"] == 2

    items = [i for i in build_inbox(sched.store, sched) if i["group"] == "attention" and i["task"] == "DM-001"]
    assert items, "reaching the review cap should raise an Inbox card under 'Needs a decision'"
    it = items[0]
    assert it["pr"] == task.pr
    labels = [a["label"] for a in it["actions"]]
    assert "One more automated review" in labels
    assert "Send back with a note" in labels
    assert "Open PR" in labels

    # "one more review": raises the cap by one round and dispatches right away
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-ok")
    run = sched.review_again(task)
    assert run.mode == "review"
    assert sched.state.get("DM-001")["review_rounds"] == 2  # rolled back one, then re-incremented
    assert not sched.state.get("DM-001").get("needs_human")

    rep = sched.tick()  # reap the extra review: approve, no third cap-reached flag
    assert "DM-001 review: approve" in rep.transitions
    assert not sched.state.get("DM-001").get("needs_human")
