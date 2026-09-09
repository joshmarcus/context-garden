"""Capture and record the redesigned Now summary against a disposable served garden."""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from garden.model import Status
from garden.qa.sandbox import MemoryGitHub, make_garden
from garden.runs import RunStore
from garden.scheduler import Scheduler
from garden.store import Store
from garden.walkthrough import PageSpec, _screenshot
from garden.web.app import create_app


def replay(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    events: list[dict[str, object]] = []
    viewport_evidence: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="cg455-now-") as tmp:
        root = make_garden(Path(tmp))
        store = Store(root)
        scheduler = Scheduler(store, github=MemoryGitHub(), log=lambda _message: None)
        run = RunStore(store.config.garden_dir).new_run("DM-001", "local", "work")
        run.harness, run.model, run.difficulty, run.pid = "claude", "qa-easy", "easy", 4242
        run.save()

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
            assert server.started
            with httpx.Client(base_url=base, timeout=10) as client:
                def get(path: str, state: str, expected: int, observed: str) -> httpx.Response:
                    response = client.get(path)
                    events.append({"state": state, "method": "GET", "url": str(response.url),
                                   "status": response.status_code, "observed": observed})
                    assert response.status_code == expected
                    return response

                populated = get("/now", "populated", 200, "One live run and queued work appear in the summary.")
                assert "1 / 4" in populated.text and "A worker that asks a question" in populated.text
                _, failure, evidence = _screenshot(base, [PageSpec(
                    "now-populated", "/now", "Now populated", "Read the top summary",
                    "Live capacity, next dispatch, phase and period are visible")], out, print)
                assert failure is None, failure
                viewport_evidence.extend(evidence)

                missing = get("/partials/now/nope", "failure", 404,
                              "An invalid live-refresh region fails without corrupting the page.")
                assert missing.status_code == 404
                recovered = get("/partials/now/head", "recovery", 200,
                                "The valid head fragment recovers with the current summary and slot facts.")
                assert 'id="now-summary"' in recovered.text and 'id="now-slots"' in recovered.text

                run.status = "done"
                run.finished_at = run.started_at
                run.save()
                for task in store.tasks().values():
                    task.status = Status.CANCELLED
                    store.save(task)
                sparse = get("/now", "sparse", 200, "No run or dispatch candidate is reported literally.")
                assert "0 / 4" in sparse.text and "Nothing queued" in sparse.text
                _, failure, evidence = _screenshot(base, [PageSpec(
                    "now-sparse", "/now", "Now sparse", "Read the empty top summary",
                    "Zero capacity use and no queued candidate remain composed")], out, print)
                assert failure is None, failure
                viewport_evidence.extend(evidence)

                scheduler = Scheduler(Store(root), github=MemoryGitHub(), log=lambda _message: None)
                scheduler.pause("cli", "maintenance window")
                paused = get("/now", "paused", 200, "The explicit pause leads the neutral measurements.")
                assert "Dispatch paused" in paused.text and "maintenance window" in paused.text
                _, failure, evidence = _screenshot(base, [PageSpec(
                    "now-paused", "/now", "Now paused", "Read the paused top summary",
                    "The pause reason leads and every literal reading remains visible")], out, print)
                assert failure is None, failure
                viewport_evidence.extend(evidence)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
        assert not thread.is_alive()
    artifacts = [str(path) for path in sorted(out.glob("*.png"))]
    receipt = {"head": head, "environment": "disposable", "command": "python scripts/replay_cg455_now.py --out OUTPUT",
               "pages": ["now"], "states": ["populated", "failure", "recovery", "sparse", "paused"],
               "events": events, "viewport_evidence": viewport_evidence, "artifacts": artifacts, "unverified": []}
    (out / "interaction.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps({"head": head, "events": len(events), "captures": len(artifacts), "status": "pass"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    replay(parser.parse_args().out.resolve())
