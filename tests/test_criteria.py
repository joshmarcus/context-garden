"""CG-179: results and reviews speak to each acceptance criterion by name, with evidence."""

from garden.criteria import (
    apply_verification,
    criteria_counts,
    parse_criteria,
    reconcile,
    verification_markdown,
    worker_verified,
)
from garden.events import EventLog, metrics
from garden.review import review_brief, review_to_markdown
from garden.store import Store

CRITERIA_BODY = """
## Goal

Do the first thing.

## Acceptance criteria

- [ ] The widget renders on the home page.
- [ ] The API returns 200 for a valid request.

## Out of scope

- Anything else.
"""


def test_parse_criteria():
    assert parse_criteria(CRITERIA_BODY) == [
        "The widget renders on the home page.",
        "The API returns 200 for a valid request.",
    ]
    assert parse_criteria("## Goal\n\nNo criteria here.\n") == []
    # a checked box counts too, and the section ends at the next heading
    body = "## Acceptance criteria\n\n- [x] Done one.\n- [ ] Do two.\n\n## Notes\n\n- [ ] not a criterion\n"
    assert parse_criteria(body) == ["Done one.", "Do two."]


def test_reconcile_aligns_worker_and_reviewer_by_quoted_criterion():
    criteria = ["A renders.", "B returns 200."]
    verified = [{"criterion": "B returns 200.", "evidence": "test_b"},
                {"criterion": "A renders.", "not_done": True, "reason": "ran out of time"}]
    review = [{"criterion": "A renders.", "met": False, "reason": "no test"},
              {"criterion": "B returns 200.", "met": True, "reason": "test_b covers it"}]
    rows = reconcile(criteria, verified, review)
    assert rows[0]["criterion"] == "A renders."
    assert rows[0]["not_done"] and rows[0]["worker_reason"] == "ran out of time"
    assert rows[0]["met"] is False
    assert rows[1]["evidence"] == "test_b" and rows[1]["met"] is True


def test_reconcile_flags_a_skipped_criterion_with_no_worker_entry():
    rows = reconcile(["A.", "B."], [{"criterion": "A.", "evidence": "test_a"}])
    assert rows[0]["evidence"] == "test_a" and rows[0]["has_worker"]
    # B has no worker entry and no evidence: not a silent pass
    assert not rows[1]["has_worker"] and not rows[1]["evidence"] and rows[1]["met"] is None


def test_verification_markdown_marks_each_row():
    rows = reconcile(["A.", "B.", "C."],
                     [{"criterion": "A.", "evidence": "test_a"},
                      {"criterion": "B.", "not_done": True, "reason": "blocked on X"}])
    md = verification_markdown(rows)
    assert "## Verification" in md
    assert "- ✅ **A.** — test_a" in md
    assert "- 🚧 **B.** — not done: blocked on X" in md
    assert "- ⚠️ **C.** — no evidence given" in md
    assert verification_markdown([]) == ""


def test_apply_verification_injects_and_replaces():
    criteria = parse_criteria(CRITERIA_BODY)
    verified = [{"criterion": criteria[0], "evidence": "test_a"},
                {"criterion": criteria[1], "evidence": "test_b"}]
    body = "## What\n\nA change.\n"
    out = apply_verification(body, criteria, verified)
    assert out.startswith("## What")
    assert "## Verification" in out and "test_a" in out and "test_b" in out
    # a Verification section the worker wrote is replaced, not duplicated
    hand = body + "\n## Verification\n\n- I tested it by hand.\n"
    out2 = apply_verification(hand, criteria, verified)
    assert out2.count("## Verification") == 1 and "by hand" not in out2
    # no verified data leaves the body untouched
    assert apply_verification(body, criteria, None) == body
    assert apply_verification(body, criteria, []) == body


def test_criteria_counts():
    assert criteria_counts([{"met": True}, {"met": False}, {"met": True}]) == (2, 3)
    assert criteria_counts(None) == (0, 0)


def test_worker_verified_reads_latest_worker_run():
    class R:
        def __init__(self, mode, result):
            self.mode, self.result = mode, result

    runs = [R("work", {"verified": [{"criterion": "A", "evidence": "old"}]}),
            R("review", {"criteria": []}),
            R("revise", {"verified": [{"criterion": "A", "evidence": "new"}]})]
    assert worker_verified(runs)[0]["evidence"] == "new"
    assert worker_verified([R("review", {})]) == []


def _give_criteria(sched):
    t = sched.store.task("DM-001")
    t.body = CRITERIA_BODY
    sched.store.save(t)
    sched.store.invalidate()


def test_review_brief_shows_the_authors_verification(garden):
    store = Store(garden)
    t = store.task("DM-001")
    t.body = CRITERIA_BODY
    store.save(t)
    store.invalidate()
    verified = [{"criterion": "The widget renders on the home page.", "evidence": "test_widget"}]
    text = review_brief(store, store.task("DM-001"), branch="b", base="main", pr_title="T",
                        pr_body="B", diff="+a", max_diff_chars=1000, verified=verified)
    assert "## Author's verification" in text
    assert "The widget renders on the home page.** — test_widget" in text
    # the criterion with no worker entry is flagged for the reviewer
    assert "The API returns 200 for a valid request.** — author gave no evidence" in text


def test_review_markdown_lists_criteria():
    rev = {"verdict": "request_changes", "summary": "s",
           "criteria": [{"criterion": "A renders.", "met": True, "reason": "test_a"},
                        {"criterion": "B returns 200.", "met": False, "reason": "no test"}]}
    md = review_to_markdown(rev)
    assert "**Acceptance criteria**" in md
    assert "- ✅ A renders. — test_a" in md
    assert "- ❌ B returns 200. — no test" in md


def test_skipped_criterion_flows_through_pr_body_review_and_metrics(sched, fake_github, monkeypatch):
    """End to end: a worker that skips a criterion leaves a ⚠️ in the generated Verification
    section, the reviewer marks it not met, and metrics report criteria met on the first review."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    _give_criteria(sched)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "skip-criterion")

    sched.tick()  # dispatch work
    sched.tick()  # reap work -> PR opened with generated Verification -> review dispatched

    body = fake_github.created[-1]["body"]
    assert "## Verification" in body
    assert "- ⚠️ **The widget renders on the home page.** — no evidence given" in body
    assert "- ✅ **The API returns 200 for a valid request.** — proved by test_criterion_1" in body

    review_brief_text = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "## Author's verification" in review_brief_text

    sched.tick()  # reap review -> criteria verdict recorded

    st = sched.state.get("DM-001")
    crit = st["last_review"]["criteria"]
    assert len(crit) == 2
    by_text = {c["criterion"]: c["met"] for c in crit}
    assert by_text["The widget renders on the home page."] is False
    assert by_text["The API returns 200 for a valid request."] is True

    log = EventLog(sched.cfg.garden_dir / "events.jsonl")
    review_ev = next(e for e in log.read(task_id="DM-001") if e["kind"] == "review")
    assert review_ev["criteria_met"] == 1 and review_ev["criteria_total"] == 2

    m = metrics(log.read(), sched.store.tasks())
    row = next(r for r in m["tasks"] if r["id"] == "DM-001")
    assert row["criteria_met"] == 1 and row["criteria_total"] == 2
    d = m["by_difficulty"]["medium"]
    assert d["criteria_met"] == 1 and d["criteria_total"] == 2 and d["criteria_rate"] == 0.5
