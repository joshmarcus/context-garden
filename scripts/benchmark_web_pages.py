"""Bounded, reproducible timings for representative Garden web pages.

This creates a disposable garden; it never reads or changes the operator's live garden.
The browser numbers split response waiting from post-response DOM work.  They are optional
because the server timings are useful on hosts without Playwright Chromium.
"""

from __future__ import annotations

import argparse
import json
import os
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

ROUTES = ("/now", "/inbox", "/board", "/tasks/BM-0001", "/runs/BM-0001/seed-0001", "/config")


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
        run.pid = os.getpid() if number == 0 else None
        run.cost_usd = 0.01
        run.save()
    event_path = root / ".garden" / "events.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("w") as stream:
        for number in range(events):
            stream.write(json.dumps({"at": "2026-01-01T00:00:00+00:00", "kind": "tick", "n": number}) + "\n")


def _summary(samples: list[float]) -> dict[str, float]:
    return {"median_ms": round(statistics.median(samples), 1), "max_ms": round(max(samples), 1)}


def _server(root: Path, repeats: int, *, cache_discovery: bool) -> dict[str, dict[str, float]]:
    output = {}
    for route in ROUTES:
        app = create_app(Store(root), watch=False, host="testserver")
        client = TestClient(app)
        cold_start = time.perf_counter()
        response = client.get(route)
        response.raise_for_status()
        cold = (time.perf_counter() - cold_start) * 1000
        warm = []
        for _ in range(repeats):
            if not cache_discovery:
                app.state.hub._page_store.invalidate_tasks()
            started = time.perf_counter()
            response = client.get(route)
            response.raise_for_status()
            warm.append((time.perf_counter() - started) * 1000)
        output[route] = {"cold_ms": round(cold, 1), **_summary(warm), "bytes": len(response.content)}
    return output


def _navigation(page, url: str) -> dict[str, float]:
    page.goto(url, wait_until="domcontentloaded")
    return page.evaluate("""() => { const n = performance.getEntriesByType('navigation')[0]; return {
        response_ms: n.responseEnd - n.requestStart,
        render_ms: n.domContentLoadedEventEnd - n.responseEnd,
        total_ms: n.domContentLoadedEventEnd - n.startTime,
    }}""")


def _browser(root: Path, repeats: int, *, cache_discovery: bool) -> dict[str, dict[str, object]]:
    from playwright.sync_api import sync_playwright

    output = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        for port, route in enumerate(ROUTES, start=8799):
            app = create_app(Store(root), watch=False, host="127.0.0.1", port=port)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
            thread = threading.Thread(target=server.run)
            thread.start()
            while not server.started:
                time.sleep(0.01)
            try:
                url = f"http://127.0.0.1:{port}{route}"
                cold = _navigation(page, url)
                warm = []
                for _ in range(repeats):
                    if not cache_discovery:
                        app.state.hub._page_store.invalidate_tasks()
                    warm.append(_navigation(page, url))
                output[route] = {
                    "cold": {key: round(value, 1) for key, value in cold.items()},
                    "warm": {key: round(statistics.median([sample[key] for sample in warm]), 1) for key in cold},
                }
            finally:
                # `/now` holds an SSE connection open. Leave the page before asking its
                # isolated server to drain so a cold sample cannot stall the benchmark.
                page.goto("about:blank")
                server.should_exit = True
                thread.join()
        browser.close()
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
        result: dict[str, object] = {"fixture": vars(args)}
        for label, cache_discovery in (("before", False), ("after", True)):
            measurements: dict[str, object] = {
                "server": _server(root, args.repeats, cache_discovery=cache_discovery),
            }
            if args.browser:
                measurements["browser"] = _browser(root, args.repeats, cache_discovery=cache_discovery)
            result[label] = measurements
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
