"""Now 1: what the garden is doing right now, what it will do next, where the phase is and
how the last period went (docs/design/now-1.md; the spec is the Now page's).

`snapshot` assembles the page's four regions into one dict from the store, the state file,
the run records and the event log, read-only and with no network call; the web route renders
it and `garden now --page 1` prints it through `render_text`. `stream` yields the live
messages the page's server-sent-events endpoint sends: each new events.jsonl line as it lands,
a run's progress (its latest words and spend) when its stream moves, and the hub's tick. This
module never imports the web package.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import operator_spend as ops
from .costs import bucket_key, cost_series
from .criteria import criteria_counts
from .events import EventLog, difficulty_by_model
from .graph import effective_status
from .inbox import merge_queue_view, needs_human_info
from .model import Status, goals_text, phase_refusal
from .plants import plant_info
from .runs import Run, RunStore
from .store import Store

WORKER_MODES = {"work", "revise", "resume", "trial", "rebase"}
CHECK_MODES = {"check"}
REVIEW_MODES = {"review", "persona", "compare"}
# The design's mode -> growth-stage glyph table (docs/design/now-1.md, Visual system): the
# glyph is a task-state name `plants.stage_svg` knows; the dot is the state colour.
MODE_GLYPH = {"work": "running", "revise": "running", "resume": "running", "trial": "running",
              "rebase": "ready", "edit": "ready", "check": "awaiting_triage",
              "review": "in_review", "persona": "in_review", "compare": "in_review"}
MODE_DOT = {"work": "running", "revise": "running", "resume": "running", "trial": "running",
            "rebase": "ready", "edit": "ready", "check": "ready",
            "review": "in_review", "persona": "in_review", "compare": "in_review"}
FINISHED_GLYPH = {"done": "done", "failed": "failed", "timeout": "failed", "blocked": "waiting_human",
                  "needs_input": "waiting_human", "cancelled": "cancelled", "superseded": "cancelled"}
# Event kinds that mean a person did something by hand (the last period's "hand steps").
HAND_KINDS = ("answer", "triaged", "decision_accepted", "decision_resolved", "dispatch_paused",
              "dispatch_resumed", "resumed", "moved", "budget_set", "config_override", "suggestion")
# Runs that reached an outcome say how long the work takes; a cancelled, superseded or
# never-started run does not.
TYPICAL_STATUSES = {"done", "failed", "timeout", "blocked"}
TYPICAL_MIN_SAMPLES = 3
# The growth-stage word for a phase's merged fraction: nothing merged is a seed, then each
# quarter is a stage, and everything merged is in fruit.
STAGE_BANDS = ((0.25, "in leaf"), (0.5, "in bud"), (0.75, "in flower"))
WINDOWS = (("hour", "last hour"), ("today", "today"), ("24h", "last 24 hours"), ("phase", "this phase"))
QUEUE_SHOWN = 8


# ---- formatting shared by the template and the text view ------------------------------

def clock(seconds: float) -> str:
    """Elapsed time as the live clock shows it (the owner's rule, 2026-09-06 02:35Z): seconds
    under a minute, then m:ss, then h:mm:ss. The server renders the first reading with this
    and the browser's `fmt` in base.html must agree; a test holds the two together."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def minutes(seconds: float | None) -> str:
    """A typical duration in words: seconds under 90, else whole minutes."""
    if seconds is None:
        return ""
    return f"{int(round(seconds))} s" if seconds < 90 else f"{int(round(seconds / 60))} min"


def ktok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def money(v: float | None) -> str:
    return f"${v:.2f}" if v is not None else "—"


def iso_utc(s: str) -> str:
    """A run record's timestamp as the clock reads it: UTC, whole seconds, an explicit offset,
    so `Date.parse` in every browser agrees with the server."""
    if not s:
        return ""
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _clip(text: str, n: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def live_clock_html(run: Run) -> str:
    """The markup base.html's clock ticks: a running run's elapsed time with the attributes
    the script reads (`data-started`, and the first reading in the clock's own format), the
    same on the Board's running cards, a task page's run row and a Now strip."""
    secs = run.elapsed_minutes() * 60
    if run.status != "running" or not run.started_at:
        return f'<span data-started="{iso_utc(run.started_at)}" data-stopped="{int(secs)}"><span data-elapsed>{clock(secs)}</span></span>'
    return f'<span data-started="{iso_utc(run.started_at)}"><span data-elapsed>{clock(secs)}</span></span>'


# ---- runs in flight -------------------------------------------------------------------

def typical_key(mode: str, harness: str, difficulty: str = "") -> str:
    """A run's key into `typical_seconds`: mode and tier, and whether a harness ran it. A
    mechanical (token-free) rebase takes seconds and an agent rebase takes minutes; the two
    must not share a median."""
    who = harness or "-"
    return f"{mode}/{who}/{difficulty}" if difficulty else f"{mode}/{who}"


def typical_seconds(all_runs: list[Run], now: dt.datetime) -> dict[str, float]:
    """Median elapsed seconds per mode, harness-or-not and tier over the last seven days,
    falling back to mode and harness-or-not all-time; only keys with at least three runs that
    ran to an outcome (done, failed, timeout, blocked)."""
    recent: dict[str, list[float]] = defaultdict(list)
    ever: dict[str, list[float]] = defaultdict(list)
    week_ago = (now - dt.timedelta(days=7)).isoformat()
    for r in all_runs:
        if r.status not in TYPICAL_STATUSES or not r.started_at or not r.finished_at:
            continue
        secs = r.elapsed_minutes() * 60
        ever[typical_key(r.mode, r.harness)].append(secs)
        if r.started_at >= week_ago:
            recent[typical_key(r.mode, r.harness, r.difficulty or "medium")].append(secs)
    out = {k: statistics.median(v) for k, v in recent.items() if len(v) >= TYPICAL_MIN_SAMPLES}
    out.update({k: statistics.median(v) for k, v in ever.items() if len(v) >= TYPICAL_MIN_SAMPLES and k not in out})
    return out


def typical_for(run: Run, typical: dict[str, float]) -> float | None:
    return typical.get(typical_key(run.mode, run.harness, run.difficulty or "medium")) or typical.get(typical_key(run.mode, run.harness))


def run_progress(run: Run, store: Store) -> dict[str, Any]:
    """The newest assistant text in a run's stream, the spend so far priced with the harness's
    table (None when the table has no entry for the model) and the tokens so far."""
    if not run.harness:
        return {"said": "", "cost_usd": None, "tokens": 0}
    stdout = run.stdout_text()
    if not stdout:
        return {"said": "", "cost_usd": None, "tokens": 0}
    p = store.config.harness(run.harness).progress(stdout, model=run.model)
    return {"said": _clip(p["said"]), "cost_usd": p["cost_usd"], "tokens": p["tokens"]}


def finished_verdict(run: Run) -> str:
    """What a strip says where its spend was once the run has ended: a review's verdict,
    else the status, with the cost when one was recorded."""
    verdict = str((run.result or {}).get("verdict") or "")
    if run.mode in REVIEW_MODES and verdict:
        head = verdict.replace("_", " ")
        met, total = criteria_counts((run.result or {}).get("criteria"))
        if total:
            head += f" · {met} of {total} criteria"
    elif run.status == "failed" and run.exit_code not in (None, 0):
        head = f"failed · exit {run.exit_code}"
    else:
        head = str((run.result or {}).get("status") or run.status)
    return head + (f" · {money(run.cost_usd)}" if run.cost_usd is not None else "")


def strip_for_run(run: Run, tasks: dict[str, Any], store: Store, typical: dict[str, float],
                  stage: str = "") -> dict[str, Any]:
    """One Now strip for a run record, running or just finished."""
    t = tasks.get(run.task_id)
    finished = run.status != "running"
    # A record with no pid and no output was written at dispatch and never launched; the
    # scheduler counts it against a slot until a tick reaps it, so the page shows it as what
    # it is rather than as a run that has said nothing for an hour.
    no_process = not finished and run.pid is None and not (run.path / "stdout.json").exists()
    p = run_progress(run, store)
    out = {
        "kind": "run", "state": "finishing" if finished else "running", "task": run.task_id,
        "title": t.title if t else "", "run": run.run_id, "mode": run.mode, "stage": stage,
        "harness": run.harness, "model": run.model, "difficulty": run.difficulty,
        "started_at": iso_utc(run.started_at), "elapsed_s": int(round(run.elapsed_minutes() * 60)),
        "typical_s": typical_for(run, typical), "said": p["said"], "spend_usd": p["cost_usd"],
        "tokens_so_far": p["tokens"], "no_process": no_process,
        "glyph": MODE_GLYPH.get(run.mode, "running"), "dot": MODE_DOT.get(run.mode, "running"),
    }
    if finished:
        out["verdict"] = finished_verdict(run)
        out["glyph"] = FINISHED_GLYPH.get(run.status, "done")
        out["dot"] = "failed" if run.status in ("failed", "timeout") else out["dot"]
        if run.status in ("failed", "timeout"):
            out["state"] = "failed"
    return out


def strips_in_flight(runs: RunStore, tasks: dict[str, Any], events: list[dict[str, Any]], store: Store,
                     now: dt.datetime) -> list[dict[str, Any]]:
    """Every run in flight as a strip, newest dispatch first, a task's runs kept together."""
    typical = typical_seconds(runs.all_runs(), now)
    stage_of_run = {e.get("run"): str(e.get("stage") or "") for e in events if e.get("kind") == "dispatch" and e.get("stage")}
    out = [strip_for_run(r, tasks, store, typical, stage_of_run.get(r.run_id, ""))
           for r in runs.active() if r.runner != "manual"]  # the scheduler's `active_runs` rule
    newest: dict[str, str] = {}
    for s in out:
        newest[s["task"]] = max(newest.get(s["task"], ""), s["started_at"])
    out.sort(key=lambda s: s["started_at"])
    out.sort(key=lambda s: newest[s["task"]], reverse=True)
    return out


def cards_needing_a_hand(tasks: dict[str, Any], state: Any, control: dict[str, Any]) -> list[dict[str, Any]]:
    """Held merges, needs-you cards and paused harnesses, in the Inbox's words."""
    out = []
    for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
        st = state.get(t.id)
        info = needs_human_info(st.get("needs_human")) if not t.status.terminal else None
        if t.status == Status.IN_REVIEW and (info or st.get("review_decision") == "changes_requested"):
            reason = (info or {}).get("reason") or "a person requested changes on the PR"
            out.append({"kind": "held", "state": "held", "task": t.id, "title": t.title, "reason": reason,
                        "glyph": "in_review", "dot": "changes_requested"})
        elif t.status == Status.WAITING_HUMAN:
            out.append({"kind": "needs_you", "state": "needs_you", "task": t.id, "title": t.title,
                        "reason": str(st.get("question") or ""), "glyph": "waiting_human", "dot": "waiting_human"})
        elif info:
            out.append({"kind": "needs_you", "state": "needs_you", "task": t.id, "title": t.title,
                        "reason": info.get("reason", ""), "glyph": "waiting_human", "dot": "waiting_human"})
    for name, entry in (control.get("paused_harnesses") or {}).items():
        out.append({"kind": "paused", "state": "paused", "task": "", "title": f"{name} harness paused",
                    "reason": f"{entry.get('reason', '')} · since {str(entry.get('at', ''))[11:16]}Z · the probe resumes it on its own",
                    "glyph": "blocked", "dot": "blocked"})
    return out


# ---- next -----------------------------------------------------------------------------

def dispatch_lines(sched: Any) -> list[dict[str, Any]]:
    """The queue the next pass takes (`Scheduler.dispatch_queue`), each line with the harness
    and model it would get and the reason the tick would skip it, if one applies."""
    phases = {ph.key: ph for p in sched.store.products() for ph in p.phases}
    out = []
    for pos, (t, mode, why) in enumerate(sched.dispatch_queue(), 1):
        runner = sched.runner_for(t)
        hname = runner.harness.name if runner.harness else ""
        model = sched.model_for(t, runner, "easy" if mode == "rebase" else "")
        skip = ""
        ph = phases.get(t.key)
        budget = sched.budget_for(t)
        if ph is not None and phase_refusal(ph, t):
            skip = "phase closed" if ph.closed else "phase frozen"
        elif budget and sched.spent_for(t.key) >= budget:
            skip = "over budget"
        elif not runner.detached:
            skip = "manual runner"
        elif hname and sched.is_harness_paused(hname):
            skip = "harness paused"
        out.append({"pos": pos, "task": t.id, "title": t.title, "mode": mode, "why": why, "difficulty": t.difficulty,
                    "harness": hname, "model": model, "skip": skip, "status": t.status.value})
    return out


def merge_queue(store: Store, tasks: dict[str, Any], state: Any, events: list[dict[str, Any]],
                strips: list[dict[str, Any]], control: dict[str, Any], review_busy: int, review_slots: int) -> dict[str, Any]:
    """The merge queue's head and candidates, every other PR in review, and the reviews
    waiting for a slot, each with the fact that says why it sits where it does."""
    view = merge_queue_view(store, state, events) or {"head": None, "candidates": [], "last_drop": None}
    queued = {c["task"] for c in view["candidates"]} | ({view["head"]["task"]} if view["head"] else set())
    reviewing = {s["task"] for s in strips if s.get("mode") in REVIEW_MODES}
    max_rounds = int((store.config.get("review") or {}).get("max_rounds", 4) or 4)
    paused = set((control.get("paused_harnesses") or {}).keys())
    review_harness = str((store.config.get("review") or {}).get("harness") or store.config.get("harness") or "claude")
    in_review, waiting = [], []
    for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
        st = state.get(t.id)
        if t.status == Status.IN_REVIEW and t.id not in queued:
            in_review.append({"task": t.id, "title": t.title, "pr": t.pr, "round": int(st.get("review_rounds", 0) or 0),
                              "max_rounds": max_rounds, "checks": str(st.get("checks") or "").lower() or "pending",
                              "reviewing": t.id in reviewing, "blocked": str(st.get("automerge_blocked") or "")})
        for item in st.get("pending_reviews") or []:
            name = str(item.get("kind", "review")) + (f":{item['name']}" if item.get("name") else "")
            why = "harness paused" if review_harness in paused else f"no review slot ({review_busy} of {review_slots} busy)"
            waiting.append({"task": t.id, "title": t.title, "what": name, "why": why})
    return {"head": view["head"], "candidates": view["candidates"], "last_drop": view["last_drop"],
            "in_review": in_review, "waiting": waiting}


# ---- where we are ---------------------------------------------------------------------

def goal_marks(text: str, tasks: dict[str, Any]) -> list[dict[str, Any]]:
    """One line per numbered goal under `## Goals`: its label (the first bold phrase, else the
    first sentence), the task ids it names and the mark those tasks earn: in fruit when every
    named task is done, in leaf when any is in flight, a seed otherwise; a goal that names no
    task is `unlinked`, because the goals text is the source and the design invents no link."""
    m = re.search(r"^## Goals\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return []
    items: list[list[str]] = []
    for line in m.group(1).splitlines():
        if re.match(r"^\s{0,3}\d+\.\s", line):
            items.append([line])
        elif items and line.strip():
            items[-1].append(line)
    out = []
    for n, lines in enumerate(items, 1):
        body = "\n".join(lines)
        head = re.sub(r"^\s*\d+\.\s+", "", lines[0])
        bold = re.match(r"\*\*(.+?)\*\*", head)
        label = bold.group(1) if bold else re.split(r"(?<=[.!?])\s", head, maxsplit=1)[0]
        ids = sorted(set(re.findall(r"\b[A-Z]{2,}-\d{3,}\b", body)))
        known = [tasks[i] for i in ids if i in tasks and tasks[i].status != Status.CANCELLED]
        if not known:
            mark, word = "", "unlinked"
        elif all(t.status == Status.DONE for t in known):
            mark, word = "done", "merged"
        elif any(t.status.value in ("running", "in_review", "changes_requested", "awaiting_triage", "merged_into_parent", "waiting_human") for t in known):
            mark, word = "running", "in flight"
        else:
            mark, word = "draft", "not started"
        out.append({"n": n, "label": label.rstrip(". "), "ids": [t.id for t in known],
                    "done": sum(1 for t in known if t.status == Status.DONE), "total": len(known), "mark": mark, "word": word})
    return out


def stage_word_for(fraction: float) -> str:
    """The growth-stage word for a phase's merged fraction: 0 seed, under a quarter sprout,
    under half in leaf, under three quarters in bud, under all in flower, all in fruit."""
    if fraction >= 1.0:
        return "in fruit"
    if fraction <= 0.0:
        return "seed"
    word = "sprout"
    for lower, name in STAGE_BANDS:
        if fraction >= lower:
            word = name
    return word


def phase_sheet(ph: Any, tasks: dict[str, Any], sched: Any, strips: list[dict[str, Any]],
                events: list[dict[str, Any]], spent: dict[str, float]) -> dict[str, Any]:
    in_scope = [t for t in ph.tasks if t.status != Status.CANCELLED]
    ids = {t.id for t in in_scope}
    done = sum(1 for t in in_scope if t.status == Status.DONE)
    fraction = done / len(in_scope) if in_scope else 0.0
    dispatches = [e["at"] for e in events if e.get("kind") == "dispatch" and e.get("task") in ids]
    return {
        "key": ph.key, "product": ph.product, "name": ph.name, "plant": ph.plant, "plate": ph.plate,
        "latin": ph.latin, "common": ph.common, "note": plant_info(ph.plant).get("note", ""),
        "closed": ph.closed, "frozen": ph.frozen,
        "done": done, "total": len(in_scope),
        "prs_open": sum(1 for t in in_scope if t.pr and t.status != Status.DONE),
        "running": sum(1 for s in strips if s.get("task") in ids and s.get("kind") == "run"),
        "fraction": round(fraction, 4), "stage_word": stage_word_for(fraction),
        "spent": round(spent.get(ph.key, 0.0), 2), "budget": sched.budget_for(ph.key) or None,
        "retro": sched.retro_verdict(ph.key),
        "goals": goal_marks(goals_text(ph.goals_path), tasks),
        "first_dispatch": min(dispatches) if dispatches else "",
    }


# ---- the last period ------------------------------------------------------------------

def resolve_window(key: str, now: dt.datetime, phase_start: str = "") -> tuple[str, str, str]:
    """A window key -> (since, bucket, key actually used). `today` is UTC midnight as the
    Costs page defines it; `phase` starts at the current phase's first dispatch and falls back
    to the last 24 hours when there is no open phase with a dispatch."""
    if key == "phase" and not phase_start:
        key = "24h"
    if key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), "hour", key
    if key == "24h":
        return (now - dt.timedelta(hours=24)).replace(microsecond=0).isoformat(), "hour", key
    if key == "phase":
        return phase_start, "day", key
    return (now - dt.timedelta(hours=1)).replace(microsecond=0).isoformat(), "hour", "hour"


def _shade_column(rows: list[dict[str, Any]], field: str) -> None:
    """The runs-by-model table's `heat` down its mean column (lower is better)."""
    if not rows:
        return
    values = [r[field] for r in rows]
    lo, hi = min(values), max(values)
    for r in rows:
        r["heat"] = round((r[field] - lo) / (hi - lo), 3) if hi > lo else 0.0


def period(events: list[dict[str, Any]], op_events: list[dict[str, Any]], tasks: dict[str, Any],
           since: str, bucket: str) -> dict[str, Any]:
    """The last period's figures, every one a function of the window's events: merged tasks,
    first-pass approval, cost (runs plus the operator ledger, so it agrees with the Costs
    page), cost per accepted task, runs by harness and model, hand steps, the cost-by-activity
    series with its annotations, throughput per bucket and the difficulty-by-model tables."""
    window = [e for e in events if str(e.get("at") or "") >= since]
    done_at: dict[str, str] = {}
    for e in window:
        if e.get("kind") == "transition" and e.get("to") == "done" and e.get("task"):
            done_at[e["task"]] = e["at"]
    first_review: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("kind") == "review" and e.get("task") and e["task"] not in first_review:
            first_review[e["task"]] = e
    reviewed = [e for e in first_review.values() if e["at"] >= since]
    approved = sum(1 for e in reviewed if e.get("verdict") == "approve")
    cost_by_task: dict[str, float] = defaultdict(float)
    for e in events:
        if e.get("kind") == "run_finished":
            cost_by_task[str(e.get("task") or "")] += float(e.get("cost_usd") or 0.0)
    finished = [e for e in window if e.get("kind") == "run_finished"]
    op_window = [e for e in op_events if str(e.get("at") or "") >= since]
    cost = sum(float(e.get("cost_usd") or 0.0) for e in finished + op_window)
    accepted_cost = sum(cost_by_task[t] for t in done_at)
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"runs": 0, "cost": 0.0})
    for e in finished:
        key = f"{e.get('harness') or 'garden'}:{e.get('model') or e.get('mode')}"
        by_model[key]["runs"] += 1
        by_model[key]["cost"] += float(e.get("cost_usd") or 0.0)
    rows = [{"who": k, "runs": int(v["runs"]), "cost": round(v["cost"], 2), "mean": round(v["cost"] / v["runs"], 2)}
            for k, v in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"])]
    _shade_column(rows, "mean")
    series = cost_series(events + op_events, tasks, since=since, bucket=bucket, group_by="activity")
    buckets = [b["bucket"] for b in series.get("buckets") or []]
    per_bucket = Counter(bucket_key(e["at"], bucket) for e in finished)
    return {
        "since": since, "bucket": bucket, "merged": len(done_at), "merged_ids": sorted(done_at),
        "first_pass": {"approved": approved, "reviewed": len(reviewed)},
        "cost": round(cost, 2), "per_accepted": round(accepted_cost / len(done_at), 2) if done_at else None,
        "runs": len(finished), "hand_steps": sum(1 for e in window if e.get("kind") in HAND_KINDS),
        "hand_kinds": dict(Counter(e["kind"] for e in window if e.get("kind") in HAND_KINDS)),
        "by_model": rows,
        "tiers": difficulty_by_model(events, tasks, since),
        "series": series, "throughput": [per_bucket.get(b, 0) for b in buckets],
        "annotations": [{"at": e["at"], "from": e.get("from") or "", "to": e.get("to") or "", "kind": e["kind"], "changed": e.get("keys") or []}
                        for e in window if e.get("kind") in ("profile_changed", "config_reloaded")],
    }


# ---- the snapshot ---------------------------------------------------------------------

def snapshot(store: Store, sched: Any, window: str = "hour", now: dt.datetime | None = None,
             tick: dict[str, Any] | None = None) -> dict[str, Any]:
    """The whole page as one dict: `garden` (slots, pause, the tick), `now` (strips and cards),
    `next` (the dispatch and merge queues), `where` (phase sheets) and `period` (the window's
    figures). `tick` is the hub's last pass (`at`, `next_at`), or None outside the web app."""
    now = now or dt.datetime.now(dt.UTC)
    cfg = store.config
    tasks = store.tasks()
    state = sched.state
    runs: RunStore = sched.runs
    events = EventLog(cfg.garden_dir / "events.jsonl").read()
    op_events = ops.to_cost_events(ops.read_records(ops.default_path(store.root)))
    control = state.get("_control")
    spent: dict[str, float] = defaultdict(float)
    for r in runs.all_runs():
        if r.task_id in tasks:
            spent[tasks[r.task_id].key] += float(r.cost_usd or 0.0)

    strips = strips_in_flight(runs, tasks, events, store, now)
    worker_busy = sum(1 for s in strips if s["mode"] in WORKER_MODES | CHECK_MODES)
    worker_without_process = sum(1 for s in strips if s["mode"] in WORKER_MODES | CHECK_MODES and s["no_process"])
    review_busy = sum(1 for s in strips if s["mode"] in REVIEW_MODES)
    max_parallel = sched.effective_max_parallel()
    review_parallel = sched.review_parallel_limit()
    hands = cards_needing_a_hand(tasks, state, control)

    open_phases = [ph for p in store.products() for ph in p.phases if not ph.closed]
    sheets = [phase_sheet(ph, tasks, sched, strips, events, spent) for ph in open_phases]
    sheets.sort(key=lambda s: (-s["running"], s["key"]))
    closed = [phase_sheet(ph, tasks, sched, strips, events, spent) for p in store.products() for ph in p.phases if ph.closed]
    closed.sort(key=lambda s: s["closed"], reverse=True)
    primary = sheets[0] if sheets else None

    stack = bool(cfg.get("stack", True))
    drafts = sum(1 for t in tasks.values() if t.status == Status.DRAFT and effective_status(t, tasks, stack) == "draft")
    since, bucket, window_used = resolve_window(window, now, (primary or {}).get("first_dispatch") or "")
    tick = tick or {}
    paused = control.get("dispatch") == "paused"
    return {
        "captured_at": now.replace(microsecond=0).isoformat(),
        "window": window_used,
        "windows": WINDOWS,
        "garden": {"name": str(cfg.get("name") or store.root.name), "tick_interval": int(cfg.get("tick_interval", 60) or 60),
                   "last_tick": str(tick.get("at") or ""), "next_tick_at": str(tick.get("next_at") or ""),
                   "max_parallel": max_parallel, "review_parallel": review_parallel,
                   "worker_busy": worker_busy, "worker_without_process": worker_without_process, "review_busy": review_busy,
                   "free": max(max_parallel - worker_busy, 0),
                   "dispatch_paused": {k: str(control.get(k) or "") for k in ("by", "at", "reason")} if paused else None,
                   "drafts": drafts, "inbox_decisions": len(hands)},
        "now": strips + hands,
        "next": {"dispatch": dispatch_lines(sched),
                 "merge": merge_queue(store, tasks, state, events, strips, control, review_busy, review_parallel)},
        "where": {"primary": primary, "others": sheets[1:], "closed": closed},
        "period": period(events, op_events, tasks, since, bucket),
    }


# ---- the text view (`garden now --page 1`) --------------------------------------------

def render_text(snap: dict[str, Any]) -> str:
    """The same four regions as plain text, one line per strip, queue line, goal and figure."""
    g = snap["garden"]
    runs = [s for s in snap["now"] if s["kind"] == "run"]
    hands = [s for s in snap["now"] if s["kind"] != "run"]
    out: list[str] = []
    if g["dispatch_paused"]:
        p = g["dispatch_paused"]
        out.append(f"Dispatch paused by {p['by']} since {p['at'][11:16]}Z" + (f": {p['reason']}" if p["reason"] else ""))
    slots = f"{g['worker_busy']} of {g['max_parallel']} worker slots"
    if g["worker_without_process"]:
        slots += f" ({g['worker_without_process']} without a process)"
    out.append(f"NOW  {len(runs)} run{'s' if len(runs) != 1 else ''} in flight on {slots} and {g['review_busy']} of {g['review_parallel']} review slots")
    for s in runs:
        who = f"{s['harness']} {s['model']}".strip() if s["harness"] else "token-free"
        line = f"  {s['task']:<8} {s['mode']:<8} {who:<28} {clock(s['elapsed_s']):>9}"
        if s["no_process"]:
            line += "  no process recorded · a slot is held until a tick reaps it"
        else:
            if s["typical_s"]:
                line += f" · typically {minutes(s['typical_s'])}" + (" · longer than usual" if s["elapsed_s"] > s["typical_s"] else "")
            if "verdict" in s:
                line += f" · {s['verdict']}"
            elif s["spend_usd"] is not None:
                line += f" · {money(s['spend_usd'])} so far"
            elif s["tokens_so_far"]:
                line += f" · {ktok(s['tokens_so_far'])} tokens so far"
            if s["said"]:
                line += f'\n           "{s["said"]}"'
        out.append(line)
    if not runs:
        nxt = snap["next"]["dispatch"]
        if nxt:
            out.append(f"  Nothing running. The next tick dispatches {', '.join(q['task'] for q in nxt[:2])} into {g['free']} free slot{'s' if g['free'] != 1 else ''}.")
        else:
            out.append(f"  The garden is quiet. Nothing is ready: {g['drafts']} drafts wait for approval, {len(hands)} cards wait on you.")
    for s in hands:
        out.append(f"  [{s['state'].replace('_', ' ')}] {s['task'] + ' ' if s['task'] else ''}{s['title']}: {s['reason']}")
    out.append("")
    nxt = snap["next"]
    tick_line = f"next tick at {g['next_tick_at'][11:19]}Z" if g["next_tick_at"] else "no scheduler loop"
    out.append(f"NEXT  {tick_line} · {g['free']} slot{'s' if g['free'] != 1 else ''} free")
    for q in nxt["dispatch"]:
        gets = q["skip"] or f"{q['harness']} {q['model']}".strip()
        out.append(f"  {q['pos']:>2}. {q['task']:<8} {q['title'][:60]:<60} {q['why']} · {q['mode']} · {q['difficulty']} → {gets}")
    if not nxt["dispatch"]:
        out.append("  Nothing queued. Approve a draft on the Board to add work.")
    mq = nxt["merge"]
    if mq["head"]:
        h = mq["head"]
        out.append(f"  merge head {h['task']}: rebased, waiting for its rollup · " + (f"CI {h['checks'] or 'pending'}" if h["waits_on_ci"] else "CI green: merges on the next tick"))
    for c in mq["candidates"]:
        out.append(f"  queued {c['task']}: " + (f"held: {c['blocked']}" if c["blocked"] else f"since {c['ready_at'][11:16]}Z"))
    for r in mq["in_review"]:
        fact = f"round {r['round']} of {r['max_rounds']} · CI {r['checks']}" + (" · review running" if r["reviewing"] else "") + (f" · {r['blocked']}" if r["blocked"] else "")
        out.append(f"  in review {r['task']}: {fact}")
    for w in mq["waiting"]:
        out.append(f"  review waiting {w['task']} ({w['what']}): {w['why']}")
    out.append("")
    p = snap["where"]["primary"]
    if p:
        out.append(f"WHERE  {p['key']} · plate {p['plate']} · {p['latin']} · {p['done']} of {p['total']} merged · {p['prs_open']} PRs open · {p['running']} running · {p['stage_word']}")
        if p["retro"]:
            out.append(f"  retro verdict {p['retro'].get('verdict', '')} · {p['retro'].get('status', '')}")
        for goal in p["goals"]:
            mark = f"{goal['done']} of {goal['total']} · {goal['word']}" if goal["mark"] else "unlinked"
            out.append(f"  {goal['n']}. {goal['label'][:70]:<70} {mark}")
    else:
        out.append("WHERE  no open phase: garden new-phase starts the next")
    for ph in snap["where"]["others"]:
        out.append(f"  also open: {ph['key']} · {ph['done']} of {ph['total']} merged" + (" · frozen" if ph["frozen"] else ""))
    for ph in snap["where"]["closed"]:
        out.append(f"  pressed: {ph['key']} · plate {ph['plate']} · closed {ph['closed']}")
    out.append("")
    w = snap["period"]
    label = dict(WINDOWS).get(snap["window"], snap["window"])
    out.append(f"THE LAST PERIOD  {label} (since {w['since'][:16]}Z)")
    if not (w["runs"] or w["merged"]):
        out.append("  No runs finished in this window.")
        return "\n".join(out) + "\n"
    fp = w["first_pass"]
    first = f"{round(100 * fp['approved'] / fp['reviewed'])} % ({fp['approved']} of {fp['reviewed']})" if fp["reviewed"] else "—"
    out.append(f"  merged {w['merged']}" + (f" ({', '.join(w['merged_ids'][:4])}{' and more' if len(w['merged_ids']) > 4 else ''})" if w["merged_ids"] else ""))
    out.append(f"  first-pass approval {first} · cost {money(w['cost'])} over {w['runs']} runs · per accepted task {money(w['per_accepted'])}")
    out.append(f"  hand steps {w['hand_steps']}" + (" (" + ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in w["hand_kinds"].items()) + ")" if w["hand_kinds"] else ""))
    if w["throughput"]:
        out.append(f"  runs finished per {w['bucket']}: " + " ".join(str(n) for n in w["throughput"]))
    for row in w["by_model"]:
        out.append(f"  {row['who']:<30} {row['runs']:>3} runs  {money(row['cost']):>8}  mean {money(row['mean'])}")
    for a in w["annotations"]:
        out.append(f"  {a['at'][11:16]}Z {a['kind'].replace('_', ' ')}" + (f": {', '.join(a['changed'])}" if a["changed"] else "") + (f": {a['from'] or '(none)'} → {a['to']}" if a["to"] else ""))
    tiers = w["tiers"]
    if tiers["models"]:
        from .events import format_cell

        out.append(f"  by difficulty and model (value · n; * best, - worst, ~ under {tiers['thin']} samples)")
        for m in tiers["metrics"]:
            out.append(f"    {m['label']:<24} " + "".join(f"{model:>22}" for model in tiers["models"]))
            for d in ("easy", "medium", "hard"):
                cells = m["rows"][d]
                row = ""
                for model in tiers["models"]:
                    c = cells.get(model)
                    if not c:
                        row += f"{'—':>22}"
                    else:
                        mark = "~" if c["thin"] else ("*" if c["best"] else ("-" if c["worst"] else ""))
                        row += f"{format_cell(m['unit'], c['value']) + ' · ' + str(c['n']) + mark:>22}"
                out.append(f"    {d:<24} {row}")
    else:
        out.append("  No model did work in this window.")
    return "\n".join(out) + "\n"


