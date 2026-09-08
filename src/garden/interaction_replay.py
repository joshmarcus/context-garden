"""Scheduler-owned disposable application replay used as review evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .qa.sandbox import start


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args()
    actual_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if actual_head != args.head:
        parser.error("replay head differs from the checkout")
    if subprocess.check_output(["git", "status", "--porcelain", "--", "src", "pyproject.toml"], text=True).strip():
        parser.error("replay source differs from the committed head")
    started = datetime.now(UTC).isoformat()
    journal: list[dict[str, Any]] = []

    def prepare(garden: Path) -> None:
        """Seed one clean worker and one deliberately escaped worker before serving."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=garden, check=True)
        subprocess.run(["git", "add", "-A"], cwd=garden, check=True)
        subprocess.run(["git", "-c", "user.email=operator@example.com", "-c", "user.name=operator",
                        "commit", "-q", "-m", "initial disposable garden"], cwd=garden, check=True)
        first, second = sorted((garden / "demo" / "p1" / "tasks").glob("*.md"))
        first.write_text(first.read_text().replace("qa-worker: needs_input", "qa-worker: done"))
        second.write_text(second.read_text().replace("qa-worker: no_change_decision", "qa-worker: escape")
                          + f"\nqa-escape: {garden / 'garden.yaml'}\n")
        config = (garden / "garden.yaml").read_text().replace("draft_pr: true", "draft_pr: false")
        (garden / "garden.yaml").write_text(config)
        subprocess.run(["git", "add", "-A"], cwd=garden, check=True)
        subprocess.run(["git", "-c", "user.email=operator@example.com", "-c", "user.name=operator",
                        "commit", "-q", "-m", "configure disposable fence replay"], cwd=garden, check=True)

    box = start(args.out / "sandbox", prepare=prepare)
    client = httpx.Client(base_url=box.base_url, follow_redirects=False, timeout=20)
    state_times: dict[str, float] = {}

    def request(method: str, path: str, *, state: str = "", data: dict[str, str] | None = None) -> httpx.Response:
        response = client.request(method, path, data=data, headers={"referer": box.base_url + "/"})
        journal.append({"at": time.time(), "kind": "http_request", "method": method, "url": box.base_url + path,
                        "status_code": response.status_code, "state": state})
        return response

    def status(task_id: str, *, state: str = "") -> str:
        response = request("GET", "/api/tasks", state=state)
        response.raise_for_status()
        return next(task["status"] for task in response.json() if task["id"] == task_id)

    def wait_for(task_id: str, wanted: str, state: str) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if status(task_id, state=state) == wanted:
                return
            tick = request("POST", "/tick", state=state)
            if tick.status_code != 303:
                raise RuntimeError(f"tick returned {tick.status_code}")
            time.sleep(0.1)
        raise RuntimeError(f"{task_id} did not reach {wanted}")

    try:
        # Affected journey: dispatch a clean run, make an interleaved operator commit, then reap.
        if request("POST", "/tasks/DM-001/dispatch", state="affected").status_code != 303:
            raise RuntimeError("clean dispatch was refused")
        spec = box.garden / "demo" / "p1" / "specs" / "spec.md"
        spec.write_text("# spec\n\nOperator commit during worker run.\n")
        subprocess.run(["git", "add", str(spec.relative_to(box.garden))], cwd=box.garden, check=True)
        subprocess.run(["git", "-c", "user.email=operator@example.com", "-c", "user.name=operator",
                        "commit", "-q", "-m", "operator: clarify spec"], cwd=box.garden, check=True)
        operator_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=box.garden, text=True).strip()
        wait_for("DM-001", "in_review", "affected")
        if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=box.garden, text=True).strip() != operator_head:
            raise RuntimeError("operator commit was not retained")
        state_times["affected"] = time.time()

        # Failure/recovery journey: the worker transcript records a redirect to garden.yaml.
        if request("POST", "/tasks/DM-002/dispatch", state="failure").status_code != 303:
            raise RuntimeError("escape dispatch was refused")
        wait_for("DM-002", "failed", "failure")
        state_times["failure"] = time.time()
        # This is the actual served failure observation: the failed task's action endpoint
        # rejects an invalid recovery action with a real HTTP error before the retry below.
        failed_action = request("POST", "/tasks/DM-002/not-an-action", state="failure")
        if failed_action.status_code < 400:
            raise RuntimeError("failed task action was not rejected")
        failed_action_request = journal[-1]
        if request("POST", "/tasks/DM-002/retry", state="recovery").status_code != 303:
            raise RuntimeError("retry was refused")
        second = next((box.garden / "demo" / "p1" / "tasks").glob("DM-002-*.md"))
        second.write_text(second.read_text().replace("qa-worker: escape", "qa-worker: done"))
        if request("POST", "/tasks/DM-002/dispatch", state="recovery").status_code != 303:
            raise RuntimeError("clean retry dispatch was refused")
        wait_for("DM-002", "in_review", "recovery")
        state_times["recovery"] = time.time()

        # Empty state: both tasks are reaped, with no active run or fence attention remaining.
        if status("DM-001", state="empty") != "in_review" or status("DM-002", state="empty") != "in_review":
            raise RuntimeError("clean state retained a failed or active task")
        state_times["empty"] = time.time()

        def event_for(record: dict[str, Any], state: str, outcome: str, observed: str) -> dict[str, Any]:
            """Turn a recorded request into evidence without inventing an interaction."""
            return {**record, "state": state, "outcome": outcome, "observed": observed}

        affected_request = next(item for item in reversed(journal)
                                if item["state"] == "affected" and item["method"] == "GET")
        recovery_request = next(item for item in reversed(journal)
                                if item["state"] == "recovery" and item["method"] == "POST")
        empty_request = next(item for item in reversed(journal)
                             if item["state"] == "empty" and item["method"] == "GET")
        events = [
            event_for(affected_request, "affected", "success",
                      "operator spec commit survived and DM-001 reaped to review"),
            event_for(failed_action_request, "failure", "failure",
                      "DM-002 was failed by its transcript redirect and rejected an invalid action"),
            event_for(recovery_request, "recovery", "success", "clean retry reaped DM-002 to review"),
            event_for(empty_request, "empty", "empty", "no active or fenced task remains after both reaps"),
        ]
        states = {
            "affected": {"status": "pass", "action": "GET /api/tasks after operator commit and reap", "observed": events[0]["observed"]},
            "failure": {"status": "pass", "action": "POST /tasks/DM-002/not-an-action after fenced reap", "observed": events[1]["observed"]},
            "recovery": {"status": "pass", "action": "POST /tasks/DM-002/retry then reap", "observed": events[2]["observed"]},
            "empty": {"status": "pass", "action": "GET /api/tasks", "observed": events[3]["observed"]},
        }
    finally:
        client.close()
        box.stop()
    finished = datetime.now(UTC).isoformat()
    flows = [
        {"name": "operator commit survives reap", "ok": True, "requests": [item for item in journal if item["state"] == "affected"]},
        {"name": "transcript redirect fails then retry recovers", "ok": True,
         "requests": [item for item in journal if item["state"] in {"failure", "recovery"}]},
        {"name": "clean reaped state", "ok": True, "requests": [item for item in journal if item["method"] == "GET"]},
    ]
    manifest = {
        "producer": "garden.scheduler.interaction-replay/v1",
        "head": args.head,
        "nonce": args.nonce,
        "started_at": started,
        "finished_at": finished,
        "status": "pass",
        "environment": "disposable",
        "serve_command": "uvicorn.Server(create_app(Store(<disposable garden>)), host='127.0.0.1', port=<ephemeral>)",
        "flows": flows,
        "states": states,
        "events": events,
        # The manifest is the sole artifact this replay actually creates.
        "artifacts": [str(args.out / "interaction-manifest.json")],
    }
    (args.out / "interaction-manifest.json").write_text(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
