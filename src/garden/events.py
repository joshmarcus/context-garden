"""Append-only event log: .garden/events.jsonl.

Every transition, dispatch, run completion, PR poll that changed something, review verdict,
answer, stall and budget event is one JSON line. It is the source for timelines, the
`garden digest` summary and `garden metrics`. Task files stay the source of truth for
*state*; the log is the source of truth for *history*.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .model import now_iso


class EventLog:
    def __init__(self, path: Path):
        self.path = path

    def emit(self, kind: str, task_id: str = "", **data: Any) -> dict[str, Any]:
        ev = {"at": now_iso(), "kind": kind, "task": task_id, **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(ev, sort_keys=True) + "\n")
        return ev

    def read(self, since: str = "", task_id: str = "", kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        wanted = set(kinds) if kinds else None
        out = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since and ev.get("at", "") < since:
                continue
            if task_id and ev.get("task") != task_id:
                continue
            if wanted and ev.get("kind") not in wanted:
                continue
            out.append(ev)
        return out


def parse_since(text: str) -> str:
    """'24h', '3d', '90m' or an ISO timestamp -> ISO timestamp."""
    text = text.strip()
    m = re.fullmatch(r"(\d+)([mhdw])", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": dt.timedelta(minutes=n), "h": dt.timedelta(hours=n), "d": dt.timedelta(days=n), "w": dt.timedelta(weeks=n)}[unit]
        return (dt.datetime.now(dt.UTC) - delta).replace(microsecond=0).isoformat()
    return text


HUMAN_KINDS = {"waiting_human", "needs_human", "stall", "budget", "pr_closed", "failed", "decision"}


def digest(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Group a window of events into what a human wants to know."""
    out: dict[str, Any] = {
        "prs_opened": [], "merged": [], "automerged": [], "needs_human": [], "reviews": [], "discovered": [],
        "failures": [],
        "dispatched": 0, "cost_usd": 0.0, "tasks": defaultdict(list),
    }
    for ev in events:
        k = ev.get("kind")
        t = ev.get("task", "")
        if t:
            out["tasks"][t].append(ev)
        if k == "pr_opened":
            out["prs_opened"].append(ev)
        elif k == "transition" and ev.get("to") == "done":
            out["merged"].append(ev)
        elif k == "automerged":
            out["automerged"].append(ev)
        elif k in HUMAN_KINDS or (k == "transition" and ev.get("to") in ("failed", "waiting_human")):
            out["needs_human"].append(ev)
        elif k == "review":
            out["reviews"].append(ev)
        elif k == "discovered":
            out["discovered"].append(ev)
        elif k == "dispatch":
            out["dispatched"] += 1
        if k == "run_finished":
            out["cost_usd"] += float(ev.get("cost_usd") or 0.0)
            if ev.get("mode") in ("work", "revise") and ev.get("status") not in ("done", "needs_input"):
                out["failures"].append(ev)
    out["tasks"] = dict(out["tasks"])
    return out


