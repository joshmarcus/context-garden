"""The operator's own session spend, recorded beside the workers' (CG-223).

The loop is token-free, but a person or an operator agent still watches it, clears cards
and moves pins. Keeping that seat cheap is a goal on a par with keeping worker runs cheap
(see "The operator seat" in `docs/design.md`), so its spend is recorded the same way a
worker's is: one JSON line per event, append-only, read by `garden costs` / the Costs page
and quoted in the phase retro.

File format — one JSON object per line at `docs/operator-spend.jsonl` (`default_path`):

- a **spend** record, one per heartbeat, summing the Claude Code transcript from its start:
  `{"at", "session", "first_turn", "last_turn", "turns", "models": {model: turn_count},
  "tokens": {"input", "output", "cache_read", "cache_write"}, "list_price_usd", "avg_context"}`.
  `list_price_usd` and `turns` are cumulative for the session, not incremental — a later
  heartbeat for the same session repeats and extends the earlier one — so `to_cost_events`
  turns consecutive heartbeats into discrete deltas before anything sums them as a cost.
- a **compacted** marker, written when the operator compacts its context at a boundary:
  `{"at", "session", "kind": "compacted"}`. It carries no cost; `compaction_marks` pulls it
  out for the Costs chart's annotations.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .model import now_iso

# $/MTok: input, output, cache_read, cache_write (list prices, 2026-06)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5), "claude-fable-5": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25), "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5), "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
}
_DEFAULT_PRICE = PRICES["claude-fable-5-1"]

DEFAULT_RELATIVE_PATH = Path("docs") / "operator-spend.jsonl"


def default_path(root: Path) -> Path:
    return root / DEFAULT_RELATIVE_PATH


def project_dir_for(root: Path) -> Path:
    """The Claude Code transcript directory for a working directory: `~/.claude/projects/`
    plus the absolute path with every `/` turned into `-`, Claude Code's own naming."""
    encoded = str(root.resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def find_transcript(project_dir: Path, session: str = "") -> Path:
    """The newest transcript under `project_dir`, or the newest whose filename contains
    `session`. Raises FileNotFoundError (never returns a made-up path) when none match."""
    files = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if session:
        files = [f for f in files if session in f.stem]
    if not files:
        where = f"under {project_dir}" + (f" matching session {session!r}" if session else "")
        raise FileNotFoundError(f"no transcript found {where}")
    return files[-1]


def record_from_transcript(path: Path) -> dict[str, Any]:
    """One heartbeat record summing every assistant turn's usage in the transcript at `path`
    (a Claude Code `.jsonl` session log) from its start, priced at list price. The session id
    is the transcript's filename stem."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    turns = 0
    cost = 0.0
    models: dict[str, int] = {}
    first = last = ""
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = e.get("message") if isinstance(e.get("message"), dict) else None
        if not m or m.get("role") != "assistant" or not m.get("usage"):
            continue
        u = m["usage"]
        model = str(m.get("model", "?"))
        turns += 1
        models[model] = models.get(model, 0) + 1
        i, o = int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)
        cr, cw = int(u.get("cache_read_input_tokens", 0) or 0), int(u.get("cache_creation_input_tokens", 0) or 0)
        tot["input"] += i
        tot["output"] += o
        tot["cache_read"] += cr
        tot["cache_write"] += cw
        pi, po, pr, pw = PRICES.get(model, _DEFAULT_PRICE)
        cost += (i * pi + o * po + cr * pr + cw * pw) / 1e6
        ts = str(e.get("timestamp") or "")
        first = first or ts
        last = ts or last
    return {"at": now_iso(), "session": path.stem, "first_turn": first, "last_turn": last, "turns": turns,
           "models": models, "tokens": tot, "list_price_usd": round(cost, 2),
           "avg_context": int(tot["cache_read"] / max(1, turns))}


def compacted_record(session: str) -> dict[str, Any]:
    return {"at": now_iso(), "session": session, "kind": "compacted"}


def append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def to_cost_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spend records, turned into `run_finished`-shaped events `costs.cost_series` can bucket
    like any other run. Each heartbeat's `list_price_usd` is a running total for its session,
    so the event's cost is the increase since that session's previous heartbeat (the first
    heartbeat's full total, since there is no earlier one to subtract). Compacted markers
    carry no cost and are never turned into a cost event; see `compaction_marks`."""
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        if r.get("kind") == "compacted":
            continue
        by_session[str(r.get("session") or "")].append(r)
    out: list[dict[str, Any]] = []
    for sid, rows in by_session.items():
        rows.sort(key=lambda r: str(r.get("at") or ""))
        prev = 0.0
        for r in rows:
            total = float(r.get("list_price_usd") or 0.0)
            delta = max(total - prev, 0.0)
            prev = total
            out.append({"kind": "run_finished", "at": str(r.get("at") or ""), "mode": "operator",
                       "session": sid, "task": "", "model": "", "harness": "",
                       "cost_usd": round(delta, 4), "usage": {}})
    return out


def total_cost(records: list[dict[str, Any]], since: str = "") -> float:
    """The operator's total spend, windowed the same way `cost_series` windows a run:
    delta events at or after `since` (empty = all time)."""
    return round(sum(e["cost_usd"] for e in to_cost_events(records) if not since or e["at"] >= since), 4)


def compaction_marks(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `compacted` records, as `{"at", "session"}` — what the Costs chart annotates."""
    return [{"at": str(r.get("at") or ""), "session": str(r.get("session") or "")}
            for r in records if r.get("kind") == "compacted"]


def session_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per session — its latest heartbeat (cumulative turns/cost/tokens) plus how
    many times it compacted — newest first. What `garden operator-spend` prints."""
    latest: dict[str, dict[str, Any]] = {}
    compactions: dict[str, int] = defaultdict(int)
    for r in records:
        sid = str(r.get("session") or "")
        if r.get("kind") == "compacted":
            compactions[sid] += 1
            continue
        if sid not in latest or str(r.get("at") or "") > str(latest[sid].get("at") or ""):
            latest[sid] = r
    rows = [{"session": sid, "at": str(r.get("at") or ""), "turns": int(r.get("turns") or 0),
            "avg_context": int(r.get("avg_context") or 0),
            "cost_usd": round(float(r.get("list_price_usd") or 0.0), 2),
            "compactions": compactions.get(sid, 0)}
           for sid, r in latest.items()]
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows
