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

# Keep the established table API for existing callers. Now 2's acceptance cohorts
# have different attribution, units and cell shapes, so expose them separately.
from .outcomes import difficulty_by_model as windowed_difficulty_by_model


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

    def patch_run_costs(self, patches: dict[str, dict[str, Any]]) -> int:
        """Rewrite each `run_finished` event whose `run` id is a key of `patches`, updating
        its `cost_usd`/`usage`/`model` in place. The log is append-only for everything else;
        this is the one exception, for a one-off correction (`garden costs --backfill`
        recomputing codex cost from stored transcripts after CG-233) where the alternative —
        the old, wrong figure standing forever alongside a corrective event that duplicates
        its `run_finished` — would double-count the run wherever something counts events
        rather than sums their cost. Returns the number of lines changed."""
        if not patches or not self.path.exists():
            return 0
        lines = self.path.read_text().splitlines()
        changed = 0
        out_lines = []
        for line in lines:
            stripped = line.strip()
            ev = None
            if stripped:
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    ev = None
            patch = patches.get(ev.get("run")) if isinstance(ev, dict) and ev.get("kind") == "run_finished" else None
            if patch is None:
                out_lines.append(line)
                continue
            ev.update(patch)
            out_lines.append(json.dumps(ev, sort_keys=True))
            changed += 1
        if changed:
            self.path.write_text("\n".join(out_lines) + "\n")
        return changed


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
                  "retro_done", "retro_question", "retro_verdict", "phase_closed")

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
        elif kind == "retro_verdict":
            if ev.get("status") != "pending":
                continue  # close/close_with_followups verdicts close at once; retro_done already notified
            title, url = f"Retro verdict needs a decision — {phase}", f"/phases/{phase}"
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
        elif (k in HUMAN_KINDS or (k == "transition" and ev.get("to") in ("failed", "waiting_human"))
              or (k == "retro_verdict" and ev.get("status") == "pending")):
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


