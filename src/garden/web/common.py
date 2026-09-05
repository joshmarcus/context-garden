"""What every page and action handler shares: the hub (store, scheduler lock, tick log),
the Jinja environment, the context every template gets, and a few helpers.

All logic lives in store/graph/scheduler; the web package only renders and forwards actions.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import markdown as md
from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..events import EventLog, metrics, parse_since
from ..graph import blockers, effective_status, validate
from ..inbox import _last_log_line, build_inbox, decisions, needs_human_info, running_now
from ..model import Status, dispatch_sort_key, now_iso
from ..profiles import describe as describe_stop
from ..runs import RunStore
from ..scheduler import Scheduler, State
from ..store import Store
from .trust import sanitize_html

TEMPLATES = Path(__file__).parent / "templates"
PLATES_DIR = Path(__file__).parent / "static" / "plates"  # scanned plates, written by `garden plants --fetch`
COLUMNS = ["draft", "blocked", "ready", "running", "waiting_human", "awaiting_triage", "in_review", "changes_requested", "done", "failed", "wont_do"]
# The list view orders sections by where the loop moves work: what needs a person first,
# then what is in flight, then what is waiting or settled. Covers every board column
# (cancelled is dropped like the columns view).
LIST_ORDER = ["waiting_human", "awaiting_triage", "changes_requested", "failed", "running", "in_review", "ready", "blocked", "draft", "done", "wont_do"]

LOGGER = logging.getLogger("garden.web")


def _flash_url(url: str, message: str, note: str = "", extra: dict[str, str] | None = None) -> str:
    """Append a flash message (and, for the answer form, the typed note) to a redirect target.
    `extra` carries a form's other typed fields back (the new-task form) so they survive a
    validation-failure redirect."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["flash"] = message
    if note:
        query["flash_note"] = note
    for k, v in (extra or {}).items():
        if v:
            query[k] = v
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Hub:
    """Shared state for request handlers: the store, a lock around scheduler passes, and
    a log of recent tick results. `github` is an optional stand-in for GitHub handed to
    every scheduler the hub builds (`garden qa` serves a throwaway garden against one)."""

    def __init__(self, store: Store, watch: bool, github: Any | None = None):
        self.store = store
        self.github = github
        self.lock = threading.Lock()  # held only by tick(): one scheduler pass at a time
        # A short lock around an action so two POSTs don't clobber one task, held *only* for
        # the action — never shared with the tick, so a button press never waits for a pass to
        # finish (CG-182). State.save() merges per key under its own file lock and task files
        # are written whole, so an action and a concurrent tick each build a scheduler, apply
        # their change and save without a shared lock.
        self.action_lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.last_tick = ""
        self.watch = watch
        self.planning: dict[str, str] = {}  # "product/phase" -> status text
        self._stop = threading.Event()
        if watch:
            threading.Thread(target=self._loop, daemon=True, name="garden-watch").start()

    def scheduler(self) -> Scheduler:
        self.store.invalidate()
        return Scheduler(self.store, github=self.github, log=self._log)

    def reader(self) -> Scheduler:
        """A scheduler for a page to read through (budgets, inbox, limits); it logs nothing."""
        return Scheduler(self.store, github=self.github, log=lambda m: None)

    def stop(self) -> None:
        """End the watch loop (a test or `garden qa` shutting the server down)."""
        self._stop.set()

    def _log(self, msg: str) -> None:
        self.events.append({"at": now_iso(), "msg": msg})
        del self.events[:-200]

    def tick(self) -> str:
        with self.lock:
            rep = self.scheduler().tick()
            self.last_tick = now_iso()
            return rep.summary()

    def _loop(self) -> None:
        interval = int(self.store.config.get("tick_interval", 60))
        try:
            with self.lock:
                self.scheduler().reap_on_start()  # reap runs the last process finished but never reaped
        except Exception as e:  # noqa: BLE001
            self._log(f"start-up reap error: {e}")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self._log(f"tick error: {e}")
            self._stop.wait(interval)

    def fresh(self) -> Store:
        self.store.invalidate()
        return self.store


