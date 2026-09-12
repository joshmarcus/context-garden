"""A task's page, its brief, its log and the live partials for its runs and output."""

from __future__ import annotations

import shlex
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from ...brief import brief_gaps, build_brief
from ...criteria import (
    parse_criteria,
    reconcile,
    required_evidence,
    required_evidence_rows,
    worker_verified,
)
from ...events import EventLog
from ...graph import dependency_after, dependents, deps_in_later_phase
from ...inbox import approve_phase_options, decision_card_view, split_log
from ...model import effective_owner
from ...outcomes import base_acceptance
from ...reference_snapshot import REFERENCE_DIR
from ...review import review_to_markdown
from ...runs import RunStore
from ...scheduler import State
from ...ssh_attach import attach_command, attachment_problem
from ...trials import TrialLog, ranking_markdown
from ..common import Site, render_md


def _return_to(request: Request, task_id: str) -> str:
    """Return a same-Garden referrer as a safe relative URL, or an empty string."""
    referrer = request.headers.get("referer", "").strip()
    if not referrer:
        return ""
    try:
        origin = urlsplit(referrer)
    except ValueError:
        return ""
    current = request.url
    if (
        origin.scheme.lower() != current.scheme.lower()
        or origin.netloc.lower() != current.netloc.lower()
        or origin.username is not None
        or origin.password is not None
        or not origin.path.startswith("/")
        or origin.path.startswith("//")
        or "\\" in origin.path
        or origin.path == f"/tasks/{task_id}"
    ):
        return ""
    return urlunsplit(("", "", origin.path, origin.query, origin.fragment))


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
        sched = hub.reader(s)
        rs = RunStore(s.config.garden_dir)
        runs = rs.runs_for(t.id)
        latest_run = rs.latest(t.id)
        attachable_runs = [run for run in reversed(runs) if attachment_problem(run) is None]
        attachable_run = attachable_runs[0] if attachable_runs else None
        st = State(s.config.garden_dir / "state.json").historical(t.id)
        manual_return_guard = hub.reader().manual_return_guard(t)
        _, log = split_log(t.body)
        evs = EventLog(s.config.garden_dir / "events.jsonl").read(task_id=t.id)
        usage = rs.usage_for(t.id)
        initial_stdout = latest_run.stdout_events() if latest_run else []
        from ...friction import extract_friction, pr_body_for
        from ...personas import DEFAULT_PERSONAS, list_personas
        from ...suggestions import APPLIES_TO, has_pending, parse_suggestions, spec_body

        friction_text = extract_friction(pr_body_for(t, rs))
        from ...defects import DefectStore
        defect_store = DefectStore(s.config.garden_dir)
        defects = defect_store.list(task_id=t.id)
        suggestions = parse_suggestions(t.body)
        edit_diff = _edit_diff(runs)
        criteria_rows = reconcile(parse_criteria(t.body), worker_verified(runs),
                                  (st.get("last_review") or {}).get("criteria"))
        evidence_rows = required_evidence_rows(required_evidence(t.body, t.extra.get("requires")), st)
        gaps = brief_gaps(s, t) if t.status.value == "draft" else []

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
        completion = _completion_view(t, evs)
        review_history = _review_history(runs) if completion else []
        manual_runner = (t.runner or s.config.product_runner(t.product)) == "manual"
        phase = s.phase(t.product, t.phase)
        phase_hold = sched.phase_admission_refusal(t)
        phase_hold_kind = ("closed phase" if phase.closed else "frozen phase" if phase.frozen
                           else "sequential phase order")
        manual_take_reason = ""
        if manual_runner and t.status.value in ("ready", "changes_requested"):
            if any(run.task_id == t.id for run in rs.active()):
                manual_take_reason = "This task is already claimed by an active manual session."
            elif t.status.value == "ready" and sched.task_blockers(t, tasks):
                manual_take_reason = "This task is waiting for its dependencies to finish."
            elif phase_hold:
                manual_take_reason = f"This task cannot be claimed while {phase_hold}."
            elif st.get("needs_human") or st.get("decision"):
                manual_take_reason = "This task is paused for an Inbox decision."
            elif t.status.value == "changes_requested" and not str(st.get("pending_feedback") or "").strip():
                manual_take_reason = "This task needs revision feedback before it can resume."
            elif t.status.value == "changes_requested" and int(st.get("revisions", 0)) >= int(s.config.get("max_revisions", 3)):
                manual_take_reason = "This task reached its revision limit and needs an Inbox decision."

        decision_card = decision_card_view(t, st, rs)
        if decision_card is None and request.query_params.get("walkthrough") == "decision":
            decision_card = {
                "type": "attention",
                "title": "Example decision card",
                "reason": "This representative card shows where a person acts when work needs a decision.",
                "blurb": "This example is captured for the walkthrough; it does not describe a live task decision.",
                "final": "",
                "evidence": [],
                "attention": {
                    "category": "Example",
                    "effect": "No live task is affected.",
                    "owner": "you",
                    "recommendation": "Review the example card",
                    "user_decision": True,
                    "blockers": [],
                    "actions": [],
                    "discuss": "",
                },
            }
        return templates.TemplateResponse(request, "task.html", ctx(
            request, page="task", personas=sorted(set(list_personas(s)) | set(DEFAULT_PERSONAS)),
            task=t, eff=sched.task_effective_status(t, tasks), blockers=sched.task_blockers(t, tasks), usage=usage,
            dependency_after=lambda dep: dependency_after(t, dep, tasks),
            dependents=dependents(t.id, tasks), runs=list(reversed(runs)), latest_run=latest_run, state=st,
            attach_command=(
                attach_command(attachable_run, exact=len(attachable_runs) > 1)
                if attachable_run else ""
            ),
            manual_reservation=(st.get("manual_reservation") if isinstance(st.get("manual_reservation"), dict) else None),
            manual_return_guard=manual_return_guard,
            body_html=render_md(spec_body(t.body)),
            criteria_rows=criteria_rows,
            evidence_rows=evidence_rows,
            brief_gaps=gaps,
            acceptance_text=_acceptance_text(t.body),
            suggestions=suggestions, applies_to=APPLIES_TO, has_pending=has_pending(t.body),
            edit_running=bool(st.get("edit_run")), edit_diff=edit_diff,
            log_lines=log, rel=s.rel(t.path), events=list(reversed(evs))[:60],
            discovered=[x for x in tasks.values() if x.discovered_from == t.id],
            review_md=review_to_markdown(st["last_review"]) if st.get("last_review") else "",
            friction_text=friction_text,
            initial_stdout=initial_stdout,
            decision_card=decision_card,
            harness_choices=s.config.harness_choices(),
            default_harness=t.harness or s.config.product_harness(t.product),
            manual_runner=manual_runner, manual_take_reason=manual_take_reason,
            phase_hold=phase_hold, phase_hold_kind=phase_hold_kind,
            move_phases=move_phases, later_deps=later_deps, approve_phases=approve_phases,
            prior_trials=prior_trials,
            trial_view=trial_view,
            owner=effective_owner(t, phase)[0],
            owner_source=effective_owner(t, phase)[1],
            return_to=_return_to(request, task_id),
            completion=completion,
            review_history=review_history,
            defects=defects,
            defect_summary=defect_store.summary(task_id=t.id),
            defect_idempotency_key=uuid.uuid4().hex,
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

    @app.get("/tasks/{task_id}/packet", response_class=PlainTextResponse)
    def task_packet(task_id: str):
        """Return the immutable packet assigned to the current manual session."""
        s = hub.fresh()
        try:
            s.task(task_id)
        except KeyError:
            raise HTTPException(404) from None
        run = RunStore(s.config.garden_dir).latest(task_id)
        packet = run.path / "brief.md" if run and run.runner == "manual" else None
        if packet is None or not packet.exists():
            raise HTTPException(404, "no assigned manual packet")
        references = run.path / REFERENCE_DIR
        if references.is_dir():
            context = shlex.quote(str(references.resolve()))
            return (
                "# Manual reference snapshot\n\n"
                f"Set `GARDEN_CONTEXT_DIR` before starting: `export GARDEN_CONTEXT_DIR={context}`.\n\n"
                + packet.read_text()
            )
        return packet.read_text()

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
        final = run.read_text("final.md")
        if final:
            parts.append("---- final message ----\n" + final)
        stderr = run.stderr_text()
        if stderr.strip():
            parts.append("---- stderr ----\n" + stderr[-8000:])
        return "\n\n".join(parts)

    @app.get("/investigations/{task_id}/{run_id}/{name}")
    def investigation_report(task_id: str, run_id: str, name: str):
        if name not in {"report.md", "report.html"}:
            raise HTTPException(404)
        run = next((item for item in RunStore(hub.fresh().config.garden_dir).runs_for(task_id)
                    if item.run_id == run_id and item.mode == "investigation"), None)
        if run is None:
            raise HTTPException(404)
        try:
            data = run.read_bytes(name)
        except FileNotFoundError:
            raise HTTPException(404) from None
        media = "text/html" if name.endswith(".html") else "text/markdown"
        headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"} if media == "text/html" else {}
        return Response(data, media_type=media, headers=headers)


def _edit_diff(runs: list[Any]) -> str:
    """Unified diff of the task body from the most recent edit run that changed it, or ''."""
    import difflib

    for run in reversed(runs):
        if run.mode != "edit":
            continue
        try:
            old = run.read_bytes("old_body.md").decode()
            new = run.read_bytes("new_body.md").decode()
        except FileNotFoundError:
            continue
        if old == new:
            return ""
        return "".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="before", tofile="after"))
    return ""
