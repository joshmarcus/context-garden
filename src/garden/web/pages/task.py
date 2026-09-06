"""A task's page, its brief, its log and the live partials for its runs and output."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ...brief import build_brief
from ...criteria import (
    parse_criteria,
    reconcile,
    required_evidence,
    required_evidence_rows,
    worker_verified,
)
from ...events import EventLog
from ...graph import blockers, dependents, deps_in_later_phase, effective_status
from ...inbox import approve_phase_options, decision_card_view, split_log
from ...review import review_to_markdown
from ...runs import RunStore
from ...scheduler import State
from ...trials import TrialLog, ranking_markdown
from ..common import Site, render_md


def _design_files(task: Any, store: Any) -> list[dict[str, str]]:
    """Design paths changed by the task branch, using the local checkout when available."""
    if not task.branch:
        return []
    from ... import gitops
    try:
        repo = gitops.ensure_repo(store.config.product_repo(task.product), store.config.repos_dir)
        base = store.config.product_base_branch(task.product)
        names = gitops.git("diff", "--name-only", f"{base}...{task.branch}", cwd=repo, check=False).splitlines()
    except Exception:  # noqa: BLE001
        return []
    return [{"name": name, "href": f"/design/{name.removeprefix('docs/design/')}?ref={task.branch}"}
            for name in names if name.startswith("docs/design/") and name != "docs/design/" and ".." not in name]


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

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
        _, log = split_log(t.body)
        evs = EventLog(s.config.garden_dir / "events.jsonl").read(task_id=t.id)
        usage = rs.usage_for(t.id)
        initial_stdout = latest_run.stdout_events() if latest_run else []
        from ...friction import extract_friction, pr_body_for
        from ...personas import DEFAULT_PERSONAS, list_personas
        from ...suggestions import APPLIES_TO, has_pending, parse_suggestions, spec_body

        friction_text = extract_friction(pr_body_for(t, rs))
        suggestions = parse_suggestions(t.body)
        edit_diff = _edit_diff(runs)
        criteria_rows = reconcile(parse_criteria(t.body), worker_verified(runs),
                                  (st.get("last_review") or {}).get("criteria"))
        evidence_rows = required_evidence_rows(required_evidence(t.body, t.extra.get("requires")), st)

        # Phases this task can move to (the product's own phases, current one always shown even
        # if closed), and any dependency that now sits in a later phase and so can never merge
        # before this task (a state a move can create).
        phase_index: dict[str, int] = {}
        move_phases: list[str] = []
        for prod in s.products():
            for i, ph in enumerate(prod.phases):
                phase_index[ph.key] = i
                if prod.name == t.product and (not ph.closed or ph.name == t.phase):
                    move_phases.append(ph.name)
        later_deps = deps_in_later_phase(t, tasks, phase_index)
        approve_phases = approve_phase_options(s, t) if t.status.value == "draft" else []
        trial_log = TrialLog(s.config.garden_dir / "trials.jsonl")
        prior_trials = [(tr, ranking_markdown(tr)) for tr in reversed(trial_log.read()) if tr.get("task") == t.id]
        trial_view = _trial_view(st.get("trial"), runs)

        return templates.TemplateResponse(request, "task.html", ctx(
            request, page="task", personas=sorted(set(list_personas(s)) | set(DEFAULT_PERSONAS)),
            task=t, eff=effective_status(t, tasks, stack), blockers=blockers(t, tasks, stack), usage=usage,
            dependents=dependents(t.id, tasks), runs=list(reversed(runs)), latest_run=latest_run, state=st,
            body_html=render_md(spec_body(t.body)),
            criteria_rows=criteria_rows,
            evidence_rows=evidence_rows,
            suggestions=suggestions, applies_to=APPLIES_TO, has_pending=has_pending(t.body),
            edit_running=bool(st.get("edit_run")), edit_diff=edit_diff,
            log_lines=log, rel=s.rel(t.path), events=list(reversed(evs))[:60],
            discovered=[x for x in tasks.values() if x.discovered_from == t.id],
            review_md=review_to_markdown(st["last_review"]) if st.get("last_review") else "",
            friction_text=friction_text,
            initial_stdout=initial_stdout,
            decision_card=decision_card_view(t, st, rs),
            harness_choices=s.config.harness_choices(),
            default_harness=t.harness or s.config.product_harness(t.product),
            move_phases=move_phases, later_deps=later_deps, approve_phases=approve_phases,
            prior_trials=prior_trials,
            trial_view=trial_view,
            design_files=_design_files(t, s),
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


def _edit_diff(runs: list[Any]) -> str:
    """Unified diff of the task body from the most recent edit run that changed it, or ''."""
    import difflib

    for run in reversed(runs):
        if run.mode != "edit":
            continue
        old_p, new_p = run.path / "old_body.md", run.path / "new_body.md"
        if old_p.exists() and new_p.exists():
            old, new = old_p.read_text(), new_p.read_text()
            if old == new:
                return ""
            return "".join(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile="before", tofile="after"))
    return ""


def _trial_view(trial: Any, runs: list[Any]) -> dict[str, Any] | None:
    """Return the safe, display-ready subset of a task's current trial state."""
    if not isinstance(trial, dict):
        return None
    runs_by_id = {run.run_id: run for run in runs}
    contenders = []
    for contender in trial.get("contenders") or []:
        if not isinstance(contender, dict):
            continue
        row = dict(contender)
        run = runs_by_id.get(contender.get("run_id"))
        row["elapsed"] = run.elapsed_minutes() if run else None
        contenders.append(row)
    return {"status": trial.get("status", ""), "winner": trial.get("winner"), "kept": trial.get("kept"),
            "contenders": contenders}
