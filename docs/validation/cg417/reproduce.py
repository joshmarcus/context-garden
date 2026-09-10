"""Measure CG-417's task snapshot against its immediate predecessor.

The two revisions use the same disposable task tree.  The predecessor is exported with
``git archive`` so the comparison never reads or changes another working checkout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[3]
BASELINE_REVISION = "f5767e103caa98fc3c7a2ba50e983504bce1abe3"


def write_fixture(root: Path, count: int) -> None:
    tasks = root / "demo" / "phase" / "tasks"
    tasks.mkdir(parents=True)
    (root / "demo" / "product.md").write_text("# Disposable profile\n")
    (tasks.parent / "goals.md").write_text("# Phase\n")
    (root / "garden.yaml").write_text(
        "products:\n  demo:\n    repo: .\n    base_branch: main\n    id_prefix: DM\n"
    )
    for number in range(count):
        (tasks / f"DM-{number:04d}.md").write_text(
            "---\n"
            f"id: DM-{number:04d}\ntitle: Retained task {number}\nstatus: draft\n"
            "depends_on: []\npriority: 2\nreading: []\n"
            "created: '2026-01-01T00:00:00+00:00'\nupdated: '2026-01-01T00:00:00+00:00'\n"
            "---\n\n## Goal\n\nProfile discovery.\n"
        )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def served(source: Path, fixture: Path, output: Path, samples: int) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    port = free_port()
    spans = output / "served.jsonl"
    server_log = (output / "server.log").open("w")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(source / "src"), str(Path(__file__).parent))),
           "CG417_GARDEN": str(fixture), "CG417_PROFILE_LOG": str(spans), "PYTHONDONTWRITEBYTECODE": "1"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "serve_profiled:app", "--host", "127.0.0.1",
                               "--port", str(port), "--workers", "1"], cwd=Path(__file__).parent, env=env,
                              stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"server did not start; see {server_log.name}")
                time.sleep(.1)
        for _ in range(samples):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/board", timeout=20) as response:
                assert response.status == 200
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=10)
        server_log.close()
    rows = [json.loads(line) for line in spans.read_text().splitlines() if json.loads(line)["path"] == "/board"]
    return {"samples": len(rows), "scan_counts": [row["store_scans"] for row in rows],
            "latency_p50_s": percentile([row["elapsed_s"] for row in rows], .5),
            "latency_p95_s": percentile([row["elapsed_s"] for row in rows], .95),
            "latency_max_s": max(row["elapsed_s"] for row in rows)}


def tick(source: Path, fixture: Path, samples: int) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(source / "src"), str(Path(__file__).parent))),
           "CG417_GARDEN": str(fixture), "PYTHONDONTWRITEBYTECODE": "1"}
    rows = [json.loads(subprocess.check_output([sys.executable, "profile_tick.py"], cwd=Path(__file__).parent,
                                                env=env, text=True)) for _ in range(samples)]
    return {"samples": len(rows), "scan_counts": [row["store_scans"] for row in rows],
            "latency_p50_s": percentile([row["elapsed_s"] for row in rows], .5),
            "latency_p95_s": percentile([row["elapsed_s"] for row in rows], .95),
            "latency_max_s": max(row["elapsed_s"] for row in rows)}


def archive_parent(destination: Path) -> None:
    archive = subprocess.check_output(["git", "archive", BASELINE_REVISION], cwd=ROOT)
    subprocess.run(["tar", "-x"], cwd=destination, input=archive, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cg417-before-") as temporary:
        before = Path(temporary)
        archive_parent(before)
        report: dict[str, Any] = {"before_revision": BASELINE_REVISION,
                                  "after_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                                  "samples": args.samples, "fixtures": {}}
        for count in (100, 1000):
            fixture = args.output / f"garden-{count}"
            write_fixture(fixture, count)
            report["fixtures"][str(count)] = {
                "before": {"served": served(before, fixture, args.output / f"before-{count}", args.samples),
                           "no_dispatch_tick": tick(before, fixture, args.samples)},
                "after": {"served": served(ROOT, fixture, args.output / f"after-{count}", args.samples),
                          "no_dispatch_tick": tick(ROOT, fixture, args.samples)},
            }
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
