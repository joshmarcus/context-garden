import asyncio

from textual.widgets import DataTable

from garden.store import Store
from garden.tui.app import GardenTUI


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
