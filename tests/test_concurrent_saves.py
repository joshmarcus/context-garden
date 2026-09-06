"""CG-243: concurrency control for task-file saves and moves.

A task file is a whole document, but a save only ever means to change a few of its fields. An
action (a button press) and a scheduler tick run concurrently over separate Store instances, so
a save must reapply only the fields its writer changed onto whatever is on disk, rather than the
later writer clobbering the earlier one's whole file with a stale in-memory copy. A move that
races a tick's save must not leave two files with the same id (which would stop the garden)."""

from __future__ import annotations

from garden.model import Status
from garden.scheduler import Scheduler
from garden.store import Store


def _run(garden, *args):
    import os

    from typer.testing import CliRunner

    from garden.cli import app

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(cwd)


def test_disjoint_field_edits_from_two_writers_both_survive(garden):
    """A priority edit (an action) and a status change (a tick), each loaded from disk into its
    own Store, both land: the later save merges its one changed field onto the current file
    instead of writing its whole stale copy."""
    action, tick = Store(garden), Store(garden)
    ta, tt = action.task("DM-001"), tick.task("DM-001")

    ta.priority = 0
    ta.log("priority 1 -> 0 (web)")
    tick_transition_status(tt, Status.CANCELLED, "cancelled (tick)")

    action.save(ta)
    tick.save(tt)  # saved second; must not clobber the priority edit

    got = Store(garden).task("DM-001")
    assert got.priority == 0
    assert got.status == Status.CANCELLED
    assert "priority 1 -> 0 (web)" in got.body
    assert "cancelled (tick)" in got.body


def test_merge_is_order_independent(garden):
    """Whichever writer saves last, both changes survive: the merge is symmetric."""
    action, tick = Store(garden), Store(garden)
    ta, tt = action.task("DM-001"), tick.task("DM-001")

    ta.difficulty = "hard"
    ta.log("difficulty medium -> hard (web)")
    tick_transition_status(tt, Status.IN_REVIEW, "opened a PR (tick)")

    tick.save(tt)
    action.save(ta)  # reversed order from the test above

    got = Store(garden).task("DM-001")
    assert got.difficulty == "hard"
    assert got.status == Status.IN_REVIEW
    assert "difficulty medium -> hard (web)" in got.body
    assert "opened a PR (tick)" in got.body


def test_concurrent_log_appends_both_kept(garden):
    """Two writers that only append a log line each keep both lines."""
    a, b = Store(garden), Store(garden)
    ta, tb = a.task("DM-001"), b.task("DM-001")
    ta.log("first note")
    tb.log("second note")
    a.save(ta)
    b.save(tb)

    body = Store(garden).task("DM-001").body
    assert "first note" in body
    assert "second note" in body


def test_a_read_only_save_keeps_a_concurrent_writers_change(garden):
    """A writer that touched no field must not roll back a field another writer changed: saving
    an untouched task reapplies nothing, so the other writer's status survives."""
    reader, writer = Store(garden), Store(garden)
    tr, tw = reader.task("DM-001"), writer.task("DM-001")

    tick_transition_status(tw, Status.DONE, "merged (tick)")
    writer.save(tw)

    reader.save(tr)  # tr changed nothing; it must not resurrect the old status

    got = Store(garden).task("DM-001")
    assert got.status == Status.DONE
    assert "merged (tick)" in got.body


def test_stale_save_after_a_move_does_not_recreate_the_task(garden):
    """A tick loads a task, a concurrent move relocates it to another phase (deleting the old
    file), then the tick saves. The stale save must not recreate the file at the old path: that
    would leave two files with the same id, and `Store.tasks()` would refuse to load the garden
    at all."""
    assert _run(garden, "new-phase", "demo", "p2").exit_code == 0

    tick = Store(garden)
    stale = tick.task("DM-001")  # loaded from demo/p1, before the move
    tick_transition_status(stale, Status.IN_REVIEW, "opened a PR (tick)")

    mover = Scheduler(Store(garden), log=print)
    mover.move(mover.store.task("DM-001"), "demo", "p2")

    old_path = garden / "demo" / "p1" / "tasks" / "DM-001-first.md"
    new_path = garden / "demo" / "p2" / "tasks" / "DM-001-first.md"
    assert not old_path.exists() and new_path.exists()

    wrote = tick.save(stale)  # the stale save lands on a file the move deleted

    assert wrote is False  # dropped, not written
    assert not old_path.exists()  # the old path was not resurrected
    # the garden still loads: exactly one file carries DM-001, so there is no duplicate id
    got = Store(garden).task("DM-001")
    assert got.phase == "p2"


def test_move_writes_the_new_path_whole(garden):
    """A move is a write to a new path: it writes the task whole (there is nothing to merge
    against there) and leaves the old file removed."""
    assert _run(garden, "new-phase", "demo", "p2").exit_code == 0
    sched = Scheduler(Store(garden), log=print)
    sched.move(sched.store.task("DM-001"), "demo", "p2")

    got = Store(garden).task("DM-001")
    assert got.phase == "p2"
    assert "moved from demo/p1 to demo/p2" in got.body
    assert not (garden / "demo" / "p1" / "tasks" / "DM-001-first.md").exists()


def tick_transition_status(task, status: Status, note: str) -> None:
    """Stand in for a tick's `_transition`: set the status and append a log line, the two field
    changes a status transition makes to the file."""
    task.status = status
    task.log(note)
