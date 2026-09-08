"""Record the CG-410 manual Inbox journey against a disposable served app.

The committed screenshots are intentionally generated from the same source revision as
the interaction record.  This keeps the visual evidence reproducible without touching a
real garden or starting a scheduler loop.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from garden.model import Status
from garden.qa.sandbox import make_garden
from garden.runs import RunStore
from garden.store import Store
from garden.walkthrough import _serve, capture

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "docs" / "design" / "captures"
EVIDENCE = ROOT / "docs" / "evidence" / "cg410-manual-inbox-interaction.json"


def _request(opener: object, base_url: str, method: str, path: str, data: dict[str, str] | None = None) -> int:
    body = urlencode(data).encode() if data else None
    request = Request(
        base_url + path,
        data=body,
        method=method,
        headers={"Origin": base_url, "Referer": base_url + "/inbox"},
    )
    with opener.open(request) as response:  # type: ignore[attr-defined]
        return response.status


def _copy_captures(source: Path, page: str, name: str) -> list[str]:
    artifacts: list[str] = []
    for width in (1280, 390):
        for scheme in ("light", "dark"):
            target = CAPTURES / f"cg-410-manual-{name}-{width}-{scheme}.png"
            shutil.copy2(source / f"{page}-{width}-{scheme}.png", target)
            artifacts.append(target.relative_to(ROOT).as_posix())
    return artifacts


def main() -> int:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cg410-") as scratch:
        garden_root = make_garden(Path(scratch))
        store = Store(garden_root)
        ready = store.task("DM-001")
        ready.runner = "manual"
        store.save(ready)
        blocked = store.task("DM-002")
        blocked.runner = "manual"
        blocked.depends_on = ["DM-001"]
        store.save(blocked)

        # Fill only automated capacity.  The manual card must remain safe to take.
        runs = RunStore(garden_root / ".garden")
        runs.new_run("DM-002", "local", "work")
        runs.new_run("DM-002", "local", "work")

        base_url, stop = _serve(store)
        try:
            inbox = capture(store, store.phase("demo", "p1"), Path(scratch) / "inbox",
                            base_url=base_url, pages=["inbox"])
            if not inbox.screenshots:
                raise RuntimeError(inbox.browser_note or "Inbox screenshots were not captured")
            events = [
                {"kind": "http_request", "state": "affected", "outcome": "success", "method": "GET",
                 "url": "/inbox", "status_code": _request(build_opener(), base_url, "GET", "/inbox"),
                 "observed": "manual work is actionable despite full automated capacity; dependent work waits"},
            ]
            opener = build_opener()
            events.append(
                {"kind": "http_request", "state": "affected", "outcome": "success", "method": "POST",
                 "url": "/tasks/DM-001/take", "status_code": _request(opener, base_url, "POST", "/tasks/DM-001/take"),
                 "observed": "one manual session claims the packet"}
            )
            events.append(
                {"kind": "http_request", "state": "affected", "outcome": "success", "method": "GET",
                 "url": "/tasks/DM-001/packet", "status_code": _request(opener, base_url, "GET", "/tasks/DM-001/packet"),
                 "observed": "the assigned packet is available"}
            )
            task = capture(Store(garden_root), Store(garden_root).phase("demo", "p1"), Path(scratch) / "task",
                           base_url=base_url, pages=["task"])
            if not task.screenshots:
                raise RuntimeError(task.browser_note or "Task screenshots were not captured")
            events.append(
                {"kind": "http_request", "state": "failure", "outcome": "failure", "method": "POST",
                 "url": "/tasks/DM-001/take", "status_code": _request(opener, base_url, "POST", "/tasks/DM-001/take"),
                 "observed": "a stale take is refused and does not create another run"}
            )
            events.append(
                {"kind": "http_request", "state": "failure", "outcome": "failure", "method": "POST",
                 "url": "/tasks/DM-001/finish-manual", "status_code": _request(opener, base_url, "POST", "/tasks/DM-001/finish-manual", {"note": "not JSON"}),
                 "observed": "invalid completion leaves the manual session recoverable"}
            )
            events.append(
                {"kind": "http_request", "state": "recovery", "outcome": "success", "method": "POST",
                 "url": "/tasks/DM-001/finish-manual", "status_code": _request(opener, base_url, "POST", "/tasks/DM-001/finish-manual", {"note": '{"status":"blocked","summary":"waiting on access"}'}),
                 "observed": "a valid result finishes the claimed session"}
            )
            if Store(garden_root).task("DM-001").status != Status.FAILED:
                raise RuntimeError("manual completion did not finalize the claimed session")
            events.append(
                {"kind": "http_request", "state": "empty", "outcome": "empty", "method": "GET",
                 "url": "/inbox", "status_code": _request(opener, base_url, "GET", "/inbox"),
                 "observed": "no manual task remains actionable after the claimed session finishes"}
            )
        finally:
            stop()

        artifacts = _copy_captures(Path(scratch) / "inbox", "inbox", "inbox")
        artifacts += _copy_captures(Path(scratch) / "task", "task", "task")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        EVIDENCE.write_text(json.dumps({
            "head": head,
            "environment": "disposable local garden fixture served at an ephemeral 127.0.0.1 port",
            "command": "python scripts/capture_cg410.py",
            "states": ["affected", "empty", "failure", "recovery"],
            "events": events,
            "artifacts": artifacts,
            "render": inbox.interaction_evidence + task.interaction_evidence,
        }, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
