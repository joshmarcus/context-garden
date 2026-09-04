"""The inbox: everything that needs a human, grouped by the kind of decision, with the
concrete action that resolves each item. The web Inbox page, `garden inbox` and the TUI
Inbox tab all render this one list."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .graph import effective_status
from .model import Status, Task
from .runs import RunStore
from .store import Store

GROUPS = [
    ("restart", "Restart required", "The garden's own code was updated by a merged PR. Restart `garden serve` to load the new version."),
    ("question", "Answer a worker's question", "A worker paused and is waiting for you. Its session resumes with your answer."),
    ("triage", "Triage a draft PR", "A worker finished and opened a draft. Your first look decides: ready for review, or send it back."),
    ("review", "Review and merge", "Ready for review on GitHub. Comments you leave become a revise run; merging unblocks dependents."),
    ("attention", "Needs a decision", "The loop stopped on purpose: a stall, a cap, a closed PR, a failed worker."),
    ("retrying", "Auto-retrying", "A previous attempt failed; a new run is queued or in progress. No action needed unless you want to cancel."),
    ("approve", "Approve planned or discovered work", "Draft tasks waiting for a go."),
    ("budget", "Budget", "A phase hit its spending cap; raise it or leave it paused."),
]


def _last_log_line(t: Task) -> str:
    """Return the message portion of the last log entry in the task body, or ''."""
    for ln in reversed(t.body.splitlines()):
        if ln.startswith("- "):
            return ln[2:].split(" ", 1)[-1]
    return ""


def _age(iso: str) -> str:
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    secs = (dt.datetime.now(dt.UTC) - t).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def build_inbox(store: Store, sched: Any) -> list[dict[str, Any]]:
    tasks = store.tasks()
    state = sched.state
    stack = bool(store.config.get("stack", True))
    items: list[dict[str, Any]] = []
    order = {g[0]: i for i, g in enumerate(GROUPS)}
    titles = {g[0]: g[1] for g in GROUPS}

    def add(group: str, t: Task, why: str, actions: list[dict[str, str]], **extra: Any) -> None:
        items.append({"group": group, "group_title": titles[group], "task": t.id, "title": t.title, "phase": t.key,
                      "status": t.status.value, "pr": t.pr, "why": why, "actions": actions, "age": _age(t.updated),
                      "difficulty": t.difficulty, **extra})

    for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
        st = state.get(t.id)
        if t.status == Status.WAITING_HUMAN:
            add("question", t, str(st.get("question") or "(no question recorded)"),
                [{"label": "Answer", "kind": "answer", "command": f'garden answer {t.id} "..."'}], question=st.get("question", ""))
        elif t.status == Status.AWAITING_TRIAGE:
            rev = st.get("last_review") or {}
            why = "draft PR open"
            if rev:
                why += f" · automated review: {str(rev.get('verdict', '')).replace('_', ' ')}"
            if st.get("review_run"):
                why += " · review running"
            add("triage", t, why, [
                {"label": "Ready for review", "kind": "triage-ready", "command": f"garden triage {t.id} --ready"},
                {"label": "Send back", "kind": "triage-changes", "command": f'garden triage {t.id} --changes "..."'},
                {"label": "Open PR", "kind": "link", "href": t.pr},
            ], review=rev)
        elif t.status == Status.IN_REVIEW and not st.get("needs_human"):
            why = (st.get("review_decision") or "no review yet").lower().replace("_", " ")
            if st.get("checks"):
                why += f" · CI {st['checks'].lower()}"
            add("review", t, why, [{"label": "Open PR", "kind": "link", "href": t.pr},
                                   {"label": "Mark done", "kind": "done", "command": f"garden set-status {t.id} done"}])
        if st.get("needs_human") and not t.status.terminal:
            add("attention", t, str(st["needs_human"]), [
                {"label": "Continue the loop", "kind": "retry", "command": f"garden retry {t.id}"},
                {"label": "Cancel", "kind": "cancel", "command": f"garden cancel {t.id}"},
            ] + ([{"label": "Open PR", "kind": "link", "href": t.pr}] if t.pr else []))
        elif t.status == Status.FAILED:
            last = _last_log_line(t)
            add("attention", t, last[:140] or "failed", [
                {"label": "Retry", "kind": "retry", "command": f"garden retry {t.id}"},
                {"label": "Cancel", "kind": "cancel", "command": f"garden cancel {t.id}"},
            ])
        elif t.status == Status.DRAFT:
            eff = effective_status(t, tasks, stack)
            why = "discovered by " + t.discovered_from if t.discovered_from else "planned, not yet approved"
            if eff == "blocked":
                why += " · blocked until deps merge"
            last = _last_log_line(t)
            if t.attempts:
                why += f" · {t.attempts} attempt{'s' if t.attempts != 1 else ''}"
            if last:
                why += f" · {last}"
            add("approve", t, why, [{"label": "Approve", "kind": "approve", "command": f"garden approve {t.id}"},
                                    {"label": "Drop", "kind": "cancel", "command": f"garden cancel {t.id}"}],
                attempts=t.attempts, last_log=last)
        if t.attempts > 0 and not st.get("needs_human") and not t.status.terminal and t.status in (Status.READY, Status.RUNNING) and not (t.status == Status.RUNNING and t.attempts <= 1):
            last = _last_log_line(t)
            why = last or f"{t.attempts} attempt{'s' if t.attempts != 1 else ''} failed"
            add("retrying", t, why, [{"label": "Cancel", "kind": "cancel", "command": f"garden cancel {t.id}"}],
                attempts=t.attempts, last_log=last)

    for key in sorted({t.key for t in tasks.values()}):
        budget = sched.budget_for(key)
        if budget and sched.spent_for(key) >= budget:
            probe = next(t for t in tasks.values() if t.key == key)
            items.append({"group": "budget", "group_title": titles["budget"], "task": "", "title": key, "phase": key,
                          "status": "", "pr": "", "why": f"spent ${sched.spent_for(key):.2f} of ${budget:.2f}; dispatch paused",
                          "actions": [{"label": "Raise in garden.yaml", "kind": "config", "command": f"# budgets: {{{key}: <usd>}}"}],
                          "age": "", "difficulty": probe.difficulty})

    self_meta = state.get("_self")
    if self_meta.get("needs_restart"):
        items.append({"group": "restart", "group_title": titles["restart"], "task": "", "title": "Garden code updated",
                      "phase": "", "status": "", "pr": "",
                      "why": "A merged PR updated the garden's own code. Restart `garden serve` to load the new version.",
                      "actions": [{"label": "Restart", "kind": "info", "command": "# stop garden serve and re-run it"}],
                      "age": _age(self_meta.get("updated_at", "")), "difficulty": ""})
    if self_meta.get("dirty_warning"):
        items.append({"group": "restart", "group_title": titles["restart"], "task": "", "title": "Garden checkout has uncommitted changes",
                      "phase": "", "status": "", "pr": "",
                      "why": self_meta["dirty_warning"],
                      "actions": [],
                      "age": "", "difficulty": ""})

    items.sort(key=lambda i: (order[i["group"]], i["task"]))
    return items


def counts(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it["group"]] = out.get(it["group"], 0) + 1
    return out


def running_now(store: Store) -> list[dict[str, Any]]:
    rs = RunStore(store.config.garden_dir)
    tasks = store.tasks()
    out = []
    for r in rs.active():
        t = tasks.get(r.task_id)
        out.append({"task": r.task_id, "title": t.title if t else "", "mode": r.mode, "model": r.model,
                    "minutes": round(r.elapsed_minutes()), "host": r.host})
    return out
