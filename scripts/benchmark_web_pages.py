"""Bounded, reproducible timings for representative Garden web pages.

This creates a disposable garden; it never reads or changes the operator's live garden.
The browser numbers split response waiting from post-response DOM work.  They are optional
because the server timings are useful on hosts without Playwright Chromium.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
import yaml
from fastapi.testclient import TestClient

from garden.runs import RunStore
from garden.store import Store
from garden.web.app import create_app

ROUTES = ("/now", "/inbox", "/board", "/tasks/BM-0001", "/runs/BM-0001/seed-0000", "/config")


def _fixture(root: Path, tasks: int, runs: int, events: int) -> None:
    (root / "garden.yaml").write_text(yaml.safe_dump({
        "name": "web benchmark", "max_parallel": 4, "review": {"enabled": False},
        "products": {"bench": {"repo": str(root), "id_prefix": "BM"}},
    }))
    phase = root / "bench" / "phase-01"
    (phase / "tasks").mkdir(parents=True)
    (phase / "goals.md").write_text("# Benchmark phase\n")
    for number in range(tasks):
        task_id = f"BM-{number:04d}"
        status = "running" if number == 0 else ("done" if number % 3 == 0 else "ready")
        (phase / "tasks" / f"{task_id}-task.md").write_text(
            "---\n"
            f"id: {task_id}\ntitle: Benchmark task {number}\nstatus: {status}\n"
            "depends_on: []\npriority: 3\nreading: []\n"
            "created: '2026-01-01T00:00:00+00:00'\nupdated: '2026-01-01T00:00:00+00:00'\n"
            "---\n\n## Goal\n\nMeasure this task.\n"
        )
    run_store = RunStore(root / ".garden")
    for number in range(runs):
        task_id = f"BM-{number % tasks:04d}"
        run = run_store.new_run(task_id, "local", run_id=f"seed-{number:04d}")
        run.status = "running" if number == 0 else "done"
        run.cost_usd = 0.01
        run.save()
    event_path = root / ".garden" / "events.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("w") as stream:
        for number in range(events):
            stream.write(json.dumps({"at": "2026-01-01T00:00:00+00:00", "kind": "tick", "n": number}) + "\n")


def _summary(samples: list[float]) -> dict[str, float]:
    return {"median_ms": round(statistics.median(samples), 1), "max_ms": round(max(samples), 1)}


def _server(root: Path, repeats: int) -> dict[str, dict[str, float]]:
    client = TestClient(create_app(Store(root), watch=False, host="testserver"))
    output = {}
    for route in ROUTES:
        cold_start = time.perf_counter()
        response = client.get(route)
        response.raise_for_status()
        cold = (time.perf_counter() - cold_start) * 1000
        warm = []
        for _ in range(repeats):
            started = time.perf_counter()
            response = client.get(route)
            response.raise_for_status()
            warm.append((time.perf_counter() - started) * 1000)
        output[route] = {"cold_ms": round(cold, 1), **_summary(warm), "bytes": len(response.content)}
    return output


def _browser(root: Path, repeats: int) -> dict[str, dict[str, float]]:
    from playwright.sync_api import sync_playwright

    app = create_app(Store(root), watch=False, host="127.0.0.1", port=8799)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8799, log_level="error"))
    thread = threading.Thread(target=server.run)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    output = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            for route in ROUTES:
                samples = []
                for _ in range(repeats):
                    page.goto(f"http://127.0.0.1:8799{route}", wait_until="domcontentloaded")
                    samples.append(page.evaluate("""() => { const n = performance.getEntriesByType('navigation')[0]; return {
                        response_ms: n.responseEnd - n.requestStart,
                        render_ms: n.domContentLoadedEventEnd - n.responseEnd,
                        total_ms: n.domContentLoadedEventEnd - n.startTime,
                    }}"""))
                output[route] = {key: round(statistics.median([s[key] for s in samples]), 1) for key in samples[0]}
            browser.close()
    finally:
        server.should_exit = True
        thread.join()
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=600)
    parser.add_argument("--runs", type=int, default=1200)
    parser.add_argument("--events", type=int, default=5000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="garden-web-benchmark-") as tmp:
        root = Path(tmp)
        _fixture(root, args.tasks, args.runs, args.events)
        result = {"fixture": vars(args), "server": _server(root, args.repeats)}
        if args.browser:
            result["browser"] = _browser(root, args.repeats)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