def metrics(events: list[dict[str, Any]], tasks: dict[str, Any], since: str = "", until: str = "") -> dict[str, Any]:
    """Per-task and per-difficulty metrics from the event stream.

    tasks: id -> object with .difficulty / .status / .key (Task) — used for grouping.
    """
    first_dispatch: dict[str, str] = {}
    done_at: dict[str, str] = {}
    revisions: dict[str, int] = defaultdict(int)
    first_review: dict[str, str] = {}
    first_review_criteria: dict[str, tuple[int, int]] = {}
    cost: dict[str, float] = defaultdict(float)
    costs_by_dimension: dict[str, dict[str, float]] = {
        "model": defaultdict(float), "harness": defaultdict(float),
    }
    runs_by_dimension: dict[str, dict[str, int]] = {
        "model": defaultdict(int), "harness": defaultdict(int),
    }
    task_dimensions: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"model": set(), "harness": set()}
    )
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
            if ev.get("mode") in ("work", "revise", "resume"):
                for dimension in ("model", "harness"):
                    value = str(ev.get(dimension) or "unknown")
                    task_dimensions[t][dimension].add(value)
        elif k == "transition" and ev.get("to") == "done":
            done_at[t] = ev["at"]
            merges += 1
        elif k == "review":
            if t not in first_review:
                first_review[t] = str(ev.get("verdict", ""))
                first_review_criteria[t] = (int(ev.get("criteria_met") or 0), int(ev.get("criteria_total") or 0))
        elif k == "run_finished":
            run_cost = float(ev.get("cost_usd") or 0.0)
            cost[t] += run_cost
            for dimension in ("model", "harness"):
                costs_by_dimension[dimension][str(ev.get(dimension) or "unknown")] += run_cost
                runs_by_dimension[dimension][str(ev.get(dimension) or "unknown")] += 1
                # Old event logs did not always retain dispatch metadata. A completed work
                # run still identifies the model and harness that made the task's first pass.
                if ev.get("mode") in ("work", "revise", "resume"):
                    task_dimensions[t][dimension].add(str(ev.get(dimension) or "unknown"))
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
            "accepted": tid in done_at,
            "model": sorted(task_dimensions[tid]["model"]),
            "harness": sorted(task_dimensions[tid]["harness"]),
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
    # Costs are recorded per run while acceptance is recorded by the base-branch merge
    # transition. Keep the two facts separate: a cost-per-accepted-task figure contains
    # only runs belonging to a task the scheduler observed merge to the base branch.
    accepted_ids = set(done_at)
    accepted_cost_by_dimension: dict[str, dict[str, float]] = {
        "difficulty": defaultdict(float), "model": defaultdict(float), "harness": defaultdict(float),
    }
    accepted_tasks_by_dimension: dict[str, dict[str, set[str]]] = {
        "difficulty": defaultdict(set), "model": defaultdict(set), "harness": defaultdict(set),
    }
    for tid in accepted_ids:
        task = tasks[tid]
        accepted_tasks_by_dimension["difficulty"][getattr(task, "difficulty", "") or "medium"].add(tid)
        for dimension in ("model", "harness"):
            for value in task_dimensions[tid][dimension]:
                accepted_tasks_by_dimension[dimension][value].add(tid)
    for tid in accepted_ids:
        # Review, edit, and check runs do not carry the model and harness of the task's
        # implementation run. They are nevertheless part of the cost to accept that
        # task, so attribute its complete cost to every implementation route it used.
        # This intentionally makes a task which changed routes visible in each route's
        # comparison, rather than silently losing its supporting-run costs to "unknown".
        accepted_cost = cost[tid]
        accepted_cost_by_dimension["difficulty"][getattr(tasks[tid], "difficulty", "") or "medium"] += accepted_cost
        for dimension in ("model", "harness"):
            for value in task_dimensions[tid][dimension]:
                accepted_cost_by_dimension[dimension][value] += accepted_cost

    def outcome_breakdown(dimension: str) -> dict[str, dict[str, Any]]:
        values = set(accepted_tasks_by_dimension[dimension])
        if dimension == "difficulty":
            values.update(by_diff)
        else:
            values.update(costs_by_dimension[dimension])
        rows: dict[str, dict[str, Any]] = {}
        for value in sorted(values):
            members = [row for row in per_task if value in (row[dimension] if dimension in ("model", "harness") else [row["difficulty"] or "medium"])]
            reviewed_members = [row for row in members if row["first_review"]]
            accepted = accepted_tasks_by_dimension[dimension][value]
            accepted_cost = accepted_cost_by_dimension[dimension][value]
            total_cost = (sum(row["cost_usd"] for row in members) if dimension == "difficulty"
                          else costs_by_dimension[dimension][value])
            run_count = (sum(row["runs"] for row in members) if dimension == "difficulty"
                         else runs_by_dimension[dimension][value])
            rows[value] = {
                "tasks": len(members),
                "accepted": len(accepted),
                "runs": run_count,
                "cost_usd": round(total_cost, 4),
                "mean_cost_usd": round(total_cost / run_count, 4) if run_count else None,
                "accepted_cost_usd": round(accepted_cost, 4),
                "cost_per_accepted_task": round(accepted_cost / len(accepted), 4) if accepted else None,
                "reviewed": len(reviewed_members),
                "first_pass_approve": sum(row["first_review"] == "approve" for row in reviewed_members),
                "first_pass_rate": round(sum(row["first_review"] == "approve" for row in reviewed_members) / len(reviewed_members), 2) if reviewed_members else None,
            }
        return rows

    outcomes = {dimension: outcome_breakdown(dimension) for dimension in ("difficulty", "model", "harness")}
    # Preserve the established tier metrics while adding the common outcome measures used
    # for model-routing comparisons.
    for tier, row in outcomes["difficulty"].items():
        if tier in by_diff:
            by_diff[tier].update(row)
    rebases = rebases_mechanical + rebases_agent
    rebase = {
        "rebases": rebases,
        "mechanical": rebases_mechanical,
        "agent": rebases_agent,
        "cost_usd": round(rebase_cost, 4),
        "merges": merges,
        "per_merge": round(rebases / merges, 2) if merges else None,
    }
    return {"tasks": per_task, "by_difficulty": by_diff, "by_model": outcomes["model"],
            "by_harness": outcomes["harness"], "rebase": rebase,
            "by_difficulty_model": difficulty_by_model(events, tasks),
            "difficulty_by_model": windowed_difficulty_by_model(events, tasks, since, until)}