# ---- the live stream ------------------------------------------------------------------

def sse(event: str, data: Any) -> str:
    """One server-sent-events message."""
    return f"event: {event}\ndata: {json.dumps(data, sort_keys=True)}\n\n"


def tail_lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """The event lines appended to events.jsonl since byte `offset`, parsed, and the new
    offset. A partial last line (a write in progress) is left for the next read."""
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if size < offset:
        offset = 0  # the log was rewritten (a cost backfill): start over from its head
    if size == offset:
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    if not chunk.endswith(b"\n"):
        cut = chunk.rfind(b"\n") + 1
        chunk, size = chunk[:cut], offset + cut
    out = []
    for raw in chunk.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out, size


def progress_messages(runs: RunStore, store: Store, seen: dict[str, float]) -> list[dict[str, Any]]:
    """A progress message for each active run whose stdout.json moved since `seen` (run id ->
    mtime, updated in place): its task, run, latest words, spend, tokens and elapsed seconds."""
    out = []
    for r in runs.active():
        if r.runner == "manual":
            continue
        try:
            mtime = (r.path / "stdout.json").stat().st_mtime
        except OSError:
            continue
        if seen.get(r.run_id) == mtime:
            continue
        seen[r.run_id] = mtime
        p = run_progress(r, store)
        out.append({"task": r.task_id, "run": r.run_id, "said": p["said"], "cost_usd": p["cost_usd"],
                    "tokens": p["tokens"], "elapsed_s": int(round(r.elapsed_minutes() * 60))})
    return out


