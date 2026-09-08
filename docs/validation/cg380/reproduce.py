"""Bounded controller/worker attribution using retained-history fixtures, never production."""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def cgroup_path() -> Path:
    relative = next(
        x.split("::", 1)[1]
        for x in Path("/proc/self/cgroup").read_text().splitlines()
        if x.startswith("0::")
    )
    return Path("/sys/fs/cgroup") / relative.lstrip("/")


def pressure() -> dict[str, Any]:
    group = cgroup_path()
    result: dict[str, Any] = {
        "path": str(group),
        "self_pid": os.getpid(),
        "self_cgroup": Path("/proc/self/cgroup").read_text().strip(),
    }
    for name in (
        "cpu.max",
        "cpu.stat",
        "memory.current",
        "memory.peak",
        "memory.high",
        "memory.max",
        "memory.events",
        "memory.stat",
        "cpu.pressure",
        "io.pressure",
        "memory.pressure",
        "pids.max",
    ):
        path = group / name
        if path.exists():
            result[name] = path.read_text().strip()
    stat = os.statvfs(os.environ.get("TMPDIR", "/tmp"))
    result["temp_free_bytes"] = stat.f_bavail * stat.f_frsize
    return result


def make_garden(
    root: Path, *, tasks_count: int, runs: int = 1549, events: int = 12500
) -> None:
    tasks = root / "demo" / "phase-05" / "tasks"
    tasks.mkdir(parents=True)
    (root / "demo" / "product.md").write_text("# Demo\n")
    (tasks.parent / "goals.md").write_text("# Phase 05\n")
    (root / "principles").mkdir()
    (root / "principles" / "00-index.md").write_text("# Principles\n")
    (root / "garden.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "CG-380 disposable profile",
                "max_parallel": 4,
                "resources": {"max_parallel": 4},
                "review": {"enabled": False},
                "products": {"demo": {"repo": str(root), "base_branch": "main", "id_prefix": "DM"}},
            }
        )
    )
    for number in range(tasks_count):
        (tasks / f"DM-{number:03d}-task.md").write_text(
            "---\n"
            + yaml.safe_dump(
                {
                    "id": f"DM-{number:03d}",
                    "title": f"Task {number}",
                    "status": "draft",
                    "depends_on": [],
                    "priority": number,
                    "reading": [],
                    "created": "2026-01-01T00:00:00+00:00",
                    "updated": "2026-01-01T00:00:00+00:00",
                }
            )
            + "---\n\n## Goal\n\nWork.\n"
        )
    for number in range(runs):
        task = f"DM-{number % tasks_count:03d}"
        directory = root / ".garden" / "runs" / task / f"20260101T{number:06d}Z-work"
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(
            json.dumps(
                {
                    "task_id": task,
                    "run_id": directory.name,
                    "dir": str(directory),
                    "runner": "local",
                    "mode": "work",
                    "status": "done",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:01:00+00:00",
                }
            )
        )
    log = root / ".garden" / "events.jsonl"
    log.parent.mkdir(exist_ok=True)
    with log.open("w") as stream:
        for number in range(events):
            stream.write(
                json.dumps(
                    {
                        "at": f"2026-09-07T03:{number % 60:02d}:{number % 60:02d}+00:00",
                        "kind": "run_finished" if number % 5 == 0 else "transition",
                        "task": f"DM-{number % tasks_count:03d}",
                        "mode": "work",
                        "status": "done",
                    }
                )
                + "\n"
            )


def fixture(kind: str) -> None:
    memory = bytearray(32 * 1024 * 1024)
    duration = float(os.environ.get("CG380_FIXTURE_SECONDS", "6"))
    if kind == "wait":
        time.sleep(duration)
        return
    stop = time.monotonic() + duration
    value = 1
    while time.monotonic() < stop:
        for number in range(150_000):
            value = (value * 33 + number) & 0xFFFFFFFF
        memory[value % len(memory)] = value & 0xFF
        if kind == "session":
            time.sleep(0.02)


def run_children(command: list[str], count: int) -> dict[str, Any]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    c_before = pressure()
    started = time.perf_counter()
    children = [
        subprocess.Popen(
            command, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for _ in range(count)
    ]
    identities = [
        {"pid": child.pid, "cgroup": Path(f"/proc/{child.pid}/cgroup").read_text().strip()}
        for child in children
    ]
    codes = [child.wait(timeout=60) for child in children]
    elapsed = time.perf_counter() - started
    c_after = pressure()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "command": command,
        "count": count,
        "elapsed_s": elapsed,
        "exit_codes": codes,
        "descendant_user_cpu_s": after.ru_utime - before.ru_utime,
        "descendant_system_cpu_s": after.ru_stime - before.ru_stime,
        "descendant_maxrss_kib": after.ru_maxrss,
        "identities": identities,
        "cgroup_before": c_before,
        "cgroup_after": c_after,
    }


