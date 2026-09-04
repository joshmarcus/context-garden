import asyncio

from textual.widgets import DataTable

from garden.model import Status
from garden.store import Store
from garden.tui.app import GardenTUI
from tests.conftest import wait_for_runs


def _setup_decisions(sched, monkeypatch):
    """Reap DM-001 in discover-kinds mode so a duplicate (DM-002) and a cancel (DM-003)
    decision are pending on disk, ready for the TUI to act on."""
    sched.store.create_task("demo", "p1", "Third task", "## Goal\n\nOld.\n",
                            status="draft", task_id="DM-003")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "discover-kinds")
    sched.tick()
    wait_for_runs(sched)
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