def _acceptance_text(body: str) -> str:
    """The editable contents of the acceptance-criteria section, without its heading."""
    import re

    match = re.search(r"(?ms)^##\s+Acceptance criteria\s*$\n?(.*?)(?=^##\s|\Z)", body)
    return match.group(1).strip() if match else ""


def _completion_view(task: Any, events: list[dict[str, Any]]) -> dict[str, str] | None:
    """Describe the current done status without treating an old review as its outcome."""
    if getattr(getattr(task, "status", None), "value", "") != "done":
        return None
    transition_index = next((index for index in range(len(events) - 1, -1, -1)
                             if events[index].get("kind") == "transition"
                             and events[index].get("to") == "done"), None)
    if transition_index is None:
        return {"kind": "Completion provenance unavailable", "source": "No completion transition recorded",
                "reason": "This task is done, but its historical completion record is unavailable.", "at": ""}
    transition = events[transition_index]
    source_event = (events[transition_index - 1] if transition_index
                    and events[transition_index - 1].get("kind") in {"mark_done", "set_status"} else {})
    reason = str(transition.get("note") or "No completion reason recorded.")
    at = str(transition.get("at") or "")
    merged_tasks = {str(event.get("task") or "") for event in events
                    if event.get("kind") == "automerged"}
    if base_acceptance(transition, merged_tasks):
        if source_event.get("kind") == "mark_done":
            actor = str(source_event.get("actor") or "owner").replace("_", " ")
            return {"kind": "Accepted completion", "source": f"Owner acceptance by {actor}",
                    "reason": reason, "at": at}
        return {"kind": "Accepted completion", "source": "Merged into the base branch",
                "reason": reason, "at": at}
    actor = str(source_event.get("actor") or "owner").replace("_", " ")
    source = (f"Status override by {actor}" if source_event.get("kind") == "set_status"
              else f"Forced completion by {actor}")
    return {"kind": "Forced status completion", "source": source,
            "reason": reason + " This is not recorded as base-branch acceptance.", "at": at}


def _review_history(runs: list[Any]) -> list[dict[str, Any]]:
    """Return dated, source-specific automated review findings for a completed task."""
    history = []
    for run in reversed(runs):
        review = run.result if run.mode == "review" and isinstance(run.result, dict) else None
        if not review or not review.get("verdict"):
            continue
        snapshot = run.env_snapshot or {}
        history.append({"run_id": run.run_id, "at": run.finished_at or run.started_at,
                        "head": str(snapshot.get("review_head") or ""),
                        "markdown": review_to_markdown(review)})
    return history


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
            "wait_reason": trial.get("compare_deferred", ""),
            "contenders": contenders}