# The difficulty-by-model tables (the owner's ask, 2026-09-06 02:30Z), in the order they are
# shown: the phase's two definition-of-done numbers first. `better` says which end of the scale
# is the good one; `n_unit` says what a cell's n counts, so a caption can say it.
DIFFICULTY_MODEL_METRICS: tuple[tuple[str, str, str, str, str], ...] = (
    ("cost_per_accepted", "cost per accepted task", "usd", "low", "accepted tasks"),
    ("first_pass", "first-pass approval", "pct", "high", "tasks first reviewed"),
    ("work_run_cost", "work-run cost", "usd", "low", "work runs"),
    ("revise_rounds", "revise rounds", "rounds", "low", "accepted tasks"),
    ("lead_time", "median lead time", "hours", "low", "accepted tasks"),
)
DIFFICULTIES = ("easy", "medium", "hard")
WORK_MODES = ("work", "revise", "resume", "trial")  # the runs that write a task's code: what a model is credited for
THIN_SAMPLE = 3  # a cell with fewer samples is shown faint and never marked best or worst


def _model_at(work_runs: list[tuple[str, str]], at: str) -> str:
    """The model credited for a task at the moment of an event: the model of the task's latest
    work-mode run finished at or before it (`work_runs` is [(finished_at, model)] in order)."""
    model = ""
    for finished, m in work_runs:
        if finished > at:
            break
        model = m
    return model


def _rank_row(cells: dict[str, dict[str, Any]], better: str) -> None:
    """Place a row's cells on a scale from its best value (rank 0) to its worst (rank 1). The
    scale is set by the solid cells (n at or above THIN_SAMPLE); a comparison needs two of them.
    A thin cell is placed on that scale, clamped, but is never the best or the worst, so a
    single lucky run cannot read as a verdict; the page shows it faint and marked. A flat row
    (every solid value the same) sits at rank 0 with no best or worst: there is no end to mark."""
    solid = [c for c in cells.values() if not c["thin"]]
    if len(solid) < 2:
        return
    lo = min(c["value"] for c in solid)
    hi = max(c["value"] for c in solid)
    if hi == lo:
        for c in cells.values():
            c["rank"] = 0.0
        return
    for c in cells.values():
        k = (c["value"] - lo) / (hi - lo)
        c["rank"] = round(min(1.0, max(0.0, k if better == "low" else 1.0 - k)), 3)
    pick_best, pick_worst = (min, max) if better == "low" else (max, min)
    pick_best(solid, key=lambda c: c["value"])["best"] = True
    pick_worst(solid, key=lambda c: c["value"])["worst"] = True