def page_case(
    source: Path,
    garden: Path,
    output: Path,
    slots: int,
    samples: int,
    *,
    task_count: int,
    profiled: bool = True,
) -> dict[str, Any]:
    log = output / "spans.jsonl"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join((str(source / "src"), str(Path(__file__).parent))),
            "CG380_GARDEN": str(garden),
            "CG380_PROFILE_LOG": str(log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    server_log = (output / "server.log").open("w")
    module = "serve_profiled:app" if profiled else "serve_plain:app"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            "18780",
            "--workers",
            "1",
        ],
        cwd=Path(__file__).parent,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    load_env = {**os.environ, "CG380_FIXTURE_SECONDS": "120"}
    loads = [
        subprocess.Popen(
            [sys.executable, __file__, "--fixture", "session"],
            env=load_env,
            start_new_session=True,
        )
        for _ in range(slots)
    ]
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:18780/api/tasks", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        rows = []
        tick_rows = []
        for period in ("expiry_1", "expiry_2", "expiry_3"):
            time.sleep(1.1)
            for path in ("/now1", "/inbox", "/config"):
                for _ in range(samples):
                    start = time.perf_counter()
                    urllib.request.urlopen(f"http://127.0.0.1:18780{path}", timeout=10).read()
                    rows.append(
                        {"period": period, "path": path, "elapsed_s": time.perf_counter() - start}
                    )
            tick = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parent / "profile_tick.py"),
                ],
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            tick_rows.append(json.loads(tick.stdout))
        spans = (
            [
                json.loads(line)
                for line in log.read_text().splitlines()
                if '"path": "/api/tasks"' not in line
            ]
            if profiled
            else []
        )
        summary = {}
        for period in ("expiry_1", "expiry_2", "expiry_3"):
            for path in ("/now1", "/inbox", "/config"):
                vals = [r["elapsed_s"] for r in rows if r["period"] == period and r["path"] == path]
                summary[f"{period}:{path}"] = {
                    "n": len(vals),
                    "p50_s": percentile(vals, 0.5),
                    "p95_s": percentile(vals, 0.95),
                    "max_s": max(vals),
                }
        return {
            "task_count": task_count,
            "slots": slots,
            "server_pid": server.pid,
            "server_cgroup": Path(f"/proc/{server.pid}/cgroup").read_text().strip(),
            "profiled": profiled,
            "load_pids": [p.pid for p in loads],
            "load_identities": [
                {"pid": p.pid, "cgroup": Path(f"/proc/{p.pid}/cgroup").read_text().strip()}
                for p in loads
            ],
            "rows": rows,
            "summary": summary,
            "background_ticks": tick_rows,
            "spans": spans,
        }
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=10)
        server_log.close()
        for child in loads:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--fixture", choices=("session", "wait", "validation"))
    args = parser.parse_args()
    if args.fixture:
        fixture(args.fixture)
        return
    if not args.source or not args.output:
        parser.error("--source and --output required")
    args.output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "build": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.source, text=True
        ).strip(),
        "python": sys.version,
        "datasets": [],
        "history_sizes": [100, 1000],
        "caps_and_initial_cgroup": pressure(),
        "commands": {},
        "pages": {},
    }
    for kind in ("session", "wait"):
        for count in (1, 4):
            report["commands"][f"{kind}:{count}"] = run_children(
                [sys.executable, __file__, "--fixture", kind], count
            )
    report["commands"]["setup:1"] = run_children(
        [sys.executable, "-m", "compileall", "-q", str(args.source / "src")], 1
    )
    report["commands"]["validation:1"] = run_children(
        [
            str(args.source / ".venv/bin/python"),
            "-m",
            "pytest",
            str(args.source / "tests/scheduler/test_resources.py")
            + "::test_effective_memory_uses_tighter_cgroup_headroom",
            "-q",
        ],
        1,
    )
    for task_count in report["history_sizes"]:
        garden = args.output / f"garden-{task_count}"
        make_garden(garden, tasks_count=task_count)
        report["datasets"].append({"tasks": task_count, "runs": 1549, "events": 12500})
        for slots in (0, 1, 4):
            key = f"tasks-{task_count}:slots-{slots}"
            case = args.output / key
            case.mkdir()
            report["pages"][key] = page_case(
                args.source, garden, case, slots, args.samples, task_count=task_count
            )
    plain = args.output / "tasks-100:slots-0-plain"
    plain.mkdir()
    report["pages"]["tasks-100:slots-0-plain"] = page_case(
        args.source,
        args.output / "garden-100",
        plain,
        0,
        args.samples,
        task_count=100,
        profiled=False,
    )
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["interaction"] = {
        "source_head": report["build"],
        "application": "disposable Uvicorn garden.web app",
        "actions": [
            "served 100-task and 1,000-task retained-history gardens",
            "requested /now1, /inbox, and /config seven times in each of three cache-expiry cycles",
            "repeated each served flow with zero, one, and four bounded model-session replay occupants",
            "ran one no-dispatch background scheduler tick after every cycle",
        ],
        "observations": {
            key: {
                "expiry_3": {
                    path: value
                    for path, value in case["summary"].items()
                    if path.startswith("expiry_3:")
                },
                "read_scan_counts": {
                    name: sum(
                        int(span["counts"].get(name, 0)) for span in case["spans"]
                    )
                    for name in (
                        "task_product_scan_s",
                        "event_read_parse_s",
                        "run_index_s",
                        "process_resource_inspection_s",
                    )
                },
            }
            for key, case in report["pages"].items()
            if "plain" not in key
        },
        "limitations": "Serial local GETs and deterministic replay occupants; no vendor or production traffic.",
    }
    report["final_cgroup"] = pressure()
    report["temp_kib"] = int(
        subprocess.check_output(["du", "-sk", str(args.output)], text=True).split()[0]
    )
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
