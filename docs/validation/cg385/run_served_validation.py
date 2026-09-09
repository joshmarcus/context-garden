"""Record head-bound HTTP responsiveness and reclaim failure/recovery states."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

from garden.runs import Run

HERE = Path(__file__).parent
root = Path(tempfile.mkdtemp(prefix="cg385-served-"))
(root / "principles").mkdir()
(root / "principles" / "00-index.md").write_text("# Principles\n")
(root / "demo" / "phase-01" / "tasks").mkdir(parents=True)
(root / "demo" / "product.md").write_text("# Demo\n")
(root / "demo" / "phase-01" / "goals.md").write_text("# Reliable admission\n")
(root / "garden.yaml").write_text(yaml.safe_dump({"name": "CG-385", "products": {"demo": {}}}))
(root / ".garden").mkdir()
counter = root / "counts.json"
counter.write_text('{"reads": 0, "scans": 0}')


def add_history(start: int, stop: int) -> None:
    for number in range(start, stop):
        directory = root / ".garden" / "runs" / "HISTORY" / f"20260101T{number:06d}Z-work"
        Run(task_id="HISTORY", run_id=directory.name, dir=str(directory), runner="local",
            status="done", started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:01:00+00:00").save()


add_history(0, 120)
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
process = subprocess.Popen([sys.executable, str(HERE / "served_app.py"), str(root), str(counter), str(port)])
load_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
url = f"http://127.0.0.1:{port}"
deadline = time.monotonic() + 10
while True:
    try:
        urllib.request.urlopen(url + "/healthz", timeout=1).read()
        break
    except Exception:
        if time.monotonic() >= deadline:
            process.terminate()
            raise
        time.sleep(0.05)

latencies: list[float] = []
events: list[dict[str, object]] = []
state_path = root / ".garden" / "resource-reclaim.json"


def request(state: str, outcome: str, path: str = "/config", expected: str = "") -> None:
    started = time.monotonic()
    with urllib.request.urlopen(url + path, timeout=3) as response:
        body = response.read().decode()
        assert not expected or expected in body
        elapsed = time.monotonic() - started
        latencies.append(elapsed)
        events.append({"kind": "http_request", "state": state, "outcome": outcome,
                       "method": "GET", "url": url + path, "status_code": response.status,
                       "observed": expected or "HTTP response rendered"})


try:
    request("empty", "empty", "/inbox", "Nothing needs a person right now")
    for cycle in range(3):
        state_path.write_text(json.dumps({"running": False, "token": f"failure-{cycle}",
                                          "result": {"status": "error", "error": "delegation unavailable"}}))
        request("failure", "failure", expected="delegation unavailable")
        state_path.write_text(json.dumps({"running": False, "token": f"recovery-{cycle}",
                                          "result": {"status": "complete",
                                                     "headroom_before_bytes": 220 << 20,
                                                     "headroom_after_bytes": 480 << 20}}))
        request("recovery", "success", expected="last bounded cache reclaim complete")
    add_history(120, 1200)
    request("affected", "success", "/now1", "Now")
finally:
    process.terminate()
    process.wait(timeout=5)
    load_process.terminate()
    load_process.wait(timeout=5)

head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
states = {
    "affected": {"status": "pass", "actions": ["GET /now1 with 1,200 retained runs"],
                 "observed": "served response succeeded under the larger retained history"},
    "empty": {"status": "pass", "actions": ["GET /inbox with no actionable task"],
              "observed": "empty inbox response succeeded"},
    "failure_recovery": {"status": "pass", "actions": ["GET /config across three failure/recovery cycles"],
                         "observed": "each failure diagnostic was followed by a successful recovery diagnostic"},
}
report = {"head": head, "environment": "disposable", "states": states, "events": events,
          "scalability": {"served_app": url, "history_sizes": [120, 1200],
                          "cache_expiry_intervals": 3, "executing_processes": 2,
                          "latencies": latencies, "read_scan_counts": json.loads(counter.read_text()),
                          "load_kind": "controlled"}}
(HERE / "served-interaction-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
