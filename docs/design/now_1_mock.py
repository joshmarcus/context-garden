"""Render the Now 1 mock (`docs/design/now-1.md`) from a real snapshot of a garden.

Two steps, so the mock can be re-rendered without the garden that produced it:

    .venv/bin/python docs/design/now_1_mock.py snapshot --garden /path/to/garden
    .venv/bin/python docs/design/now_1_mock.py render

`snapshot` reads the garden through the store, the state file, the run records and the
event log (read-only: nothing here imports a writer) and writes `now-1-snapshot.json`
beside this file. `render` turns that JSON into `now-1.html` beside it, inlining
`base.html`'s stylesheet and the plant drawings so the mock tracks the app's tokens.

This is a design tool, not part of the package: the build task lifts the data assembly
into `garden/now1.py` and the markup into a template.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "src"))

from garden import operator_spend as ops  # noqa: E402
from garden.charts import cost_stack_svg, sparkline_svg  # noqa: E402
from garden.costs import bucket_key, cost_series  # noqa: E402
from garden.events import EventLog  # noqa: E402
from garden.graph import effective_status, ready  # noqa: E402
from garden.harness import Harness, _usage_cost  # noqa: E402
from garden.inbox import merge_queue_view, needs_human_info  # noqa: E402
from garden.model import Status, dispatch_sort_key, goals_text, phase_refusal  # noqa: E402
from garden.plants import DEFS, plant_info, plant_svg, stage_svg, vine_svg  # noqa: E402
from garden.runs import Run, RunStore  # noqa: E402
from garden.scheduler.state import State  # noqa: E402
from garden.store import Store  # noqa: E402

SNAPSHOT = HERE / "now-1-snapshot.json"
TEMPLATE = HERE / "now-1.html.j2"
OUT = HERE / "now-1.html"
BASE_HTML = REPO / "src" / "garden" / "web" / "templates" / "base.html"
PLATES = REPO / "src" / "garden" / "web" / "static" / "plates"

WORKER_MODES = {"work", "revise", "resume", "trial", "rebase"}
CHECK_MODES = {"check"}
REVIEW_MODES = {"review", "persona", "compare"}
# The design's mode -> growth-stage glyph table (docs/design/now-page.md, Visual system).
MODE_GLYPH = {"work": "running", "revise": "running", "resume": "running", "trial": "running",
              "rebase": "ready", "edit": "ready", "check": "awaiting_triage",
              "review": "in_review", "persona": "in_review", "compare": "in_review"}
MODE_DOT = {"work": "running", "revise": "running", "resume": "running", "trial": "running",
            "rebase": "ready", "edit": "ready", "check": "ready",
            "review": "in_review", "persona": "in_review", "compare": "in_review"}
HAND_KINDS = ("answer", "triaged", "decision_accepted", "decision_resolved", "dispatch_paused",
              "dispatch_resumed", "resumed", "moved", "budget_set", "config_override", "suggestion")
STAGE_BANDS = ((0.0, "seed"), (0.25, "sprout"), (0.5, "in leaf"), (0.75, "in bud"), (1.0, "in flower"))


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def _clip(text: str, n: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ---- runs in flight -------------------------------------------------------------------

TYPICAL_STATUSES = {"done", "failed", "timeout", "blocked"}


def typical_key(mode: str, harness: str, difficulty: str = "") -> str:
    """A run's key into `typical_seconds`: mode and tier, and whether a harness ran it. A
    mechanical (token-free) rebase takes seconds and an agent rebase takes minutes; the two
    must not share a median."""
    who = harness or "-"
    return f"{mode}/{who}/{difficulty}" if difficulty else f"{mode}/{who}"


def typical_seconds(all_runs: list[Run], now: dt.datetime) -> dict[str, float]:
    """Median elapsed seconds per mode, harness-or-not and tier over the last seven days,
    falling back to mode and harness-or-not all-time; only keys with at least three runs
    that ran to an outcome (done, failed, timeout, blocked). A run that was cancelled,
    superseded or never started for an environment error says nothing about how long the
    work takes, so it is left out."""
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
    out = {k: statistics.median(v) for k, v in recent.items() if len(v) >= 3}
    out.update({k: statistics.median(v) for k, v in ever.items() if len(v) >= 3 and k not in out})
    return out


def progress(run: Run, harness_cfgs: dict[str, Any]) -> tuple[str, float | None, int]:
    """The newest assistant text in a run's stream, the spend so far priced with the
    harness's table (None when the table has no entry for the model) and the tokens read
    and written so far (what `Harness.progress` will do in the build)."""
    said, usage = "", {}
    harness = Harness(run.harness, harness_cfgs.get(run.harness) or {}) if run.harness else None
    for ev in run.stdout_events(n=None):
        kind = ev.get("type")
        if kind == "assistant":  # claude stream-json
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    said = str(block["text"])
            u = msg.get("usage") or {}
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
                usage[k] = usage.get(k, 0) + int(u.get(k) or 0)
        elif kind == "item.completed":  # codex jsonl
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                said = str(item["text"])
        elif kind == "turn.completed":
            u = ev.get("usage") or {}
            usage = {"input_tokens": max(0, int(u.get("input_tokens", 0)) - int(u.get("cached_input_tokens", 0))),
                     "cache_read_input_tokens": int(u.get("cached_input_tokens", 0)),
                     "cache_creation_input_tokens": int(u.get("cache_write_input_tokens", 0)),
                     "output_tokens": int(u.get("output_tokens", 0)) + int(u.get("reasoning_output_tokens", 0))}
    spend = None
    if harness is not None and usage:
        spend, _ = _usage_cost(usage, run.model, harness.cfg.get("prices") or {})
    first_line = next((ln for ln in said.splitlines() if ln.strip()), "")
    return _clip(first_line), spend, sum(int(v) for v in usage.values())


def strips_in_flight(runs: RunStore, tasks: dict, events: list[dict], harness_cfgs: dict, now: dt.datetime) -> list[dict]:
    typical = typical_seconds(runs.all_runs(), now)
    stage_of_run = {e.get("run"): e.get("stage") for e in events if e.get("kind") == "dispatch" and e.get("stage")}
    out = []
    for r in runs.active():
        if r.runner == "manual":
            continue  # the scheduler's `active_runs` rule: a manual run holds no slot
        t = tasks.get(r.task_id)
        said, spend, tokens = progress(r, harness_cfgs)
        # A record with no pid and no output was written at dispatch and never launched; the
        # scheduler counts it against a slot until a tick reaps it, so the page shows it as
        # what it is rather than as a run that has said nothing for an hour.
        no_process = r.pid is None and not (r.path / "stdout.json").exists()
        out.append({
            "kind": "run", "state": "running", "task": r.task_id, "title": t.title if t else "", "run": r.run_id,
            "mode": r.mode, "stage": stage_of_run.get(r.run_id, ""), "harness": r.harness, "model": r.model,
            "difficulty": r.difficulty, "started_at": r.started_at[:19], "elapsed_s": round(r.elapsed_minutes() * 60),
            "typical_s": typical.get(typical_key(r.mode, r.harness, r.difficulty or "medium")) or typical.get(typical_key(r.mode, r.harness)),
            "said": said, "spend_usd": spend, "tokens_so_far": tokens, "no_process": no_process,
            "glyph": MODE_GLYPH.get(r.mode, "running"), "dot": MODE_DOT.get(r.mode, "running"),
        })
    # Newest dispatch first, with a task's runs kept together (its older run first).
    newest = {}
    for s_ in out:
        newest[s_["task"]] = max(newest.get(s_["task"], ""), s_["started_at"])
    out.sort(key=lambda s_: s_["started_at"])
    out.sort(key=lambda s_: newest[s_["task"]], reverse=True)
    return out


def cards_needing_a_hand(tasks: dict, state: State, control: dict) -> list[dict]:
    """Held merges, paused harnesses and needs-you cards, in that order (the design's Now region)."""
    out = []
    for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
        st = state.get(t.id)
        info = needs_human_info(st.get("needs_human")) if not t.status.terminal else None
        if t.status == Status.IN_REVIEW and (info or st.get("review_decision") == "changes_requested"):
            reason = (info or {}).get("reason") or "a person requested changes on the PR"
            out.append({"kind": "held", "state": "held", "task": t.id, "title": t.title, "reason": reason, "glyph": "in_review", "dot": "changes_requested"})
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

def dispatch_queue(store: Store, tasks: dict, state: State, control: dict, spent: dict[str, float]) -> list[dict]:
    """The queue `dispatch_ready` builds, with the tier's harness and model and the reason a
    line would be skipped (what `Scheduler.dispatch_queue()` returns in the build)."""
    cfg = store.config
    max_rev = int(cfg.get("max_revisions", 3) or 3)
    stack = bool(cfg.get("stack", True))
    harness_cfgs = cfg.get("harnesses") or {}
    phases = {ph.key: ph for p in store.products() for ph in p.phases}
    products = {p.name: p for p in store.products()}
    budgets = cfg.get("budgets") or {}
    paused = set((control.get("paused_harnesses") or {}).keys())
    queue: list[tuple[Any, str, str]] = []
    for t in tasks.values():
        st = state.get(t.id)
        if t.status != Status.CHANGES_REQUESTED or st.get("needs_human"):
            continue
        if st.get("rebase_pending"):
            queue.append((t, "rebase", "rebase round, goes first"))
        elif st.get("pending_feedback") and (st.get("pending_feedback_rebase") or int(st.get("revisions", 0)) < max_rev):
            queue.append((t, "revise", f"revise round {int(st.get('revisions', 0)) + 1} of {max_rev}"))
    for t in sorted(ready(tasks, stack=stack), key=dispatch_sort_key):
        why = f"priority {t.priority}" + (f" · order {t.order}" if t.order is not None else "")
        queue.append((t, "work", why))
    out = []
    for pos, (t, mode, why) in enumerate(queue, 1):
        prod = products.get(t.product)
        pcfg = (cfg.get("products") or {}).get(t.product) or {}
        hname = t.harness or pcfg.get("harness") or str(cfg.get("harness") or "claude")
        runner = t.runner or pcfg.get("runner") or str(cfg.get("runner") or "local")
        model = t.model or Harness(hname, harness_cfgs.get(hname) or {}).model_for(t.difficulty)
        skip = ""
        ph = phases.get(t.key)
        if ph is not None and phase_refusal(ph, t):
            skip = "phase closed" if ph.closed else "phase frozen"
        elif budgets.get(t.key) and spent.get(t.key, 0.0) >= float(budgets[t.key]):
            skip = "over budget"
        elif runner == "manual":
            skip = "manual runner"
        elif hname in paused:
            skip = "harness paused"
        out.append({"pos": pos, "task": t.id, "title": t.title, "mode": mode, "why": why, "difficulty": t.difficulty,
                    "harness": hname, "model": model, "skip": skip, "status": t.status.value, "product": prod.name if prod else t.product})
    return out


def merge_queue(store: Store, tasks: dict, state: State, events: list[dict], active: list[dict], control: dict,
                review_slots_busy: int, review_slots: int) -> dict:
    view = merge_queue_view(store, state, events) or {"head": None, "candidates": [], "last_drop": None}
    queued = {c["task"] for c in view["candidates"]} | ({view["head"]["task"]} if view["head"] else set())
    reviewing = {s["task"] for s in active if s.get("mode") in REVIEW_MODES}
    max_rounds = int((store.config.get("review") or {}).get("max_rounds", 4) or 4)
    in_review, waiting = [], []
    paused = set((control.get("paused_harnesses") or {}).keys())
    review_harness = str((store.config.get("review") or {}).get("harness") or store.config.get("harness") or "claude")
    for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
        st = state.get(t.id)
        if t.status == Status.IN_REVIEW and t.id not in queued:
            in_review.append({"task": t.id, "title": t.title, "pr": t.pr, "round": int(st.get("review_rounds", 0) or 0),
                              "max_rounds": max_rounds, "checks": str(st.get("checks") or "").lower() or "pending",
                              "reviewing": t.id in reviewing, "blocked": str(st.get("automerge_blocked") or "")})
        for item in st.get("pending_reviews") or []:
            name = item.get("kind", "review") + (f":{item['name']}" if item.get("name") else "")
            why = "harness paused" if review_harness in paused else f"no review slot ({review_slots_busy} of {review_slots} busy)"
            waiting.append({"task": t.id, "title": t.title, "what": name, "why": why})
    return {"head": view["head"], "candidates": view["candidates"], "last_drop": view["last_drop"],
            "in_review": in_review, "waiting": waiting}


# ---- where we are ---------------------------------------------------------------------

def goal_marks(text: str, tasks: dict) -> list[dict]:
    """One line per numbered goal under `## Goals`: its label, the task ids it names and the
    mark those tasks earn (what `now.goal_marks` does in the build)."""
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
        out.append({"n": n, "label": label.rstrip(". "), "ids": [t.id for t in known], "done": sum(1 for t in known if t.status == Status.DONE),
                    "total": len(known), "mark": mark, "word": word})
    return out


def stage_word_for(fraction: float) -> str:
    if fraction >= 1.0:
        return "in fruit"
    word = "seed"
    for lower, name in STAGE_BANDS:
        if fraction > lower:
            word = name
    return word


def phase_sheet(ph: Any, tasks: dict, state: State, active: list[dict], events: list[dict], budgets: dict, spent: dict) -> dict:
    in_scope = [t for t in ph.tasks if t.status != Status.CANCELLED]
    done = sum(1 for t in in_scope if t.status == Status.DONE)
    prs_open = sum(1 for t in in_scope if t.pr and t.status != Status.DONE)
    running = sum(1 for s in active if s.get("task") in {t.id for t in in_scope})
    fraction = done / len(in_scope) if in_scope else 0.0
    info = plant_info(ph.plant)
    verdict = state.get("_retro_verdicts").get(ph.key)
    dispatches = [e["at"] for e in events if e.get("kind") == "dispatch" and e.get("task") in {t.id for t in ph.tasks}]
    return {
        "key": ph.key, "product": ph.product, "name": ph.name, "plant": ph.plant, "plate": ph.plate,
        "latin": ph.latin, "common": ph.common, "note": info.get("note", ""), "closed": ph.closed, "frozen": ph.frozen,
        "done": done, "total": len(in_scope), "prs_open": prs_open, "running": running, "fraction": round(fraction, 4),
        "stage_word": stage_word_for(fraction), "spent": round(spent.get(ph.key, 0.0), 2), "budget": budgets.get(ph.key),
        "retro": dict(verdict) if isinstance(verdict, dict) else None,
        "goals": goal_marks(goals_text(ph.goals_path), tasks),
        "first_dispatch": min(dispatches) if dispatches else "",
    }


# ---- the last period ------------------------------------------------------------------

def period(events: list[dict], op_events: list[dict], tasks: dict, since: str, bucket: str) -> dict:
    window = [e for e in events if str(e.get("at") or "") >= since]
    done_at: dict[str, str] = {}
    for e in window:
        if e.get("kind") == "transition" and e.get("to") == "done" and e.get("task"):
            done_at[e["task"]] = e["at"]
    first_review: dict[str, dict] = {}
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
    series = cost_series(events + op_events, tasks, since=since, bucket=bucket, group_by="activity")
    buckets = [b["bucket"] for b in series.get("buckets") or []]
    per_bucket = Counter(bucket_key(e["at"], bucket) for e in finished)
    return {
        "since": since, "bucket": bucket, "merged": len(done_at), "merged_ids": sorted(done_at),
        "first_pass": {"approved": approved, "reviewed": len(reviewed)},
        "cost": round(cost, 2), "per_accepted": round(accepted_cost / len(done_at), 2) if done_at else None,
        "runs": len(finished), "hand_steps": sum(1 for e in window if e.get("kind") in HAND_KINDS),
        "hand_kinds": dict(Counter(e["kind"] for e in window if e.get("kind") in HAND_KINDS)),
        "by_model": [{"who": k, "runs": int(v["runs"]), "cost": round(v["cost"], 2)} for k, v in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"])],
        "series": series, "throughput": [per_bucket.get(b, 0) for b in buckets],
        "annotations": [{"at": e["at"], "from": e.get("from") or "", "to": e.get("to") or "", "kind": e["kind"], "changed": e.get("keys") or []}
                        for e in window if e.get("kind") in ("profile_changed", "config_reloaded")],
    }


# ---- the snapshot ---------------------------------------------------------------------

def take_snapshot(root: Path) -> dict:
    store = Store(root)
    cfg = store.config
    tasks = store.tasks()
    state = State(cfg.garden_dir / "state.json")
    runs = RunStore(cfg.garden_dir)
    events = EventLog(cfg.garden_dir / "events.jsonl").read()
    op_records = ops.read_records(ops.default_path(store.root))
    op_events = ops.to_cost_events(op_records)
    now = dt.datetime.now(dt.UTC)
    control = state.get("_control")
    harness_cfgs = cfg.get("harnesses") or {}
    spent: dict[str, float] = defaultdict(float)
    for e in events:
        if e.get("kind") == "run_finished" and e.get("task") in tasks:
            spent[tasks[e["task"]].key] += float(e.get("cost_usd") or 0.0)

    strips = strips_in_flight(runs, tasks, events, harness_cfgs, now)
    worker_busy = sum(1 for s in strips if s["mode"] in WORKER_MODES | CHECK_MODES)
    worker_without_process = sum(1 for s in strips if s["mode"] in WORKER_MODES | CHECK_MODES and s["no_process"])
    review_busy = sum(1 for s in strips if s["mode"] in REVIEW_MODES)
    max_parallel = int(cfg.get("max_parallel", 2) or 2)
    review_parallel = int(cfg.get("review_parallel") or max_parallel)
    hands = cards_needing_a_hand(tasks, state, control)

    budgets = cfg.get("budgets") or {}
    open_phases = [ph for p in store.products() for ph in p.phases if not ph.closed]
    sheets = [phase_sheet(ph, tasks, state, strips, events, budgets, spent) for ph in open_phases]
    sheets.sort(key=lambda s: (-s["running"], s["key"]))
    closed = [phase_sheet(ph, tasks, state, strips, events, budgets, spent) for p in store.products() for ph in p.phases if ph.closed]
    closed.sort(key=lambda s: s["closed"], reverse=True)
    primary = sheets[0] if sheets else None

    stack = bool(cfg.get("stack", True))
    drafts = sum(1 for t in tasks.values() if t.status == Status.DRAFT and effective_status(t, tasks, stack) == "draft")
    windows = {
        "hour": ((now - dt.timedelta(hours=1)).replace(microsecond=0).isoformat(), "hour"),
        "today": (now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), "hour"),
        "24h": ((now - dt.timedelta(hours=24)).replace(microsecond=0).isoformat(), "hour"),
        "phase": ((primary or {}).get("first_dispatch") or (now - dt.timedelta(days=7)).isoformat(), "day"),
    }
    last_tick = max((e["at"] for e in events), default=now.isoformat())
    return {
        "captured_at": now.replace(microsecond=0).isoformat(),
        "garden": {"name": str(cfg.get("name") or root.name), "tick_interval": int(cfg.get("tick_interval", 60) or 60),
                   "last_tick": last_tick, "max_parallel": max_parallel, "review_parallel": review_parallel,
                   "worker_busy": worker_busy, "worker_without_process": worker_without_process, "review_busy": review_busy,
                   "dispatch_paused": {k: control.get(k) for k in ("dispatch", "by", "at", "reason")} if control.get("dispatch") == "paused" else None,
                   "drafts": drafts, "inbox_decisions": len(hands)},
        "now": strips + hands,
        "next": {"dispatch": dispatch_queue(store, tasks, state, control, spent),
                 "merge": merge_queue(store, tasks, state, events, strips, control, review_busy, review_parallel)},
        "where": {"primary": primary, "others": sheets[1:], "closed": closed},
        "period": {name: period(events, op_events, tasks, since, bucket) for name, (since, bucket) in windows.items()},
    }


# ---- the render -----------------------------------------------------------------------

def render(snapshot: dict) -> str:
    import jinja2

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(HERE)), autoescape=True, undefined=jinja2.StrictUndefined)
    css = re.search(r"<style>(.*?)</style>", BASE_HTML.read_text(), re.S).group(1)

    def plate(key: str, thumb: bool = False) -> str:
        name = f"{key}-thumb.webp" if thumb else f"{key}.webp"
        return f"../../src/garden/web/static/plates/{name}" if (PLATES / name).exists() else ""

    def chart(p: dict, width: int = 640) -> Any:
        marks = [a for a in p["annotations"] if a["kind"] == "profile_changed"]
        return _markup(cost_stack_svg(p["series"], width=width, annotations=marks))

    env.globals.update({
        "DEFS": _markup(DEFS), "base_css": _markup(css), "vine": _markup(vine_svg()), "plate": plate,
        "plant": lambda *a, **k: _markup(plant_svg(*a, **k)), "stage": lambda *a, **k: _markup(stage_svg(*a, **k)),
        "chart": chart,
        "spark": lambda values: _markup(sparkline_svg([float(v) for v in values], width=100, height=26)),
        "minutes": lambda s: ("" if s is None else f"{int(round(s))} s" if s < 90 else f"{int(round(s / 60))} min"),
        "money": lambda v: f"${v:.2f}" if v is not None else "—",
        "ktok": lambda n: f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1000:.0f}k" if n >= 1000 else str(n),
    })
    return env.get_template(TEMPLATE.name).render(**snapshot)


def _markup(s: str) -> Any:
    from markupsafe import Markup

    return Markup(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot", help="read a garden and write now-snapshot.json")
    snap.add_argument("--garden", required=True, type=Path)
    sub.add_parser("render", help="write the mock HTML from now-snapshot.json")
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        SNAPSHOT.write_text(json.dumps(take_snapshot(args.garden.resolve()), indent=1, sort_keys=True, default=str) + "\n")
        print(f"wrote {SNAPSHOT}")
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(render(json.loads(SNAPSHOT.read_text())))
        print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
