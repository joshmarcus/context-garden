"""Tests for friction.py: extraction, pr_body_for, harvest, write_friction_doc, reported friction."""

from __future__ import annotations

from garden.friction import (
    FRICTION_COMMENT_MARKER,
    append_friction_report,
    collect_comment_friction,
    declined_improvement_items,
    extract_friction,
    extract_section,
    friction_comment,
    friction_from_comment,
    friction_items,
    harvest,
    pr_body_for,
    record_friction,
    write_friction_doc,
)

# --------------------------------------------------------------------------- extract_friction


def test_extract_friction_basic():
    body = "## Summary\n\nDid the thing.\n\n## Friction\n\nNo docs for X.\n\n## Notes\n\nOK."
    assert extract_friction(body) == "No docs for X."


def test_declined_improvements_are_retro_visible_friction():
    items = declined_improvement_items({"improvements_declined": [
        {"suggestion": "Rename x.", "reason": "Public API compatibility."},
    ]})
    assert items == ["Declined review improvement: Rename x. — Public API compatibility."]


def test_extract_friction_missing():
    body = "## Summary\n\nDid the thing.\n\n## Notes\n\nOK."
    assert extract_friction(body) == ""


def test_extract_friction_empty_body():
    assert extract_friction("") == ""


def test_extract_friction_at_end():
    body = "## Summary\n\nDone.\n\n## Friction\n\n- point one\n- point two\n"
    assert extract_friction(body) == "- point one\n- point two"


def test_extract_friction_multiline():
    body = "## Friction\n\nFirst para.\n\nSecond para.\n\n## Notes\n\nx"
    assert extract_friction(body) == "First para.\n\nSecond para."


def test_extract_friction_level3_does_not_match():
    body = "### Friction\n\nshould not be extracted"
    assert extract_friction(body) == ""


# --------------------------------------------------------------------------- pr_body_for


class _FakeRun:
    def __init__(self, task_id, mode, result):
        self.task_id = task_id
        self.mode = mode
        self.result = result


class _FakeRunStore:
    def __init__(self, runs):
        self._runs = runs

    def runs_for(self, task_id):
        return [r for r in self._runs if r.task_id == task_id]


class _FakeTask:
    def __init__(self, task_id, pr=""):
        self.id = task_id
        self.pr = pr


class _FakeGitHub:
    """Stand-in for garden.github.GitHub."""

    def __init__(self, body=""):
        self.available = True
        self._body = body
        self.calls = []

    def get_pr(self, slug, number):
        self.calls.append((slug, number))
        from garden.github import PRInfo
        return PRInfo(number=number, url=f"https://example.com/pull/{number}", state="OPEN", body=self._body)


def test_pr_body_from_run():
    task = _FakeTask("T-001")
    runs = [_FakeRun("T-001", "work", {"pr_body": "## Friction\n\nHard."})]
    rs = _FakeRunStore(runs)
    assert pr_body_for(task, rs) == "## Friction\n\nHard."


def test_pr_body_latest_run_wins():
    task = _FakeTask("T-001")
    runs = [
        _FakeRun("T-001", "work", {"pr_body": "old body"}),
        _FakeRun("T-001", "revise", {"pr_body": "new body"}),
    ]
    rs = _FakeRunStore(runs)
    assert pr_body_for(task, rs) == "new body"


def test_pr_body_github_fallback():
    task = _FakeTask("T-001", pr="https://example.com/pull/42")
    rs = _FakeRunStore([])
    gh = _FakeGitHub(body="## Friction\n\nGitHub body.")
    result = pr_body_for(task, rs, github=gh, slug="owner/repo")
    assert result == "## Friction\n\nGitHub body."
    assert gh.calls == [("owner/repo", 42)]


def test_pr_body_run_takes_priority_over_github():
    task = _FakeTask("T-001", pr="https://example.com/pull/42")
    runs = [_FakeRun("T-001", "work", {"pr_body": "run body"})]
    rs = _FakeRunStore(runs)
    gh = _FakeGitHub(body="github body")
    result = pr_body_for(task, rs, github=gh, slug="owner/repo")
    assert result == "run body"
    assert gh.calls == []  # GitHub not called


def test_pr_body_no_slug_skips_github():
    task = _FakeTask("T-001", pr="https://example.com/pull/42")
    rs = _FakeRunStore([])
    gh = _FakeGitHub(body="github body")
    result = pr_body_for(task, rs, github=gh, slug=None)
    assert result == ""
    assert gh.calls == []


# --------------------------------------------------------------------------- harvest


class _FakePhase:
    def __init__(self, tasks):
        self.tasks = tasks


def test_harvest_with_friction():
    tasks = [
        _FakeTask("T-001", pr="https://example.com/pull/1"),
        _FakeTask("T-002", pr="https://example.com/pull/2"),
    ]
    runs = [
        _FakeRun("T-001", "work", {"pr_body": "## Friction\n\nWas hard."}),
        _FakeRun("T-002", "work", {"pr_body": "## Summary\n\nNo friction here."}),
    ]
    phase = _FakePhase(tasks)
    rs = _FakeRunStore(runs)
    entries = harvest(phase, rs)
    assert len(entries) == 1
    task, pr_url, text = entries[0]
    assert task.id == "T-001"
    assert pr_url == "https://example.com/pull/1"
    assert text == "Was hard."


def test_harvest_empty_when_no_friction():
    tasks = [_FakeTask("T-001")]
    runs = [_FakeRun("T-001", "work", {"pr_body": "## Summary\n\nDone."})]
    phase = _FakePhase(tasks)
    rs = _FakeRunStore(runs)
    assert harvest(phase, rs) == []


# --------------------------------------------------------------------------- write_friction_doc


def test_write_friction_doc_creates_file(tmp_path):
    tasks = [_FakeTask("T-001")]
    tasks[0].title = "My task"
    doc = tmp_path / "docs" / "friction.md"
    write_friction_doc(doc, [(tasks[0], "https://example.com/pull/1", "Was hard.")])
    text = doc.read_text()
    assert "## T-001: My task" in text
    assert "PR: https://example.com/pull/1" in text
    assert "Was hard." in text


def test_write_friction_doc_empty(tmp_path):
    doc = tmp_path / "docs" / "friction.md"
    write_friction_doc(doc, [])
    assert "No friction reported yet." in doc.read_text()


def test_write_friction_doc_idempotent(tmp_path):
    task = _FakeTask("T-001")
    task.title = "A task"
    doc = tmp_path / "docs" / "friction.md"
    entries = [(task, "https://example.com/pull/1", "Hard.")]
    write_friction_doc(doc, entries)
    first = doc.read_text()
    write_friction_doc(doc, entries)
    assert doc.read_text() == first


def test_write_friction_doc_no_pr_url(tmp_path):
    task = _FakeTask("T-001")
    task.title = "A task"
    doc = tmp_path / "docs" / "friction.md"
    write_friction_doc(doc, [(task, "", "Had issues.")])
    text = doc.read_text()
    assert "T-001" in text
    assert "PR:" not in text
    assert "Had issues." in text


def test_write_friction_doc_preserves_reported_section(tmp_path):
    task = _FakeTask("T-001")
    task.title = "A task"
    doc = tmp_path / "docs" / "friction.md"
    # Pre-populate with a Reported section
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Friction\n\n_No friction reported yet._\n\n## Reported\n\n### 2026-01-01 · cli\n\nSome friction.\n")
    write_friction_doc(doc, [(task, "https://example.com/pull/1", "PR friction.")])
    text = doc.read_text()
    assert "PR friction." in text
    assert "## Reported" in text
    assert "Some friction." in text


