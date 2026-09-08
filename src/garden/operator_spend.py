"""The operator's own session spend, recorded beside the workers' (CG-223).

The loop is token-free, but a person or an operator agent still watches it, clears cards
and moves pins. Keeping that seat cheap is a goal on a par with keeping worker runs cheap
(see "The operator seat" in `docs/design.md`), so its spend is recorded the same way a
worker's is: one JSON line per event, append-only, read by `garden costs` / the Costs page
and quoted in the phase retro.

File format — one JSON object per line at `docs/operator-spend.jsonl` (`default_path`):

- a **spend** record, one per heartbeat, summing a Claude Code or Codex transcript from
  its start:
  `{"at", "session", "first_turn", "last_turn", "turns", "models": {model: turn_count},
  "tokens": {"input", "output", "cache_read", "cache_write"}, "list_price_usd", "avg_context"}`.
  `list_price_usd` is `null` when the transcript has no known price; it is never guessed.
  `turns` and known prices are cumulative for the session, not incremental — a later
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
DEFAULT_RELATIVE_PATH = Path("docs") / "operator-spend.jsonl"


def selected_product_path(root: Path, config: Any | None = None) -> Path | None:
    """Return the garden directory for the product that owns the tool, when identified."""
    products = (getattr(config, "data", {}) or {}).get("products", {}) if config is not None else {}
    for name, product in products.items():
        if isinstance(product, dict) and (product.get("provides_tool") or product.get("self")):
            return root / str(name)
    return None


def default_path(root: Path, config: Any | None = None,
                 product_path: Path | str | None = None) -> Path:
    """Return the operator ledger path shared by the CLI, costs surfaces and retros.

    A configured path is relative to the garden root unless absolute.  Without an
    override, the ledger belongs to the selected product directory.
    """
    configured = config.get("operator_spend.path") if config is not None else None
    if configured:
        path = Path(str(configured))
        return path if path.is_absolute() else root / path
    base = Path(product_path) if product_path is not None else selected_product_path(root, config) or root
    return base / DEFAULT_RELATIVE_PATH


def project_dir_for(root: Path) -> Path:
    """The Claude Code transcript directory for a working directory: `~/.claude/projects/`
    plus the absolute path with every `/` turned into `-`, Claude Code's own naming."""
    encoded = str(root.resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def codex_session_dir() -> Path:
    """Codex CLI and desktop session transcripts, organized below this directory by date."""
    return Path.home() / ".codex" / "sessions"


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


def find_codex_transcript(session_dir: Path, session: str = "") -> Path:
    """The newest Codex session transcript, optionally matched by its filename or thread id."""
    files = sorted(session_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if session:
        files = [f for f in files if session in f.stem or _codex_session_id(f) == session]
    if not files:
        where = f"under {session_dir}" + (f" matching session {session!r}" if session else "")
        raise FileNotFoundError(f"no transcript found in Codex sessions {where}")
    return files[-1]


def record_from_transcript(path: Path) -> dict[str, Any]:
    """One cumulative heartbeat from a Claude Code or Codex transcript at ``path``."""
    events = _events(path)
    if any(e.get("type") == "session_meta" or (e.get("type") == "event_msg" and isinstance(e.get("payload"), dict)
           and e["payload"].get("type") == "token_count") for e in events):
        return _record_codex(path, events)
    return _record_claude(path, events)


def _events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _record_claude(path: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    turns = 0
    cost = 0.0
    models: dict[str, int] = {}
    first = last = ""
    price_available = True
    for e in events:
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
        price = PRICES.get(model)
        if price is None:
            price_available = False
        else:
            pi, po, pr, pw = price
            cost += (i * pi + o * po + cr * pr + cw * pw) / 1e6
        ts = str(e.get("timestamp") or "")
        first = first or ts
        last = ts or last
    return {"at": now_iso(), "harness": "claude", "session": path.stem,
            "first_turn": first, "last_turn": last, "turns": turns, "models": models,
            "tokens": tot, "list_price_usd": round(cost, 2) if price_available else None,
            "price_status": "available" if price_available else "unavailable",
            "usage_status": "available", "avg_context": int(tot["cache_read"] / max(1, turns))}


def _codex_session_id(path: Path) -> str:
    for event in _events(path):
        if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
            return str(event["payload"].get("id") or "")
    return ""


def _record_codex(path: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Use Codex's latest cumulative ``token_count`` rather than summing its snapshots."""
    total: dict[str, Any] | None = None
    session = path.stem
    turn_models: dict[str, int] = {}
    first = last = ""
    turns = 0
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("type") == "session_meta":
            session = str(payload.get("id") or session)
        if event.get("type") == "turn_context":
            # A turn can publish several cumulative token snapshots.  The context
            # event is its boundary, so it alone owns turn/model attribution.
            model = str(payload.get("model") or "")
            turns += 1
            if model:
                turn_models[model] = turn_models.get(model, 0) + 1
            timestamp = str(event.get("timestamp") or "")
            first = first or timestamp
            last = timestamp or last
        if event.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        usage = info.get("total_token_usage")
        if not isinstance(usage, dict):
            continue
        total = usage
    if total is None:
        return {"at": now_iso(), "harness": "codex", "session": session,
                "first_turn": "", "last_turn": "", "turns": 0, "models": {}, "tokens": None,
                "list_price_usd": None, "price_status": "unavailable", "usage_status": "unavailable",
                "avg_context": None}
    cached = int(total.get("cached_input_tokens", 0) or 0)
    input_tokens = max(0, int(total.get("input_tokens", 0) or 0) - cached)
    tokens = {"input": input_tokens,
              "output": int(total.get("output_tokens", 0) or 0),
              "cache_read": cached,
              "cache_write": int(total.get("cache_write_input_tokens", 0) or 0)}
    return {"at": now_iso(), "harness": "codex", "session": session,
            "first_turn": first, "last_turn": last, "turns": turns,
            "models": turn_models, "tokens": tokens,
            "list_price_usd": None, "price_status": "unavailable", "usage_status": "available",
            "avg_context": int(tokens["cache_read"] / max(1, turns))}


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
            reported_total = r.get("list_price_usd")
            if not isinstance(reported_total, (int, float)):
                continue
            total = float(reported_total)
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


def total_turns(records: list[dict[str, Any]], since: str = "") -> int:
    """Return turns added in the window, accounting for cumulative heartbeats."""
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("kind") != "compacted":
            by_session[str(record.get("session") or "")].append(record)
    total = 0
    for rows in by_session.values():
        rows.sort(key=lambda row: str(row.get("at") or ""))
        previous = 0
        for row in rows:
            turns = int(row.get("turns") or 0)
            if not since or str(row.get("at") or "") >= since:
                total += max(turns - previous, 0)
            previous = turns
    return total


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
    rows = [{"session": sid, "harness": str(r.get("harness") or "claude"),
            "at": str(r.get("at") or ""), "turns": int(r.get("turns") or 0),
            "avg_context": r.get("avg_context"),
            "cost_usd": round(float(r["list_price_usd"]), 2)
            if isinstance(r.get("list_price_usd"), (int, float)) else None,
            "usage_status": str(r.get("usage_status") or "available"),
            "compactions": compactions.get(sid, 0)}
           for sid, r in latest.items()]
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows
