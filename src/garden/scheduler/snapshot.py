"""Safe, read-only context for design and UI workers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..events import EventLog, metrics, parse_since
from ..runs import RunStore

_ABSOLUTE_PATH = re.compile(r"(?<![\w])(?:/|[A-Za-z]:[\\/])(?:[^\s'\"<>]+)")
_WINDOWS_HOME = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+(?:\\[^\\\s]+)*", re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(\b(?:api[_ -]?key|token|secret|password|authorization|credential)\b\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)([^\s]+)")
_TOKEN_SHAPES = re.compile(r"\b(?:gh[pousr]_\w+|sk-[A-Za-z0-9_-]+)\b")


def _scrub_text(value: str) -> str:
    value = _BEARER.sub(r"\1<redacted>", value)
    value = _SENSITIVE_VALUE.sub(r"\1<redacted>", value)
    value = _TOKEN_SHAPES.sub("<redacted>", value)
    value = _WINDOWS_HOME.sub("<path>", value)
    return _ABSOLUTE_PATH.sub("<path>", value)


def _safe(value: Any, key: str = "") -> Any:
    """Drop filesystem and credential-shaped values from copied garden data."""
    lowered = key.lower()
    if any(word in lowered for word in ("secret", "token", "password", "credential", "api_key")):
        return None
    if lowered in {"path", "dir", "worktree", "root", "cwd", "command"}:
        return None
    if isinstance(value, dict):
        return {k: _safe(v, str(k)) for k, v in value.items() if _safe(v, str(k)) is not None}
    if isinstance(value, list):
        return [_safe(v, key) for v in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def write_snapshot(scheduler: Any, task: Any, worktree: Path) -> None:
    """Write live garden facts into a design task's checkout, without secrets or paths."""
    text = f"{task.title}\n{task.body}".lower()
    if not any(word in text for word in ("design", "ui", "mock", "capture")):
        return
    store = scheduler.store
    runs = RunStore(store.config.garden_dir)
    state = scheduler.state.data if hasattr(scheduler.state, "data") else {}
    tasks = [{"id": t.id, "title": t.title, "status": t.status.value, "product": t.product,
              "phase": t.phase, "difficulty": t.difficulty} for t in store.tasks().values()]
    run_rows = [{"task": r.task_id, "run": r.run_id, "mode": r.mode, "status": r.status,
                 "harness": r.harness, "model": r.model} for r in runs.all_runs()
                if r.status == "running" or r.finished_at >= parse_since("24h")]
    phases = [{"product": p.name, "phase": ph.name, "closed": ph.closed, "frozen": ph.frozen,
               "tasks": len(ph.tasks)} for p in store.products() for ph in p.phases]
    payload = {
        "tasks": tasks,
        "runs": run_rows,
        "control": {k: v for k, v in scheduler.control().items() if k not in {"token", "secret"}},
        "queue": {k: v for k, v in state.get("_queue", {}).items() if k in {"head", "order"}},
        "phases": phases,
        "events": EventLog(store.config.garden_dir / "events.jsonl").read(since=parse_since("24h")),
        "metrics": metrics(EventLog(store.config.garden_dir / "events.jsonl").read(), store.tasks()),
    }
    out = worktree / "docs" / "design" / "snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_safe(payload), indent=2, sort_keys=True) + "\n")
