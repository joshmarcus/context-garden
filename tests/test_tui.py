import asyncio
import textwrap

from textual.widgets import DataTable

from garden.model import Status
from garden.store import Store
from garden.tui.app import GardenTUI


def _setup_decisions(sched, monkeypatch):
    """Reap DM-001 in discover-kinds mode so a duplicate (DM-002) and a cancel (DM-003)
    decision are pending on disk, ready for the TUI to act on."""
    sched.store.create_task("demo", "p1", "Third task", "## Goal\n\nOld.\n",
                            status="draft", task_id="DM-003")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "discover-kinds")
    sched.tick()
    sched.tick(dispatch=False)
    sched.store.invalidate()


def test_tui_wires_decision_cards_to_accept_and_reject(sched, fake_github, monkeypatch):
    _setup_decisions(sched, monkeypatch)
    dup = next(d for d in sched.pending_decisions() if d["kind"] == "duplicate")
    can = next(d for d in sched.pending_decisions() if d["kind"] == "cancel")

    async def run():
        app = GardenTUI(Store(sched.store.root))
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause()  # starts on the Inbox tab
            inbox = app.query_one("#inbox", DataTable)
            # Accept the duplicate with `a`: DM-002 is cancelled, its card removed.
            inbox.move_cursor(row=inbox.get_row_index(dup["id"]))
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            # Reject the cancel with `x`: DM-003 is kept, its card removed.
            inbox.move_cursor(row=inbox.get_row_index(can["id"]))
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()

    asyncio.run(run())
    sched.store.invalidate()
    assert sched.store.task("DM-002").status == Status.CANCELLED
    assert sched.store.task("DM-003").status == Status.DRAFT  # rejected, not cancelled
    assert "decision rejected" in sched.store.task("DM-003").body
    # The TUI resolved on disk through its own scheduler; read it back fresh.
    from garden.scheduler import Scheduler

    assert Scheduler(Store(sched.store.root)).pending_decisions() == []  # both cards resolved


def test_tui_dispatch_refuses_a_draft_task(sched, fake_github):
    """CG-248: the 'd' key must not bypass the approve gate the way it used to -- a draft
    task's placeholder brief stays undispatched, same as the web's hidden button."""
    sched.store.create_task("demo", "p1", "Third task", "## Goal\n\nOld.\n",
                            status="draft", task_id="DM-003")

    async def run():
        app = GardenTUI(Store(sched.store.root))
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.press("i")  # switch to the Tasks tab
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            table.move_cursor(row=table.get_row_index("DM-003"))
            await pilot.pause()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "draft" in app._msg

    asyncio.run(run())
    sched.store.invalidate()
    assert sched.store.task("DM-003").status == Status.DRAFT
    from garden.runs import RunStore

    assert RunStore(sched.store.root / ".garden").runs_for("DM-003") == []


def test_tui_dispatch_refuses_a_run_already_in_flight(sched, fake_github):
    """CG-248: pressing 'd' twice in quick succession must not start a second run."""
    from garden.runner.manual import ManualRunner

    sched.dispatch(sched.store.task("DM-001"), runner=ManualRunner({}), worktree=False)

    async def run():
        app = GardenTUI(Store(sched.store.root))
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.press("i")  # switch to the Tasks tab
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            table.move_cursor(row=table.get_row_index("DM-001"))
            await pilot.pause()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "already has a run in flight" in app._msg

    asyncio.run(run())
    sched.store.invalidate()
    from garden.runs import RunStore

    assert len(RunStore(sched.store.root / ".garden").runs_for("DM-001")) == 1


def test_tui_inbox_decisions_count_excludes_retrying(garden):
    """A retrying (notice) task doesn't inflate the 'need you' figure in the status bar,
    but still shows up (dimmed) in the Inbox tab."""
    (garden / "demo" / "p1" / "tasks" / "DM-001-first.md").write_text(textwrap.dedent("""\
        ---
        id: DM-001
        title: First task
        status: ready
        depends_on: []
        priority: 1
        reading: []
        attempts: 1
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the first thing.

        ## Log

        - 2026-01-01T00:00:00+00:00 attempt 1 failed: max turns reached; will retry
        """))
    (garden / "demo" / "p1" / "tasks" / "DM-002-second.md").write_text(textwrap.dedent("""\
        ---
        id: DM-002
        title: Second task
        status: draft
        depends_on: []
        priority: 2
        reading: []
        created: '2026-01-01T00:00:00+00:00'
        updated: '2026-01-01T00:00:00+00:00'
        ---

        ## Goal

        Do the second thing.
        """))

    async def run():
        app = GardenTUI(Store(garden))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox = app.query_one("#inbox", DataTable)
            assert inbox.row_count == 2
            assert app._inbox_decisions == 1

    asyncio.run(run())


