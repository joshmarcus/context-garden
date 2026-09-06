"""Now 2's read model. No scheduler actions, network calls or state writes."""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict
from statistics import median

from . import operator_spend as ops
from .costs import cost_series
from .events import EventLog, metrics, with_run_records
from .graph import blockers
from .inbox import build_inbox, decisions
from .model import Status, goals_text, phase_refusal
from .outcomes import format_cell, timestamp
from .runs import Run, RunStore
from .scheduler import Scheduler
from .scheduler.selection import worker_candidates
from .store import Store

WINDOWS = {"hour": "Last hour", "today": "Today (UTC)", "24h": "Last 24 hours", "phase": "This phase"}


def window_bounds(window: str, now: dt.datetime, phase_start: str = "") -> tuple[str, str]:
    now = now.astimezone(dt.UTC)
    start = {"hour": now - dt.timedelta(hours=1), "24h": now - dt.timedelta(hours=24),
             "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
             "phase": timestamp(phase_start) or now}[window]
    return min(start, now).isoformat(), now.isoformat()


def typical_durations(runs: list[Run], now: dt.datetime) -> dict[tuple[str, str], dict]:
    samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in runs:
        start, end = timestamp(r.started_at), timestamp(r.finished_at)
        if r.status == "done" and start and end and now - dt.timedelta(days=30) <= end < now and end >= start:
            samples[r.mode, r.difficulty].append((end-start).total_seconds())
    return {k: {"seconds": median(v) if len(v) >= 3 else None, "n": len(v)} for k, v in samples.items()}


def output_tail(run: Run) -> str:
    """Read at most 32 KiB of output; never parse a full growing transcript per card."""
    try:
        with (run.path / "stdout.json").open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            f.seek(max(0, end - 32768))
            raw = f.read(32768).decode("utf-8", errors="replace")
    except OSError:
        return ""
    return raw


