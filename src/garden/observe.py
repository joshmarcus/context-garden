"""`garden observe`: the one feed an operator (a person, or an agent's heartbeat) reads the
garden through — a status line, cards that need a hand, stuck runs, tracebacks and a digest
of the window, all in one pass. Replaces a hand-rolled heartbeat script and a firehose event
tail with one command whose cadence and volume are config, not a script someone maintains
(CG-219). Offline like `inbox.py`: everything here reads `Store`/`State`/`EventLog`/`RunStore`,
never the network.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .events import EventLog, parse_duration
from .events import digest as _digest
from .inbox import build_inbox, decisions
from .model import STATUS_ORDER, now_iso
from .runs import RunStore

# The fields a profile may set; anything a profile omits leaves the caller's base value (the
# garden.yaml `observe:` block, or the built-in defaults) standing.
PROFILE_FIELDS = ("interval", "digest_window", "events", "stuck_after", "line_width", "phases")

# Built-ins, named in the task brief: "sometimes you want to run efficiently, sometimes you
# want more observability." `observe.profile` or `--profile` picks one by name; a same-named
# entry under `observe.profiles` in garden.yaml replaces a built-in outright.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "quiet": {
        "interval": "30m", "digest_window": "30m",
        "events": ["question", "needs_human", "failed"],
    },
    "watch": {
        "interval": "10m", "digest_window": "10m",
        "events": ["question", "needs_human", "failed", "decision", "stall", "budget",
                   "phase", "retro", "review_changes_requested"],
    },
    "debug": {
        "interval": "5m", "stuck_after": "5m",
        "events": ["transition", "dispatch", "review", "merge"],
    },
}


# ---- event kinds and aliases -------------------------------------------------------------
# `observe.events` names either a literal event kind (as emitted by EventLog.emit: transition,
# dispatch, review, needs_human, stall, decision, budget, ...) or one of the aliases below, for
# a kind that only means something to a watching operator with one more field checked: a
# worker's question is a `waiting_human` event (not a `decision`, which is a wont_do/no_change
# call), a failure is a `transition` to `failed`, and so on.
def _is_question(ev: dict[str, Any]) -> bool:
    return ev.get("kind") == "waiting_human"


def _is_failed(ev: dict[str, Any]) -> bool:
    return ev.get("kind") == "transition" and ev.get("to") == "failed"


def _is_phase(ev: dict[str, Any]) -> bool:
    return ev.get("kind") in ("phase_closed", "phase_reopened")


def _is_retro(ev: dict[str, Any]) -> bool:
    return str(ev.get("kind") or "").startswith("retro_")


def _is_review_changes_requested(ev: dict[str, Any]) -> bool:
    return ev.get("kind") == "review" and str(ev.get("verdict") or "") == "request_changes"


def _is_merge(ev: dict[str, Any]) -> bool:
    return ev.get("kind") in ("merge_head", "automerged") or (ev.get("kind") == "transition" and ev.get("to") == "done")


EVENT_ALIASES: dict[str, Callable[[dict[str, Any]], bool]] = {
    "question": _is_question,
    "failed": _is_failed,
    "phase": _is_phase,
    "retro": _is_retro,
    "review_changes_requested": _is_review_changes_requested,
    "merge": _is_merge,
}


def event_matches(ev: dict[str, Any], wanted: list[str]) -> bool:
    """Whether `ev` is one of the configured `observe.events` kinds or aliases (see above)."""
    kind = str(ev.get("kind") or "")
    for name in wanted:
        if name == kind:
            return True
        pred = EVENT_ALIASES.get(name)
        if pred and pred(ev):
            return True
    return False


def format_event_line(ev: dict[str, Any]) -> str:
    """One line for a streamed `--follow` event: time, task, kind, and whatever it carries."""
    extra = {k: v for k, v in ev.items() if k not in ("at", "kind", "task") and v not in ("", None, False, 0)}
    bits = " ".join(f"{k}={v}" for k, v in extra.items())
    at = str(ev.get("at") or "")[5:19]
    task = str(ev.get("task") or "")
    return f"{at}  {task:<8} {str(ev.get('kind') or ''):<12} {bits}".rstrip()


# ---- resolving the effective settings ----------------------------------------------------
@dataclass
class ObserveSettings:
    interval_s: int
    digest_window_s: int
    events: list[str]
    stuck_after_s: int
    line_width: int
    phases: str | list[str]
    profile: str


def resolve(cfg: Any, sched: Any = None, profile_override: str = "") -> ObserveSettings:
    """The effective `garden observe` settings: the garden.yaml `observe:` block, then the
    selected profile's fields on top (a profile only needs to name the fields it changes).
    Profile selection, in order: `profile_override` (`--profile`), a live override
    (`garden set observe.profile ...` or the Config page, via `sched.effective`, so a running
    `--follow` picks up a switch on its next pass), then `observe.profile` in garden.yaml. An
    unrecognised profile name is ignored and the base fields stand."""
    base = dict(cfg.get("observe") or {})
    profiles = dict(BUILTIN_PROFILES)
    profiles.update({k: v for k, v in (base.get("profiles") or {}).items() if isinstance(v, dict)})
    profile = profile_override or (sched.effective("observe.profile", "") if sched is not None else "") or str(base.get("profile") or "")
    merged = dict(base)
    preset = profiles.get(profile)
    if preset:
        for k in PROFILE_FIELDS:
            if k in preset:
                merged[k] = preset[k]
    return ObserveSettings(
        interval_s=parse_duration(merged.get("interval", "30m")),
        digest_window_s=parse_duration(merged.get("digest_window", "30m")),
        events=list(merged.get("events") or []),
        stuck_after_s=parse_duration(merged.get("stuck_after", "15m")),
        line_width=int(merged.get("line_width", 160) or 160),
        phases=merged.get("phases", "open"),
        profile=profile,
    )


# ---- the pieces of one pass ---------------------------------------------------------------
def _phase_keys(store: Any, phases: str | list[str]) -> set[str] | None:
    """None means "every phase"; otherwise the "product/phase" keys to count. `phases: open`
    (the default) is every phase that is not closed."""
    if isinstance(phases, list):
        return set(phases)
    if phases == "open":
        return {ph.key for prod in store.products() for ph in prod.phases if not ph.closed}
    return None


def status_line(store: Any, sched: Any, settings: ObserveSettings) -> str:
    """service, slots, spend, and counts per status (blocked included, as `garden status`
    computes it) for the configured phases."""
    from .graph import effective_status

    tasks = store.tasks()
    stack = bool(store.config.get("stack", True))
    keys = _phase_keys(store, settings.phases)
    counts: dict[str, int] = {}
    for t in tasks.values():
        if keys is not None and t.key not in keys:
            continue
        s = effective_status(t, tasks, stack)
        counts[s] = counts.get(s, 0) + 1
    count_bits = " ".join(f"{s} {counts[s]}" for s in [*STATUS_ORDER, "blocked"] if counts.get(s))
    totals = RunStore(store.config.garden_dir).totals()
    bits = [
        f"garden: {store.config.get('name')}",
        f"service {_service_state(store, sched)}",
        f"workers {len(sched.worker_runs_active())}/{sched.effective_max_parallel()}",
        f"spend ${totals['cost_usd']:.2f}",
    ]
    if count_bits:
        bits.append(count_bits)
    return "  ".join(bits)


def _service_state(store: Any, sched: Any) -> str:
    """ok | paused | stale (Nm) — the loop's pulse. `.garden/state.json`'s mtime is the tick
    clock (every pass re-reads it, then rewrites it at the end); more than three ticks since
    the last write means `garden serve`/`garden watch` is dead or wedged, the first thing
    worth flagging (this is what the operator used to run `stat -c %y .garden/state.json` for)."""
    if sched.is_dispatch_paused():
        return "paused"
    path = store.config.garden_dir / "state.json"
    if not path.exists():
        return "no runs yet"
    age = time.time() - path.stat().st_mtime
    tick_interval = float(store.config.get("tick_interval", 60) or 60)
    if age > tick_interval * 3:
        return f"stale ({round(age / 60)}m)"
    return "ok"


def cards(store: Any, sched: Any) -> list[dict[str, Any]]:
    """One line's worth of data per decision the inbox is waiting on: the task, its kind and
    the action (from inbox.py's decision table) that clears it."""
    out = []
    for it in decisions(build_inbox(store, sched)):
        command = next((a["command"] for a in it["actions"] if a.get("command")), "")
        out.append({"task": it["task"], "title": it["title"], "group": it["group"], "why": it["why"], "command": command})
    return out


def card_line(c: dict[str, Any]) -> str:
    return f"{c['task'] or '-':<10} {c['group']:<10} {c['why'][:70]:<70} {c['command']}".rstrip()


def stuck_runs(store: Any, stuck_after_s: int) -> list[dict[str, Any]]:
    """Active runs with no output for longer than `stuck_after_s`, or whose process is gone
    (an `exit_code` file landed, or the pid died) without being reaped yet."""
    rs = RunStore(store.config.garden_dir)
    tasks = store.tasks()
    out = []
    for r in rs.active():
        gone = r.process_finished()
        idle = r.idle_minutes()
        if not (gone or idle * 60 >= stuck_after_s):
            continue
        t = tasks.get(r.task_id)
        out.append({"task": r.task_id, "title": t.title if t else "", "mode": r.mode,
                    "idle_minutes": round(idle), "gone": gone})
    return out


def stuck_line(s: dict[str, Any]) -> str:
    why = "process is gone" if s["gone"] else f"idle {s['idle_minutes']}m"
    return f"{s['task']:<10} {s['mode']:<8} {why}  {s['title'][:60]}".rstrip()


def tracebacks(store: Any, since_iso: str) -> list[dict[str, Any]]:
    """Runs since `since_iso` whose stderr shows an unhandled Python exception — a bug in the
    harness wrapper or the scheduler itself, not an ordinary task failure the worker reported.
    Bounded to the digest window so a pass never re-scans the whole run history."""
    rs = RunStore(store.config.garden_dir)
    out = []
    for r in rs.all_runs():
        stamp = r.finished_at or r.started_at
        if stamp < since_iso:
            continue
        text = r.stderr_text()
        if "Traceback (most recent call last)" not in text:
            continue
        last = [ln for ln in text.splitlines() if ln.strip()]
        out.append({"task": r.task_id, "run": r.run_id, "mode": r.mode, "line": (last[-1][:200] if last else "")})
    return out


def traceback_line(t: dict[str, Any]) -> str:
    return f"{t['task']:<10} {t['run']:<24} {t['line']}".rstrip()


def digest_lines(d: dict[str, Any]) -> list[str]:
    """`garden digest`'s summary, trimmed to a few lines."""
    out = []
    if d["prs_opened"]:
        out.append(f"{len(d['prs_opened'])} PR(s) opened")
    if d["merged"]:
        by_garden = len({ev.get("task") for ev in d["automerged"]})
        out.append(f"{len(d['merged'])} merged" + (f" ({by_garden} by the garden)" if by_garden else ""))
    if d["reviews"]:
        out.append(f"{len(d['reviews'])} automated review(s)")
    if d["discovered"]:
        out.append(f"{len(d['discovered'])} discovered task(s) to approve")
    if d["needs_human"]:
        n = len({(ev["task"], ev.get("kind")) for ev in d["needs_human"]})
        out.append(f"{n} decision(s) needed you")
    if d["failures"]:
        out.append(f"{len(d['failures'])} failed run(s)")
    if d["dispatched"] or d["cost_usd"]:
        out.append(f"${d['cost_usd']:.2f} spent, {d['dispatched']} dispatch(es)")
    return out


def _clip(text: str, width: int) -> str:
    """Clip `text` to `width` characters (an ellipsis marks the cut), so a line stays one
    line regardless of the terminal's actual width — the point of `observe.line_width`: an
    operator agent parsing the text output gets a predictable format, not a soft wrap that
    turns one card into two lines."""
    if width <= 0 or len(text) <= width:
        return text
    return text[: max(0, width - 1)].rstrip() + "…"


# ---- one pass, and the text/json it renders as --------------------------------------------
@dataclass
class ObservePass:
    at: str
    profile: str
    status_line: str
    cards: list[dict[str, Any]] = field(default_factory=list)
    stuck: list[dict[str, Any]] = field(default_factory=list)
    tracebacks: list[dict[str, Any]] = field(default_factory=list)
    digest: dict[str, Any] = field(default_factory=dict)
    digest_lines: list[str] = field(default_factory=list)
    line_width: int = 160

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at, "profile": self.profile, "status_line": self.status_line,
            "cards": self.cards, "stuck": self.stuck, "tracebacks": self.tracebacks,
            "digest": {k: v for k, v in self.digest.items() if k != "tasks"}, "digest_lines": self.digest_lines,
        }

    def render_lines(self) -> list[str]:
        w = self.line_width
        out = [_clip(self.status_line, w)]
        if self.cards:
            out.append("")
            out.append(f"needs you ({len(self.cards)})")
            out += [_clip(f"  {card_line(c)}", w) for c in self.cards]
        if self.stuck:
            out.append("")
            out.append(f"stuck runs ({len(self.stuck)})")
            out += [_clip(f"  {stuck_line(s)}", w) for s in self.stuck]
        if self.tracebacks:
            out.append("")
            out.append(f"tracebacks ({len(self.tracebacks)})")
            out += [_clip(f"  {traceback_line(t)}", w) for t in self.tracebacks]
        if self.digest_lines:
            out.append("")
            out.append("digest")
            out += [_clip(f"  {line}", w) for line in self.digest_lines]
        return out


