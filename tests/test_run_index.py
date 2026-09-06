from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from garden.runs import RunStore


def _finished(rs: RunStore, task: str, run_id: str, cost: float = 1.0):
    run = rs.new_run(task, "local", run_id=run_id)
    run.status = "done"
    run.finished_at = "2026-01-01T00:00:00+00:00"
    run.cost_usd = cost
    run.save()
    return run


def test_shared_index_coalesces_concurrent_history_reads(tmp_path: Path):
    rs = RunStore(tmp_path)
    for n in range(200):
        _finished(rs, f"CG-{n % 10:03d}", f"20260101T000{n:03d}Z-work")

    before = rs.scan_count
    with ThreadPoolExecutor(max_workers=12) as pool:
        sizes = list(pool.map(lambda _: len(RunStore(tmp_path).all_runs()), range(24)))

    assert sizes == [200] * 24
    assert rs.scan_count - before == 1


def test_run_save_invalidates_index_and_results_are_isolated(tmp_path: Path):
    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work")
    first = rs.all_runs()
    first[0].cost_usd = 99
    assert rs.totals()["cost_usd"] == 1.0

    run.cost_usd = 2.5
    run.save()
    assert rs.totals()["cost_usd"] == 2.5


def test_archive_round_trip_preserves_summary_and_artifacts(tmp_path: Path):
    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work", 3.25)
    (run.path / "final.md").write_text("review evidence")

    moved = rs.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC))

    assert moved == 1
    archived = rs.all_runs()[0]
    assert archived.run_id == run.run_id
    assert archived.cost_usd == 3.25
    assert (archived.path / "final.md").read_text() == "review evidence"
    assert rs.totals()["cost_usd"] == 3.25
    assert rs.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC)) == 0

    assert rs.restore_archived("CG-001", run.run_id)
    restored = rs.all_runs()[0]
    assert restored.path.parent.parent == rs.dir
    assert (restored.path / "final.md").read_text() == "review evidence"
    assert rs.totals()["runs"] == 1


def test_archive_retains_active_and_recovery_referenced_runs(tmp_path: Path):
    rs = RunStore(tmp_path)
    protected = _finished(rs, "CG-001", "20260101T000000Z-work")
    active = rs.new_run("CG-002", "local", run_id="20260101T000001Z-work")

    assert rs.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC), {protected.run_id}) == 0
    assert protected.path.exists()
    assert active.path.exists()


def test_archive_health_reports_missing_or_corrupt_index(tmp_path: Path):
    rs = RunStore(tmp_path)
    rs.archive_dir.mkdir()
    assert "missing" in rs.archive_health()
    (rs.archive_dir / "index.json").write_text("not json")
    assert "unreadable" in rs.archive_health()
