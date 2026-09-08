"""Disposable served-app replay for CG-443 reviewer clarification recovery evidence.

Run explicitly with pytest; this evidence helper is not part of ordinary test discovery.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn

from garden.model import Status
from garden.scheduler import Scheduler
from garden.store import Store
from garden.web.app import create_app

pytest_plugins = ["tests.conftest"]


def test_served_reviewer_clarification_failure_and_recovery(garden, fake_github) -> None:
    output = Path("docs/design/cg443-validation/interaction-manifest.json")
    started = datetime.now(UTC).isoformat()
    store = Store(garden)
    task = store.task("DM-001")
    task.status = Status.IN_REVIEW
    pr = fake_github.create_pr("test/demo", "garden/dm-001", "main", "First task", "Review fixture")
    task.pr = pr.url
    store.save(task)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base_url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        create_app(Store(garden), watch=False, github=fake_github, port=port),
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    events = []
    try:
        with httpx.Client(base_url=base_url, follow_redirects=False) as client:
            empty = client.get("/inbox")
            assert empty.status_code == 200
            assert "Reviewer clarification needs attention" not in empty.text
            events.append({
                "kind": "http_request", "state": "empty", "outcome": "empty",
                "method": "GET", "url": f"{base_url}/inbox", "status_code": 200,
                "observed": "Inbox has no reviewer-clarification attention card.",
            })

            scheduler = Scheduler(Store(garden), github=fake_github)
            scheduler.state.get(task.id)["needs_human"] = {
                "kind": "review_clarification",
                "reason": "reviewer clarification remained malformed",
                "owner": "reviewer",
            }
            scheduler.state.save()
            affected = client.get("/inbox")
            assert affected.status_code == 200
            assert "Reviewer clarification needs attention" in affected.text
            assert "One more automated review" in affected.text
            events.append({
                "kind": "http_request", "state": "affected", "outcome": "success",
                "method": "GET", "url": f"{base_url}/inbox", "status_code": 200,
                "observed": "Reviewer-owned clarification stop is visible with its recovery action.",
            })

            missing = client.get("/tasks/NOT-A-TASK")
            assert missing.status_code == 404
            events.append({
                "kind": "http_request", "state": "failure", "outcome": "failure",
                "method": "GET", "url": f"{base_url}/tasks/NOT-A-TASK", "status_code": 404,
                "observed": "An invalid task request is rejected with HTTP 404; the clarification stop remains intact.",
            })
            persisted = client.get("/inbox")
            assert persisted.status_code == 200
            assert "Reviewer clarification needs attention" in persisted.text

            recovered = client.post(f"/tasks/{task.id}/review")
            assert recovered.status_code == 303
            events.append({
                "kind": "http_request", "state": "recovery", "outcome": "success",
                "method": "POST", "url": f"{base_url}/tasks/{task.id}/review", "status_code": 303,
                "observed": "The valid operator action dispatches reviewer-owned work without an author revision.",
            })
            final = client.get("/inbox")
            assert final.status_code == 200
            assert "Reviewer clarification needs attention" not in final.text
            events.append({
                "kind": "http_request", "state": "recovery", "outcome": "success",
                "method": "GET", "url": f"{base_url}/inbox", "status_code": 200,
                "observed": "The recovered Inbox no longer shows the clarification stop.",
            })

        runs = scheduler.runs.runs_for(task.id)
        assert not [run for run in runs if run.mode == "revise"]
        manifest = json.loads(output.read_text())
        manifest.update({
            "producer": "cg443.disposable-served-review-clarification/v2",
            "head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True, timeout=10,
            ).strip(),
            "command": (
                "timeout --signal=TERM --kill-after=10s 900 $GARDEN_VALIDATION_RUNNER "
                "-m garden.validation -- .venv/bin/python -m pytest --timeout=120 "
                "--timeout-method=thread docs/design/cg443-validation/replay_test.py -q"
            ),
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "status": "pass",
            "fixture_setup": (
                "A disposable garden was seeded with a reviewer-owned clarification stop; "
                "the served app then received an invalid request before the valid recovery action."
            ),
            "states": {
                "empty": {
                    "status": "pass", "actions": ["GET /inbox before reviewer-owned stop"],
                    "observed": "Inbox has no reviewer-clarification attention card.",
                },
                "affected": {
                    "status": "pass", "actions": ["GET /inbox after reviewer-owned stop"],
                    "observed": "Inbox explains the stop and offers One more automated review.",
                },
                "failure_recovery": {
                    "status": "pass",
                    "actions": ["GET an invalid task URL", "POST /tasks/DM-001/review", "GET /inbox"],
                    "observed": "HTTP 404 is followed by successful reviewer-owned recovery; no author revision is queued.",
                },
            },
            "events": events,
            "automated_checks": [
                "served replay test passed on the recorded source head",
                "focused review and web tests run separately",
            ],
            "unverified": [],
        })
        output.write_text(json.dumps(manifest, indent=2) + "\n")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
