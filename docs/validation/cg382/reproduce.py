"""Replay the CG-382 Store-scan profile through disposable served applications.

This intentionally uses two short-lived CPU/memory workloads and two fixture sizes.  It
never reads a live garden or alters cache, pressure, or scheduler limits.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def write_fixture(root: Path, history_size: int) -> None:
    tasks = root / "demo" / "phase" / "tasks"
    tasks.mkdir(parents=True)
    (root / "demo" / "product.md").write_text("# Disposable profile\n")
    (tasks.parent / "goals.md").write_text("# Phase\n")
    (root / "garden.yaml").write_text("products:\n  demo:\n    repo: .\n    base_branch: main\n    id_prefix: DM\n")
    for number in range(122):
        (tasks / f"DM-{number:03d}.md").write_text(
            "---\n"
            f"id: DM-{number:03d}\ntitle: Retained task {number}\nstatus: draft\n"
            "depends_on: []\npriority: 2\nreading: []\n"
            "created: '2026-01-01T00:00:00+00:00'\nupdated: '2026-01-01T00:00:00+00:00'\n"
            "---\n\n## Goal\n\nProfile discovery.\n"
        )
    runs = root / ".garden" / "runs"
    for number in range(history_size):
        directory = runs / f"DM-{number % 122:03d}" / f"20260101T{number:06d}Z-work"
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(json.dumps({
            "task_id": f"DM-{number % 122:03d}", "run_id": directory.name, "dir": str(directory),
            "runner": "local", "mode": "work", "status": "done",
            "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:01:00+00:00",
        }))


def workload() -> None:
    memory = bytearray(16 * 1024 * 1024)
    value = 1
    while True:
        for number in range(100_000):
            value = (value * 33 + number) & 0xFFFFFFFF
        memory[value % len(memory)] = value & 0xFF
        time.sleep(0.01)


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def request(url: str) -> tuple[int, float]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            response.read()
            return response.status, time.perf_counter() - started
    except urllib.error.HTTPError as error:
        error.read()
        return error.code, time.perf_counter() - started


def run_case(source: Path, fixture: Path, output: Path, legacy: bool, samples: int) -> dict[str, Any]:
    log = output / "request-spans.jsonl"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(source / "src"), str(Path(__file__).parent))),
           "CG382_GARDEN": str(fixture), "CG382_PROFILE_LOG": str(log), "PYTHONDONTWRITEBYTECODE": "1"}
    if legacy:
        env["CG382_LEGACY_SNAPSHOT"] = "1"
    port = 18382
    server_log = (output / "server.log").open("w")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "serve_profiled:app", "--host", "127.0.0.1",
                               "--port", str(port), "--workers", "1"], env=env, stdout=server_log,
                              stderr=subprocess.STDOUT, start_new_session=True)
    workers = [subprocess.Popen([sys.executable, __file__, "--workload"], start_new_session=True) for _ in range(2)]
    events: list[dict[str, Any]] = []
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                status, elapsed = request(f"http://127.0.0.1:{port}/healthz")
                if status == 200:
                    events.append({"kind": "health", "url": f"http://127.0.0.1:{port}/healthz", "status": status, "elapsed_s": elapsed})
                    break
            except urllib.error.URLError as error:
                if time.monotonic() > deadline:
                    raise RuntimeError("served profile did not start") from error
                time.sleep(0.1)
        for interval in range(3):
            if interval:
                time.sleep(1.05)
            for path, kind in (("/board", "affected"), ("/tasks/NOPE", "empty_or_failure"), ("/board", "recovery")):
                for _ in range(samples):
                    status, elapsed = request(f"http://127.0.0.1:{port}{path}")
                    events.append({"kind": kind, "interval": interval, "url": f"http://127.0.0.1:{port}{path}",
                                   "status": status, "elapsed_s": elapsed})
                    expected = 404 if path == "/tasks/NOPE" else 200
                    if status != expected:
                        raise RuntimeError(f"{path} returned {status}, expected {expected}")
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=10)
        server_log.close()
        for worker in workers:
            os.killpg(worker.pid, signal.SIGTERM)
            worker.wait(timeout=10)
    spans = [json.loads(line) for line in log.read_text().splitlines()]
    board = [row for row in spans if row["path"] == "/board"]
    return {"served_app": "http://127.0.0.1:18382", "executing_processes": 2,
            "cache_expiry_intervals": 3, "samples_per_interval": samples, "events": events,
            "board_latency": {"n": len(board), "p50_s": percentile([r["elapsed_s"] for r in board], .5),
                              "p95_s": percentile([r["elapsed_s"] for r in board], .95),
                              "max_s": max(r["elapsed_s"] for r in board)}, "spans": spans}


def source_at_parent(destination: Path) -> Path:
    archive = subprocess.check_output(["git", "archive", "HEAD^"], cwd=Path(__file__).parents[3])
    subprocess.run(["tar", "-x"], cwd=destination, input=archive, check=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--workload", action="store_true")
    args = parser.parse_args()
    if args.workload:
        workload()
        return
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cg382-before-") as temporary:
        before_source = source_at_parent(Path(temporary))
        after_source = Path(__file__).parents[3]
        report: dict[str, Any] = {"command": f"{sys.executable} docs/validation/cg382/reproduce.py --output {args.output} --samples {args.samples}",
                                  "before_revision": subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip(),
                                  "after_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                                  "fixture_tasks": 122, "history_sizes": [1546, 6000], "cases": {}}
        for label, size in (("representative", 1546), ("larger", 6000)):
            fixture = args.output / f"fixture-{label}"
            write_fixture(fixture, size)
            for revision, source, legacy in (("before", before_source, True), ("after", after_source, False)):
                case_dir = args.output / f"{revision}-{label}"
                case_dir.mkdir()
                report["cases"][f"{revision}:{label}"] = run_case(source, fixture, case_dir, legacy, args.samples)
        (args.output / "manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
