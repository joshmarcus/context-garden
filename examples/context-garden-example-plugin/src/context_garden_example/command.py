"""Minimal implementation of the Context Garden JSON Lines command protocol."""

from __future__ import annotations

import json
import sys

from garden.plugins import COMMAND_PROTOCOL_VERSION


def main() -> None:
    handshake = json.loads(sys.stdin.readline())
    request = json.loads(sys.stdin.readline())
    malformed = request.get("payload", {}).get("label") == "malformed"
    if malformed:
        print("not-json")
        return
    print(json.dumps({
        "type": "handshake",
        "protocol_version": COMMAND_PROTOCOL_VERSION,
        "capabilities": ["check_provider"],
    }))
    print(json.dumps({
        "type": "response",
        "idempotency_key": request["idempotency_key"],
        "result": {
            "status": "pass",
            "observed_revision": request["payload"]["revision"],
            "evidence": {"mode": "json-lines", "request": handshake["type"]},
        },
    }))


if __name__ == "__main__":
    main()

