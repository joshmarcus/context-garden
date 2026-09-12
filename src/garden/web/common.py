"""What every page and action handler shares: the hub (store, scheduler lock, tick log),
the Jinja environment, the context every template gets, and a few helpers.

All logic lives in store/graph/scheduler; the web package only renders and forwards actions.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import logging
import os
import select
import threading
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import markdown as md
from fastapi import Request
from fastapi.templating import Jinja2Templates

from .. import operator_spend as ops
from ..events import EventLog, metrics, parse_since
from ..github import PRInfo, RepositorySlug, is_safe_pr_url, pull_request_number
from ..graph import validate
from ..inbox import _last_log_line, build_inbox, decisions, needs_human_info, running_now
from ..members import Principal
from ..model import Status, dispatch_sort_key, now_iso
from ..multiplayer_client import AuthoritativeView, MultiplayerClient, MultiplayerUnavailable
from ..profiles import describe as describe_stop
from ..runs import RunStore, _rollup
from ..scheduler import REVIEW_MODES, WORKER_MODES, Scheduler, State
from ..scheduler_health import scheduler_health
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


class _DiscoveryWatch:
    """A small Linux inotify invalidator for the shared discovery generation.

    It observes directories, rather than statting every discovered file for every web
    request.  A platform without inotify falls back to Store's conservative signature
    check, retaining correctness where the cheap notification mechanism is unavailable.
    """

    _MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800

    def __init__(self, root: Path):
        self.root = root
        self.fd = -1
        self._libc: Any | None = None
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                return
            self.fd = fd
            self._libc = libc
            self.rebuild()
        except (AttributeError, OSError):
            self.close()

    @property
    def available(self) -> bool:
        return self.fd >= 0 and self._libc is not None

    def rebuild(self) -> None:
        if not self.available:
            return
        # Recreate the descriptor so removed directories cannot leave stale watches behind.
        old_fd = self.fd
        try:
            new_fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            self.close()
            return
        if new_fd < 0:
            self.close()
            return
        self.fd = new_fd
        try:
            os.close(old_fd)
        except OSError:
            self.close()
            return
        complete = True

        def walk_error(_error: OSError) -> None:
            nonlocal complete
            complete = False

        try:
            for directory, names, _files in os.walk(self.root, onerror=walk_error):
                names[:] = [name for name in names if name not in {".git", ".garden", ".venv", "node_modules"}]
                if self._libc.inotify_add_watch(self.fd, os.fsencode(directory), self._MASK) < 0:
                    complete = False
                    break
        except OSError:
            complete = False
        if not complete:
            # A partial watch tree would make an edit below an unwatched directory invisible.
            # Fall back to Store's metadata signature rather than serving a stale generation.
            self.close()

    def changed(self) -> bool:
        if not self.available:
            return False
        try:
            if not select.select([self.fd], [], [], 0)[0]:
                return False
            while True:
                try:
                    if not os.read(self.fd, 65536):
                        break
                except BlockingIOError:
                    break
            return True
        except OSError:
            # A broken watcher must never make a stale view look current.
            return True

    def close(self) -> None:
        try:
            if self.fd >= 0:
                os.close(self.fd)
        finally:
            self.fd = -1
            self._libc = None

    def __del__(self) -> None:
        """Release the raw inotify descriptor when a short-lived app is discarded."""
        try:
            self.close()
        except (AttributeError, OSError):
            # Interpreter shutdown can tear down ``os`` before an abandoned app is collected.
            pass


def product_checkout(store: Store, product: str) -> Path:
    """Return the configured local checkout, falling back to its garden metadata."""
    configured = store.config.product_repo(product)
    if isinstance(configured, str) and "://" in configured:
        return next((p.path for p in store.products() if p.name == product), store.root)
    return Path(configured)


def product_design_root(store: Store, product: str) -> Path:
    """The design directory belonging to a product's code checkout."""
    return product_checkout(store, product) / "docs" / "design"


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

    def __init__(self, store: Store, watch: bool, github: Any | None = None,
                 scheduler_blocked_reason: str = ""):
        self.store = store
        try:
            self.coordinator = MultiplayerClient.from_config(store.config)
        except MultiplayerUnavailable:
            self.coordinator = None
        # A Store has mutable discovery caches.  A web request gets its own instance so its
        # first read observes files written by another process, while its page body and base
        # template share one stable discovery snapshot.  The scheduler/watch thread keeps using
        # ``self.store`` and continues to invalidate it at the start of a pass.
        self._request_store: ContextVar[Store | None] = ContextVar("garden_web_request_store", default=None)
        self._request_is_read_only: ContextVar[bool] = ContextVar("garden_web_request_is_read_only", default=False)
        self._discovery_lock = threading.Lock()
        self._page_store = Store(store.root, config=store.config)
        self._discovery_watch = _DiscoveryWatch(store.root)
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
        # What the last pass was, for a page that keeps a countdown: the Now page's stream reads
        # this after each tick without the lock (a plain dict swapped whole, never mutated).
        self.tick_seq = 0
        self.tick_record: dict[str, Any] = {}
        self.watch = watch
        self.scheduler_blocked_reason = scheduler_blocked_reason
        if watch and not scheduler_blocked_reason:
            execution = self.execution_status()
            if execution["state"] in {"viewer", "unavailable"}:
                self.scheduler_blocked_reason = execution["label"]
        self._embedded_state = "waiting" if self.scheduler_blocked_reason else ("starting" if watch else "off")
        self._embedded_heartbeat = ""
        self._embedded_error = ""
        self._watch_thread: threading.Thread | None = None
        self.planning: dict[str, str] = {}  # "product/phase" -> status text
        self._stop = threading.Event()
        if watch and not self.scheduler_blocked_reason:
            self._watch_thread = threading.Thread(target=self._loop, daemon=True, name="garden-watch")
            self._watch_thread.start()

    def authoritative_view(self) -> AuthoritativeView | None:
        """Return current shared authority, explicitly marked stale during an outage."""
        return self.coordinator.refresh() if self.coordinator else None

    def coordinator_status(self) -> dict[str, Any]:
        if self.coordinator is None:
            return {"configured": False, "stale": False, "error": "", "projection_lag": []}
        try:
            view = self.authoritative_view()
            assert view is not None
            return {"configured": True, "stale": view.stale, "error": view.error,
                    "projection_lag": [
                        *view.projection_lag(), *self.coordinator.projection_lag(view.snapshot),
                    ]}
        except MultiplayerUnavailable as exc:
            return {"configured": True, "stale": True, "error": str(exc), "projection_lag": []}

    def execution_status(self) -> dict[str, str]:
        """The local execution scope, separate from an HTTP caller's view scope."""
        if not self.store.config.get("multiplayer.enabled", False):
            return {"state": "legacy", "label": "Single-user execution"}
        if self.coordinator is None:
            return {"state": "unavailable", "label": (
                "multiplayer execution is waiting for an authenticated operator assignment and "
                "scoped coordinator; identity-less scheduling is disabled"
            )}
        try:
            view = self.authoritative_view()
            assert view is not None
        except MultiplayerUnavailable as exc:
            return {"state": "unavailable", "label": str(exc)}
        snapshot = view.snapshot
        if snapshot.get("role") == "viewer":
            return {"state": "viewer", "label": "Viewer session — execution is disabled"}
        assignment = snapshot.get("assignment")
        if not assignment:
            return {"state": "unassigned", "label": "No work assignment"}
        if not assignment.get("enabled"):
            return {"state": "paused", "label": "Work assignment is paused"}
        return {"state": "assigned", "label": (
            f"Executing {assignment.get('project', '')}/{assignment.get('phase', '')}"
        )}

    def scheduler(self) -> Scheduler:
        # Tasks only: a config edit on disk is picked up by tick()'s own gate (CG-242), not by
        # every action's scheduler() call, so a button press between ticks can't hand a held
        # reload's executable fields (notify.command, checks, ...) a route around the gate.
        store = self._request_store.get()
        if store is None:
            store = self.store
            store.invalidate_tasks()
        return Scheduler(store, github=self.github, log=self._log)

    def reader(self, store: Store | None = None) -> Scheduler:
        """A scheduler-shaped read facade for pages; it never runs startup migrations."""
        store = store or self._request_store.get() or self.fresh()
        return Scheduler(store,
                         github=self.github, log=lambda m: None, read_only=True)

    def begin_request(self) -> tuple[Token[Store | None], Token[bool]]:
        """Install a read-only request Store backed by the shared generation."""
        return self._begin_request(read_only=True)

    def begin_action_request(self) -> tuple[Token[Store | None], Token[bool]]:
        """Install an isolated Store for an action that may mutate task models."""
        return self._begin_request(read_only=False)

    def _begin_request(self, read_only: bool) -> tuple[Token[Store | None], Token[bool]]:
        """Install a request Store and return its context tokens.

        A read-only request borrows the current discovery generation in ``fresh``.  An action
        gets an independent Store: its scheduler is free to mutate task models before saving,
        without changing an in-flight reader's view.
        """
        snapshot = Store(self.store.root, config=self.store.config)
        # Store.__init__ samples the current config mtime. Keep the shared Store's accepted
        # signature instead: a POST /tick still needs to notice an edit made before this
        # request and route it through the scheduler's fence-aware reload gate.
        snapshot._config_sig = self.store._config_sig
        return self._request_store.set(snapshot), self._request_is_read_only.set(read_only)

    def end_request(self, tokens: tuple[Token[Store | None], Token[bool]]) -> None:
        if not self._request_is_read_only.get():
            # An action may have written a task, phase, or config file.  Publish no partial
            # update: readers retain their old generation until the next complete scan swaps it.
            with self._discovery_lock:
                self._page_store.invalidate_tasks()
        store_token, read_only_token = tokens
        self._request_store.reset(store_token)
        self._request_is_read_only.reset(read_only_token)

    def stop(self) -> None:
        """End the watch loop (a test or `garden qa` shutting the server down)."""
        self._stop.set()
        self._discovery_watch.close()

    def _log(self, msg: str) -> None:
        self.events.append({"at": now_iso(), "msg": msg})
        del self.events[:-200]

    def tick(self) -> str:
        with self.lock:
            rep = self.scheduler().tick()
            with self._discovery_lock:
                self._page_store.invalidate_tasks()
            self.last_tick = now_iso()
            self.tick_seq += 1
            self.tick_record = {"seq": self.tick_seq, "at": self.last_tick, "duration_s": round(rep.duration_s, 2),
                                "summary": rep.summary(), "next_at": self.next_tick_at()}
            return rep.summary()

    def tick_interval(self) -> int:
        return int(self.store.config.get("tick_interval", 60))

    def next_tick_at(self) -> str:
        """When the watch loop's next pass starts, as ISO UTC, or '' when no loop runs."""
        if not self.watch or not self.last_tick:
            return ""
        import datetime as dt

        return (dt.datetime.fromisoformat(self.last_tick) + dt.timedelta(seconds=self.tick_interval())).isoformat()

    def tick_state(self) -> dict[str, Any]:
        """The last pass as a message for the Now page's stream (`seq` tells one from the next)."""
        return {"seq": self.tick_seq, **self.tick_record, "next_at": self.next_tick_at(),
                "scheduler_status": self.scheduler_health()}

    def scheduler_health(self) -> dict[str, Any]:
        """Report embedded-watch state separately from effective scheduler health."""
        standalone = scheduler_health(self.store.config.garden_dir)
        embedded = self._embedded_health()
        effective = embedded if self.watch else standalone
        if self.watch and standalone["kind"] != "missing":
            if standalone["kind"] in {"failed", "stale"}:
                effective = standalone
            elif embedded["kind"] in {"failed", "stale"}:
                effective = embedded
            else:
                effective = {"kind": "duplicated", "label": "embedded and standalone watchers both enabled"}
        return {"embedded": embedded["kind"], "embedded_health": embedded, "effective": effective,
                "standalone": standalone}

    def _embedded_health(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """Derive embedded health from bounded pass evidence and thread liveness."""
        if not self.watch:
            return {"kind": "off", "label": "embedded watcher off", "state": "off"}
        if self.scheduler_blocked_reason:
            return {"kind": "waiting", "label": self.scheduler_blocked_reason, "state": "waiting"}
        thread = self._watch_thread
        if thread is not None and not thread.is_alive():
            return {"kind": "failed", "label": "embedded watcher stopped", "state": "failed",
                    "heartbeat_at": self._embedded_heartbeat, "error": self._embedded_error}
        kind = self._embedded_state
        if self._embedded_heartbeat:
            checked = now or dt.datetime.now(dt.UTC)
            heartbeat = dt.datetime.fromisoformat(self._embedded_heartbeat)
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=dt.UTC)
            if (checked - heartbeat).total_seconds() > max(75, self.tick_interval() * 2 + 15):
                kind = "stale"
        labels = {
            "starting": "embedded watcher starting",
            "healthy": "embedded watcher healthy",
            "failed": "embedded watcher failed",
            "stale": "embedded watcher stale",
            "waiting": self.scheduler_blocked_reason,
        }
        return {"kind": kind, "label": labels[kind], "state": self._embedded_state,
                "heartbeat_at": self._embedded_heartbeat, "error": self._embedded_error}

    def _record_embedded(self, state: str, error: str = "") -> None:
        self._embedded_state = state
        self._embedded_heartbeat = now_iso()
        self._embedded_error = error[:500]

    def _loop(self) -> None:
        interval = int(self.store.config.get("tick_interval", 60))
        try:
            with self.lock:
                self.scheduler().reap_on_start()  # reap runs the last process finished but never reaped
        except Exception as e:  # noqa: BLE001
            self._log(f"start-up reap error: {e}")
            self._record_embedded("failed", str(e))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self._log(f"tick error: {e}")
                self._record_embedded("failed", str(e))
            else:
                self._record_embedded("healthy")
            self._stop.wait(interval)

    def fresh(self) -> Store:
        """Return the current request's fresh discovery snapshot.

        Outside an HTTP request this retains the original re-scan behaviour used by direct
        callers and the scheduler. Config reload remains gated by tick() (CG-242).
        """
        store = self._request_store.get()
        if store is not None:
            if not self._request_is_read_only.get():
                return store
            if store._products is None:
                with self._discovery_lock:
                    if self._page_store.config is not self.store.config:
                        self._page_store.config = self.store.config
                        self._page_store.invalidate_tasks()
                    if self._discovery_watch.changed():
                        self._page_store.invalidate_tasks()
                        self._discovery_watch.rebuild()
                    # On platforms without inotify, retain Store's old conservative external
                    # edit detection. Linux web requests take the notification path above.
                    if not self._discovery_watch.available:
                        self._page_store.refresh_tasks_if_changed()
                    self._page_store.tasks()
                # The page Store is immutable to read handlers by convention; mutations receive
                # their own Store above. Sharing this completed generation avoids a per-request
                # deepcopy of every Product, Phase, and Task.
                store._products = self._page_store._products
                store._tasks = self._page_store._tasks
                store._duplicate_ids = self._page_store._duplicate_ids
            return store
        self.store.invalidate_tasks()
        return self.store