def _since_iso(seconds: int) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def make_pass(store: Any, sched: Any, settings: ObserveSettings) -> ObservePass:
    since_iso = _since_iso(settings.digest_window_s)
    ev_log = EventLog(store.config.garden_dir / "events.jsonl")
    events = ev_log.read(since=since_iso)
    d = _digest(events)
    return ObservePass(
        at=now_iso(), profile=settings.profile, status_line=status_line(store, sched, settings),
        cards=cards(store, sched), stuck=stuck_runs(store, settings.stuck_after_s),
        tracebacks=tracebacks(store, since_iso), digest=d, digest_lines=digest_lines(d),
        line_width=settings.line_width,
    )


def follow_pass(store: Any, settings: ObserveSettings, since: str, log: Callable[[str], None]) -> str:
    """Read events emitted since `since`, print one line (via `log`) for each that matches
    `settings.events` (literal kinds or the aliases above), and return the new cursor. The CLI's
    `--follow` loop calls this once per `observe.interval`; it is also the unit tested surface
    for "streams only the configured events", independent of the sleep loop around it."""
    ev_log = EventLog(store.config.garden_dir / "events.jsonl")
    events = ev_log.read(since=since)
    for ev in events:
        if event_matches(ev, settings.events):
            log(format_event_line(ev))
    return now_iso()