def test_write_friction_doc_idempotent_with_reported(tmp_path):
    task = _FakeTask("T-001")
    task.title = "A task"
    doc = tmp_path / "docs" / "friction.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Friction\n\n_No friction reported yet._\n\n## Reported\n\n### 2026-01-01 · cli\n\nSome friction.\n")
    write_friction_doc(doc, [])
    first = doc.read_text()
    write_friction_doc(doc, [])
    assert doc.read_text() == first


def test_write_friction_doc_preserves_hand_written_section(tmp_path):
    """A rewrite must not drop a section it didn't generate, whatever a human names it."""
    task = _FakeTask("T-001")
    task.title = "A task"
    doc = tmp_path / "docs" / "friction.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Friction\n\n_No friction reported yet._\n\n## First live run\n\nIt actually worked end to end.\n")
    write_friction_doc(doc, [(task, "https://example.com/pull/1", "PR friction.")])
    text = doc.read_text()
    assert "PR friction." in text
    assert "## First live run" in text
    assert "It actually worked end to end." in text


def test_write_friction_doc_preserves_multiple_hand_written_sections(tmp_path):
    doc = tmp_path / "docs" / "friction.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "# Friction\n\n_No friction reported yet._\n\n"
        "## First live run\n\nWent well.\n\n"
        "## Reported\n\n### 2026-01-01 · cli\n\nSome friction.\n"
    )
    write_friction_doc(doc, [])
    text = doc.read_text()
    assert "## First live run" in text and "Went well." in text
    assert "## Reported" in text and "Some friction." in text


def test_write_friction_doc_does_not_duplicate_a_regenerated_section(tmp_path):
    """A hand-written section named exactly like a currently-generated task heading is
    regenerated, not duplicated."""
    task = _FakeTask("T-001")
    task.title = "A task"
    doc = tmp_path / "docs" / "friction.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Friction\n\n## T-001: A task\n\nOld hand-edited text.\n")
    write_friction_doc(doc, [(task, "", "New harvested text.")])
    text = doc.read_text()
    assert text.count("## T-001: A task") == 1
    assert "New harvested text." in text
    assert "Old hand-edited text." not in text


# --------------------------------------------------------------------------- extract_section


def test_extract_section_returns_named_section():
    text = "# Friction\n\n_No friction reported yet._\n\n## First live run\n\nWent well.\n\n## Reported\n\nx\n"
    assert extract_section(text, "First live run") == "## First live run\n\nWent well."


def test_extract_section_missing_returns_empty():
    assert extract_section("# Friction\n\nnothing here\n", "Reported") == ""
    assert extract_section("", "Reported") == ""


# --------------------------------------------------------------------------- append_friction_report


def test_append_friction_report_creates_reported_section(tmp_path):
    doc = tmp_path / "docs" / "friction.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Friction\n\n_No friction reported yet._\n")
    append_friction_report(doc, "Widget X is confusing.", "/inbox", "2026-09-04")
    text = doc.read_text()
    assert "## Reported" in text
    assert "Widget X is confusing." in text
    assert "2026-09-04" in text
    assert "/inbox" in text


def test_append_friction_report_appends_to_existing_section(tmp_path):
    doc = tmp_path / "docs" / "friction.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Friction\n\n_No friction reported yet._\n\n## Reported\n\n### 2026-01-01 · cli\n\nFirst report.\n")
    append_friction_report(doc, "Second report.", "/inbox", "2026-09-04")
    text = doc.read_text()
    assert text.count("## Reported") == 1
    assert "First report." in text
    assert "Second report." in text


def test_append_friction_report_creates_file(tmp_path):
    doc = tmp_path / "docs" / "friction.md"
    append_friction_report(doc, "Brand new report.", "cli", "2026-09-04")
    text = doc.read_text()
    assert "Brand new report." in text
    assert "## Reported" in text


# --------------------------------------------------------------------------- friction field


