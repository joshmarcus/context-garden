"""Tests for friction.py: extraction, pr_body_for, harvest, write_friction_doc."""

from __future__ import annotations

from garden.friction import extract_friction, harvest, pr_body_for, write_friction_doc

# --------------------------------------------------------------------------- extract_friction


def test_extract_friction_basic():
    body = "## Summary\n\nDid the thing.\n\n## Friction\n\nNo docs for X.\n\n## Notes\n\nOK."
    assert extract_friction(body) == "No docs for X."


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
