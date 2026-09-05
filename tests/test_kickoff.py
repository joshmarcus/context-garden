"""`garden kickoff`: before a phase starts, flag design gaps, goals with no measurable
outcome, questions for the owner, and docs that need attention. See CG-224."""

from __future__ import annotations

from pathlib import Path

from garden.kickoff import (
    KICKOFF_MARKER,
    append_goal_gaps,
    cited_doc_paths,
    kickoff_brief,
    kickoff_doc_path,
    parse_kickoff,
    previous_phase,
    render_kickoff_doc,
)
from garden.model import Phase, Status
from garden.store import Store
from tests.conftest import write


# --------------------------------------------------------------------------- pure logic
def test_parse_kickoff_reads_the_last_marker_line():
    text = ('noise\n' + KICKOFF_MARKER + ' {"design_needed": [], "goals_gaps": [], '
            '"questions": [], "docs": [], "ready": true, "summary": "s"}\ntrailer')
    data = parse_kickoff(text)
    assert data["summary"] == "s" and data["ready"] is True
    assert parse_kickoff("no marker here") == {}


def test_kickoff_brief_includes_goals_tasks_and_cited_docs(garden):
    store = Store(garden)
    write(garden / "demo" / "p1" / "specs" / "cited.md", "# cited\n\nStale content.\n")
    t = store.task("DM-001")
    t.reading = ["demo/p1/specs/cited.md"]
    store.save(t)
    store.invalidate()
    ph = store.phase("demo", "p1")
    brief = kickoff_brief(store, ph)
    assert "# Kickoff review: demo/p1" in brief
    assert "Do the first thing." in brief  # a draft task's body is inlined
    assert "Stale content." in brief  # a cited doc is inlined
    assert KICKOFF_MARKER in brief


def test_cited_doc_paths_dedupes_and_skips_missing(garden):
    store = Store(garden)
    ph = store.phase("demo", "p1")
    paths = cited_doc_paths(store, ph)
    assert paths == [store.root / "demo" / "p1" / "specs" / "spec.md"]


def test_previous_phase_is_none_for_the_first_phase(garden):
    store = Store(garden)
    ph = store.phase("demo", "p1")
    assert previous_phase(store, ph) is None
    write(garden / "demo" / "p2" / "goals.md", "# p2\n\nNext.\n")
    store.invalidate()
    ph2 = store.phase("demo", "p2")
    assert previous_phase(store, ph2).name == "p1"


def test_render_kickoff_doc_carries_every_list_and_the_verdict():
    phase = Phase(product="demo", name="p1", path=Path("/x/demo/p1"), goals_path=None, specs=[], docs=[], tasks=[])
    data = {
        "design_needed": [{"topic": "shape", "why": "unclear", "tasks": ["DM-001"], "spike": "write a note"}],
        "goals_gaps": [{"goal": "ship it", "missing": "no criteria prove it"}],
        "questions": [{"question": "which db?", "context": "matters", "options": ["a", "b"]}],
        "docs": [{"path": "docs/x.md", "issue": "stale", "tasks": ["DM-001"]}],
        "ready": False, "summary": "not quite ready",
    }
    filed_design = [{"topic": "shape", "task_id": "DM-010"}]
    filed_docs = [{"path": "docs/x.md", "task_id": "DM-011"}]
    filed_questions = [{"question": "which db?", "decision_id": "run1-q0"}]
    goals_gaps = data["goals_gaps"]
    doc = render_kickoff_doc(phase, data, filed_design, filed_docs, filed_questions, goals_gaps)
    assert "# Kickoff: demo/p1" in doc
    assert "not ready yet" in doc and "not quite ready" in doc
    assert "shape" in doc and "DM-010" in doc
    assert "ship it" in doc and "no criteria prove it" in doc
    assert "which db?" in doc and "run1-q0" in doc
    assert "docs/x.md" in doc and "DM-011" in doc


def test_append_goal_gaps_creates_and_dedupes_open_section(garden):
    store = Store(garden)
    ph = store.phase("demo", "p1")
    gaps = [{"goal": "ship it", "missing": "no criteria"}]
    append_goal_gaps(ph, gaps)
    text = (ph.goals_path).read_text()
    assert "## Open" in text and "**ship it**: no criteria" in text
    # a second run with the identical gap does not duplicate the line
    append_goal_gaps(ph, gaps)
    assert text.count("**ship it**") == (ph.goals_path.read_text()).count("**ship it**")


