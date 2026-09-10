#!/usr/bin/env python3
"""Tiny Codex stand-in that makes the provider request expected by the adapter test."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


def ancestor_environments() -> list[bytes]:
    """Read every available Linux ancestor environment for credential-isolation coverage."""
    environments = []
    pid = os.getppid()
    while pid > 1:
        try:
            environments.append(Path(f"/proc/{pid}/environ").read_bytes())
            fields = Path(f"/proc/{pid}/stat").read_text().split()
            pid = int(fields[3])
        except (OSError, ValueError, IndexError):
            break
    return environments


def config(args: list[str], name: str) -> str:
    for index, value in enumerate(args[:-1]):
        if value == "-c" and args[index + 1].startswith(name + "="):
            return args[index + 1].split("=", 1)[1].strip('"')
    return ""

args = sys.argv[1:]
if os.environ.get("OPENROUTER_API_KEY") != "garden-local-openrouter-proxy":
    raise SystemExit("provider key leaked to Codex child")
if any(b"offline-test-key" in environment for environment in ancestor_environments()):
    raise SystemExit("provider key leaked through a Codex ancestor environment")
base_url = config(args, "model_providers.openrouter.base_url")
model = args[args.index("-m") + 1]
brief = sys.stdin.read()
for _ in range(int(os.environ.get("FAKE_OPENROUTER_REQUESTS", "1"))):
    request = urllib.request.Request(f"{base_url}/responses",
        data=json.dumps({"model": model, "input": brief}).encode(),
        headers={"Authorization": "Bearer not-the-provider-key", "Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        if b"response.completed" not in response.read():
            raise SystemExit("provider did not return a Responses stream")
final = 'GARDEN_RESULT: {"status":"done","summary":"adapter completed"}'
print(json.dumps({"type": "thread.started", "thread_id": "fake-codex"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": final}}))
