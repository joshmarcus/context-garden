from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

import pytest

from garden.runs import HistoryUnavailable, Run, RunMutationConflict, RunStore


def _finished(rs: RunStore, task: str, run_id: str, cost: float = 1.0):
    run = rs.new_run(task, "local", run_id=run_id)
    run.status = "done"
    run.finished_at = "2026-01-01T00:00:00+00:00"
    run.cost_usd = cost
    run.save()
    return run


def test_shared_index_coalesces_concurrent_history_reads(tmp_path: Path, monkeypatch):
    rs = RunStore(tmp_path)
    for n in range(200):
        _finished(rs, f"CG-{n % 10:03d}", f"20260101T000{n:03d}Z-work")

    clock = [100.0]
    monkeypatch.setattr("garden.runs.time.monotonic", lambda: clock[0])
    before = rs.scan_count
    with ThreadPoolExecutor(max_workers=12) as pool:
        sizes = list(pool.map(lambda _: len(RunStore(tmp_path).all_runs()), range(24)))

    assert sizes == [200] * 24
    assert rs.scan_count - before == 1

    reads = rs.read_count
    clock[0] += rs.MAX_INDEX_AGE_SECONDS + 0.05
    assert len(rs.all_runs()) == 200
    assert rs.read_count == reads, "cache expiry must not re-read unchanged run records"


def test_run_save_invalidates_index_and_results_are_isolated(tmp_path: Path):
    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work")
    first = rs.all_runs()
    first[0].cost_usd = 99
    assert rs.totals()["cost_usd"] == 1.0

    run.cost_usd = 2.5
    run.save()
    assert rs.totals()["cost_usd"] == 2.5


def test_stale_scheduler_save_preserves_authenticated_worker_completion(tmp_path: Path):
    rs = RunStore(tmp_path)
    run = rs.new_run("CG-001", "remote", run_id="20260101T000000Z-work")
    run.host = "worker-1"
    run.lease_token = "generation-one"
    run.pushed_ref = "refs/heads/garden-worker/run/generation-one"
    run.claim_history = [{"host": run.host, "lease_token_sha256": "hash-one",
                          "pushed_ref": run.pushed_ref}]
    run.save()
    stale = Run.load(run.path)

    script = """
import json
import sys
from pathlib import Path
from garden.runs import Run

run = Run.load(Path(sys.argv[1]))
run.pushed_head = "abc123"
run.final_received_at = "2026-01-01T00:01:00+00:00"
(run.path / "remote_result.json").write_text(
    json.dumps({"result": {"status": "done"}, "usage": {"input_tokens": 7}})
)
run.save()
(run.path / "exit_code").write_text("0")
"""
    subprocess.run([sys.executable, "-c", script, str(run.path)], check=True)

    stale.status = "failed"
    stale.error = "no commits pushed"
    stale.save()

    saved = Run.load(run.path)
    assert saved.pushed_head == "abc123"
    assert saved.final_received_at == "2026-01-01T00:01:00+00:00"
    assert json.loads((saved.path / "remote_result.json").read_text())["usage"] == {
        "input_tokens": 7
    }


def test_obsolete_finish_cannot_cross_reclaimed_lease_generation(tmp_path: Path):
    rs = RunStore(tmp_path)
    run = rs.new_run("CG-001", "remote", run_id="20260101T000000Z-work")
    run.lease_token = "generation-one"
    run.claim_history = [{"lease_token_sha256": "hash-one"}]
    run.save()
    obsolete = Run.load(run.path)

    current = Run.load(run.path)
    current.lease_token = "generation-two"
    current.claim_history.append({"lease_token_sha256": "hash-two"})
    current.save()
    obsolete.final_received_at = "2026-01-01T00:01:00+00:00"

    with pytest.raises(RunMutationConflict):
        obsolete.save()