def latest_line(run: Run) -> str:
    for line in reversed(output_tail(run).splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        item = e.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
            return str(item["text"])[-2000:]
        if e.get("type") == "assistant":
            message = e.get("message") or {}
            content = message.get("content", []) if isinstance(message, dict) else []
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                return " ".join(texts)[-2000:]
        if e.get("type") == "result" and e.get("result"):
            return str(e["result"])[-2000:]
    return "No assistant output reported yet"


def running_rows(runs: list[Run], tasks: dict, now: dt.datetime, store: Store | None = None) -> list[dict]:
    typical = typical_durations(runs, now)
    rows = []
    for r in sorted(runs, key=lambda r: (r.started_at, r.task_id, r.run_id)):
        end = timestamp(r.finished_at)
        if r.status != "running" and not (end and 0 <= (now-end).total_seconds() <= 8):
            continue
        spend, freshness = r.cost_usd, "run metadata"
        if store and r.status == "running" and r.harness:
            try:
                parsed = store.config.harness(r.harness).parse(output_tail(r), model=r.model)
                if parsed.get("cost_usd") is not None:
                    spend = parsed["cost_usd"]
                    freshness = "latest reported usage"
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        title_url = f"/tasks/{r.task_id}" if r.task_id in tasks else f"/runs/{r.task_id}/{r.run_id}"
        rows.append({"title_url": title_url, "spend": spend, "freshness": freshness, "run": r, "title": tasks[r.task_id].title if r.task_id in tasks else f"{r.mode.title()} run",
                     "said": latest_line(r), "typical": typical.get((r.mode, r.difficulty), {"seconds": None, "n": 0}),
                     "verdict": str(r.result.get("verdict") or r.result.get("status") or r.status),
                     "missing_process": r.status == "running" and r.pid is not None and r.process_finished()})
    return rows


def next_queues(s: Store, sched: Scheduler) -> dict:
    tasks = s.tasks()
    phases = {ph.key: ph for p in s.products() for ph in p.phases}
    workers, waiting = [], []
    candidates = worker_candidates(tasks, sched.state, int(s.config.get("max_revisions", 3)),
                                   sched.stack_enabled, sched._edit_pending)
    spent = {key: sched.spent_for(key) for key in phases}
    for task, mode in candidates:
        runner = sched.runner_for(task)
        harness = runner.harness.name if runner.harness else ""
        ph = phases.get(task.key)
        budget = sched.budget_for(task)
        reason = (phase_refusal(ph, task) if ph else "")
        reason = reason or ("Dispatch paused" if sched.is_dispatch_paused() else "")
        reason = reason or ("Phase budget reached" if budget and spent.get(task.key, 0) >= budget else "")
        reason = reason or ("Manual worker: waiting to be taken" if not runner.detached else "")
        reason = reason or (f"{harness} paused" if sched.is_harness_paused(harness) else "")
        row = {"task": task, "mode": mode, "reason": reason or {
            "rebase": "Rebase before revisions; unblocks a merge", "revise": "Revision before new work",
            "work": "Ready in scheduler priority and task order; waiting for a worker"}[mode]}
        (waiting if reason else workers).append(row)
    candidates_ids = {t.id for t, _ in candidates}
    for task in tasks.values():
        if task.status == Status.READY and task.id not in candidates_ids:
            deps = blockers(task, tasks, sched.stack_enabled)
            waiting.append({"task": task, "mode": "work", "reason": "Blocked by " + ", ".join(tasks[d].title for d in deps)
                            if deps else "Pending task edits"})
    merges, reviews = [], []
    for task in tasks.values():
        if task.status.terminal:
            continue
        st = sched.state.get(task.id)
        if st.get("automerge_candidate") or st.get("merge_head"):
            merges.append({"task": task, "mode": "merge", "head": bool(st.get("merge_head")),
                           "at": st.get("automerge_ready_at", ""), "reason": st.get("automerge_blocked") or (
                               "Queue head; awaiting rollup" if st.get("merge_head") else "Oldest eligible merge first"),
                           "checks": st.get("checks") or "not reported", "round": st.get("review_rounds", 0)})
        elif st.get("automerge_blocked"):
            waiting.append({"task": task, "mode": "merge", "reason": st["automerge_blocked"]})
        for review in st.get("pending_reviews") or []:
            harness = sched.resolved_harness_name(task, str(s.config.get("review.harness") or ""))
            reason = ("Waiting for this task's worker" if any(r.task_id == task.id for r in sched.worker_runs_active()) else
                      f"{harness} paused" if sched.is_harness_paused(harness) else
                      "Waiting for a review slot" if sched.review_slots_free() <= 0 else "Waiting for the next scheduler pass")
            reviews.append({"task": task, "mode": review.get("kind", "review"), "reason": reason})
    merges.sort(key=lambda r: (not r["head"], r["at"], r["task"].id))
    return {"workers": workers, "waiting": waiting, "merges": merges, "reviews": reviews,
            "worker_busy": len(sched.worker_runs_active()), "worker_limit": sched.effective_max_parallel(),
            "review_busy": len(sched.review_runs_active()), "review_limit": sched.review_parallel_limit()}


def phase_rows(s: Store, sched: Scheduler) -> list[dict]:
    out = []
    for p in s.products():
        for ph in p.phases:
            done = sum(t.status == Status.DONE for t in ph.tasks)
            total = len(ph.tasks)
            fraction = done / total if total else 0
            growth = ("fruit" if total and ph.closed and all(t.status.terminal for t in ph.tasks) else
                      "flower" if fraction >= .8 else "bud" if fraction >= .5 else
                      "leaf" if fraction >= .2 else "sprout" if done else "seed")
            text = goals_text(ph.goals_path)
            section = re.split(r"^## Goals\s*$", text, flags=re.M)
            goal_text = re.split(r"^## ", section[1], maxsplit=1, flags=re.M)[0] if len(section) > 1 else text
            labels = re.findall(r"^\d+\.\s+(.+)|^###\s+(.+)", goal_text, re.M)
            goals = [a or b for a, b in labels]
            goals = [re.match(r"\*\*(.+?)\*\*", g)[1] if re.match(r"\*\*(.+?)\*\*", g) else g for g in goals]
            out.append({"phase": ph, "done": done, "total": total, "stage": growth, "fraction": fraction,
                        "cancelled": sum(t.status == Status.CANCELLED for t in ph.tasks),
                        "wont_do": sum(t.status == Status.WONT_DO for t in ph.tasks), "goals": goals,
                        "verdict": sched.state.get("_retro_verdicts").get(ph.key) or {}})
    return out


def period_data(events: list[dict], tasks: dict, records: list[dict], since: str, until: str,
                phase: str = "") -> dict:
    events = with_run_records(events, [])
    selected = {k: t for k, t in tasks.items() if not phase or t.key == phase}
    scoped = [e for e in events if not phase or e.get("task") in selected or e.get("phase") == phase]
    matrices = metrics(scoped, selected, since, until)["difficulty_by_model"]
    # Convert cumulative operator spend before windowing, preserving earlier heartbeats.
    operator = ops.to_cost_events(records)
    def in_window(e: dict) -> bool:
        return bool(timestamp(e.get("at")) and timestamp(since) <= timestamp(e["at"]) < timestamp(until))
    window_events = [e for e in scoped if in_window(e)]
    series = cost_series(scoped + ([] if phase else operator), selected, since=since, until=until, group_by="activity")
    runs = {(e.get("task"), e.get("run") or e.get("at"), e.get("mode")): e for e in window_events if e.get("kind") == "run_finished"}
    rebases = Counter("mechanical" if e.get("how") == "mechanical" else "agent" for e in runs.values() if e.get("mode") == "rebase")
    annotations = [e for e in window_events if e.get("kind") in ("profile_changed", "config_reloaded", "config_override", "upgraded")]
    annotations += [{**e, "kind": "operator compacted"} for e in ops.compaction_marks(records) if in_window(e)]
    buckets = [0] * 12
    duration = max(1, (timestamp(until)-timestamp(since)).total_seconds())
    for annotation in annotations:
        annotation["position"] = max(0, min(100, (timestamp(annotation["at"])-timestamp(since)).total_seconds() / duration * 100))
    for at in matrices["accepted"].values():
        buckets[min(11, int((timestamp(at)-timestamp(since)).total_seconds()/duration*12))] += 1
    accepted = matrices["accepted_count"]
    # Legacy merge transitions record garden attribution in their note; other sources are unknown.
    merge_events = list({e["task"]: e for e in window_events
                         if e.get("task") in matrices["accepted"] and e.get("kind") == "transition"
                         and e.get("to") == "done" and timestamp(e["at"]) == timestamp(matrices["accepted"][e["task"]])}.values())
    hand = sum(e.get("source") == "human" for e in merge_events)
    unknown = sum(e.get("source") not in ("human", "garden") and "by the garden" not in str(e.get("note")) for e in merge_events)
    operator_total = sum(e["cost_usd"] for e in operator if in_window(e))
    total = cost_series(events + operator, tasks, since=since, until=until)["grand_total"]["cost_usd"]
    return {"since": since, "until": until, "matrices": matrices, "series": series,
            "runs": dict(Counter(f"{e.get('harness') or 'unknown harness'} / {e.get('model') or 'local / unknown model'}" for e in runs.values())),
            "annotations": sorted(annotations, key=lambda e: e["at"]), "buckets": buckets,
            "hand": hand, "hand_unknown": unknown,
            "rebase": {k: rebases[k] / accepted if accepted else None for k in ("mechanical", "agent")},
            "operator": operator_total, "operator_share": operator_total / total if total else None,
            "operator_scope": "All-garden operator spend and share (phase attribution unavailable; excluded from phase worker spend)" if phase else "All-garden ledger",
            "unpriced": sum(e.get("cost_usd") is None for e in runs.values()), "has_history": bool(scoped)}


def snapshot(s: Store, window: str = "hour", phase: str = "", now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.UTC)
    window = window if window in WINDOWS else "hour"
    sched = Scheduler(s, log=lambda _: None)
    tasks = s.tasks()
    all_runs = RunStore(s.config.garden_dir).all_runs()
    events = with_run_records(EventLog(s.config.garden_dir / "events.jsonl").read(), all_runs)
    phases = phase_rows(s, sched)
    phase = phase if any(p["phase"].key == phase for p in phases) else next((p["phase"].key for p in phases if not p["phase"].closed), "")
    starts = [e["at"] for e in events if e.get("task") in tasks and tasks[e["task"]].key == phase and e.get("kind") == "dispatch"]
    since, until = window_bounds(window, now, min(starts) if starts else "")
    inbox = build_inbox(s, sched)
    attention = decisions(inbox) + [a for a in inbox if a["group"] in ("harness", "budget")]
    for t in tasks.values():
        reason = sched.state.get(t.id).get("automerge_blocked")
        if reason and not t.status.terminal and not any(a.get("task") == t.id and reason in a["why"] for a in attention):
            existing = next((a for a in attention if a.get("task") == t.id), None)
            if existing:
                existing["why"] += f" · Merge held: {reason}"
            else:
                attention.append({"title": t.title, "why": f"Merge held: {reason}", "task": t.id})
    for a in attention:
        if a.get("group") == "harness":
            a["title"] += " paused"
    control = sched.control()
    if sched.is_dispatch_paused():
        attention.append({"title": "Dispatch paused", "why": control.get("reason") or "Paused by operator", "task": ""})
    queues = next_queues(s, sched)
    quiet = ("Dispatch paused" if sched.is_dispatch_paused() else "Harness paused" if sched.paused_harnesses() else
             "A decision needs you" if attention else "Ready; waiting for the next scheduler pass" if queues["workers"] else
             "Dependencies or task edits are blocking work" if queues["waiting"] else
             "Drafts awaiting approval" if any(t.status == Status.DRAFT for t in tasks.values()) else
             "No approved work" if tasks else "No tasks yet · plan a phase to begin")
    return {"server_now": now.isoformat(), "window": window, "phase": phase, "windows": WINDOWS,
            "running": running_rows(all_runs, tasks, now, s), "attention": attention, "quiet": quiet,
            "ever_run": bool(all_runs), "next": queues, "phases": phases,
            "period": period_data(events, tasks, ops.read_records(ops.default_path(s.root)), since, until, phase if window == "phase" else "")}


def text_view(data: dict) -> str:
    lines = ["Now", data["quiet"] if not data["running"] else ""]
    for row in data["running"]:
        r = row["run"]
        lines += [f"{row['title']} · {r.mode} · {r.harness} / {r.model} · {r.difficulty}",
                  f"  {r.elapsed_minutes():.1f} min · typical {row['typical']} · spend {row['spend'] if row['spend'] is not None else 'unknown'}",
                  f"  {row['said']} · {row['verdict']}"]
    lines += [f"{a['title']}: {a['why']}" for a in data["attention"]]
    lines += ["", "Next"]
    for name in ("workers", "merges", "reviews", "waiting"):
        lines.append(name.title())
        lines += [f"  {r['task'].title} · {r['mode']} · {r['reason']}" for r in data["next"][name]] or ["  None"]
    lines += ["", "Where we are"]
    for p in data["phases"]:
        lines.append(f"{p['phase'].key} · {p['stage']} · {p['done']}/{p['total']} · {p['verdict']}")
        lines += [f"  ○ {g} · Progress not mapped" for g in p["goals"]]
    p = data["period"]
    lines += ["", "The last period", f"{p['since']} → {p['until']}",
              f"{p['matrices']['accepted_count']} tasks merged · first pass {format_cell(p['matrices']['first_pass'])}",
              f"Recorded spend ${p['series']['grand_total']['cost_usd']:.2f} · activity {p['series']['totals']}",
              f"Cost / accepted {format_cell(p['matrices']['total_cost'])} · runs {p['runs']}",
              f"Throughput {p['buckets']} · annotations {p['annotations']}",
              f"Hand merges {p['hand']} known; {p['hand_unknown']} unattributed · rebases/merge {p['rebase']}",
              f"Operator ${p['operator']:.2f} · share {p['operator_share']} · {p['operator_scope']}"]
    for matrix in p["matrices"]["metrics"].values():
        lines.append(matrix["label"] + " · " + " | ".join(p["matrices"]["models"]))
        for tier, row in matrix["rows"].items():
            lines.append(tier + " | " + " | ".join(f"{format_cell(c)} n={c['n']} missing={c['missing']}" for c in row.values()))
    return "\n".join(lines)