def render_md(text: str) -> str:
    """Markdown to HTML, sanitised: much of what the pages render was written by an agent or
    a PR commenter, and markdown passes raw HTML through (see web/trust.py)."""
    return sanitize_html(md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"]))


def tier_rows(s: Store, tasks: dict[str, Any]) -> list[dict[str, Any]]:
    m = metrics(EventLog(s.config.garden_dir / "events.jsonl").read(), tasks)
    return [{"tier": t, **m["by_difficulty"][t]} for t in ("easy", "medium", "hard") if m["by_difficulty"].get(t)]


def closed_phase_keys(s: Store) -> set[str]:
    return {ph.key for p in s.products() for ph in p.phases if ph.closed}


def board_view(view: str | None) -> str:
    return view if view in ("list", "backlog") else "columns"


class Site:
    """The hub and the templates, plus the two things most pages build: the base template
    context and the board's columns. Every `pages.*.register(app, site)` and
    `actions.*.register(app, site)` gets one of these."""

    def __init__(self, hub: Hub, templates: Jinja2Templates, plates: Path):
        self.hub = hub
        self.templates = templates
        self.plates = plates

    def ctx(self, request: Request, page: str = "", **kw: Any) -> dict[str, Any]:
        hub = self.hub
        s = hub.fresh()
        sched = hub.reader()
        items = build_inbox(s, sched)
        ctrl = sched.control()
        stops = sched.operating_profile_stops()
        active = sched.operating_profile_name()
        return {
            "request": request,
            "page": page,
            "garden_name": s.config.get("name"),
            "root": str(s.root),
            "watch": hub.watch,
            "last_tick": hub.last_tick,
            "products": s.products(),
            "phases_by_product": {p.name: [ph.name for ph in p.phases] for p in s.products()},
            "inbox_count": len(decisions(items)),
            "env": s.config.env,
            "running": running_now(s),
            "workers_running": len(sched.worker_runs_active()),
            "reviews_running": len(sched.review_runs_active()),
            "max_parallel": sched.effective_max_parallel(),
            "review_parallel": sched.review_parallel_limit(),
            "totals": RunStore(s.config.garden_dir).totals(),
            "dispatch_paused": ctrl.get("dispatch") == "paused",
            "pause_ctrl": ctrl,
            "closed_count": sum(1 for p in s.products() for ph in p.phases if ph.closed),
            "flash": request.query_params.get("flash", ""),
            "flash_note": request.query_params.get("flash_note", ""),
            "operating_profile_names": list(stops),
            "operating_profile": active,
            "operating_profile_meaning": describe_stop(stops.get(active) or {}) if active else "",
            "operating_profile_spend_rate": RunStore(s.config.garden_dir).spend_since(parse_since("1h")),
            **kw,
        }

    def board_data(self, product: str | None, phase: str | None, include_closed: bool = False) -> dict[str, Any]:
        s = self.hub.fresh()
        tasks = s.tasks()
        stack = bool(s.config.get("stack", True))
        state = State(s.config.garden_dir / "state.json")
        closed_keys = closed_phase_keys(s)
        cols: dict[str, list] = {c: [] for c in COLUMNS}
        for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
            if product and t.product != product:
                continue
            if phase and t.phase != phase:
                continue
            # closed phases stay off the board unless asked for (or picked explicitly)
            if t.key in closed_keys and not include_closed and (t.product, t.phase) != (product, phase):
                continue
            eff = effective_status(t, tasks, stack)
            if eff == "cancelled":
                continue
            st = state.get(t.id)
            # The one fact the list view surfaces for a PR-bearing state: the human review
            # decision if GitHub has one, else the last automated review verdict.
            rev = st.get("last_review") or {}
            review = str(st.get("review_decision") or rev.get("verdict") or "").replace("_", " ").strip()
            # A merged_into_parent task's own PR is closed for good; it is not yet `done`, but it
            # is not waiting on a human review either, so it sits with the in_review cards, tagged
            # with a badge naming the parent it is waiting on (CG-228).
            merged_parent = ""
            col = eff
            if eff == "merged_into_parent":
                col = "in_review"
                info = st.get("merged_into_parent") or {}
                merged_parent = str(info.get("parent") or st.get("stack_parent") or info.get("branch") or "")
            cols[col].append({"task": t, "blockers": blockers(t, tasks, stack) if eff == "blocked" else [],
                              "stack": "" if merged_parent else st.get("stack_parent", ""),
                              "merged_parent": merged_parent,
                              "needs_human": "" if t.status.terminal else (needs_human_info(st.get("needs_human")) or {}).get("reason", ""),
                              "question": st.get("question", "") if eff == "waiting_human" else "",
                              "review": review if eff in ("awaiting_triage", "in_review", "changes_requested") else "",
                              "reason": _last_log_line(t) if eff == "failed" else ""})
        runs = RunStore(s.config.garden_dir)
        active = {r.task_id: r for r in runs.active()}
        return {"cols": cols, "active": active, "product": product, "phase": phase, "totals": runs.totals(),
                "closed": include_closed, "problems": validate(tasks)}

    def backlog_data(self, product: str | None, include_closed: bool = False) -> dict[str, Any]:
        """The backlog view: each open phase of a product (all products when none is picked) as a
        section of its non-terminal tasks in dispatch order, plus what each row's controls need
        (the phases it can move to, whether a cross-phase move is allowed). Closed phases stay in
        the Herbarium unless `include_closed`."""
        s = self.hub.fresh()
        tasks = s.tasks()
        stack = bool(s.config.get("stack", True))
        state = State(s.config.garden_dir / "state.json")
        runs = RunStore(s.config.garden_dir)
        active = {r.task_id: r for r in runs.active()}
        # Phases a row can move to: its product's phases, closed ones dropped (the row's own is
        # always kept so the pulldown shows where it is).
        move_phases: dict[str, list[str]] = {}
        sections: list[dict[str, Any]] = []
        for p in s.products():
            if product and p.name != product:
                continue
            move_phases[p.name] = [ph.name for ph in p.phases if include_closed or not ph.closed]
            for ph in p.phases:
                if ph.closed and not include_closed:
                    continue
                rows = []
                for t in sorted(ph.tasks, key=dispatch_sort_key):
                    eff = effective_status(t, tasks, stack)
                    if eff in ("done", "cancelled", "wont_do"):
                        continue
                    st = state.get(t.id)
                    # A running or in-review task can be reordered but not moved to another phase.
                    movable = not (t.status == Status.RUNNING or t.status.pr_open or t.id in active)
                    rows.append({"task": t, "eff": eff,
                                 "blockers": blockers(t, tasks, stack) if eff == "blocked" else [],
                                 "needs_human": (needs_human_info(st.get("needs_human")) or {}).get("reason", ""),
                                 "movable": movable,
                                 "move_reason": "" if movable else f"{eff.replace('_', ' ')}: reorder it here, but finish or cancel the run before moving it"})
                sections.append({"phase": ph, "rows": rows})
        return {"sections": sections, "move_phases": move_phases, "active": active,
                "product": product, "phase": None, "closed": include_closed, "problems": validate(tasks)}
