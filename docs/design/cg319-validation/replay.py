"""Replay CG-319's canonical Now journey in a disposable served application."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8769"
FIXTURE = REPO / ".pytest_cache" / "now2-live-fixture"


def wait_for_server() -> None:
    for _ in range(100):
        try:
            if httpx.get(f"{BASE}/", timeout=0.2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError("disposable Now server did not start")


def finish_fixture_runs() -> None:
    changed_tasks: set[Path] = set()
    for path in (FIXTURE / ".garden" / "runs").glob("*/*/run.json"):
        run = json.loads(path.read_text())
        if run.get("status") == "running":
            run["status"] = "done"
            run["finished_at"] = "2000-01-01T00:00:00+00:00"
            path.write_text(json.dumps(run, indent=2) + "\n")
            changed_tasks.add(path.parent.parent)
    for task_dir in changed_tasks:
        task_dir.touch()


def main() -> int:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "NOW_DEMO_NAME": "Demonstration"}
    command = [sys.executable, str(REPO / "docs/design/now2-live/serve_fixture.py")]
    server = subprocess.Popen(command, cwd=REPO, env=env)
    observations: list[dict[str, str]] = []
    try:
        wait_for_server()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 2400}, color_scheme="light")
            page.goto(f"{BASE}/")
            nav = page.locator("nav.nav a").all_text_contents()
            assert nav[:3] == ["Inbox 2", "Now", "Board"]
            with page.expect_request(lambda request: request.url.endswith("/now/stream")):
                page.get_by_role("link", name="Now", exact=True).click()
            assert page.url == f"{BASE}/now"
            assert page.get_by_role("heading", name="Now", exact=True).is_visible()
            assert page.locator("article.strip").count() == 5
            observations.append({"action": "click Inbox navigation link Now", "observed": "canonical /now opened with five live run/attention strips"})

            page.get_by_role("link", name="last 24 hours", exact=True).click()
            assert "window=24h" in page.url
            assert page.locator('#period-body a.on').get_attribute("href") == "/now?window=24h#period"
            assert page.locator("#period-body .figures").is_visible()
            observations.append({"action": "click last 24 hours after observing the browser's stream request", "observed": "24-hour ledger became visible and /now/stream was connected"})

            for legacy in ("/now1", "/now2"):
                response = page.goto(f"{BASE}{legacy}")
                assert response is not None and response.request.redirected_from is not None
                assert page.url == f"{BASE}/now"
            observations.append({"action": "open /now1 and /now2 bookmarks", "observed": "both redirected to the single /now implementation"})

            page.screenshot(path=OUT / "now-1280-light.png", full_page=True)
            dark = browser.new_page(viewport={"width": 1280, "height": 2400}, color_scheme="dark")
            dark.goto(f"{BASE}/now")
            dark.screenshot(path=OUT / "now-1280-dark.png", full_page=True)
            for scheme in ("light", "dark"):
                narrow = browser.new_page(viewport={"width": 390, "height": 2400}, color_scheme=scheme)
                narrow.goto(f"{BASE}/now")
                widths = narrow.evaluate("({client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})")
                assert widths == {"client": 390, "scroll": 390}
                narrow.screenshot(path=OUT / f"now-390-{scheme}.png", full_page=True)
                narrow.close()

            finish_fixture_runs()
            time.sleep(1.1)  # let the served RunStore's bounded read index expire
            page.goto(f"{BASE}/now")
            assert page.locator('article.strip[data-run]:not([data-run=""])').count() == 0
            assert page.get_by_text("Nothing running.", exact=True).is_visible()
            assert page.locator("#next .queue").is_visible()
            observations.append({"action": "finish all synthetic runs and reload /now", "observed": "quiet state showed Nothing running while Next remained actionable"})
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    manifest = {
        "head": head,
        "environment": "disposable served app with Chromium",
        "command": ".venv/bin/python docs/design/cg319-validation/replay.py",
        "status": "pass",
        "states": {
            "affected": {"status": "pass", "actions": observations[:2], "observed": "navigation, live content, period control and event stream worked"},
            "empty": {"status": "pass", "actions": observations[3:], "observed": "quiet state retained the scheduler's next-work explanation"},
            "failure_recovery": {"status": "pass", "actions": observations[2:3], "observed": "both retired bookmarks recovered to canonical /now"},
        },
        "viewport_checks": {"1280": ["light", "dark"], "390": {"themes": ["light", "dark"], "clientWidth": 390, "scrollWidth": 390}},
        "artifacts": [
            "docs/design/cg319-validation/interaction-manifest.json",
            "docs/design/cg319-validation/now-1280-light.png",
            "docs/design/cg319-validation/now-1280-dark.png",
            "docs/design/cg319-validation/now-390-light.png",
            "docs/design/cg319-validation/now-390-dark.png",
        ],
        "ui_scope": [
            {"path": "src/garden/web/pages/__init__.py", "consumers": ["now", "now1 redirect", "now2 redirect"]},
            {"path": "src/garden/web/templates/_now1_period.html", "consumers": ["now"]},
            *({"path": path, "consumers": [], "reason": "retired file; no rendered consumer remains"} for path in (
                "src/garden/web/templates/_now2.js", "src/garden/web/templates/_now2_attention.html",
                "src/garden/web/templates/_now2_next.html", "src/garden/web/templates/_now2_period.html",
                "src/garden/web/templates/_now2_run.html", "src/garden/web/templates/_now2_summary.html",
                "src/garden/web/templates/_now2_where.html")),
        ],
    }
    (OUT / "interaction-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
