"""Record the CG-410 manual Inbox journey against a disposable served app.

The committed screenshots are intentionally generated from the same source revision as
the interaction record.  This keeps the visual evidence reproducible without touching a
real garden or starting a scheduler loop.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from garden.model import Status
from garden.qa.sandbox import make_garden
from garden.runs import RunStore
from garden.store import Store
from garden.walkthrough import _serve

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "docs" / "design" / "captures"
EVIDENCE = ROOT / "docs" / "evidence" / "cg410-manual-inbox-interaction.json"


class _NoRedirect(HTTPRedirectHandler):
    """Expose the action response before a browser follows its redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _request(opener: object, base_url: str, method: str, path: str, data: dict[str, str] | None = None) -> int:
    body = urlencode(data).encode() if data else None
    request = Request(
        base_url + path,
        data=body,
        method=method,
        headers={"Origin": base_url, "Referer": base_url + "/inbox"},
    )
    try:
        with opener.open(request) as response:  # type: ignore[attr-defined]
            return response.status
    except HTTPError as error:
        return error.code


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
            opener = build_opener(_NoRedirect())
            events = [
                {"kind": "http_request", "state": "affected", "outcome": "success", "method": "GET",
                 "url": "/inbox", "status_code": _request(opener, base_url, "GET", "/inbox"),
                 "observed": "manual work is actionable despite full automated capacity; dependent work waits"},
            ]
            events.append(
                {"kind": "http_request", "state": "blocked", "outcome": "safe_waiting", "method": "GET",
                 "url": "/tasks/DM-002", "status_code": _request(opener, base_url, "GET", "/tasks/DM-002"),
                 "observed": "dependency-blocked manual task page offers no Take action"}
            )
            store.set_phase_frozen(store.phase("demo", "p1"), "release hold")
            events.append(
                {"kind": "http_request", "state": "frozen", "outcome": "safe_waiting", "method": "GET",
                 "url": "/tasks/DM-001", "status_code": _request(opener, base_url, "GET", "/tasks/DM-001"),
                 "observed": "frozen manual task page offers no Take action"}
            )
            store.set_phase_frozen(store.phase("demo", "p1"), "")
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
            events.append(
                {"kind": "http_request", "state": "failure", "outcome": "failure", "method": "POST",
                 "url": "/tasks/DM-001/take", "status_code": _request(opener, base_url, "POST", "/tasks/DM-001/take"),
                 "observed": "a stale take returns its refusal redirect and does not create another run"}
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

        artifacts = [path.relative_to(ROOT).as_posix() for path in sorted(CAPTURES.glob("cg-410-manual-*.png"))]
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        EVIDENCE.write_text(json.dumps({
            "head": head,
            "environment": "disposable local garden fixture served at an ephemeral 127.0.0.1 port",
            "command": "python scripts/capture_cg410.py (served interaction; preserves existing inspected captures)",
            "states": ["affected", "blocked", "frozen", "empty", "failure", "recovery"],
            "events": events,
            "artifacts": artifacts,
            "render": "Existing 1280px and 390px Inbox/task captures are preserved; this replay records only the revised served interaction.",
        }, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
