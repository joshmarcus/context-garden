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


class Event(dict):
    """A parsed event JSONL line: only the fields relevant to its `kind` were ever recorded,
    so a template branching on `e.kind` must see a real falsy value for a field another kind
    would have set, not raise, under a strict Jinja environment."""

    def __missing__(self, key: str) -> Any:
        return ""


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
                ev = Event(json.loads(line))
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


_DURATION_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """'30m', '2h', '1d', '1w' -> seconds; a bare number is already seconds. The sibling of
    `parse_since`: that returns a cutoff timestamp for a "since" window, this returns a length
    for a sleep or a staleness threshold (`garden observe`'s interval, digest_window,
    stuck_after)."""
    text = str(text).strip()
    m = re.fullmatch(r"(\d+)([mhdw])", text)
    if m:
        return int(m.group(1)) * _DURATION_UNIT_SECONDS[m.group(2)]
    return int(float(text))


# The event kinds that mean a person's call is needed and so are worth a browser
# notification (CG-208): a worker's question, a won't-do or nothing-to-change, a
# discovered-work decision, a review/revision cap or a broken base, a stall, a retro verdict
# or question, a phase closing. Everything else in the log is a notice — a merge, a
# dispatch, progress — and never notifies.
DECISION_KINDS = ("waiting_human", "decision", "needs_human", "stall", "discovered",
                  "retro_done", "retro_question", "phase_closed")

# stop_kind -> a short phrase for a needs_human notification (mirrors inbox.ATTENTION_KINDS,
# kept local so events.py stays free of the inbox's store/graph imports).
_STOP_TITLES = {
    "revision_cap": "revision cap reached",
    "review_cap": "automated review rounds used",
    "base_broken": "the base branch is broken",
    "parent_closed": "the stacked-on PR was closed",
    "stall": "the loop stalled",
}