def difficulty_by_model(events: list[dict[str, Any]], tasks: dict[str, Any], since: str = "") -> dict[str, Any]:
    """The difficulty-by-model tables for a window: rows easy, medium, hard; a column per model
    that did work in the window; each cell a value, its n and its place on the row's scale.

    Every table uses the rule `metrics` uses for its per-difficulty figures (per-task facts
    from the event stream, grouped by the task's difficulty), with one addition: a task is
    credited to the model of its latest work-mode run finished at or before the event the
    metric hangs on, so a task the loop escalated is counted for the model that got it
    accepted. `since` is an ISO timestamp; the history before it still counts (a task's first
    review ever, its cost to date), only the crediting event must fall inside the window.

    - cost per accepted task: tasks whose latest `transition` to done is in the window; the
      task's whole run cost up to that moment (every mode); mean; n = accepted tasks.
    - first-pass approval: tasks whose first `review` ever is in the window; the share whose
      verdict is approve; n = tasks first reviewed.
    - work-run cost: `run_finished` in the window with a work mode and a model; mean cost;
      n = runs, each credited to its own model.
    - revise rounds: per accepted task, `dispatch` events with mode revise before its done
      transition; mean; n = accepted tasks.
    - median lead time: per accepted task, hours from its first dispatch to its done
      transition; median; n = accepted tasks.

    A cell is {value, n, thin, rank, best, worst}: `rank` runs from 0 at the row's best value
    to 1 at its worst (None when the row has fewer than two solid cells), `best` and `worst`
    mark the two ends among cells with at least THIN_SAMPLE samples, `thin` marks the rest.
    """
    import statistics

    work_runs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    cost_to: dict[str, list[tuple[str, float]]] = defaultdict(list)
    revises: dict[str, list[str]] = defaultdict(list)
    first_dispatch: dict[str, str] = {}
    first_review: dict[str, dict[str, Any]] = {}
    done_at: dict[str, str] = {}
    for e in events:
        t, k, at = str(e.get("task") or ""), e.get("kind"), str(e.get("at") or "")
        if not t or t not in tasks:
            continue
        if k == "run_finished":
            cost_to[t].append((at, float(e.get("cost_usd") or 0.0)))
            if e.get("mode") in WORK_MODES and e.get("model"):
                work_runs[t].append((at, str(e["model"])))
        elif k == "dispatch":
            first_dispatch.setdefault(t, at)
            if e.get("mode") == "revise":
                revises[t].append(at)
        elif k == "review":
            first_review.setdefault(t, e)
        elif k == "transition" and e.get("to") == "done":
            done_at[t] = at
    samples: dict[str, dict[str, dict[str, list[float]]]] = {
        key: {d: defaultdict(list) for d in DIFFICULTIES} for key, *_ in DIFFICULTY_MODEL_METRICS}

    def add(metric: str, task_id: str, model: str, value: float) -> None:
        d = getattr(tasks[task_id], "difficulty", "") or "medium"
        if model and d in samples[metric]:
            samples[metric][d][model].append(value)

    for t, at in done_at.items():
        if at < since:
            continue
        model = _model_at(work_runs[t], at)
        add("cost_per_accepted", t, model, sum(c for when, c in cost_to[t] if when <= at))
        add("revise_rounds", t, model, sum(1 for when in revises[t] if when <= at))
        if t in first_dispatch:
            add("lead_time", t, model, (_ts(at) - _ts(first_dispatch[t])).total_seconds() / 3600)
    for t, e in first_review.items():
        if e["at"] >= since:
            add("first_pass", t, _model_at(work_runs[t], e["at"]), 1.0 if e.get("verdict") == "approve" else 0.0)
    for e in events:
        if (e.get("kind") == "run_finished" and e.get("mode") in WORK_MODES and e.get("model")
                and str(e.get("at") or "") >= since and e.get("task") in tasks):
            add("work_run_cost", str(e["task"]), str(e["model"]), float(e.get("cost_usd") or 0.0))

    weight: dict[str, int] = defaultdict(int)
    for by_d in samples.values():
        for cells in by_d.values():
            for model, vals in cells.items():
                weight[model] += len(vals)
    models = sorted(weight, key=lambda m: (-weight[m], m))
    tables = []
    for key, label, unit, better, n_unit in DIFFICULTY_MODEL_METRICS:
        rows: dict[str, dict[str, dict[str, Any]]] = {}
        for d in DIFFICULTIES:
            cells = {}
            for model, vals in samples[key][d].items():
                value = statistics.median(vals) if key == "lead_time" else sum(vals) / len(vals)
                cells[model] = {"value": round(value, 3), "n": len(vals), "thin": len(vals) < THIN_SAMPLE,
                                "rank": None, "best": False, "worst": False}
            _rank_row(cells, better)
            rows[d] = cells
        tables.append({"key": key, "label": label, "unit": unit, "better": better, "n_unit": n_unit, "rows": rows})
    return {"models": models, "metrics": tables, "thin": THIN_SAMPLE}


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


def with_run_records(events: list[dict[str, Any]], runs: list[Any]) -> list[dict[str, Any]]:
    """Fill old event metadata from run records and include not-yet-reaped completions.

    Event timestamps remain authoritative when present. Run identities deduplicate
    repeated completion events, while records supply dispatched tier/model and costs.
    This read-side join is shared by the CLI and Now; it never changes the log.
    """
    records = {(r.task_id, r.run_id): r for r in runs}
    out = []
    seen = set()
    for event in events:
        e = dict(event)
        key = (e.get("task"), e.get("run"))
        kind = e.get("kind")
        if kind in ("dispatch", "run_finished") and key[1]:
            identity = (kind, *key)
            if identity in seen:
                continue
            seen.add(identity)
            r = records.get(key)
            if r:
                for name in ("mode", "harness", "model", "difficulty"):
                    if not e.get(name):
                        e[name] = getattr(r, name)
                if kind == "run_finished" and r.cost_usd is not None:
                    e["cost_usd"] = r.cost_usd
        out.append(e)
    for r in runs:
        fields = {"task": r.task_id, "run": r.run_id, "mode": r.mode, "harness": r.harness,
                  "model": r.model, "difficulty": r.difficulty}
        if r.started_at and ("dispatch", r.task_id, r.run_id) not in seen:
            out.append({**fields, "kind": "dispatch", "at": r.started_at})
        if r.finished_at and ("run_finished", r.task_id, r.run_id) not in seen:
            out.append({**fields, "kind": "run_finished", "at": r.finished_at, "status": r.status,
                        "cost_usd": r.cost_usd, "usage": r.usage})
    return out
