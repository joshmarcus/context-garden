from __future__ import annotations

from garden.preflight import PREFLIGHT_ITEMS, mechanical_results, missing_preflight
from garden.review import review_brief
from garden.store import Store


def test_brief_requires_a_complete_preflight(garden):
    from garden.brief import build_brief

    text = build_brief(Store(garden), Store(garden).task("DM-001")).text
    assert "## Review pre-flight" in text
    assert "pre_flight" in text
    assert missing_preflight([]) == list(PREFLIGHT_ITEMS)
    rows = [{"item": item, "status": "pass"} for item in PREFLIGHT_ITEMS]
    assert missing_preflight(rows) == []


def test_mechanical_preflight_checks_each_failure_shape(garden, monkeypatch):
    worktree = garden / "work"
    worktree.mkdir()
    (worktree / "bad.py").write_text("def broken(:\n")
    from garden import gitops

    monkeypatch.setattr(gitops, "diff", lambda *_args: "<<<<<<< ours\n=======\n>>>>>>> theirs\n")
    monkeypatch.setattr(gitops, "diff_names", lambda *_args: ["bad.py", "src/garden/web/page.html"])
    results = mechanical_results(worktree, "main", "", require_description=True, ui_changed=True, captures=[])
    failed = {row["name"] for row in results if row["status"] == "fail"}
    assert failed == {"conflict markers", "syntax", "UI captures", "PR description"}


def test_review_brief_uses_frozen_criteria_and_marks_delta(garden):
    store = Store(garden)
    task = store.task("DM-001")
    frozen = ["The original criterion is met."]
    task.body += "\n## Acceptance criteria\n\n- [ ] The later criterion is met.\n"
    text = review_brief(store, task, branch="b", base="main", pr_title="T", pr_body="B", diff="",
                        max_diff_chars=1000, criteria_snapshot=frozen,
                        verified=[{"criterion": frozen[0], "evidence": "test_original"}])
    assert "Criteria frozen for this dispatch" in text
    assert frozen[0] in text
    assert "## Criteria changed after dispatch" in text
    assert "The later criterion is met." in text


def test_missing_preflight_is_sent_back_before_a_pr(sched, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "omit-preflight")
    sched.tick()
    report = sched.tick()
    task = sched.store.task("DM-001")
    assert task.status.value == "running"  # the automatic revise starts in the same tick
    assert not task.pr
    first = sched.runs.runs_for(task.id)[0]
    assert "missing review pre-flight" in first.error
    assert "DM-001 -> changes_requested (checks)" in report.transitions
