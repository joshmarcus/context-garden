"""Browser notifications (CG-208): the /api/decisions endpoint, the decision_notifications
mapper, the rail toggle and the polling script in base.html."""

from __future__ import annotations

from garden.events import DECISION_KINDS, EventLog, decision_notifications
from garden.store import Store
from tests.test_web import client


def _log(garden):
    return EventLog(Store(garden).config.garden_dir / "events.jsonl")


# --------------------------------------------------------------------------- the mapper
def test_every_decision_kind_becomes_an_item():
    # One synthetic event of each decision kind, with the fields the scheduler writes.
    evs = [
        {"at": "2026-09-05T10:00:00+00:00", "kind": "waiting_human", "task": "DM-001", "question": "Postgres or SQLite?"},
        {"at": "2026-09-05T10:01:00+00:00", "kind": "decision", "task": "DM-002", "decision": "wont_do", "reason": "out of scope"},
        {"at": "2026-09-05T10:02:00+00:00", "kind": "decision", "task": "DM-003", "target": "DM-004", "decision_kind": "duplicate", "of": "DM-005"},
        {"at": "2026-09-05T10:03:00+00:00", "kind": "needs_human", "task": "DM-006", "stop_kind": "review_cap", "reason": "rounds used"},
        {"at": "2026-09-05T10:04:00+00:00", "kind": "needs_human", "task": "DM-007", "stop_kind": "base_broken", "reason": "base red"},
        {"at": "2026-09-05T10:05:00+00:00", "kind": "stall", "task": "DM-008", "reason": "nothing changed"},
        {"at": "2026-09-05T10:06:00+00:00", "kind": "discovered", "task": "DM-009", "new_task": "DM-010", "title": "A follow-up"},
        {"at": "2026-09-05T10:07:00+00:00", "kind": "retro_done", "task": "", "phase": "demo/p1"},
        {"at": "2026-09-05T10:08:00+00:00", "kind": "retro_question", "task": "", "phase": "demo/p1"},
        {"at": "2026-09-05T10:09:00+00:00", "kind": "retro_verdict", "task": "", "phase": "demo/p1", "status": "pending"},
        {"at": "2026-09-05T10:10:00+00:00", "kind": "phase_closed", "task": "", "phase": "demo/p1"},
        # a retro_verdict that closed the phase at once needs no separate notification
        # (retro_done already fired in the same tick); it must not become an item.
        {"at": "2026-09-05T10:11:00+00:00", "kind": "retro_verdict", "task": "", "phase": "demo/p2", "status": "accepted"},
    ]
    items = decision_notifications(evs, titles={"DM-001": "Set up the store"})
    # every listed kind produces exactly one item, except the accepted retro_verdict, which
    # is filtered out (it does not need a fresh decision)
    assert len(items) == len(evs) - 1
    assert {i["kind"] for i in items} == set(DECISION_KINDS)
    # each item has a non-empty title and a URL to open the task or phase it is about
    assert all(i["title"] and i["url"] for i in items)
    by_kind = {i["kind"]: i for i in items}
    assert by_kind["waiting_human"]["title"] == "Set up the store asks: Postgres or SQLite?"
    assert by_kind["waiting_human"]["url"] == "/tasks/DM-001"
    assert by_kind["decision"]["url"] == "/tasks/DM-004"  # the duplicate's target, not the proposer
    assert "duplicates" in by_kind["decision"]["title"]
    assert by_kind["needs_human"]["url"] == "/tasks/DM-007"
    assert "base branch is broken" in by_kind["needs_human"]["title"]
    assert by_kind["stall"]["url"] == "/tasks/DM-008"
    assert by_kind["discovered"]["url"] == "/tasks/DM-010"
    for k in ("retro_done", "retro_question", "retro_verdict", "phase_closed"):
        assert by_kind[k]["url"] == "/phases/demo/p1"


def test_notices_never_notify():
    notices = [
        {"at": "t", "kind": "dispatch", "task": "DM-001"},
        {"at": "t", "kind": "run_finished", "task": "DM-001"},
        {"at": "t", "kind": "pr_opened", "task": "DM-001"},
        {"at": "t", "kind": "review", "task": "DM-001", "verdict": "approve"},
        {"at": "t", "kind": "triaged", "task": "DM-001"},
        {"at": "t", "kind": "transition", "task": "DM-001", "to": "done"},
        {"at": "t", "kind": "automerged", "task": "DM-001"},
    ]
    assert decision_notifications(notices) == []


# --------------------------------------------------------------------------- the endpoint
def test_endpoint_returns_decisions_and_uses_task_titles(garden):
    log = _log(garden)
    log.emit("waiting_human", "DM-001", question="Postgres or SQLite?")
    log.emit("dispatch", "DM-002")  # a notice: must not appear
    c = client(garden)
    items = c.get("/api/decisions").json()
    assert [i["kind"] for i in items] == ["waiting_human"]
    it = items[0]
    assert it["title"] == "First task asks: Postgres or SQLite?"  # DM-001's title from the store
    assert it["url"] == "/tasks/DM-001"
    assert it["at"]  # an ISO timestamp the client tracks as last-seen


def test_endpoint_since_excludes_older_and_page_load_sees_nothing(garden):
    log = _log(garden)
    ev = log.emit("decision", "DM-001", decision="no_change", reason="nothing to change")
    c = client(garden)
    # since a moment after the event: nothing (this is what a freshly-loaded page does)
    assert c.get("/api/decisions", params={"since": "2999-01-01T00:00:00+00:00"}).json() == []
    # since the event's own timestamp: the event is included (>= since)
    got = c.get("/api/decisions", params={"since": ev["at"]}).json()
    assert [i["kind"] for i in got] == ["decision"]
    assert "nothing to change" in got[0]["title"]


def test_endpoint_repeats_the_boundary_event_so_the_client_must_dedupe(garden):
    # The client advances its last-seen mark to the newest event's own `at`, then re-polls
    # with since=that timestamp. Because read() is inclusive of `since`, that boundary event
    # comes back on the very next poll — the client must filter it out itself, or it would
    # re-notify the most-recent decision every interval until a newer one arrives.
    log = _log(garden)
    ev = log.emit("waiting_human", "DM-001", question="Postgres or SQLite?")
    c = client(garden)
    again = c.get("/api/decisions", params={"since": ev["at"]}).json()
    assert [i["kind"] for i in again] == ["waiting_human"]  # same event, returned again
    assert again[0]["at"] == ev["at"]


# --------------------------------------------------------------------------- the rail + script
def test_rail_shows_the_toggle_and_the_page_carries_the_script(garden):
    c = client(garden)
    html = c.get("/").text
    # the toggle is server-rendered (so the walkthrough's rail shows it and JS-off is identical)
    assert 'id="notify-toggle"' in html
    assert "Notify me in this browser" in html
    # the polling script and the permission request are present
    assert "/api/decisions?since=" in html
    assert "Notification.requestPermission" in html
    assert 'tag: "garden-decisions"' in html


def test_script_dedupes_the_inclusive_boundary_across_polls(garden):
    # Guard against a regression to "advance to max `at`, fire every returned item": because
    # /api/decisions is inclusive of `since`, that re-notifies the newest decision every poll.
    # The script must remember the events seen at the boundary second and fire only the rest.
    html = client(garden).get("/").text
    assert "garden.notify.seenKeys" in html          # the tie-breaker set is persisted
    assert "const fresh = items.filter" in html       # new items are filtered from the batch
    assert "if (fresh.length) fire(fresh)" in html    # only the fresh ones notify, not all items
