"""Local web UI: a board, task pages, the graph, runs and cost. FastAPI + Jinja + HTMX.

All logic lives in store/graph/scheduler; this module only renders and forwards actions.
The scheduler loop runs in a background thread when `watch=True` (the `garden serve` default).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import markdown as md
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..brief import build_brief
from ..charts import burnup_svg, tier_bars_svg
from ..events import EventLog, digest, metrics, parse_since, phase_summary
from ..graph import (
    blockers,
    critical_path,
    dependents,
    effective_status,
    mermaid,
    ready,
    svg,
    validate,
)
from ..inbox import attention_view, build_inbox, needs_human_info, running_now
from ..model import STATUS_ORDER, Status, now_iso
from ..plants import (
    DEFS,
    PLATE_CREDIT,
    plant_info,
    plant_svg,
    plate_filename,
    stage_svg,
    stage_word,
    vine_svg,
)
from ..review import review_to_markdown
from ..runs import RunStore
from ..scheduler import Scheduler, State
from ..store import Store
from ..trials import TrialLog, ranking_markdown

TEMPLATES = Path(__file__).parent / "templates"
PLATES_DIR = Path(__file__).parent / "static" / "plates"  # scanned plates, written by `garden plants --fetch`
COLUMNS = ["draft", "blocked", "ready", "running", "waiting_human", "awaiting_triage", "in_review", "changes_requested", "done", "failed", "wont_do"]


class Hub:
    """Shared state for request handlers: the store, a lock around scheduler passes, and
    a log of recent tick results."""

    def __init__(self, store: Store, watch: bool):
        self.store = store
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.last_tick = ""
        self.watch = watch
        self.planning: dict[str, str] = {}  # "product/phase" -> status text
        self._stop = threading.Event()
        if watch:
            threading.Thread(target=self._loop, daemon=True, name="garden-watch").start()

    def scheduler(self) -> Scheduler:
        self.store.invalidate()
        return Scheduler(self.store, log=self._log)

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
    return md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])


def create_app(store: Store, watch: bool = False, plates_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="context-garden")
    hub = Hub(store, watch)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["md"] = render_md
    templates.env.globals["columns"] = COLUMNS
    templates.env.globals["statuses"] = STATUS_ORDER
    templates.env.globals["DEFS"] = DEFS
    templates.env.globals["VINE"] = Markup(vine_svg())
    plates = plates_dir or PLATES_DIR
    plates.mkdir(parents=True, exist_ok=True)
    app.mount("/static/plates", StaticFiles(directory=str(plates)), name="plates")

    def plate_url(key: str, thumb: bool = False) -> str:
        """The scanned plate for a plant when it has been fetched, else '' (the drawing is used)."""
        name = plate_filename(key, thumb=thumb)
        if (plates / name).exists():
            return f"/static/plates/{name}"
        if thumb and (plates / plate_filename(key)).exists():
            return f"/static/plates/{plate_filename(key)}"
        return ""

    templates.env.globals["plate"] = plate_url
    templates.env.globals["PLATE_CREDIT"] = PLATE_CREDIT
    # The drawings are trusted SVG built from fixed symbols; mark them safe so Jinja does not escape them.
    templates.env.globals["plant"] = lambda *a, **k: Markup(plant_svg(*a, **k))
    templates.env.globals["stage"] = lambda *a, **k: Markup(stage_svg(*a, **k))
    templates.env.globals["stage_word"] = stage_word
    templates.env.globals["plant_info"] = plant_info

    def ctx(request: Request, page: str = "", **kw: Any) -> dict[str, Any]:
        s = hub.fresh()
        sched = Scheduler(s, log=lambda m: None)
        items = build_inbox(s, sched)
        ctrl = sched.control()
        return {
            "request": request,
            "page": page,
            "garden_name": s.config.get("name"),
            "root": str(s.root),
            "watch": hub.watch,
            "last_tick": hub.last_tick,
            "products": s.products(),
            "inbox_count": len(items),
            "env": s.config.env,
            "running": running_now(s),
            "totals": RunStore(s.config.garden_dir).totals(),
            "dispatch_paused": ctrl.get("dispatch") == "paused",
            "pause_ctrl": ctrl,
            "closed_count": sum(1 for p in s.products() for ph in p.phases if ph.closed),
            **kw,
        }

    def tier_rows(s: Store, tasks: dict[str, Any]) -> list[dict[str, Any]]:
        m = metrics(EventLog(s.config.garden_dir / "events.jsonl").read(), tasks)
        return [{"tier": t, **m["by_difficulty"][t]} for t in ("easy", "medium", "hard") if m["by_difficulty"].get(t)]

    def closed_phase_keys(s: Store) -> set[str]:
        return {ph.key for p in s.products() for ph in p.phases if ph.closed}

    def board_data(product: str | None, phase: str | None, include_closed: bool = False) -> dict[str, Any]:
        s = hub.fresh()
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
            cols[eff].append({"task": t, "blockers": blockers(t, tasks, stack) if eff == "blocked" else [],
                              "stack": st.get("stack_parent", ""),
                              "needs_human": (needs_human_info(st.get("needs_human")) or {}).get("reason", ""),
                              "question": st.get("question", "") if eff == "waiting_human" else ""})
        runs = RunStore(s.config.garden_dir)
        active = {r.task_id: r for r in runs.active()}
        return {"cols": cols, "active": active, "product": product, "phase": phase, "totals": runs.totals(),
                "closed": include_closed, "problems": validate(tasks)}

    # ---- pages -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    @app.get("/inbox", response_class=HTMLResponse, include_in_schema=False)
    def inbox_page(request: Request):
        from ..inbox import GROUPS

        s = hub.fresh()
        sched = Scheduler(s, log=lambda m: None)
        items = build_inbox(s, sched)
        tasks = s.tasks()
        evs = EventLog(s.config.garden_dir / "events.jsonl")
        open_tasks = [t for t in tasks.values() if not t.status.terminal and t.status.value != "cancelled"]
        in_scope = [t for t in tasks.values() if t.status.value != "cancelled"]
        spent_24h = digest(evs.read(since=parse_since("24h")))["cost_usd"]
        return templates.TemplateResponse(request, "inbox.html", ctx(
            request, page="inbox", items=items, groups=GROUPS, prs_open=sum(1 for t in open_tasks if t.pr),
            spent_24h=spent_24h, burnup=burnup_svg(evs.read(), len(in_scope), done_ids={t.id for t in in_scope if t.status.value == 'done'}), tiers=tier_bars_svg(tier_rows(s, tasks))))

    @app.get("/board", response_class=HTMLResponse)
    def board(request: Request, product: str | None = None, phase: str | None = None, closed: bool = False):
        return templates.TemplateResponse(request, "board.html", ctx(request, page="board", **board_data(product, phase, closed)))

    @app.get("/partials/board", response_class=HTMLResponse)
    def board_partial(request: Request, product: str | None = None, phase: str | None = None, closed: bool = False):
        return templates.TemplateResponse(request, "_board.html", ctx(request, **board_data(product, phase, closed)))

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_page(request: Request, task_id: str):
        s = hub.fresh()
        try:
            t = s.task(task_id)
        except KeyError:
            raise HTTPException(404) from None
        tasks = s.tasks()
        stack = bool(s.config.get("stack", True))
        rs = RunStore(s.config.garden_dir)
        runs = rs.runs_for(t.id)
        latest_run = rs.latest(t.id)
        st = State(s.config.garden_dir / "state.json").get(t.id)
        body, log = _split_log(t.body)
        evs = EventLog(s.config.garden_dir / "events.jsonl").read(task_id=t.id)
        usage = rs.usage_for(t.id)
        initial_stdout = latest_run.stdout_events() if latest_run else []
        from ..friction import extract_friction, pr_body_for
        from ..personas import DEFAULT_PERSONAS, list_personas

        friction_text = extract_friction(pr_body_for(t, rs))

        return templates.TemplateResponse(request, "task.html", ctx(
            request, page="task", personas=sorted(set(list_personas(s)) | set(DEFAULT_PERSONAS)),
            task=t, eff=effective_status(t, tasks, stack), blockers=blockers(t, tasks, stack), usage=usage,
            dependents=dependents(t.id, tasks), runs=list(reversed(runs)), state=st, body_html=render_md(body),
            log_lines=log, rel=s.rel(t.path), events=list(reversed(evs))[:60],
            discovered=[x for x in tasks.values() if x.discovered_from == t.id],
            review_md=review_to_markdown(st["last_review"]) if st.get("last_review") else "",
            friction_text=friction_text,
            initial_stdout=initial_stdout,
            attention=attention_view(t, st, rs),
        ))

    @app.get("/partials/tasks/{task_id}/runs", response_class=HTMLResponse)
    def task_runs_partial(request: Request, task_id: str):
        s = hub.fresh()
        t = s.task(task_id)
        runs = RunStore(s.config.garden_dir).runs_for(t.id)
        return templates.TemplateResponse(request, "_runs.html", ctx(request, runs=list(reversed(runs)), task=t))

    @app.get("/partials/tasks/{task_id}/stdout", response_class=HTMLResponse)
    def task_stdout_partial(request: Request, task_id: str):
        s = hub.fresh()
        rs = RunStore(s.config.garden_dir)
        run = rs.latest(task_id)
        events = run.stdout_events() if run else []
        return templates.TemplateResponse(request, "_stdout.html", ctx(request, events=events))

    @app.get("/tasks/{task_id}/brief", response_class=PlainTextResponse)
    def task_brief(task_id: str, revise: bool = False):
        s = hub.fresh()
        t = s.task(task_id)
        fb = str(State(s.config.garden_dir / "state.json").get(t.id).get("pending_feedback") or "") if revise else ""
        b = build_brief(s, t, review_feedback=fb)
        return f"# ~{b.tokens:,} tokens\n\n" + b.text

    @app.get("/tasks/{task_id}/log", response_class=PlainTextResponse)
    def task_log(task_id: str, run_id: str | None = None):
        s = hub.fresh()
        rs = RunStore(s.config.garden_dir)
        runs = rs.runs_for(task_id)
        run = next((r for r in runs if r.run_id == run_id), None) if run_id else rs.latest(task_id)
        if not run:
            return "no runs"
        parts = [f"run {run.run_id}  status={run.status}  runner={run.runner}  mode={run.mode}  dir={run.dir}"]
        if run.error:
            parts.append(f"error: {run.error}")
        final = run.path / "final.md"
        if final.exists():
            parts.append("---- final message ----\n" + final.read_text())
        stderr = run.stderr_text()
        if stderr.strip():
            parts.append("---- stderr ----\n" + stderr[-8000:])
        return "\n\n".join(parts)

    @app.get("/trellis", response_class=HTMLResponse)
    @app.get("/graph", response_class=HTMLResponse, include_in_schema=False)
    def trellis_page(request: Request, product: str | None = None, phase: str | None = None, closed: bool = False):
        s = hub.fresh()
        closed_keys = closed_phase_keys(s)
        tasks = {k: v for k, v in s.tasks().items()
                 if (not product or v.product == product) and (not phase or v.phase == phase)
                 and (closed or v.key not in closed_keys or (v.product, v.phase) == (product, phase))}
        try:
            cp = critical_path(tasks)
        except Exception:  # noqa: BLE001
            cp = []
        return templates.TemplateResponse(request, "trellis.html", ctx(
            request, page="trellis", svg=svg(tasks, stack=bool(s.config.get("stack", True))), mermaid=mermaid(tasks), product=product, phase=phase,
            closed=closed, critical=cp, ready=[t.id for t in ready(tasks)], problems=validate(tasks)))

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        s = hub.fresh()
        rs = RunStore(s.config.garden_dir)
        return templates.TemplateResponse(request, "runs.html", ctx(
            request, page="runs", runs=list(reversed(rs.all_runs())), events=list(reversed(hub.events))[:100]))

    @app.get("/trials", response_class=HTMLResponse)
    def trials_page(request: Request):
        s = hub.fresh()
        log = TrialLog(s.config.garden_dir / "trials.jsonl")
        return templates.TemplateResponse(request, "trials.html", ctx(
            request, page="trials", rows=log.leaderboard(), trials=[(t, ranking_markdown(t)) for t in reversed(log.read())]))

    @app.get("/events", response_class=HTMLResponse)
    def events_page(request: Request, since: str = "24h"):
        s = hub.fresh()
        since_iso = parse_since(since) if since else ""
        evs = EventLog(s.config.garden_dir / "events.jsonl").read(since=since_iso)
        d = digest(evs)
        tasks = s.tasks()
        return templates.TemplateResponse(request, "events.html", ctx(
            request, page="events", events=list(reversed(evs))[:300], digest=d, since=since, tasks=tasks,
            metrics=metrics(EventLog(s.config.garden_dir / "events.jsonl").read(), tasks), tiers=tier_bars_svg(tier_rows(s, tasks))))

    @app.get("/phases/{product}/{phase}", response_class=HTMLResponse)
    def phase_page(request: Request, product: str, phase: str):
        s = hub.fresh()
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        tasks = s.tasks()
        from ..model import goals_text

        goals = goals_text(ph.goals_path)
        specs = [(s.rel(p), p.read_text()) for p in ph.specs]
        docs = [(s.rel(p), p.read_text()) for p in ph.docs if p.suffix == ".md"]
        state = State(s.config.garden_dir / "state.json")
        stack = bool(s.config.get("stack", True))
        sched = hub.scheduler()
        phase_tasks = {t.id: t for t in ph.tasks}
        all_events = EventLog(s.config.garden_dir / "events.jsonl").read()
        m = metrics(all_events, phase_tasks)
        reviews = sorted((ph.path / "docs" / "reviews").glob("*.md"), reverse=True) if (ph.path / "docs" / "reviews").exists() else []
        usage = RunStore(s.config.garden_dir).usage_by_task()
        in_scope = [t for t in ph.tasks if t.status.value != "cancelled"]
        merged = sum(1 for t in in_scope if t.status.value == "done")
        prs_open = sum(1 for t in in_scope if t.pr and t.status.value != "done")
        complete = bool(in_scope) and merged == len(in_scope)
        sheet = {"merged": merged, "total": len(in_scope), "prs_open": prs_open, "complete": complete, "info": plant_info(ph.plant)}
        spent = sched.spent_for(ph.key)

        if ph.closed:
            # the closing header: the record of what the phase did, no working controls
            summary = phase_summary(all_events, phase_tasks)

            def doc_url(p: Path) -> str:
                return f"/phases/{ph.product}/{ph.name}/doc/{p.relative_to(ph.path)}"

            merged_rows = [{"t": t, "number": t.pr.rsplit("/", 1)[-1], "merged": (summary["done_at"].get(t.id) or "")[:10]}
                           for t in ph.tasks if t.pr and t.status.value == "done"]
            unmerged_rows = [{"t": t, "why": (_split_log(t.body)[1] or ["closed unmerged"])[-1]}
                             for t in ph.tasks if t.pr and t.status.value != "done"]
            review_heads = []
            for p in reviews:
                head = _review_head(p)
                head["url"] = doc_url(p)
                head["tasks"] = [t for t in ph.tasks if t.discovered_from == f"persona:{head['persona']}"]
                review_heads.append(head)
            closing = next((p for p in ph.docs if "closing" in p.name and p.suffix == ".md"), None)
            friction = ph.path / "docs" / "friction.md"
            artifacts = [("closing document", doc_url(closing))] if closing else []
            if friction.exists():
                artifacts.append(("friction report (docs/friction.md)", doc_url(friction)))
            artifacts += [(s.rel(p), doc_url(p)) for p in ph.specs]
            artifacts += [(s.rel(p), doc_url(p)) for p in ph.docs
                          if p.suffix == ".md" and p != closing and p != friction and "reviews" not in p.parts]
            trials_n = sum(1 for tr in TrialLog(s.config.garden_dir / "trials.jsonl").read() if tr.get("task") in phase_tasks)
            return templates.TemplateResponse(request, "phase_closed.html", ctx(
                request, page="phase", phase_key=ph.key, phase=ph, goals_html=render_md(goals), sheet=sheet,
                summary=summary, metrics=m, spent=spent, review_heads=review_heads, artifacts=artifacts,
                trials_n=trials_n, merged_rows=merged_rows, unmerged_rows=unmerged_rows,
                rows=[(t, effective_status(t, tasks, stack), state.get(t.id), usage.get(t.id, {}))
                      for t in sorted(ph.tasks, key=lambda t: (t.priority, t.id))],
            ))

        fixed = build_brief(s, ph.tasks[0], include_rules=True) if ph.tasks else None
        fixed_tokens = fixed.fixed_tokens if fixed else 0
        from ..brief import estimate_brief_tokens
        from ..personas import DEFAULT_PERSONAS, list_personas

        phase_events = [e for e in all_events if e.get("task") in phase_tasks]
        return templates.TemplateResponse(request, "phase.html", ctx(
            request, page="phase", phase_key=ph.key, phase=ph, goals_html=render_md(goals), specs=specs, docs=docs,
            sheet=sheet,
            burnup=burnup_svg(phase_events, len(in_scope), done_ids={t.id for t in in_scope if t.status.value == 'done'}), tiers=tier_bars_svg(tier_rows(s, phase_tasks)),
            personas=sorted(set(list_personas(s)) | set(DEFAULT_PERSONAS)), reviews=[(s.rel(p), p.read_text()) for p in reviews[:10]],
            budget=sched.budget_for(ph.key), spent=spent, metrics=m,
            rows=[(t, effective_status(t, tasks, stack), state.get(t.id), usage.get(t.id, {}), fixed_tokens + estimate_brief_tokens(s, t)[1]) for t in sorted(ph.tasks, key=lambda t: (t.priority, t.id))],
            planning=hub.planning.get(ph.key, ""), fixed_tokens=fixed_tokens,
        ))

    @app.get("/herbarium", response_class=HTMLResponse)
    def herbarium(request: Request):
        s = hub.fresh()
        all_events = EventLog(s.config.garden_dir / "events.jsonl").read()
        sched = Scheduler(s, log=lambda m: None)
        entries = []
        for p in s.products():
            for ph in p.phases:
                if not ph.closed:
                    continue
                phase_tasks = {t.id: t for t in ph.tasks}
                friction = ph.path / "docs" / "friction.md"
                closing = next((f for f in ph.docs if "closing" in f.name and f.suffix == ".md"), None)
                entries.append({
                    "phase": ph, "info": plant_info(ph.plant),
                    "summary": phase_summary(all_events, phase_tasks),
                    "spent": sched.spent_for(ph.key),
                    "friction_url": f"/phases/{ph.product}/{ph.name}/doc/docs/friction.md" if friction.exists() else "",
                    "closing_url": f"/phases/{ph.product}/{ph.name}/doc/{closing.relative_to(ph.path)}" if closing else "",
                })
        entries.sort(key=lambda e: str(e["phase"].closed), reverse=True)
        groups: list[tuple[str, list]] = []
        if len({e["phase"].product for e in entries}) > 1:
            for e in entries:
                if not groups or groups[-1][0] != e["phase"].product:
                    groups.append((e["phase"].product, []))
                groups[-1][1].append(e)
        else:
            groups = [("", entries)]
        return templates.TemplateResponse(request, "herbarium.html", ctx(request, page="herbarium", groups=groups, n=len(entries)))

    @app.get("/phases/{product}/{phase}/doc/{name:path}", response_class=HTMLResponse)
    def phase_doc(request: Request, product: str, phase: str, name: str):
        s = hub.fresh()
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        target = (ph.path / name).resolve()
        allowed = {p.resolve() for p in [*ph.docs, *ph.specs]}
        if target not in allowed or target.suffix != ".md":
            raise HTTPException(404)
        return templates.TemplateResponse(request, "doc.html", ctx(
            request, page="phase", phase_key=ph.key, phase=ph, name=name, doc_html=render_md(target.read_text())))

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request):
        s = hub.fresh()
        cfg = s.config
        effective = {
            "max_parallel": cfg.get("max_parallel"),
            "auto_dispatch": cfg.get("auto_dispatch"),
            "auto_revise": cfg.get("auto_revise"),
            "tick_interval": cfg.get("tick_interval"),
            "review.enabled": cfg.get("review.enabled"),
            "review.max_rounds": cfg.get("review.max_rounds"),
            "review.difficulty": cfg.get("review.difficulty") or "(task tier)",
            "github.draft_pr": cfg.get("github.draft_pr"),
            "stack": cfg.get("stack"),
        }
        budgets = dict(cfg.get("budgets") or {})
        for pname, pdata in (cfg.data.get("products") or {}).items():
            if isinstance(pdata, dict) and pdata.get("budget_usd"):
                budgets.setdefault(pname, pdata["budget_usd"])
        return templates.TemplateResponse(request, "config.html", ctx(
            request, page="config", sources=cfg.sources, effective=effective, budgets=budgets))

    # ---- actions -----------------------------------------------------------
    @app.post("/tick")
    def tick(request: Request):
        summary = hub.tick()
        hub._log(f"manual tick: {summary}")
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)

    @app.post("/pause")
    def web_pause(request: Request, reason: str = Form("")):
        with hub.lock:
            sched = hub.scheduler()
            sched.pause(by="web", reason=reason.strip())
        hub._log("dispatch paused via web" + (f": {reason.strip()}" if reason.strip() else ""))
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)

    @app.post("/resume")
    def web_resume(request: Request):
        with hub.lock:
            sched = hub.scheduler()
            sched.resume(by="web")
        hub._log("dispatch resumed via web")
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)

    @app.post("/upgrade")
    def web_upgrade(request: Request):
        with hub.lock:
            sched = hub.scheduler()
            result = sched.upgrade(restart=True)
        hub._log("tool upgrade: " + ("restarting" if result.get("ok") else f"failed ({result.get('reason')})"))
        # On success the process re-execs and never reaches here; a failure falls through.
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)

    @app.post("/tasks/{task_id}/{action}")
    def task_action(request: Request, task_id: str, action: str, note: str = Form("")):
        s = hub.fresh()
        try:
            t = s.task(task_id)
        except KeyError:
            raise HTTPException(404) from None
        with hub.lock:
            sched = hub.scheduler()
            t = sched.store.task(task_id)
            if action == "approve":
                if t.status == Status.DRAFT:
                    t.status = Status.READY
                    t.log("approved (web)")
                    s.save(t)
            elif action == "priority":
                old = t.priority
                try:
                    t.priority = int(note.strip())
                except ValueError:
                    raise HTTPException(400, "priority must be an integer") from None
                t.log(f"priority {old} -> {t.priority} (web)")
                s.save(t)
            elif action == "difficulty":
                from ..harness import DIFFICULTIES

                tier = note.strip()
                if tier not in DIFFICULTIES:
                    raise HTTPException(400, f"difficulty must be one of {', '.join(DIFFICULTIES)}")
                old = t.difficulty
                t.difficulty = tier
                t.log(f"difficulty {old} -> {tier} (web)")
                s.save(t)
            elif action == "unapprove":
                if t.status == Status.READY:
                    t.status = Status.DRAFT
                    t.log("back to draft (web)")
                    s.save(t)
            elif action == "dispatch":
                sched.dispatch(t, mode="revise" if t.status == Status.CHANGES_REQUESTED else "work")
            elif action == "cancel":
                sched.cancel(t, note or "cancelled (web)")
            elif action == "retry":
                sched.retry(t)
            elif action == "resume":
                sched.resume_task(t)
            elif action == "done":
                t.status = Status.DONE
                t.log(note or "marked done (web)")
                s.save(t)
            elif action == "review":
                if t.pr:
                    sched.dispatch_review(t)
            elif action == "answer":
                if t.status == Status.WAITING_HUMAN and note.strip():
                    sched.answer(t, note.strip())
            elif action == "accept":
                if sched.pending_decision(t):
                    sched.accept_decision(t, note.strip())
            elif action == "reject":
                if sched.pending_decision(t):
                    sched.reject_decision(t, note.strip() or "please carry out the task as originally asked")
            elif action == "triage-ready":
                sched.triage(t, ready=True)
            elif action == "triage-changes":
                sched.triage(t, changes=note.strip() or "please revisit; see the PR")
            elif action == "persona":
                for name in [n.strip() for n in note.split(",") if n.strip()]:
                    sched.dispatch_persona_pr(t, name)
            elif action == "trial":
                contenders = [n.strip() for n in note.split(",") if n.strip()]
                sched.start_trial(t, contenders)
            elif action == "reset-revisions":
                st = sched.state.get(t.id)
                st["revisions"] = 0
                sched.state.save()
                t.log("revision counter reset (web)")
                s.save(t)
            else:
                raise HTTPException(400, f"unknown action {action}")
        back = request.headers.get("referer", "")
        return RedirectResponse(back if back.endswith("/") or back.endswith("/inbox") else f"/tasks/{task_id}", status_code=303)

    @app.post("/decisions/{decision_id}/{action}")
    def decision_action(request: Request, decision_id: str, action: str):
        if action not in ("accept", "reject"):
            raise HTTPException(400, f"unknown action {action}")
        with hub.lock:
            sched = hub.scheduler()
            try:
                sched.resolve_decision(decision_id, accept=(action == "accept"))
            except KeyError:
                raise HTTPException(404) from None
        back = request.headers.get("referer", "")
        return RedirectResponse(back if back.endswith("/") or back.endswith("/inbox") else "/inbox", status_code=303)

    @app.post("/phases/{product}/{phase}/approve-all")
    def approve_all(product: str, phase: str):
        s = hub.fresh()
        for t in s.tasks().values():
            if t.key == f"{product}/{phase}" and t.status == Status.DRAFT:
                t.status = Status.READY
                t.log("approved (web)")
                s.save(t)
        return RedirectResponse(f"/phases/{product}/{phase}", status_code=303)

    @app.post("/phases/{product}/{phase}/persona")
    def persona_phase(product: str, phase: str, personas: str = Form(""), file_tasks: str = Form("")):
        s = hub.fresh()
        ph = s.phase(product, phase)
        with hub.lock:
            sched = hub.scheduler()
            for name in [n.strip() for n in personas.split(",") if n.strip()]:
                sched.dispatch_persona_phase(ph, name, file_tasks=bool(file_tasks))
        return RedirectResponse(f"/phases/{product}/{phase}", status_code=303)

    @app.post("/phases/{product}/{phase}/plan")
    def plan_phase(product: str, phase: str, background: BackgroundTasks, guidance: str = Form("")):
        key = f"{product}/{phase}"
        if hub.planning.get(key, "").startswith("running"):
            return RedirectResponse(f"/phases/{product}/{phase}", status_code=303)
        hub.planning[key] = f"running since {now_iso()}"

        def job() -> None:
            from ..planner import import_plan, parse_plan, plan_prompt, run_planner

            try:
                s = hub.fresh()
                raw = run_planner(s, plan_prompt(s, product, phase, extra=guidance))
                created = import_plan(s, product, phase, parse_plan(raw))  # ready by default (plan.auto_approve)
                hub.planning[key] = f"done {now_iso()}: created {', '.join(t.id for t in created) or 'nothing new'}"
            except Exception as e:  # noqa: BLE001
                hub.planning[key] = f"failed {now_iso()}: {e}"

        background.add_task(job)
        return RedirectResponse(f"/phases/{product}/{phase}", status_code=303)

    @app.post("/friction-report")
    def friction_report_web(
        request: Request,
        product: str = Form(...),
        phase: str = Form(...),
        text: str = Form(...),
        page: str = Form(""),
        task_id: str = Form(""),
    ):
        import datetime as _dt

        from ..friction import append_friction_report, create_friction_draft_task

        s = hub.fresh()
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        doc = ph.path / "docs" / "friction.md"
        provenance = page or "web"
        if task_id:
            provenance = f"{page} ({task_id})" if page else task_id
        date = _dt.date.today().isoformat()
        append_friction_report(doc, text, provenance, date)
        create_friction_draft_task(s, product, phase, text, provenance, date)
        s.invalidate()
        back = request.headers.get("referer", "/")
        return RedirectResponse(back, status_code=303)

    # ---- json --------------------------------------------------------------
    @app.get("/api/tasks")
    def api_tasks():
        s = hub.fresh()
        tasks = s.tasks()
        stack = bool(s.config.get("stack", True))
        return JSONResponse([{**t.to_frontmatter(), "effective_status": effective_status(t, tasks, stack)} for t in tasks.values()])

    @app.get("/api/events")
    def api_events():
        return JSONResponse(hub.events[-50:])

    return app


def _review_head(path: Path) -> dict[str, Any]:
    """Persona, date, score, headline and high findings of a docs/reviews report
    (written by personas.report_markdown as <persona>-<date>[-n].md)."""
    m = re.match(r"(.+?)-(\d{4}-\d{2}-\d{2})(?:-\d+)?$", path.stem)
    persona, date = (m.group(1), m.group(2)) if m else (path.stem, "")
    score = ""
    overall = ""
    highs: list[str] = []
    section = ""
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].lower()
            continue
        if "**Score:**" in stripped:
            sm = re.search(r"\*\*Score:\*\*\s*([^·]+)", stripped)
            score = sm.group(1).strip() if sm else ""
            continue
        if not stripped or stripped.startswith("#") or stripped.startswith("_"):
            continue
        if not section and not overall:
            overall = stripped
        elif section == "high" and stripped.startswith("- "):
            highs.append(stripped[2:])
    return {"persona": persona, "date": date, "score": score, "overall": overall, "highs": highs}


def _split_log(body: str) -> tuple[str, list[str]]:
    if "\n## Log" in body:
        head, _, tail = body.partition("\n## Log")
        lines = [ln[2:] for ln in tail.strip().splitlines() if ln.startswith("- ")]
        return head, lines
    return body, []