def test_three_way_save_preserves_concurrent_scheduler_lifecycle_state(tmp_path: Path):
    run = RunStore(tmp_path).new_run("CG-001", "remote", run_id="lifecycle-race")
    stale = Run.load(run.path)
    completed = Run.load(run.path)
    completed.status = "done"
    completed.finished_at = "2026-01-01T00:02:00+00:00"
    completed.result = {"status": "done", "summary": "original"}
    completed.usage = {"input_tokens": 17}
    completed.cost_usd = 0.25
    completed.completion_attempts = [{"status": "accepted"}]
    completed.recovery_artifacts = [{"name": "saved-work", "sha": "abc123"}]
    completed.save()

    stale.diff_stat = "one file changed"
    stale.save()

    saved = Run.load(run.path)
    assert saved.status == "done"
    assert saved.finished_at == completed.finished_at
    assert saved.result == completed.result
    assert saved.usage == completed.usage
    assert saved.cost_usd == completed.cost_usd
    assert saved.completion_attempts == completed.completion_attempts
    assert saved.recovery_artifacts == completed.recovery_artifacts
    assert saved.diff_stat == "one file changed"


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
    with pytest.raises(HistoryUnavailable, match="unreadable"):
        rs.totals()


def test_archive_rebuild_refuses_to_hide_a_corrupt_record(tmp_path: Path):
    rs = RunStore(tmp_path)
    bad = rs.archive_dir / "CG-001" / "bad" / "run.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("not json")
    try:
        rs.rebuild_archive_index()
    except ValueError as exc:
        assert "unreadable run record" in str(exc)
    else:
        raise AssertionError("corrupt archived history was silently omitted")
    assert not (rs.archive_dir / "index.json").exists()


def test_archived_cost_backfill_updates_manifest_and_fresh_store(tmp_path: Path):
    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work", 1.0)
    run.harness = "codex"
    run.save()
    (run.path / "stdout.json").write_text("usage")
    assert rs.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC)) == 1

    class Harness:
        def parse(self, *_args, **_kwargs):
            return {"usage": {"input_tokens": 5}, "cost_usd": 4.5, "model": "fixed"}

    class Config:
        def harness(self, _name):
            return Harness()

    assert rs.backfill_codex_costs(Config()) == 1
    manifest = json.loads((rs.archive_dir / "index.json").read_text())
    assert manifest["runs"][0]["cost_usd"] == 4.5
    assert RunStore(tmp_path).totals()["cost_usd"] == 4.5


@pytest.mark.parametrize("expire", [False, True])
def test_archive_mutations_survive_colliding_fingerprints(tmp_path, monkeypatch, expire):
    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work", 3.25)
    (run.path / "final.md").write_text("review evidence")
    fingerprints = rs._task_fingerprints()
    monkeypatch.setattr(RunStore, "_task_fingerprints", lambda _self: fingerprints)
    monkeypatch.setattr(RunStore, "_archive_fingerprint", lambda _self: (0, 0, 0))
    if expire:
        monkeypatch.setattr(RunStore, "MAX_INDEX_AGE_SECONDS", -1)

    def check(root, cost):
        for reader in (rs, RunStore(tmp_path)):
            records = reader.all_runs()
            assert len(records) == 1
            record = records[0]
            assert record.path == root / run.task_id / run.run_id
            assert record.cost_usd == cost
            assert (record.path / "final.md").read_text() == "review evidence"
            assert reader.totals()["runs"] == 1
            assert reader.totals()["cost_usd"] == cost
            assert reader.costs_by_task() == {run.task_id: cost}

    check(rs.dir, 3.25)
    assert rs.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC)) == 1
    check(rs.archive_dir, 3.25)
    archived = rs.all_runs()[0]
    archived.cost_usd = 4.25  # Same serialized length as the old cost.
    rs.update_archived(archived)
    check(rs.archive_dir, 4.25)
    assert rs.restore_archived(run.task_id, run.run_id)
    check(rs.dir, 4.25)
    assert not rs.restore_archived(run.task_id, run.run_id)
    reads = rs.read_count
    check(rs.dir, 4.25)
    assert rs.read_count == reads, "unchanged history must remain cached"


def test_external_archive_changes_refresh_unchanged_live_bucket(tmp_path, monkeypatch):
    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work", 3.25)
    (run.path / "final.md").write_text("review evidence")
    fingerprints = rs._task_fingerprints()
    monkeypatch.setattr(rs, "_task_fingerprints", lambda: fingerprints)
    monkeypatch.setattr(rs, "MAX_INDEX_AGE_SECONDS", -1)
    assert rs.totals()["runs"] == 1

    def external(operation):
        subprocess.run([sys.executable, "-c", """
import datetime as dt
import sys
from pathlib import Path
from garden.runs import RunStore
rs = RunStore(Path(sys.argv[1]))
if sys.argv[2] == "archive":
    assert rs.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC)) == 1
else:
    assert rs.restore_archived("CG-001", "20260101T000000Z-work")
assert rs.totals()["runs"] == 1
assert rs.totals()["cost_usd"] == 3.25
""", str(tmp_path), operation], check=True, timeout=10)

    external("archive")
    assert rs.totals()["runs"] == 1
    assert rs.all_runs()[0].path == rs.archive_dir / run.task_id / run.run_id
    external("restore")
    records = rs.all_runs()
    assert len(records) == 1
    assert records[0].path == run.path
    assert (records[0].path / "final.md").read_text() == "review evidence"
    assert rs.totals()["cost_usd"] == 3.25


