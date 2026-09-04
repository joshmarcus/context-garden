"""A worker's discovery can be a decision (duplicate / cancel) or a note, not only a task.

Covers the four `kind`s of a discovered item: `task` (files work, as before), `duplicate`
and `cancel` (a decision card, Accept cancels / Reject keeps), and `note` (friction record,
no card). Driven through the fake harness end to end.
"""

from __future__ import annotations

from garden.events import EventLog, digest
from garden.inbox import build_inbox
from garden.model import Status
from tests.conftest import wait_for_runs


def _reap_with_decisions(sched, monkeypatch):
    """Run DM-001 in discover-kinds mode and reap it, without dispatching anything else."""
    # DM-003 is the target of the `cancel` decision; create it up front.
    sched.store.create_task("demo", "p1", "Third task", "## Goal\n\nOld thing.\n",
                            status="draft", task_id="DM-003")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "discover-kinds")
    sched.tick()  # dispatches DM-001
    wait_for_runs(sched)
    sched.tick(dispatch=False)  # reap only; leaves DM-002/DM-003 alone
    sched.store.invalidate()


def test_duplicate_and_cancel_make_decision_cards_not_tasks(sched, fake_github, monkeypatch):
    _reap_with_decisions(sched, monkeypatch)
    tasks = sched.store.tasks()

    # The `task` kind still files work; the decisions and note do not.
    filed = [t for t in tasks.values() if t.discovered_from == "DM-001"]
    assert [t.title for t in filed] == ["A real follow-up task"]
    assert filed[0].status == Status.DRAFT

    decisions = sched.pending_decisions()
    by_kind = {d["kind"]: d for d in decisions}
    assert set(by_kind) == {"duplicate", "cancel"}
    assert by_kind["duplicate"]["target"] == "DM-002" and by_kind["duplicate"]["of"] == "DM-001"
    assert by_kind["cancel"]["target"] == "DM-003"
    assert by_kind["duplicate"]["proposed_by"] == "DM-001"

    # Both show up under "Needs a decision" with Accept/Reject, quoting the worker's reason.
    cards = [i for i in build_inbox(sched.store, sched) if i.get("decision")]
    assert len(cards) == 2
    dup_card = next(c for c in cards if c["task"] == "DM-002")
    assert c_labels(dup_card) == ["Accept", "Reject"]
    assert "DM-002 restates DM-001" in dup_card["why"] and dup_card["group"] == "attention"


def test_accept_cancels_named_task_with_provenance(sched, fake_github, monkeypatch):
    _reap_with_decisions(sched, monkeypatch)
    did = next(d["id"] for d in sched.pending_decisions() if d["kind"] == "duplicate")
    run_id = next(d["run"] for d in sched.pending_decisions() if d["kind"] == "duplicate")

    sched.resolve_decision(did, accept=True)
    sched.store.invalidate()
    dm002 = sched.store.task("DM-002")
    assert dm002.status == Status.CANCELLED
    assert "duplicate of DM-001" in dm002.body and "proposed by DM-001" in dm002.body
    assert run_id in dm002.body  # which run proposed it

    # The card is gone, and only the still-open `cancel` decision remains.
    remaining = sched.pending_decisions()
    assert [d["kind"] for d in remaining] == ["cancel"]


def test_reject_keeps_task_and_logs_disagreement(sched, fake_github, monkeypatch):
    _reap_with_decisions(sched, monkeypatch)
    did = next(d["id"] for d in sched.pending_decisions() if d["kind"] == "cancel")

    sched.resolve_decision(did, accept=False)
    sched.store.invalidate()
    dm003 = sched.store.task("DM-003")
    assert dm003.status == Status.DRAFT  # not cancelled
    assert "decision rejected" in dm003.body and "DM-001" in dm003.body

    assert [d["kind"] for d in sched.pending_decisions()] == ["duplicate"]


def test_note_reaches_friction_record_and_makes_no_card(sched, fake_github, monkeypatch):
    _reap_with_decisions(sched, monkeypatch)
    doc = sched.store.root / "demo" / "p1" / "docs" / "friction.md"
    assert doc.exists()
    text = doc.read_text()
    assert "The brief for DM-001 was missing the spec link." in text
    assert "## Reported" in text and "discovered by DM-001" in text

    # No inbox card was made for the note.
    cards = [i for i in build_inbox(sched.store, sched) if i.get("decision")]
    assert all("missing the spec link" not in c["why"] for c in cards)


def test_decisions_are_listed_in_the_digest(sched, fake_github, monkeypatch):
    _reap_with_decisions(sched, monkeypatch)
    events = EventLog(sched.cfg.garden_dir / "events.jsonl").read()
    d = digest(events)
    decided = {ev["task"] for ev in d["needs_human"] if ev.get("kind") == "decision"}
    assert decided == {"DM-002", "DM-003"}


def test_no_kind_discovery_is_still_a_task(sched, fake_github, monkeypatch):
    """Existing workers that emit `discovered` without a `kind` keep filing tasks."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "discover")
    sched.tick()
    wait_for_runs(sched)
    sched.tick(dispatch=False)
    sched.store.invalidate()
    tasks = sched.store.tasks()
    filed = sorted(t.title for t in tasks.values() if t.discovered_from == "DM-001")
    assert filed == ["Add the missing config schema", "Fix the flaky widget test"]
    assert sched.pending_decisions() == []


def c_labels(card: dict) -> list[str]:
    return [a["label"] for a in card["actions"]]
