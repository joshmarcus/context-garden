"""Replay only the affected Inbox ownership states against a disposable served app."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from typer.testing import CliRunner

from garden.cli import app as cli_app
from garden.model import Status
from garden.qa.sandbox import MemoryGitHub, make_garden
from garden.scheduler import State
from garden.store import Store
from garden.walkthrough import PageSpec, _screenshot
from garden.web.app import create_app


def replay(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    events: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="cg381-inbox-") as tmp:
        root = make_garden(Path(tmp))

        def command(*args: str) -> str:
            before = Path.cwd()
            os.chdir(root)
            try:
                result = CliRunner().invoke(cli_app, list(args))
            finally:
                os.chdir(before)
            assert result.exit_code == 0, result.output
            return result.output

        store = Store(root)
        review = store.task("DM-001")
        review.status = Status.IN_REVIEW
        review.pr = "https://github.com/qa/demo/pull/71"
        store.save(review)
        recovery = store.task("DM-002")
        recovery.status = Status.FAILED
        store.save(recovery)
        state = State(root / ".garden" / "state.json")
        state.get("DM-001").update({
            "head_sha": "head", "last_review_head": "head",
            "last_review": {"verdict": "request_changes", "summary": "add a boundary test"},
            "pending_reviews": [{"kind": "review"}],
        })
        state.get("DM-002")["needs_human"] = {
            "kind": "deployment", "reason": "deploy the verified build to staging",
            "prior_status": "in_review", "at": "2026-09-07T00:00:00+00:00",
        }
        state.save()
        command("new-phase", "demo", "p2")
        command("new-task", "demo/p1", "Deferred work")
        command("freeze", "demo/p1")
        cli = command("inbox")
        (out / "inbox-cli.txt").write_text(cli)
        assert "set-status DM-001 done" not in cli
        assert "review queued" in cli and "request changes" in cli
        assert "Deferred" in cli and "deployment" in cli.lower()

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        app = create_app(Store(root), watch=False, github=MemoryGitHub(), host="127.0.0.1", port=port)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started, "disposable server did not start"
            with httpx.Client(base_url=base, follow_redirects=False, timeout=10) as client:
                def request(method: str, path: str, expected: int, state_name: str,
                            outcome: str, observed: str) -> httpx.Response:
                    response = client.request(method, path)
                    events.append({"kind": "http_request", "state": state_name, "outcome": outcome,
                                   "method": method, "url": str(response.url),
                                   "status_code": response.status_code, "observed": observed})
                    assert response.status_code == expected, response.text
                    (out / f"{len(events):02d}-{state_name}.html").write_text(response.text)
                    return response

                page = request("GET", "/inbox", 200, "affected", "success",
                               "Queued review, prior verdict, deferred draft and operator deployment have zero owner decisions.")
                for text in ['<div class="v">0</div><div class="l">need you</div>',
                             "automated review queued: queued: the next tick starts it",
                             "prior automated verdict: request changes", "Deferred work",
                             "View freeze policy", "Operator recovery: Deployment prerequisite",
                             "Deployment completed, resume"]:
                    assert text in page.text, text
                assert "set-status DM-001 done" not in page.text
                shot, failure, viewport_events = _screenshot(base, [PageSpec(
                    "inbox-ownership", "/inbox", "Inbox ownership", "Review the affected ownership states",
                    "Zero owner decisions while automated, deferred and operator notices remain visible")], out, print)
                assert shot == {"inbox-ownership"} and failure is None, failure
                request("POST", "/tasks/DM-999/resume", 404, "failure", "failure",
                        "Resuming a missing task fails without changing the Inbox.")
                request("POST", "/tasks/DM-002/resume", 303, "recovery", "success",
                        "The operator completes deployment and resumes through the served action.")
                recovered = request("GET", "/inbox", 200, "recovery", "success",
                                    "The deployment prerequisite disappears while the queued review remains.")
                assert "Operator recovery: Deployment prerequisite" not in recovered.text
                assert "automated review queued" in recovered.text
                for tid in ("DM-001", "DM-002", "DM-003"):
                    request("POST", f"/tasks/{tid}/cancel", 303, "empty", "success",
                            f"Cancel disposable fixture {tid} through the served action.")
                empty = request("GET", "/inbox", 200, "empty", "empty",
                                "No task action cards remain after fixture cancellation.")
                assert '<div class="v">0</div><div class="l">need you</div>' in empty.text
                assert "Operator recovery:" not in empty.text and "automated review queued" not in empty.text
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
        assert not thread.is_alive(), "disposable server failed to stop"
    receipt = {"head": head, "environment": "disposable", "command": "python scripts/replay_cg381_inbox.py --out OUTPUT",
               "pages": ["inbox"], "events": events, "viewport_evidence": viewport_events,
               "artifacts": [str(p) for p in sorted(out.glob("*.png"))], "unverified": []}
    (out / "interaction.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps({"head": head, "events": len(events), "captures": len(receipt["artifacts"]), "status": "pass"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    replay(parser.parse_args().out.resolve())
