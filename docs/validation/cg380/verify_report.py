"""Check CG-380's generated README tables against its raw samples."""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def latency_table(pages: dict[str, object]) -> str:
    rows = ["| tasks | slots | Now 1 | Inbox | Config |", "| ---: | ---: | ---: | ---: | ---: |"]
    for tasks in (100, 1000):
        for slots in (0, 1, 4):
            case = pages[f"tasks-{tasks}:slots-{slots}"]
            cells = []
            for path in ("/now1", "/inbox", "/config"):
                values = [row["elapsed_s"] for row in case["rows"] if row["path"] == path]
                cell = f"{percentile(values, .5):.3f}/{percentile(values, .95):.3f}/{max(values):.3f}s"
                cells.append(cell.replace("0.", "."))
            rows.append(f"| {tasks:,} | {slots} | {' | '.join(cells)} |")
    return "\n".join(rows)


def cold_warm_table(pages: dict[str, object]) -> str:
    rows = [
        "| tasks | page | cold (n/p50/p95/max) | warm (n/p50/p95/max) |",
        "| ---: | --- | ---: | ---: |",
    ]
    for tasks in (100, 1000):
        case = pages[f"tasks-{tasks}:slots-0"]
        for path, label in (("/now1", "Now 1"), ("/inbox", "Inbox"), ("/config", "Config")):
            samples = [row for row in case["rows"] if row["path"] == path]
            cold = [row["elapsed_s"] for index, row in enumerate(samples) if index % 7 == 0]
            warm = [row["elapsed_s"] for index, row in enumerate(samples) if index % 7 != 0]

            def cell(values: list[float]) -> str:
                return (
                    f"{len(values)}/{percentile(values, .5):.3f}/"
                    f"{percentile(values, .95):.3f}/{max(values):.3f}s"
                ).replace("0.", ".")

            rows.append(f"| {tasks:,} | {label} | {cell(cold)} | {cell(warm)} |")
    return "\n".join(rows)


def tick_table(pages: dict[str, object]) -> str:
    rows = [
        "| tasks | slots | tick wall min/p50/max | tick CPU p50 | controller-other wall/CPU p50 | scan wall p50 | reap wall p50 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    median = statistics.median
    for tasks in (100, 1000):
        for slots in (0, 1, 4):
            ticks = pages[f"tasks-{tasks}:slots-{slots}"]["background_ticks"]
            wall = [tick["total_wall_s"] for tick in ticks]
            values = (
                min(wall), median(wall), max(wall),
                median(tick["total_process_cpu_s"] for tick in ticks),
                median(tick["controller_other_wall_s"] for tick in ticks),
                median(tick["controller_other_process_cpu_s"] for tick in ticks),
                median(tick["controller_other_counters"]["task_product_scan_wall_s"] for tick in ticks),
                median(tick["phases"]["reap"]["wall_s"] for tick in ticks),
            )
            cells = (
                f"{values[0]:.3f}/{values[1]:.3f}/{values[2]:.3f}s | {values[3]:.3f}s | "
                f"{values[4]:.3f}/{values[5]:.3f}s | {values[6]:.3f}s | {values[7]:.3f}s"
            ).replace("0.", ".")
            rows.append(f"| {tasks:,} | {slots} | {cells} |")
    return "\n".join(rows)


def flat_cgroup_values(value: str) -> dict[str, int]:
    result = {}
    for line in value.splitlines():
        parts = line.split()
        if parts[0] in ("some", "full"):
            result[parts[0]] = int(next(p for p in parts if p.startswith("total=")).split("=")[1])
        elif len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def delta(report: dict[str, object], group: str, field: str) -> int:
    before = flat_cgroup_values(report["caps_and_initial_cgroup"][group])
    after = flat_cgroup_values(report["final_cgroup"][group])
    return after[field] - before[field]


def verify_resource_and_workload_summary(report: dict[str, object], readme: str) -> None:
    cpu = delta(report, "cpu.stat", "usage_usec") / 1_000_000
    elapsed = (
        datetime.fromisoformat(report["finished_at"].replace("Z", "+00:00"))
        - datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
    ).total_seconds()
    peak_mib = int(report["final_cgroup"]["memory.peak"]) / 1024**2
    assert f"used {cpu:.3f} CPU-seconds in {elapsed:.3f} seconds, peaked at {peak_mib:.1f} MiB" in readme

    commands = report["commands"]

    def cpu_for(name: str) -> float:
        return commands[name]["descendant_user_cpu_s"] + commands[name]["descendant_system_cpu_s"]

    expected = (
        f"One/four CPU-active replays used {cpu_for('session:1'):.3f}/{cpu_for('session:4'):.3f} "
        f"descendant CPU-seconds over {commands['session:1']['elapsed_s']:.3f}/"
        f"{commands['session:4']['elapsed_s']:.3f}s"
    )
    assert expected in readme


def main() -> None:
    report = json.loads((HERE / "report.json").read_text())
    readme = (HERE / "README.md").read_text()
    assert report["history_sizes"] == [100, 1000]
    assert latency_table(report["pages"]) in readme
    assert cold_warm_table(report["pages"]) in readme
    assert tick_table(report["pages"]) in readme
    verify_resource_and_workload_summary(report, readme)
    assert report["interaction"]["source_head"] == report["build"]
    assert report["interaction"]["actions"] and report["interaction"]["observations"]
    for observation in report["interaction"]["observations"].values():
        counts = observation["read_scan_counts"]
        assert counts
        assert all(isinstance(value, (int, float)) and value >= 0 for value in counts.values())
    print("CG-380 README summary matches report.json")


if __name__ == "__main__":
    main()