def stream(store: Store, tick_state: Callable[[], dict[str, Any]], *, start: int | None = None,
           limit: int | None = None, deadline: float | None = None, interval: float = 1.0,
           progress_every: float = 10.0, sleep: Callable[[float], None] = time.sleep) -> Iterator[str]:
    """The Now page's live messages, tailed from events.jsonl and the run records: `event`
    (one log line), `progress` (a run's words and spend, every `progress_every` seconds and
    only when its stream moved) and `tick` (the hub's last pass, when its `seq` changes). The
    tail starts at the log's end unless `start` gives a byte offset; `limit` messages or a
    monotonic `deadline` end it (a test's, and a client that went away is ended by the server).
    Nothing here takes a lock or touches the tick's path."""
    path = store.config.garden_dir / "events.jsonl"
    offset = (path.stat().st_size if path.exists() else 0) if start is None else start
    runs = RunStore(store.config.garden_dir)
    seen_tick = tick_state().get("seq")
    mtimes: dict[str, float] = {}
    sent = 0
    last_progress = 0.0
    last_alive = time.monotonic()
    while True:
        pending: list[str] = []
        lines, offset = tail_lines(path, offset)
        pending += [sse("event", ev) for ev in lines]
        ts = tick_state()
        if ts.get("seq") != seen_tick:
            seen_tick = ts.get("seq")
            pending.append(sse("tick", ts))
        if time.monotonic() - last_progress >= progress_every:
            last_progress = time.monotonic()
            pending += [sse("progress", p) for p in progress_messages(runs, store, mtimes)]
        for msg in pending:
            yield msg
            sent += 1
            last_alive = time.monotonic()
            if limit and sent >= limit:
                return
        if deadline is not None and time.monotonic() >= deadline:
            return
        if time.monotonic() - last_alive >= 15:
            yield ": keep-alive\n\n"
            last_alive = time.monotonic()
        sleep(interval)