def test_history_refresh_waits_for_complete_archive_move(tmp_path, monkeypatch):
    import garden.runs as runs_module

    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work", 3.25)
    assert rs.totals()["runs"] == 1
    monkeypatch.setattr(RunStore, "MAX_INDEX_AGE_SECONDS", -1)
    moved = threading.Event()
    release = threading.Event()
    replace = runs_module.os.replace

    def pause_after_move(source, target):
        replace(source, target)
        if Path(source) == run.path:
            moved.set()
            assert release.wait(5)

    monkeypatch.setattr(runs_module.os, "replace", pause_after_move)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(rs.archive_terminal, dt.datetime(2026, 2, 1, tzinfo=dt.UTC))
        try:
            assert moved.wait(5)
            reader = pool.submit(RunStore(tmp_path).totals)
            with pytest.raises(TimeoutError):
                reader.result(timeout=0.1)
        finally:
            release.set()
        assert writer.result(timeout=5) == 1
        assert reader.result(timeout=5)["cost_usd"] == 3.25


def test_concurrent_archivers_move_each_record_once(tmp_path):
    rs = RunStore(tmp_path)
    for n in range(8):
        _finished(rs, "CG-001", f"run-{n}", 3.25)
    start = threading.Barrier(2)

    def archive():
        start.wait(timeout=5)
        return RunStore(tmp_path).archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: archive(), range(2)))
    assert sorted(results) == [0, 8]
    assert rs.totals()["runs"] == 8
    assert rs.totals()["cost_usd"] == 26


def test_stale_archived_update_cannot_recreate_a_restored_run(tmp_path):
    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work", 3.25)
    rs.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC))
    archived = rs.all_runs()[0]
    assert rs.restore_archived(run.task_id, run.run_id)
    archived.cost_usd = 9.25
    with pytest.raises(FileNotFoundError, match="reload"):
        rs.update_archived(archived)
    assert not archived.path.exists()
    assert rs.totals()["runs"] == 1
    assert rs.totals()["cost_usd"] == 3.25


def test_external_archive_move_is_atomic_to_refresh(tmp_path, monkeypatch):
    rs = RunStore(tmp_path)
    run = _finished(rs, "CG-001", "20260101T000000Z-work", 3.25)
    assert rs.totals()["runs"] == 1
    monkeypatch.setattr(rs, "MAX_INDEX_AGE_SECONDS", -1)
    moved, release = tmp_path / "moved", tmp_path / "release"
    script = """
import datetime as dt
import os
import sys
import time
from pathlib import Path
from garden.runs import RunStore
root = Path(sys.argv[1])
rs = RunStore(root)
replace = os.replace
def pause_after_move(source, target):
    replace(source, target)
    if Path(source) == rs.dir / "CG-001" / "20260101T000000Z-work":
        (root / "moved").touch()
        deadline = time.monotonic() + 5
        while not (root / "release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("reader did not release writer")
            time.sleep(0.01)
os.replace = pause_after_move
assert rs.archive_terminal(dt.datetime(2026, 2, 1, tzinfo=dt.UTC)) == 1
"""
    writer = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            deadline = time.monotonic() + 5
            while not moved.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert moved.exists(), "external writer did not move the run"
            reader = pool.submit(rs.totals)
            with pytest.raises(TimeoutError):
                reader.result(timeout=0.1)
        finally:
            release.touch()
            try:
                stdout, stderr = writer.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                writer.kill()
                writer.communicate()
                raise
        assert writer.returncode == 0, stdout + stderr
        assert reader.result(timeout=5)["cost_usd"] == 3.25
    assert rs.all_runs()[0].path == rs.archive_dir / run.task_id / run.run_id
