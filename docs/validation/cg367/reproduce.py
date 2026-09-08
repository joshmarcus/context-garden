"""Reproduce CG-367's bounded served-garden before/after request comparison."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml


def make_garden(root: Path, runs: int, events: int) -> None:
    tasks = root / "demo" / "phase-05" / "tasks"
    tasks.mkdir(parents=True)
    (root / "demo" / "product.md").write_text("# Demo\n")
    (tasks.parent / "goals.md").write_text("# Phase 05\n\nKeep the loop dependable.\n")
    (root / "principles").mkdir()
    (root / "principles" / "00-index.md").write_text("# Principles\n\n- Keep work bounded.\n")
    (root / "garden.yaml").write_text(yaml.safe_dump({
        "name": "CG-367 disposable profile",
        "max_parallel": 4,
        "resources": {"max_parallel": 4},
        "review": {"enabled": False},
        "products": {"demo": {"repo": str(root), "base_branch": "main", "id_prefix": "DM"}},
    }))
    for number in range(100):
        status = "running" if number < 4 else "draft"
        (tasks / f"DM-{number:03d}-task.md").write_text(
            "---\n"
            f"id: DM-{number:03d}\ntitle: Representative task {number}\nstatus: {status}\n"
            f"depends_on: []\npriority: {number}\nreading: []\n"
            "created: '2026-01-01T00:00:00+00:00'\nupdated: '2026-01-01T00:00:00+00:00'\n"
            "---\n\n## Goal\n\nDo representative work.\n"
        )
    run_root = root / ".garden" / "runs"
    for number in range(runs):
        task_id = f"DM-{number % 100:03d}"
        run_id = f"20260101T{number:06d}Z-work"
        directory = run_root / task_id / run_id
        directory.mkdir(parents=True)
        record = {
            "task_id": task_id, "run_id": run_id, "dir": str(directory), "runner": "local",
            "mode": "work", "status": "done", "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:01:00+00:00", "cost_usd": 0.01,
        }
        (directory / "run.json").write_text(json.dumps(record))
    event_log = root / ".garden" / "events.jsonl"
    with event_log.open("w") as stream:
        for number in range(events):
            row = {
                "at": f"2026-09-07T03:{number % 60:02d}:{number % 60:02d}+00:00",
                "kind": "run_finished" if number % 5 == 0 else "transition",
                "task": f"DM-{number % 100:03d}", "mode": "work", "status": "done",
                "cost_usd": 0.01,
            }
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def workload() -> None:
    memory = bytearray(32 * 1024 * 1024)
    value = 1
    while True:
        for number in range(250_000):
            value = (value * 33 + number) & 0xFFFFFFFF
        memory[value % len(memory)] = value & 0xFF
        time.sleep(0.02)


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def proc_stats(pid: int) -> dict[str, int]:
    status: dict[str, int] = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(("VmHWM:", "VmRSS:")):
            key, value, _unit = line.split()
            status[key.rstrip(":")] = int(value)
    ticks = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()
    status["cpu_ticks"] = int(ticks[11]) + int(ticks[12])
    return status


def cgroup_files() -> dict[str, str]:
    cgroup = Path("/proc/self/cgroup").read_text().splitlines()[0].split(":")[-1]
    base = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
    names = ("cpu.stat", "cpu.max", "memory.current", "memory.peak", "memory.max",
             "memory.pressure", "io.pressure", "cpu.pressure")
    return {name: (base / name).read_text().strip() for name in names if (base / name).exists()}


def run_case(source: Path, garden: Path, output: Path, slots: int, samples: int) -> dict[str, Any]:
    profile_log = output / "request-spans.jsonl"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": os.pathsep.join((str(source / "src"), str(Path(__file__).resolve().parent))),
        "CG367_GARDEN": str(garden), "CG367_PROFILE_LOG": str(profile_log),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    port = 18767
    server_log = (output / "server.log").open("w")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "serve_profiled:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
        env=env, stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    workers = [subprocess.Popen([sys.executable, __file__, "--workload"], start_new_session=True)
               for _ in range(slots)]
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tasks", timeout=2).read()
                break
            except Exception as exc:
                if time.monotonic() > deadline:
                    raise RuntimeError("profile server did not start") from exc
                time.sleep(0.1)
        before = proc_stats(server.pid)
        pressure_before = cgroup_files()
        rows = []
        for period in ("cold", "warm", "cache_expiry"):
            if period == "cache_expiry":
                time.sleep(1.1)
            for path in ("/now1", "/inbox"):
                for _sample in range(samples):
                    started = time.perf_counter()
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=20) as response:
                        response.read()
                    rows.append({"period": period, "path": path, "elapsed_s": time.perf_counter() - started})
        after = proc_stats(server.pid)
        pressure_after = cgroup_files()
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=10)
        server_log.close()
        for worker in workers:
            os.killpg(worker.pid, signal.SIGTERM)
            worker.wait(timeout=10)
    spans = [json.loads(line) for line in profile_log.read_text().splitlines()]
    summary = {}
    for period in ("cold", "warm", "cache_expiry"):
        for path in ("/now1", "/inbox"):
            values = [row["elapsed_s"] for row in rows if row["period"] == period and row["path"] == path]
            summary[f"{period}:{path}"] = {
                "n": len(values), "p50_s": percentile(values, 0.5),
                "p95_s": percentile(values, 0.95), "max_s": max(values),
            }
    return {
        "slots": slots, "samples_per_period_path": samples, "summary": summary,
        "server_cpu_ticks": after["cpu_ticks"] - before["cpu_ticks"],
        "server_peak_rss_kib": after.get("VmHWM", 0), "server_final_rss_kib": after.get("VmRSS", 0),
        "spans": spans, "cgroup_before": pressure_before, "cgroup_after": pressure_after,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before-source", type=Path)
    parser.add_argument("--after-source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--workload", action="store_true")
    args = parser.parse_args()
    if args.workload:
        workload()
        return
    if not args.before_source or not args.after_source or not args.output:
        parser.error("--before-source, --after-source and --output are required")
    args.output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "before_source": str(args.before_source.resolve()), "after_source": str(args.after_source.resolve()),
        "python": sys.version, "datasets": {}, "limits": cgroup_files(), "cases": {},
    }
    for label, run_count, event_count in (("representative", 1549, 12500), ("larger", 6003, 50000)):
        garden = args.output / f"garden-{label}"
        make_garden(garden, run_count, event_count)
        report["datasets"][label] = {"tasks": 100, "runs": run_count, "events": event_count}
        for revision, source in (("before", args.before_source), ("after", args.after_source)):
            for slots in (1, 4):
                case_dir = args.output / f"{revision}-{label}-{slots}slots"
                case_dir.mkdir()
                report["cases"][f"{revision}:{label}:{slots}"] = run_case(
                    source, garden, case_dir, slots, args.samples
                )
    report["temp_kib"] = int(subprocess.check_output(["du", "-sk", str(args.output)], text=True).split()[0])
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
