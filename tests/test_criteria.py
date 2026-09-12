"""CG-179: results and reviews speak to each acceptance criterion by name, with evidence."""

from garden.criteria import (
    amend_criteria,
    apply_verification,
    browser_capture_authorized,
    criteria_counts,
    evidence_gap_diagnosis,
    evidence_gaps,
    normalize_verified,
    parse_criteria,
    reconcile,
    required_evidence,
    unmatched_worker_entries,
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


def test_parse_criteria_joins_wrapped_continuation_lines():
    # CGS-011: a checklist item that wraps onto an indented continuation line is one criterion
    body = (
        "## Acceptance criteria\n\n"
        "- [ ] `parse_criteria()` returns the complete normalized text of acceptance criteria\n"
        "      that span indented continuation lines.\n"
        "- [ ] A single-line criterion.\n"
    )
    assert parse_criteria(body) == [
        "`parse_criteria()` returns the complete normalized text of acceptance criteria that span indented continuation lines.",
        "A single-line criterion.",
    ]
    # a blank line ends the item even if more indented text follows
    body_blank = (
        "## Acceptance criteria\n\n"
        "- [ ] First criterion.\n"
        "      continues here.\n\n"
        "      not part of the criterion.\n"
        "- [ ] Second criterion.\n"
    )
    assert parse_criteria(body_blank) == ["First criterion. continues here.", "Second criterion."]

    # a following heading also ends and preserves the current criterion without requiring
    # a separating blank line
    body_heading = (
        "## Acceptance criteria\n"
        "- [ ] Criterion before the next heading.\n"
        "## Out of scope\n"
    )
    assert parse_criteria(body_heading) == ["Criterion before the next heading."]


def test_amend_criteria_replaces_a_wrapped_criterion_with_no_stale_text():
    # CGS-011: amending a wrapped item must drop its old continuation line, not just its head
    body = (
        "## Acceptance criteria\n\n"
        "- [ ] Old criterion first line\n"
        "      and its stale continuation.\n"
        "- [ ] Keep this one.\n"
    )
    updated, applied = amend_criteria(
        body, [{"index": 0, "text": "Replacement criterion.", "reason": "Old one was wrong."}]
    )
    assert "- [ ] Replacement criterion." in updated
    assert "stale continuation" not in updated
    assert "and its" not in updated
    assert "- [ ] Keep this one." in updated
    assert parse_criteria(updated) == ["Replacement criterion.", "Keep this one."]
    assert applied == [{"index": 0, "text": "Replacement criterion.", "reason": "Old one was wrong."}]


def test_browser_capture_authority_requires_an_explicit_signal():
    assert browser_capture_authorized("", ["captures"])
    assert browser_capture_authorized(
        "## Acceptance criteria\n\n- [ ] UI captures are required at 390px and 1280px.\n"
    )
    assert browser_capture_authorized(
        "## Acceptance criteria\n\n- [ ] Provide light and dark browser captures.\n"
    )
    incidental = (
        "## Acceptance criteria\n\n"
        "- [ ] Automatic UI/capture checks do not launch Playwright.\n"
        "- [ ] Captures must not run in unsupported environments.\n"
        "- [ ] Documentation explains why captures are optional.\n"
    )
    assert not browser_capture_authorized(incidental)
    assert not any(item["kind"] == "capture" for item in required_evidence(incidental))


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


def test_verified_row_for_wrapped_criterion_reconciles_without_false_no_evidence():
    # CGS-011: the worker quotes the complete wrapped text; it must match, not fall through
    # to a false "no evidence given" row in the generated Verification section.
    body = (
        "## Acceptance criteria\n\n"
        "- [ ] A criterion that wraps onto an\n"
        "      indented continuation line.\n"
    )
    criteria = parse_criteria(body)
    assert criteria == ["A criterion that wraps onto an indented continuation line."]
    verified = [{"criterion": criteria[0], "evidence": "covered by test_x"}]
    rows = reconcile(criteria, verified)
    assert rows[0]["has_worker"] and rows[0]["evidence"] == "covered by test_x"
    md = verification_markdown(rows)
    assert "no evidence given" not in md
    assert "- ✅ **A criterion that wraps onto an indented continuation line.** — covered by test_x" in md


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


def test_unmatched_worker_entries_finds_paraphrased_criteria():
    criteria = ["A renders.", "B returns 200."]
    # the worker's wording for "A renders." drifted; reconcile can't line it up positionally
    # once every entry quotes a criterion, so this entry's evidence would otherwise vanish
    verified = [{"criterion": "A shows up on the page.", "evidence": "test_a"},
                {"criterion": "B returns 200.", "evidence": "test_b"}]
    unmatched = unmatched_worker_entries(criteria, verified)
    assert len(unmatched) == 1
    assert unmatched[0]["evidence"] == "test_a"
    # a purely positional list (no entry quotes a criterion) has nothing to call unmatched
    assert unmatched_worker_entries(criteria, [{"evidence": "test_a"}, {"evidence": "test_b"}]) == []


def test_evidence_gaps_require_wording_drift_to_be_reconciled():
    criteria = ["A renders.", "B returns 200."]
    # a genuine, unexplained gap: no evidence anywhere that could cover "B returns 200."
    assert evidence_gaps(criteria, [{"criterion": "A renders.", "evidence": "test_a"}]) == ["B returns 200."]
    # The worker's evidence for B used different wording. Preserve it as unmatched, but keep the
    # exact frozen criterion blocked until the worker reconciles the statement.
    verified = [{"criterion": "A renders.", "evidence": "test_a"},
                {"criterion": "B returns two hundred.", "evidence": "test_b"}]
    assert evidence_gaps(criteria, verified) == ["B returns 200."]
    diagnosis = evidence_gap_diagnosis(criteria, verified, "run-123")
    assert "run-123" in diagnosis
    assert "B returns 200." in diagnosis
    assert "B returns two hundred." in diagnosis and "test_b" in diagnosis
    # not_done with a reason is not a gap at all
    verified2 = [{"criterion": "A renders.", "evidence": "test_a"},
                 {"criterion": "B returns 200.", "not_done": True, "reason": "blocked"}]
    assert evidence_gaps(criteria, verified2) == []


def test_normalize_verified_expands_a_done_summary_or_notes_without_rewriting_worker_rows():
    criteria = ["A renders.", "B returns 200.", "C remains visible."]
    partial = {"status": "done", "summary": "Focused tests passed.", "verified": [
        {"criterion": "A renders.", "evidence": "test_a"},
        {"criterion": "B returns 200.", "not_done": True, "reason": "service unavailable"},
        {"criterion": "Unknown criterion.", "evidence": "must remain visible"},
    ]}
    normalized = normalize_verified(criteria, partial)
    assert normalized is not None
    assert normalized[:3] == partial["verified"]
    generated = normalized[3]
    assert generated["criterion"] == "C remains visible."
    assert generated["evidence"] == "summary: Focused tests passed."
    assert generated["provenance"] == "normalized from worker summary attestation"
    replay = {**partial, "verified": normalized}
    assert normalize_verified(criteria, replay) == normalized

    notes_only = normalize_verified(criteria[:1], {"status": "done", "notes": "Inspected the result."})
    assert notes_only == [{"criterion": "A renders.", "evidence": "notes: Inspected the result.",
                           "provenance": "normalized from worker notes attestation"}]


def test_normalize_verified_rejects_empty_malformed_or_non_done_attestations_and_bounds_evidence():
    criteria = ["A."]
    for result in (
        {"status": "done"}, {"status": "done", "summary": "  ", "notes": "\t"},
        {"status": "done", "summary": 1}, {"status": "blocked", "summary": "looks good"},
    ):
        assert normalize_verified(criteria, result) is None
    normalized = normalize_verified(criteria, {"status": "done", "summary": "x" * 2000})
    assert normalized is not None
    assert len(normalized[0]["evidence"]) == 1000


def test_verification_markdown_surfaces_reconciliation_notes():
    criteria = ["A renders.", "B returns 200."]
    verified = [{"criterion": "A renders.", "evidence": "test_a"},
                {"criterion": "B returns two hundred.", "evidence": "test_b"}]
    rows = reconcile(criteria, verified)
    unmatched = unmatched_worker_entries(criteria, verified)
    md = verification_markdown(rows, unmatched)
    assert "- ⚠️ **B returns 200.** — no evidence given" in md
    assert "### Reconciliation notes" in md
    assert "test_b" in md
    # default (no unmatched passed) stays exactly as before
    assert "### Reconciliation notes" not in verification_markdown(rows)


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


def test_review_brief_surfaces_reconciliation_notes_for_wording_drift(garden):
    store = Store(garden)
    t = store.task("DM-001")
    t.body = CRITERIA_BODY
    store.save(t)
    store.invalidate()
    # the worker's wording for the first criterion drifted; its evidence would otherwise
    # look like a silent gap on "The widget renders on the home page."
    verified = [{"criterion": "The widget shows up on the home page.", "evidence": "test_widget"},
                {"criterion": "The API returns 200 for a valid request.", "evidence": "test_api"}]
    text = review_brief(store, store.task("DM-001"), branch="b", base="main", pr_title="T",
                        pr_body="B", diff="+a", max_diff_chars=1000, verified=verified)
    assert "The widget renders on the home page.** — author gave no evidence" in text
    assert "### Reconciliation notes" in text
    assert "The widget shows up on the home page." in text and "test_widget" in text


def test_review_markdown_lists_criteria():
    rev = {"verdict": "request_changes", "summary": "s",
           "criteria": [{"criterion": "A renders.", "met": True, "reason": "test_a"},
                        {"criterion": "B returns 200.", "met": False, "reason": "no test"}]}
    md = review_to_markdown(rev)
    assert "**Acceptance criteria**" in md
    assert "- ✅ A renders. — test_a" in md
    assert "- ❌ B returns 200. — no test" in md


def test_silently_skipped_criterion_blocks_pr_and_requests_revision(sched, fake_github, monkeypatch):
    """A worker that silently omits a criterion (no evidence, no reason) must not get an
    apparently valid PR out of it: the pre-PR gate rejects the unexplained gap and sends the
    worker back for a revise round instead of opening the PR."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    _give_criteria(sched)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "skip-criterion")

    sched.tick()  # dispatch work
    sched.tick()  # reap work -> gate rejects the silent gap -> revise dispatched, no PR

    assert fake_github.created == []
    revise_run = sched.runs.latest("DM-001")
    revise_brief = (revise_run.path / "brief.md").read_text()
    assert "## Revision round" in revise_brief
    findings = (revise_run.path / "references" / "context" / "review-findings.md").read_text()
    assert "acceptance criteria evidence" in findings
    assert "The widget renders on the home page." in findings


def test_summary_only_result_is_persisted_for_pr_and_review(sched, fake_github, monkeypatch):
    """A global attestation becomes durable per-criterion evidence before consumers run."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    _give_criteria(sched)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "summary-only")

    sched.tick()  # dispatch work
    sched.tick()  # reap work -> persist normalization -> open PR and dispatch review

    work_run = next(run for run in sched.runs.runs_for("DM-001") if run.mode == "work")
    verified = work_run.result["verified"]
    assert [row["criterion"] for row in verified] == parse_criteria(CRITERIA_BODY)
    assert all(row["provenance"] == "normalized from worker summary attestation" for row in verified)
    assert all(row["evidence"] == "summary: implemented the thing" for row in verified)
    assert "summary: implemented the thing" in fake_github.created[-1]["body"]
    review_run = sched.runs.latest("DM-001")
    assert review_run.mode == "review"
    assert "summary: implemented the thing" in (review_run.path / "brief.md").read_text()


def test_targeted_check_evidence_opens_pr_without_full_suite_finding(sched, fake_github, monkeypatch):
    """CGS-012 criterion 4: a narrow configured pre-PR check plus per-criterion evidence for the
    current head is enough to open the PR — nothing is reported missing just because an
    unlisted full-suite or browser-backed run never happened, and the mechanical gate only
    reports the checks that actually ran."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["checks"] = {"pre_pr": [{"name": "focused", "command": "true"}], "ci": []}
    _give_criteria(sched)
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)  # default "done" worker: full evidence

    for _ in range(6):
        sched.tick()
        if fake_github.created:
            break
    assert fake_github.created, "PR did not open"

    check_run = sched.runs.latest("DM-001")
    results = check_run.result["checks"]
    names = {r["name"] for r in results}
    assert "focused" in names
    assert not any(name for name in names if "full" in name or "suite" in name)
    assert all(r["status"] in ("pass", "advisory") for r in results)


def test_not_done_criterion_with_reason_still_opens_pr_and_flows_to_review(sched, fake_github, monkeypatch):
    """A worker that explicitly reports a criterion `not_done` with a reason (rather than
    silence) satisfies the evidence-or-explanation contract: the PR opens with a 🚧 row, and the
    reviewer marks it not met."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    _give_criteria(sched)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "not-done-criterion")

    sched.tick()  # dispatch work
    sched.tick()  # reap work -> PR opened with generated Verification -> review dispatched

    body = fake_github.created[-1]["body"]
    assert "## Verification" in body
    assert "- 🚧 **The widget renders on the home page.** — not done: ran out of time" in body
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
    # The unmet criterion is an objective implementation failure. Metrics group the
    # task under its current effective tier, while the dispatch event retains the
    # historical medium/sonnet route used for the first attempt.
    assert row["difficulty"] == "hard"
    d = m["by_difficulty"]["hard"]
    assert d["criteria_met"] == 1 and d["criteria_total"] == 2 and d["criteria_rate"] == 0.5
    first_work = next(r for r in sched.runs.runs_for("DM-001") if r.mode == "work")
    assert first_work.difficulty == "medium" and first_work.model == "sonnet"
    escalation = st["implementation_failure_escalations"][-1]
    assert escalation["signal"] == "unmet_acceptance_criteria"
    assert escalation["prior_tier"] == "medium" and escalation["new_tier"] == "hard"
    assert escalation["prior_model"] == "sonnet" and escalation["model"] == "opus"