def metrics(events: list[dict[str, Any]], tasks: dict[str, Any]) -> dict[str, Any]:
    """Per-task and per-difficulty metrics from the event stream.

    tasks: id -> object with .difficulty / .status / .key (Task) — used for grouping.
    """
    first_dispatch: dict[str, str] = {}
    done_at: dict[str, str] = {}
    revisions: dict[str, int] = defaultdict(int)
    first_review: dict[str, str] = {}
    cost: dict[str, float] = defaultdict(float)
    runs: dict[str, int] = defaultdict(int)
    rebases = 0
    rebase_cost = 0.0
    merges = 0
    for ev in events:
        t = ev.get("task", "")
        k = ev.get("kind")
        if not t:
            continue
        if k == "dispatch":
            first_dispatch.setdefault(t, ev["at"])
            runs[t] += 1
            if ev.get("mode") == "revise":
                revisions[t] += 1
        elif k == "transition" and ev.get("to") == "done":
            done_at[t] = ev["at"]
            merges += 1
        elif k == "review":
            first_review.setdefault(t, str(ev.get("verdict", "")))
        elif k == "run_finished":
            cost[t] += float(ev.get("cost_usd") or 0.0)
            if ev.get("mode") == "rebase":
                rebases += 1
                rebase_cost += float(ev.get("cost_usd") or 0.0)
    per_task = []
    for tid, task in tasks.items():
        if tid not in first_dispatch:
            continue
        lead_h = None
        if tid in done_at:
            lead_h = (_ts(done_at[tid]) - _ts(first_dispatch[tid])).total_seconds() / 3600
        per_task.append({
            "id": tid, "difficulty": getattr(task, "difficulty", ""), "phase": getattr(task, "key", ""),
            "status": getattr(task, "status", ""), "runs": runs[tid], "revisions": revisions[tid],
            "first_review": first_review.get(tid, ""), "cost_usd": round(cost[tid], 4), "lead_hours": lead_h,
        })
    by_diff: dict[str, dict[str, Any]] = {}
    for row in per_task:
        d = by_diff.setdefault(row["difficulty"] or "medium", {"tasks": 0, "cost_usd": 0.0, "revisions": 0, "first_pass_approve": 0, "reviewed": 0, "done": 0, "lead_hours": []})
        d["tasks"] += 1
        d["cost_usd"] += row["cost_usd"]
        d["revisions"] += row["revisions"]
        if row["first_review"]:
            d["reviewed"] += 1
            if row["first_review"] == "approve":
                d["first_pass_approve"] += 1
        if row["lead_hours"] is not None:
            d["done"] += 1
            d["lead_hours"].append(row["lead_hours"])
    for d in by_diff.values():
        d["cost_usd"] = round(d["cost_usd"], 4)
        d["avg_lead_hours"] = round(sum(d["lead_hours"]) / len(d["lead_hours"]), 2) if d["lead_hours"] else None
        d["first_pass_rate"] = round(d["first_pass_approve"] / d["reviewed"], 2) if d["reviewed"] else None
        d["avg_revisions"] = round(d["revisions"] / d["tasks"], 2) if d["tasks"] else 0
        del d["lead_hours"]
    rebase = {
        "rebases": rebases,
        "cost_usd": round(rebase_cost, 4),
        "merges": merges,
        "per_merge": round(rebases / merges, 2) if merges else None,
    }
    return {"tasks": per_task, "by_difficulty": by_diff, "rebase": rebase}


def phase_summary(events: list[dict[str, Any]], tasks: dict[str, Any]) -> dict[str, Any]:
    """The figures a closed phase is remembered by: dates, tasks done, merged PRs, lead
    time, revise rounds, first-pass rate and cost. `tasks` is id -> Task for the phase."""
    m = metrics(events, tasks)
    dispatches = [ev["at"] for ev in events if ev.get("kind") == "dispatch" and ev.get("task") in tasks]
    done_at = {ev["task"]: ev["at"] for ev in events
               if ev.get("kind") == "transition" and ev.get("to") == "done" and ev.get("task") in tasks}
    leads = [r["lead_hours"] for r in m["tasks"] if r["lead_hours"] is not None]
    reviewed = [r for r in m["tasks"] if r["first_review"]]

    def status_of(t: Any) -> str:
        s = getattr(t, "status", "")
        return getattr(s, "value", str(s))

    return {
        "metrics": m,
        "first_dispatch": min(dispatches)[:10] if dispatches else "",
        "done_at": done_at,
        "tasks_done": sum(1 for t in tasks.values() if status_of(t) == "done"),
        "tasks_total": len(tasks),
        "prs_merged": sum(1 for t in tasks.values() if getattr(t, "pr", "") and status_of(t) == "done"),
        "revisions": sum(r["revisions"] for r in m["tasks"]),
        "avg_lead_hours": round(sum(leads) / len(leads), 1) if leads else None,
        "first_pass_rate": round(sum(1 for r in reviewed if r["first_review"] == "approve") / len(reviewed), 2) if reviewed else None,
        "cost_usd": round(sum(r["cost_usd"] for r in m["tasks"]), 2),
    }


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)