def test_friction_items_normalises():
    assert friction_items({"friction": ["a", " b ", "", "  "]}) == ["a", "b"]
    assert friction_items({"friction": "just one"}) == ["just one"]
    assert friction_items({"friction": None}) == []
    assert friction_items({}) == []
    assert friction_items({"friction": 5}) == []


def test_friction_comment_round_trips():
    items = ["First thing.", "Second thing."]
    body = friction_comment(items)
    assert FRICTION_COMMENT_MARKER in body
    assert "**Friction reported**" in body
    assert friction_from_comment(body) == items


def test_friction_from_comment_ignores_unmarked():
    assert friction_from_comment("- not friction\n- also not") == []
    assert friction_from_comment("") == []


def test_record_friction_skips_duplicates(tmp_path):
    doc = tmp_path / "docs" / "friction.md"
    first, second = "Spec had no schema link.", "Env needed PYTHONPATH."
    third = "The CLI help was stale."
    fresh = record_friction(doc, [first, second], "run r1", "2026-09-04")
    assert fresh == [first, second]
    # second already recorded; only third is new
    fresh = record_friction(doc, [second, third], "run r2", "2026-09-04")
    assert fresh == [third]
    text = doc.read_text()
    assert text.count(second) == 1
    assert third in text


class _FakeCommentGitHub:
    def __init__(self, comments):
        self.available = True
        self._comments = comments

    def issue_comments(self, slug, number):
        return list(self._comments)


def test_collect_comment_friction():
    tasks = [_FakeTask("T-001", pr="https://example.com/pull/7")]
    phase = _FakePhase(tasks)
    gh = _FakeCommentGitHub([friction_comment(["Config was undocumented."]), "unrelated comment"])
    out = collect_comment_friction(phase, gh, "owner/repo")
    assert out == [(tasks[0], ["Config was undocumented."])]


def test_collect_comment_friction_without_github():
    phase = _FakePhase([_FakeTask("T-001", pr="https://example.com/pull/7")])
    assert collect_comment_friction(phase, None, "owner/repo") == []


# --------------------------------------------------------------------------- through the scheduler


def test_worker_friction_reaches_record_and_comment_not_body(sched, fake_github, monkeypatch):
    from garden.runs import RunStore

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "friction")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> friction recorded

    items = ["The spec never linked the schema.", "Tests needed PYTHONPATH set."]
    # one marked PR comment carries every item
    friction_comments = [c for c in fake_github.comments if "Friction reported" in c]
    assert len(friction_comments) == 1
    for it in items:
        assert it in friction_comments[0]
    # the phase's friction record has them under ## Reported
    doc = sched.store.phase("demo", "p1").path / "docs" / "friction.md"
    text = doc.read_text()
    assert "## Reported" in text
    for it in items:
        assert it in text
    # never in the PR body
    body = fake_github.created[-1]["body"]
    for it in items:
        assert it not in body
    # garden friction harvests it: the record survives a rewrite of the doc
    entries = harvest(sched.store.phase("demo", "p1"), RunStore(sched.cfg.garden_dir))
    write_friction_doc(doc, entries)
    text = doc.read_text()
    for it in items:
        assert it in text


def test_revise_omitting_pr_body_keeps_the_description(sched, fake_github, monkeypatch):
    from garden.model import Status

    sched.tick()
    sched.tick()  # reap work -> PR opened with a description
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    original_body = fake_github.prs[task.branch].body
    assert original_body

    # a revise round that only reworded: it omits pr_body, so the description must stay
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "omit-body")
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "please reword the summary"
    task.status = Status.CHANGES_REQUESTED
    sched.store.save(task)
    sched.state.save()
    sched.dispatch(task, mode="revise")
    sched.tick()  # reap revise -> PR updated (title only), body untouched

    assert fake_github.prs[task.branch].body == original_body
    assert fake_github.updated[-1]["body"] == ""  # scheduler passed no fabricated body