def render_md(text: str) -> str:
    """Markdown to HTML, sanitised: much of what the pages render was written by an agent or
    a PR commenter, and markdown passes raw HTML through (see web/trust.py)."""
    return sanitize_html(md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"]))


def tier_rows(
    s: Store,
    tasks: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build tier rows from one caller-supplied history snapshot when available."""
    if events is None:
        events = EventLog(s.config.garden_dir / "events.jsonl").read()
    m = metrics(events, tasks)
    return [{"tier": t, **m["by_difficulty"][t]} for t in ("easy", "medium", "hard") if m["by_difficulty"].get(t)]


def closed_phase_keys(s: Store) -> set[str]:
    return {ph.key for p in s.products() for ph in p.phases if ph.closed}


def board_view(view: str | None) -> str:
    return view if view in ("list", "backlog", "prs") else "columns"


class Site:
    """The hub and the templates, plus the two things most pages build: the base template
    context and the board's columns. Every `pages.*.register(app, site)` and
    `actions.*.register(app, site)` gets one of these."""

    def __init__(self, hub: Hub, templates: Jinja2Templates, plates: Path):
        self.hub = hub
        self.templates = templates
        self.plates = plates

    @staticmethod
    def allowed_projects(request: Request) -> frozenset[str] | None:
        """Projects visible to this request, or ``None`` for legacy/all-project access."""
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal) or principal.project_visibility == "all":
            return None
        return principal.projects

    def visible_tasks(self, request: Request, store: Store | None = None) -> dict[str, Any]:
        s = store or self.hub.fresh()
        allowed = self.allowed_projects(request)
        return {task_id: task for task_id, task in s.tasks().items()
                if allowed is None or task.product in allowed}

    def visible_events(self, request: Request, events: list[dict[str, Any]],
                       tasks: dict[str, Any]) -> list[dict[str, Any]]:
        """Drop history that cannot be attributed to a visible project.

        Garden-wide events and operator ledger entries may contain private configuration,
        cost, or run facts, so restricted members only receive task/project-attributed rows.
        """
        if self.allowed_projects(request) is None:
            return events
        task_ids = set(tasks)
        projects = self.allowed_projects(request) or frozenset()
        return [event for event in events
                if (event.get("task") in task_ids
                    or (event.get("product") in projects and event.get("product")))]

    def ctx(
        self,
        request: Request,
        page: str = "",
        history: list[dict[str, Any]] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        hub = self.hub
        s = hub.fresh()
        sched = hub.reader()
        visible_tasks = self.visible_tasks(request, s)
        items = [item for item in build_inbox(s, sched)
                 if self.allowed_projects(request) is None or item.get("task") in visible_tasks]
        ctrl = sched.control()
        stops = sched.operating_profile_stops()
        active = sched.operating_profile_name()
        profile_overrides = sched.overrides()
        profile_facets = ("max_parallel", "review_parallel", "models", "review.difficulty",
                          "retro.difficulty", "observe.profile")
        overridden_facets = [key for key in profile_facets if key in profile_overrides]
        run_store = sched.runs
        visible_runs = [run for run in run_store.all_runs() if run.task_id in visible_tasks]
        visible_active_runs = [run for run in run_store.active() if run.task_id in visible_tasks]
        totals = run_store.totals() if self.allowed_projects(request) is None else _rollup(visible_runs)
        resources = sched.resource_status()
        rail_events = history if history is not None else EventLog(s.config.garden_dir / "events.jsonl").read()
        if self.allowed_projects(request) is None:
            rail_events += ops.to_cost_events(ops.read_records(ops.default_path(s.root)))
        rail_events = self.visible_events(request, rail_events, visible_tasks)
        rail_metrics = metrics(rail_events, visible_tasks)
        visible_products = [p for p in s.products()
                            if self.allowed_projects(request) is None or p.name in self.allowed_projects(request)]
        profile_tradeoffs = {
            "economy": (
                "Economy favors fewer concurrent runs and lower-cost models; "
                "completion time and total cost can still vary."
            ),
            "balanced": (
                "Balanced mixes concurrency, model capability, and cost; "
                "completion time and total cost can still vary."
            ),
            "fast": (
                "Fast favors more concurrency and higher-capability models; "
                "it may cost more and does not guarantee faster completion."
            ),
        }

        def profile_tradeoff_for(name: str) -> str:
            if name in profile_tradeoffs:
                return profile_tradeoffs[name]
            if name:
                return (
                    "This custom profile changes the requested concurrency and model mix; "
                    "its completion time and total cost can vary."
                )
            return (
                "Plain config uses individually configured concurrency and models; "
                "completion time and total cost depend on those settings."
            )

        profile_options = [{
            "value": "",
            "label": "Plain config",
            "stop_label": "Plain",
            "tradeoff": profile_tradeoff_for(""),
            "meaning": "No profile requested; using plain garden.yaml values.",
        }]
        profile_options.extend({
            "value": name,
            "label": name.capitalize(),
            "tradeoff": profile_tradeoff_for(name),
            "meaning": describe_stop(stop),
        } for name, stop in stops.items())
        # A live override may name a custom stop that was later removed from
        # garden.yaml.  The scheduler deliberately treats that as an empty
        # profile until the operator chooses another stop; keep it visible in
        # the rail rather than making the rendered control claim a different
        # selection (or fail to render).
        if active and active not in stops:
            profile_options.append({
                "value": active,
                "label": f"Unavailable: {active}",
                "tradeoff": profile_tradeoff_for(active),
                "meaning": "",
            })
        active_option = next(option for option in profile_options if option["value"] == active)
        return {
            "request": request,
            "page": page,
            "garden_name": s.config.get("name"),
            "root": str(s.root),
            "watch": hub.watch,
            "last_tick": hub.last_tick,
            "scheduler_status": hub.scheduler_health(),
            "coordinator_status": hub.coordinator_status(),
            "execution_status": hub.execution_status(),
            "server_now": now_iso(),  # the clock every live elapsed counter is offset against
            "products": visible_products,
            "has_design": any(product_design_root(s, p.name).is_dir() for p in visible_products),
            "phases_by_product": {p.name: [ph.name for ph in p.phases] for p in visible_products},
            "inbox_count": len(decisions(items)),
            "env": s.config.env,
            "running": [run for run in running_now(s) if run.get("task") in visible_tasks],
            "worker_busy": sum(run.runner != "manual" and run.mode in WORKER_MODES
                               for run in visible_active_runs),
            "workers_running": sum(run.runner != "manual" and run.mode in WORKER_MODES
                                   for run in visible_active_runs),
            "reviews_running": sum(run.runner != "manual" and run.mode in REVIEW_MODES
                                   for run in visible_active_runs),
            "max_parallel": sched.effective_max_parallel(),
            "review_parallel": sched.review_parallel_limit(),
            "resource_status": resources,
            "totals": totals,
            "dispatch_paused": ctrl.get("dispatch") == "paused",
            "pause_ctrl": ctrl,
            "closed_count": sum(1 for p in visible_products for ph in p.phases if ph.closed),
            "flash": request.query_params.get("flash", ""),
            "flash_note": request.query_params.get("flash_note", ""),
            "operating_profile_names": list(stops),
            # The empty value is a real, supported setting: it clears the live profile
            # override and leaves the garden's ordinary configuration in effect.  Include
            # it in the rail picker so its selected state is never represented as an
            # arbitrary named profile.
            "operating_profile_options": profile_options,
            "operating_profile": active,
            "operating_profile_label": active_option["label"],
            "operating_profile_source": (
                "live override" if "operating_profile" in profile_overrides
                else ("garden.yaml" if active else "plain garden.yaml values")
            ),
            "operating_profile_meaning": describe_stop(stops.get(active) or {}) if active else "",
            "operating_profile_tradeoff": profile_tradeoff_for(active),
            "operating_profile_overrides": overridden_facets,
            "operating_profile_spend_rate": (run_store.spend_since(parse_since("1h"))
                                               if self.allowed_projects(request) is None else 0.0),
            "rail_metrics": rail_metrics,
            # The installed revision is useful when diagnosing a served garden, but it is
            # secondary to the operational controls and warnings in the rail and Inbox.
            "tool_build": sched.upgrade_status(),
            **kw,
        }

    def board_data(self, product: str | None, phase: str | None, include_closed: bool = False,
                   allowed_projects: frozenset[str] | None = None) -> dict[str, Any]:
        s = self.hub.fresh()
        tasks = {task_id: task for task_id, task in s.tasks().items()
                 if allowed_projects is None or task.product in allowed_projects}
        sched = self.hub.reader(s)
        state = State(s.config.garden_dir / "state.json")
        closed_keys = closed_phase_keys(s)
        cols: dict[str, list] = {c: [] for c in COLUMNS}
        for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
            if allowed_projects is not None and t.product not in allowed_projects:
                continue
            if product and t.product != product:
                continue
            if phase and t.phase != phase:
                continue
            # closed phases stay off the board unless asked for (or picked explicitly)
            if t.key in closed_keys and not include_closed and (t.product, t.phase) != (product, phase):
                continue
            eff = sched.task_effective_status(t, tasks)
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
            cols[col].append({"task": t, "blockers": sched.task_blockers(t, tasks) if eff == "blocked" else [],
                              "stack": "" if merged_parent else st.get("stack_parent", ""),
                              "merged_parent": merged_parent,
                              "needs_human": "" if t.status.terminal else (needs_human_info(st.get("needs_human")) or {}).get("reason", ""),
                              "question": st.get("question", "") if eff == "waiting_human" else "",
                              "review": review if eff in ("awaiting_triage", "in_review", "changes_requested") else "",
                              "reason": _last_log_line(t) if eff == "failed" else ""})
        runs = RunStore(s.config.garden_dir)
        visible_tasks = {task_id: task for task_id, task in tasks.items()
                         if allowed_projects is None or task.product in allowed_projects}
        active = {r.task_id: r for r in runs.active() if r.task_id in visible_tasks}
        visible_runs = [r for r in runs.all_runs() if r.task_id in visible_tasks]
        return {"cols": cols, "active": active, "product": product, "phase": phase,
                "totals": runs.totals() if allowed_projects is None else _rollup(visible_runs),
                "closed": include_closed, "problems": validate(visible_tasks)}

    def backlog_data(self, product: str | None, include_closed: bool = False,
                     allowed_projects: frozenset[str] | None = None) -> dict[str, Any]:
        """The backlog view: each open phase of a product (all products when none is picked) as a
        section of its non-terminal tasks in dispatch order, plus what each row's controls need
        (the phases it can move to, whether a cross-phase move is allowed). Closed phases stay in
        the Herbarium unless `include_closed`."""
        s = self.hub.fresh()
        tasks = {task_id: task for task_id, task in s.tasks().items()
                 if allowed_projects is None or task.product in allowed_projects}
        sched = self.hub.reader(s)
        state = State(s.config.garden_dir / "state.json")
        runs = RunStore(s.config.garden_dir)
        active = {r.task_id: r for r in runs.active() if r.task_id in tasks}
        # Phases a row can move to: its product's phases, closed ones dropped (the row's own is
        # always kept so the pulldown shows where it is).
        move_phases: dict[str, list[str]] = {}
        sections: list[dict[str, Any]] = []
        for p in s.products():
            if allowed_projects is not None and p.name not in allowed_projects:
                continue
            if product and p.name != product:
                continue
            move_phases[p.name] = [ph.name for ph in p.phases if include_closed or not ph.closed]
            for ph in p.phases:
                if ph.closed and not include_closed:
                    continue
                rows = []
                for t in sorted(ph.tasks, key=dispatch_sort_key):
                    eff = sched.task_effective_status(t, tasks)
                    if eff in ("done", "cancelled", "wont_do"):
                        continue
                    st = state.get(t.id)
                    # A running or in-review task can be reordered but not moved to another phase.
                    movable = not (t.status == Status.RUNNING or t.status.pr_open or t.id in active)
                    rows.append({"task": t, "eff": eff,
                                 "blockers": sched.task_blockers(t, tasks) if eff == "blocked" else [],
                                 "needs_human": (needs_human_info(st.get("needs_human")) or {}).get("reason", ""),
                                 "movable": movable,
                                 "move_reason": "" if movable else f"{eff.replace('_', ' ')}: reorder it here, but finish or cancel the run before moving it"})
                sections.append({"phase": ph, "rows": rows})
        return {"sections": sections, "move_phases": move_phases, "active": active,
                "product": product, "phase": None, "closed": include_closed, "problems": validate(tasks)}

    def pr_data(self, product: str | None,
                allowed_projects: frozenset[str] | None = None) -> dict[str, Any]:
        """Build one repository's open-PR rows from the scheduler-owned observation."""
        s = self.hub.fresh()
        configured = [p.name for p in s.products() if s.config.product_github(p.name)
                      and (allowed_projects is None or p.name in allowed_projects)]
        selected = product or (configured[0] if configured else "")
        visible_tasks = {task_id: task for task_id, task in s.tasks().items()
                         if allowed_projects is None or task.product in allowed_projects}
        base = {"product": selected or None, "phase": None, "closed": False,
                "problems": validate(visible_tasks)}
        if selected not in configured:
            return {**base, "pr_product": selected, "pr_rows": [], "pr_error": "No GitHub repository is configured for this product."}

        route = s.config.product_github(selected)
        slug = RepositorySlug(route["slug"], route["host"])
        observation = State(s.config.garden_dir / "state.json").get("__open_prs__").get(selected) or {}
        prs = [PRInfo(**{k: v for k, v in row.items() if k in PRInfo.__dataclass_fields__})
               for row in observation.get("prs", [])]

        tasks_by_number: dict[int, Any] = {}
        for task in visible_tasks.values():
            if task.product != selected or not task.pr:
                continue
            number = pull_request_number(task.pr, str(slug), slug.host)
            if number is not None:
                tasks_by_number[number] = task

        validation = s.config.product_validation(selected)["provider"]
        rows = []
        raw_rows = {int(row["number"]): row for row in observation.get("prs", [])}
        for pr in prs:
            task = tasks_by_number.get(pr.number)
            if task is not None and task.status.terminal:
                continue
            task_state = State(s.config.garden_dir / "state.json").get(task.id) if task is not None else {}
            checks = pr.checks
            if validation == "command" and task is not None:
                # The scheduler's exact-head observation is the only evidence here, and it
                # counts for this row only while it is green and bound to this same head.
                exact_head = task_state.get("ci_status") or {}
                checks = "SUCCESS" if (pr.head_sha and exact_head.get("green")
                                       and exact_head.get("queried_sha") == pr.head_sha) else ""
            rows.append({
                "pr": pr,
                "task": task,
                "safe_url": pr.url if is_safe_pr_url(pr.url) else "",
                "review": pr.review_decision.replace("_", " ").lower() or "not reported",
                "checks": self._pr_check_state(checks, validation),
                "new_feedback": int(raw_rows[pr.number].get("new_feedback") or 0),
                "feedback_count": int(raw_rows[pr.number].get("feedback_count") or 0),
            })
        return {**base, "pr_product": selected, "pr_rows": rows,
                "pr_error": str(observation.get("error") or ""),
                "pr_stale": bool(observation.get("stale")),
                "pr_refreshed_at": str(observation.get("refreshed_at") or "")}

    @staticmethod
    def _pr_check_state(checks: str, provider: str) -> str:
        if checks:
            return checks.replace("_", " ").lower()
        if provider == "none":
            return "not configured"
        if provider == "command":
            return "not available (configured command)"
        return "not reported"
