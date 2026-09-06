"""The merge queue's four state facts have one writer each: `scheduler/queue.py` (CG-202).

`merge_head`, `automerge_candidate`, `automerge_ready_at` and `automerge_blocked` used to be
written from a scattering of places across poll, rebase, checkruns, reap, human and the base
transition. This test reads the source and asserts that no module other than `queue.py` assigns,
pops or `setdefault`s any of them, so the invariant cannot quietly regress. Reads (`st.get(...)`,
event kinds, continuation-dict keys) are deliberately not matched."""

from __future__ import annotations

import re
from pathlib import Path

KEYS = ("automerge_candidate", "automerge_ready_at", "merge_head", "automerge_blocked")
_alt = "|".join(KEYS)
# a write is `.pop("key"` / `.setdefault("key"` / `["key"] =` (assignment, not `==`).
WRITE = re.compile(
    rf"""\.(?:pop|setdefault)\(\s*["'](?:{_alt})["']"""
    rf"""|\[\s*["'](?:{_alt})["']\s*\]\s*=(?!=)"""
)

SRC = Path(__file__).resolve().parents[1] / "src" / "garden"
OWNER = SRC / "scheduler" / "queue.py"


def _writes_in(path: Path) -> list[tuple[int, str]]:
    return [(i, ln.rstrip()) for i, ln in enumerate(path.read_text().splitlines(), 1)
            if WRITE.search(ln)]


def test_only_queue_py_writes_queue_state():
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in SRC.rglob("*.py"):
        if path == OWNER:
            continue
        hits = _writes_in(path)
        if hits:
            offenders[str(path.relative_to(SRC))] = hits
    assert not offenders, (
        "queue state must be written only through scheduler/queue.py; found direct writes: "
        + "; ".join(f"{f}:{n} {ln}" for f, hits in offenders.items() for n, ln in hits)
    )


def test_queue_py_actually_writes_every_key():
    """Guard against the regex silently matching nothing (e.g. a key renamed everywhere)."""
    written = " ".join(ln for _, ln in _writes_in(OWNER))
    for key in KEYS:
        assert key in written, f"{key} is never written in queue.py"


# Run records also have a string ``status`` field. Task writes assign a ``Status`` enum,
# except the parameter assignment inside ``_transition`` itself.
TASK_STATUS_WRITE = re.compile(r"\.status\s*=\s*Status\.|\btask\.status\s*=\s*status\b")
TRANSITION_OWNER = SRC / "scheduler" / "__init__.py"


def test_only_transition_assigns_task_status():
    """Task status changes retain transition logging, events and queue cleanup."""
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in SRC.rglob("*.py"):
        hits = [(i, ln.rstrip()) for i, ln in enumerate(path.read_text().splitlines(), 1)
                if TASK_STATUS_WRITE.search(ln)]
        if path != TRANSITION_OWNER and hits:
            offenders[str(path.relative_to(SRC))] = hits
    assert not offenders, (
        "task status must be assigned only by Scheduler._transition; found direct writes: "
        + "; ".join(f"{f}:{n} {ln}" for f, hits in offenders.items() for n, ln in hits)
    )
    owner_writes = [(i, ln.rstrip()) for i, ln in enumerate(TRANSITION_OWNER.read_text().splitlines(), 1)
                    if TASK_STATUS_WRITE.search(ln)]
    assert len(owner_writes) == 1, "Scheduler._transition must remain the sole task status writer"