def _clip(text: Any, n: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def decision_notifications(events: list[dict[str, Any]], titles: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Turn the decision-kind events (see DECISION_KINDS) into notification items for an open
    browser tab: each is {kind, at, task, phase, title, url}. `titles` maps a task id to its
    title for a friendlier one-line; the URL opens the task or phase the decision is about.
    Notice-kind events are dropped, so what comes back is only what should notify."""
    titles = titles or {}
    out: list[dict[str, str]] = []
    for ev in events:
        kind = str(ev.get("kind") or "")
        if kind not in DECISION_KINDS:
            continue
        phase = str(ev.get("phase") or "")
        task = str(ev.get("target") or ev.get("task") or "")
        who = titles.get(task) or task
        title, url = "", ""
        if kind == "waiting_human":
            title, url = f"{who} asks: {_clip(ev.get('question'))}", f"/tasks/{task}"
        elif kind == "decision":
            if ev.get("decision_kind"):
                verb = "duplicates another task" if ev.get("decision_kind") == "duplicate" else "is now obsolete"
                title = f"{who}: a worker says it {verb}"
            else:
                word = "won't do this task" if ev.get("decision") == "wont_do" else "found nothing to change"
                title = f"{who}: worker {word}"
            url = f"/tasks/{task}"
        elif kind in ("needs_human", "stall"):
            what = _STOP_TITLES.get(str(ev.get("stop_kind") or ""))
            if not what:
                what = _clip(ev.get("reason")) or ("the loop stalled" if kind == "stall" else "needs a decision")
            title, url = f"{who}: {what}", f"/tasks/{task}"
        elif kind == "discovered":
            nt = str(ev.get("new_task") or "")
            task = nt or task
            title = f"Discovered work to approve: {_clip(ev.get('title') or nt)}"
            url = f"/tasks/{nt}" if nt else "/"
        elif kind == "retro_done":
            title, url = f"Retro ready to review — {phase}", f"/phases/{phase}"
        elif kind == "retro_question":
            title, url = f"Retro needs a decision — {phase}", f"/phases/{phase}"
        elif kind == "phase_closed":
            title, url = f"Phase closed — {phase}", f"/phases/{phase}"
        out.append({"kind": kind, "at": str(ev.get("at") or ""), "task": task, "phase": phase, "title": title, "url": url})
    return out


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
    first_review_criteria: dict[str, tuple[int, int]] = {}
    cost: dict[str, float] = defaultdict(float)
    runs: dict[str, int] = defaultdict(int)
    rebases_mechanical = 0
    rebases_agent = 0
    rebase_cost = 0.0
    merges = 0
    task_ids = set(tasks)  # scope every count (rebases and merges included) to the phase filter
    for ev in events:
        t = ev.get("task", "")
        k = ev.get("kind")
        if not t or t not in task_ids:
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
            if t not in first_review:
                first_review[t] = str(ev.get("verdict", ""))
                first_review_criteria[t] = (int(ev.get("criteria_met") or 0), int(ev.get("criteria_total") or 0))
        elif k == "run_finished":
            cost[t] += float(ev.get("cost_usd") or 0.0)
            if ev.get("mode") == "rebase":
                rebase_cost += float(ev.get("cost_usd") or 0.0)
                # A mechanical (git-only) rebase records its run with how="mechanical"; an agent
                # rebase run has no such marker. Splitting them keeps the free git rebases from
                # hiding the ones that cost a model run (CG-197).
                if ev.get("how") == "mechanical":
                    rebases_mechanical += 1
                else:
                    rebases_agent += 1
    per_task = []
    for tid, task in tasks.items():
        if tid not in first_dispatch:
            continue
        lead_h = None
        if tid in done_at:
            lead_h = (_ts(done_at[tid]) - _ts(first_dispatch[tid])).total_seconds() / 3600
        crit_met, crit_total = first_review_criteria.get(tid, (0, 0))
        per_task.append({
            "id": tid, "difficulty": getattr(task, "difficulty", ""), "phase": getattr(task, "key", ""),
            "status": getattr(task, "status", ""), "runs": runs[tid], "revisions": revisions[tid],
            "first_review": first_review.get(tid, ""), "cost_usd": round(cost[tid], 4), "lead_hours": lead_h,
            "criteria_met": crit_met, "criteria_total": crit_total,
        })
    by_diff: dict[str, dict[str, Any]] = {}
    for row in per_task:
        d = by_diff.setdefault(row["difficulty"] or "medium", {"tasks": 0, "cost_usd": 0.0, "revisions": 0, "first_pass_approve": 0, "reviewed": 0, "done": 0, "lead_hours": [], "criteria_met": 0, "criteria_total": 0})
        d["tasks"] += 1
        d["cost_usd"] += row["cost_usd"]
        d["revisions"] += row["revisions"]
        if row["first_review"]:
            d["reviewed"] += 1
            if row["first_review"] == "approve":
                d["first_pass_approve"] += 1
            d["criteria_met"] += row["criteria_met"]
            d["criteria_total"] += row["criteria_total"]
        if row["lead_hours"] is not None:
            d["done"] += 1
            d["lead_hours"].append(row["lead_hours"])
    for d in by_diff.values():
        d["cost_usd"] = round(d["cost_usd"], 4)
        d["avg_lead_hours"] = round(sum(d["lead_hours"]) / len(d["lead_hours"]), 2) if d["lead_hours"] else None
        d["first_pass_rate"] = round(d["first_pass_approve"] / d["reviewed"], 2) if d["reviewed"] else None
        d["avg_revisions"] = round(d["revisions"] / d["tasks"], 2) if d["tasks"] else 0
        d["criteria_rate"] = round(d["criteria_met"] / d["criteria_total"], 2) if d["criteria_total"] else None
        del d["lead_hours"]
    rebases = rebases_mechanical + rebases_agent
    rebase = {
        "rebases": rebases,
        "mechanical": rebases_mechanical,
        "agent": rebases_agent,
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
        # No dispatch event for any task in the phase means it predates run records (the
        # scheduler didn't exist yet, or events.jsonl was started later): PRs-merged and cost
        # read as a real zero then, which looks like the phase did nothing rather than that
        # the loop was never tracking it (CG-205).
        "has_records": bool(dispatches),
    }


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)