def test_tui_mounts_and_lists_tasks(garden, tmp_path):
    async def run():
        app = GardenTUI(Store(garden))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            assert table.row_count == 2
            await pilot.press("i")  # switch to the tasks tab
            await pilot.pause()
            await pilot.press("a")  # approve is a no-op on a ready task
            await pilot.press("b")
            await pilot.pause()
            assert "tokens" in app._msg
            await pilot.press("f")
            await pilot.pause()
            app.save_screenshot(str(tmp_path / "tui.svg"))
        return tmp_path / "tui.svg"

    out = asyncio.run(run())
    assert out.exists()


def _setup_kickoff(sched):
    phase = sched.store.phase("demo", "p1")
    path = sched.file_kickoff(phase, {"questions": [
        {"question": "Which storage should the phase use?", "context": "We need offline access.",
         "options": ["SQLite", "Files"]},
        {"question": "Should we keep the old format?"},
    ]}, "kickoff-test")
    sched.state.save()
    return path


def test_tui_kickoff_answer_and_dismiss(sched):
    from textual.widgets import Input, Markdown

    from garden.scheduler import Scheduler

    path = _setup_kickoff(sched)

    async def run():
        app = GardenTUI(Store(sched.store.root))
        async with app.run_test(size=(160, 48)) as pilot:
            inbox = app.query_one("#inbox", DataTable)
            inbox.move_cursor(row=inbox.get_row_index("kickoff-test-q0"))
            inbox.focus()
            await pilot.pause()
            assert inbox.get_row("kickoff-test-q0")[-1] == "w answer · x dismiss"
            detail = app.query_one("#detail", Markdown)
            assert "Which storage should the phase use?" in detail._markdown
            assert "We need offline access." in detail._markdown
            assert "SQLite" in detail._markdown
            await pilot.press("a")  # accepting must not resolve a question
            assert "w to answer" in app._msg
            await pilot.press("w")
            box = app.query_one("#answer", Input)
            box.value = "   "
            await pilot.press("enter")
            assert box.has_class("visible")
            assert len(Scheduler(Store(sched.store.root)).pending_decisions()) == 2
            box.value = "abandoned answer"
            await pilot.press("escape")
            assert not box.has_class("visible")
            assert box.value == ""
            assert app._answer_decision is None
            await pilot.press("w")
            app.action_refresh()
            assert app._selected_decision()["decision"] == "kickoff-test-q0"
            # Moving the cursor while composing must not change which card is answered.
            inbox.move_cursor(row=inbox.get_row_index("kickoff-test-q1"))
            box.value = "SQLite"
            await pilot.press("enter")
            await pilot.pause()
            assert "kickoff-test-q0" not in app._inbox_by_key
            assert "kickoff-test-q1" in app._inbox_by_key
            assert "kickoff question answered" in app._msg
            inbox.move_cursor(row=inbox.get_row_index("kickoff-test-q1"))
            await pilot.press("x")
            await pilot.pause()
            assert "kickoff-test-q1" not in app._inbox_by_key
            assert "dismissed" in app._msg

    asyncio.run(run())
    assert Scheduler(Store(sched.store.root)).pending_decisions() == []
    assert "**Which storage should the phase use?** — answered: SQLite" in path.read_text()
    assert "**Should we keep the old format?** — dismissed" in path.read_text()
    assert all(t.status != Status.CANCELLED for t in sched.store.tasks().values())


def test_tui_kickoff_answer_handles_stale_card(sched):
    from textual.widgets import Input

    from garden.scheduler import Scheduler

    path = _setup_kickoff(sched)

    async def run():
        app = GardenTUI(Store(sched.store.root))
        async with app.run_test(size=(160, 48)) as pilot:
            inbox = app.query_one("#inbox", DataTable)
            inbox.move_cursor(row=inbox.get_row_index("kickoff-test-q0"))
            inbox.focus()
            await pilot.press("w")
            Scheduler(Store(sched.store.root)).dismiss_kickoff_question("kickoff-test-q0")
            app.action_refresh()
            box = app.query_one("#answer", Input)
            box.value = "Too late"
            await pilot.press("enter")
            await pilot.pause()
            assert "answer failed:" in app._msg
            assert "kickoff-test-q1" in app._inbox_by_key

    asyncio.run(run())
    assert "Too late" not in path.read_text()
    assert len(Scheduler(Store(sched.store.root)).pending_decisions()) == 1
