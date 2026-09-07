"""Read-side SSE invalidation; independent of the controller and its lock.

Observe source versions, including output and ledger changes that have no domain
event. Render only after a source changes, or a UTC minute advances the window.
The browser receives changed HTML fragments, never a polling timer.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from .operator_spend import default_path


def versions(root: Path, garden_dir: Path) -> tuple:
    paths = [garden_dir / "events.jsonl", garden_dir / "state.json", default_path(root)]
    paths += list(root.glob("*/*/goals.md")) + list(root.glob("*/*/tasks/*.md"))
    paths += list(garden_dir.glob("runs/*/*/run.json")) + list(garden_dir.glob("runs/*/*/stdout.json"))
    result = []
    for p in paths:
        try:
            st = p.stat()
            result.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(sorted(result)), int(time.time() // 60)


class Fragments:
    """Per-connection monotonic cursor and last-sent fragment set."""
    def __init__(self, render: Callable[[], dict[str, str]]):
        self.render = render
        self.previous: dict[str, str] = {}
        self.revision = 0

    def message(self, reset: bool = False) -> str:
        current = self.render()
        changed = {key: html for key, html in current.items() if reset or self.previous.get(key) != html}
        removed = [key for key in self.previous if key not in current]
        self.previous = current
        self.revision += 1
        payload = {"revision": self.revision, "fragments": changed, "removed": removed}
        return f"id: {self.revision}\nevent: {'snapshot' if reset else 'fragments'}\ndata: {json.dumps(payload)}\n\n"


async def stream(fragments: Fragments, version: Callable[[], tuple],
                 disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
    prior = await asyncio.to_thread(version)
    yield await asyncio.to_thread(fragments.message, True)
    heartbeat = time.monotonic()
    while not await disconnected():
        await asyncio.sleep(1)
        current = await asyncio.to_thread(version)
        if current != prior:
            yield await asyncio.to_thread(fragments.message)
            prior = current
        if time.monotonic() - heartbeat >= 15:
            yield "event: heartbeat\ndata: {}\n\n"
            heartbeat = time.monotonic()