# --------------------------------------------------------------------------- end to end
def test_kickoff_dispatch_and_reap_files_everything(sched, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    ph = sched.store.phase("demo", "p1")
    assert not sched.has_kickoff(ph)
    run = sched.start_kickoff(ph)
    assert "# Kickoff review: demo/p1" in (run.path / "brief.md").read_text()
    assert sched.kickoff_pending(ph.key)
    rep = sched.tick()
    assert not rep.errors, rep.errors
    assert not sched.kickoff_pending(ph.key)

    doc = kickoff_doc_path(sched.store.phase("demo", "p1"))
    assert doc.exists()
    text = doc.read_text()
    assert "# Kickoff: demo/p1" in text
    assert "Undecided storage format" in text
    assert "docs/architecture.md" in text

    sched.store.invalidate()
    filed = [t for t in sched.store.tasks().values() if t.discovered_from == "kickoff:demo/p1"]
    spikes = [t for t in filed if t.extra.get("spike")]
    docs_tasks = [t for t in filed if not t.extra.get("spike")]
    assert len(spikes) == 1 and spikes[0].status == Status.DRAFT
    assert "Undecided storage format" in spikes[0].title
    assert len(docs_tasks) == 1 and docs_tasks[0].status == Status.DRAFT
    assert "docs/architecture.md" in docs_tasks[0].title

    decisions = sched.pending_decisions()
    qs = [d for d in decisions if d.get("kind") == "question"]
    assert len(qs) == 1
    assert qs[0]["phase"] == "demo/p1"
    assert "hard-tier merges" in qs[0]["question"]

    goals_text = ph.goals_path.read_text()
    assert "## Open" in goals_text and "overnight" in goals_text


def test_kickoff_refuses_a_second_dispatch_while_one_is_in_flight(sched, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    ph = sched.store.phase("demo", "p1")
    sched.start_kickoff(ph)
    try:
        sched.start_kickoff(ph)
        raise AssertionError("expected a refusal")
    except RuntimeError as e:
        assert "already has a kickoff run in flight" in str(e)


def test_answer_and_dismiss_kickoff_question(sched, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    ph = sched.store.phase("demo", "p1")
    sched.start_kickoff(ph)
    sched.tick()
    decisions = sched.pending_decisions()
    did = next(d["id"] for d in decisions if d["kind"] == "question")
    sched.answer_kickoff_question(did, "two rounds")
    assert sched.pending_decisions() == []
    doc_text = kickoff_doc_path(sched.store.phase("demo", "p1")).read_text()
    assert "answered: two rounds" in doc_text

    # a second question, dismissed instead of answered
    sched.state.get("_decisions")["fake-q1"] = {
        "id": "fake-q1", "kind": "question", "target": "", "phase": "demo/p1",
        "question": "another one?", "context": "", "options": [], "at": "2026-01-01T00:00:00+00:00",
        "status": "pending",
    }
    sched.dismiss_kickoff_question("fake-q1")
    assert sched.pending_decisions() == []
    doc_text = kickoff_doc_path(sched.store.phase("demo", "p1")).read_text()
    assert "dismissed" in doc_text


def test_resolve_decision_refuses_a_question_card(sched, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    ph = sched.store.phase("demo", "p1")
    sched.start_kickoff(ph)
    sched.tick()
    did = next(d["id"] for d in sched.pending_decisions() if d["kind"] == "question")
    try:
        sched.resolve_decision(did, accept=True)
        raise AssertionError("expected a refusal")
    except RuntimeError as e:
        assert "--answer/--dismiss" in str(e)


def test_approve_warns_on_a_phase_start_with_no_kickoff(garden, sched):
    write(garden / "demo" / "p2" / "goals.md", "# p2\n\nNext.\n")
    write(garden / "demo" / "p2" / "tasks" / "DM-010-a.md", """
        ---
        id: DM-010
        title: A task
        status: draft
        depends_on: []
        priority: 2
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do it.

        ## Acceptance criteria

        - [ ] It works, proven by a test.
        """)
    write(garden / "demo" / "p2" / "tasks" / "DM-011-b.md", """
        ---
        id: DM-011
        title: Another task
        status: draft
        depends_on: []
        priority: 2
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do it too.

        ## Acceptance criteria

        - [ ] It works too, proven by a test.
        """)
    sched.store.invalidate()
    ph = sched.store.phase("demo", "p2")
    t1 = sched.store.task("DM-010")
    warning = sched.approve(t1, by="test", phase=ph)
    assert warning and "garden kickoff demo/p2" in warning

    # the second task in the same phase is no longer "the phase's first task"
    ph = sched.store.phase("demo", "p2")
    t2 = sched.store.task("DM-011")
    warning2 = sched.approve(t2, by="test", phase=ph)
    assert not warning2


def test_approve_is_quiet_once_a_kickoff_report_exists(garden, sched, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    write(garden / "demo" / "p2" / "goals.md", "# p2\n\nNext.\n")
    write(garden / "demo" / "p2" / "tasks" / "DM-010-a.md", """
        ---
        id: DM-010
        title: A task
        status: draft
        depends_on: []
        priority: 2
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do it.

        ## Acceptance criteria

        - [ ] It works, proven by a test.
        """)
    sched.store.invalidate()
    ph = sched.store.phase("demo", "p2")
    sched.start_kickoff(ph)
    sched.tick()
    sched.store.invalidate()
    ph = sched.store.phase("demo", "p2")
    t1 = sched.store.task("DM-010")
    warning = sched.approve(t1, by="test", phase=ph)
    assert not warning
