"""Task actions: one function per button on a task page or Inbox card, registered by name.
`task_action` looks the name up in the table and runs it under the hub's lock."""

from __future__ import annotations

import os
import re

from fastapi import BackgroundTasks, Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...brief import brief_gaps
from ...github import GitHubError
from ...gitops import GitError
from ...model import Status, Task, ensure_open
from ...runs import RecoveryLaunchConflict, Run, RunStore
from ...scheduler import Scheduler
from ...store import Store
from ...trials import parse_contender
from ..common import LOGGER, Site, _flash_url
from . import ACTIONS, action


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


@action("approve")
def approve(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> str | None:
    target = note.strip()
    if target:
        product, _, phase = target.partition("/")
        if phase and phase != t.phase:
            sched.move(t, product, phase)
    try:
        ph = s.phase(t.product, t.phase)
    except KeyError:
        ph = None
    return sched.approve(t, by="web", phase=ph) or None


@action("priority")
def priority(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    old = t.priority
    try:
        t.priority = int(note.strip())
    except ValueError:
        raise HTTPException(400, "priority must be an integer") from None
    t.log(f"priority {old} -> {t.priority} (web)")
    s.save(t)


@action("difficulty")
def difficulty(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    from ...harness import DIFFICULTIES

    tier = note.strip()
    if tier not in DIFFICULTIES:
        raise HTTPException(400, f"difficulty must be one of {', '.join(DIFFICULTIES)}")
    old = t.difficulty
    t.difficulty = tier
    t.log(f"difficulty {old} -> {tier} (web)")
    s.save(t)


@action("unapprove")
def unapprove(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    if t.status != Status.READY:
        raise RuntimeError(f"{t.id} is {t.status.value}, not ready; nothing to send back to draft")
    sched._transition(t, Status.DRAFT, "back to draft (web)")


@action("dispatch")
def dispatch(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    if any(r.task_id == t.id for r in sched.runs.active()):
        raise RuntimeError(f"{t.id} already has a run in flight")
    # dispatch() itself logs "dispatched <mode> run <run_id> via ..." on the task (_transition),
    # so the run id is already on the task page's Log and its "Latest run" link the moment this
    # redirects back there -- no separate flash is needed for a plain success.
    sched.dispatch(t, mode="revise" if t.status == Status.CHANGES_REQUESTED else "work")


@action("move")
def move(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    target = note.strip()
    if "/" not in target:
        raise RuntimeError("move target must be product/phase")
    product, phase = target.split("/", 1)
    sched.move(t, product, phase.strip("/"))


@action("order")
def order(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    arg = note.strip()
    if arg in ("up", "down"):
        sched.reorder(t, direction=arg)
    else:
        # `note` is the id this row should follow ("" = top of the section); the backlog's
        # drag script and the "move up/down" buttons both post here.
        sched.reorder(t, after=arg)


@action("cancel")
def cancel(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    sched.cancel(t, note or "cancelled (web)")


@action("retry")
def retry(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    sched.retry(t)


@action("resume")
def resume(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    sched.resume_task(t)


@action("done")
def done(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    sched.mark_done(t, note or "marked done without merging (web)", force=True)


@action("review")
def review(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    if not t.pr:
        raise RuntimeError(f"{t.id} has no PR yet to review")
    sched.review_again(t)


@action("answer")
def answer(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    if note.strip():
        if t.status != Status.WAITING_HUMAN:
            raise RuntimeError(f"{t.id} is no longer waiting for you (now {t.status.value}); your answer was not sent")
        sched.answer(t, note.strip())


@action("accept")
def accept(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    if not sched.pending_decision(t):
        raise RuntimeError(f"{t.id} has no pending worker decision to accept")
    sched.accept_decision(t, note.strip())


@action("reject")
def reject(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    if not sched.pending_decision(t):
        raise RuntimeError(f"{t.id} has no pending worker decision to reject")
    sched.reject_decision(t, note.strip() or "please carry out the task as originally asked")


@action("triage-ready")
def triage_ready(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    sched.triage(t, ready=True)


@action("triage-changes")
def triage_changes(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    sched.triage(t, changes=note.strip() or "please revisit; see the PR")


@action("persona")
def persona(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    for name in [n.strip() for n in note.split(",") if n.strip()]:
        sched.dispatch_persona_pr(t, name)


@action("trial")
def trial(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    contenders = [n.strip() for n in note.split(",") if n.strip()]
    if len(contenders) < 2:
        raise RuntimeError("a trial needs at least two contenders, e.g. claude:sonnet, claude:opus")
    default_h = t.harness or sched.cfg.product_harness(t.product)
    labels = [parse_contender(c, default_h)[0] for c in contenders]
    dupe = next((label for label in labels if labels.count(label) > 1), "")
    if dupe:
        raise RuntimeError(f"contenders must be distinct; {dupe} was picked more than once")
    # A task with an open PR only reaches this action through the "Model trial again…" button
    # (task.html shows it only once a trial has concluded), so `again` here mirrors the person
    # clicking that button — the same confirmation `--again` gives the CLI.
    sched.start_trial(t, contenders, again=True, keep_prs=applies_to.strip() == "keep_prs")


@action("suggest")
def suggest(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    from ...suggestions import record_suggestion

    if note.strip():
        record_suggestion(s, t, note.strip(), author="web", applies_to=applies_to.strip())


@action("integrate")
def integrate(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    sched.integrate_now(t)


@action("reset-revisions")
def reset_revisions(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    st = sched.state.get(t.id)
    st["revisions"] = 0
    sched.state.save()
    t.log("revision counter reset (web)")
    s.save(t)


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    def prepare_recovery_launch(task_id: str, run: Run) -> None:
        """Continue a reserved launch after its 202 response has left the server."""
        try:
            sched = hub.scheduler()
            task = sched.store.task(task_id)
            ensure_open(task)
            runner = sched.runner_for(task)
            run.runner = runner.name
            run.save()
            sched.dispatch(
                task,
                mode="revise" if task.status == Status.CHANGES_REQUESTED else "work",
                runner=runner,
                reserved_run=run,
            )
        except Exception as exc:  # dispatch persists a terminal operation before re-raising
            LOGGER.exception("recovery launch %s for %s failed", run.run_id, task_id)
            hub._log(f"{task_id} recovery launch {run.run_id} failed: {exc}")

    @app.post("/api/control/tasks/{task_id}/launch", status_code=202)
    async def recovery_launch(
        task_id: str,
        background: BackgroundTasks,
        payload: dict[str, object] = Body(...),
    ) -> JSONResponse:
        """Compare-and-reserve a retry-safe launch, then prepare it after responding."""
        key = payload.get("idempotency_key")
        expected = payload.get("expected_run_id")
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise HTTPException(422, "idempotency_key must be a non-empty string of at most 200 characters")
        if not isinstance(expected, str):
            raise HTTPException(422, "expected_run_id must be a string (empty means no current run)")
        try:
            task = hub.store.control_task(task_id)
            ensure_open(task)
            mode = "revise" if task.status == Status.CHANGES_REQUESTED else "work"
            runs = RunStore(hub.store.config.garden_dir)
            run, created = runs.reserve_recovery_launch(
                task_id, "", key.strip(), expected, os.getpid()
            )
            if created:
                run.mode = mode
                run.save()
                background.add_task(prepare_recovery_launch, task_id, run)
            elif run.status in ("requested", "preparing") and (
                run.preparer_pid is None or not _process_exists(run.preparer_pid)
            ):
                run.preparer_pid = os.getpid()
                run.save()
                background.add_task(prepare_recovery_launch, task_id, run)
        except KeyError:
            raise HTTPException(404) from None
        except RecoveryLaunchConflict as exc:
            return JSONResponse(
                {"detail": "expected_run_id is stale", "current_run_id": exc.current_run_id},
                status_code=409,
            )
        except (RuntimeError, GitError, GitHubError) as exc:
            raise HTTPException(409, str(exc)) from None
        location = f"/api/operations/{task_id}/{run.run_id}"
        return JSONResponse(
            {"operation_id": run.run_id, "task_id": task_id, "state": run.lifecycle_state},
            status_code=202,
            headers={"Location": location},
        )

    @app.post("/tasks/{task_id}/brief")
    def save_brief(task_id: str, acceptance: str = Form(""), reading: str = Form("")):
        """Save a draft's brief repair only when it clears the shared approval gate."""
        s = hub.fresh()
        try:
            t = s.task(task_id)
        except KeyError:
            raise HTTPException(404) from None
        if t.status != Status.DRAFT:
            raise HTTPException(400, "only draft briefs can be edited")
        try:
            with hub.action_lock:
                t = hub.scheduler().store.task(task_id)
                if t.status != Status.DRAFT:
                    raise HTTPException(400, "only draft briefs can be edited")
                t.body = _replace_acceptance_criteria(t.body, acceptance)
                t.reading = [path.strip() for path in reading.splitlines() if path.strip()]
                gaps = brief_gaps(s, t)
                if gaps:
                    return JSONResponse({"detail": "; ".join(gaps), "gaps": gaps}, status_code=422)
                s.save(t)
        except HTTPException:
            raise
        except Exception:
            LOGGER.exception("brief edit on %s failed", task_id)
            raise HTTPException(500, "something failed; see the log") from None
        return {"gaps": []}

    @app.post("/tasks/{task_id}/{action}")
    def task_action(request: Request, task_id: str, action: str, note: str = Form(""), applies_to: str = Form("")):
        s = hub.fresh()
        try:
            t = s.task(task_id)
        except KeyError:
            raise HTTPException(404) from None
        run_action = ACTIONS.get(action)
        if run_action is None:
            raise HTTPException(400, f"unknown action {action}")
        back = request.headers.get("referer", "")
        # Board actions (backlog reorder/move) return to the board so the flash and the new order
        # show there; task-page and Inbox actions stay where they were pressed.
        stay = back.endswith("/") or back.endswith("/inbox") or "/board" in back
        redirect_to = back if stay else f"/tasks/{task_id}"
        try:
            with hub.action_lock:
                sched = hub.scheduler()
                t = sched.store.task(task_id)
                ensure_open(t)
                warning = run_action(s, sched, t, note, applies_to)
        except HTTPException:
            raise
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"{task_id}/{action} failed: {message}")
            note_to_keep = note if action == "answer" else ""
            return RedirectResponse(_flash_url(redirect_to, message, note_to_keep), status_code=303)
        except Exception:
            LOGGER.exception("action %s on %s failed", action, task_id)
            hub._log(f"{task_id}/{action} failed: unexpected error, see the log")
            note_to_keep = note if action == "answer" else ""
            return RedirectResponse(_flash_url(redirect_to, "something failed; see the log", note_to_keep), status_code=303)
        if warning:
            return RedirectResponse(_flash_url(redirect_to, str(warning)), status_code=303)
        return RedirectResponse(redirect_to, status_code=303)

def _replace_acceptance_criteria(body: str, acceptance: str) -> str:
    """Replace or add the editable checklist while leaving every other task section intact."""
    content = re.sub(r"(?im)^##\s+Acceptance criteria\s*$\n?", "", acceptance).strip()
    section = f"## Acceptance criteria\n\n{content}\n"
    match = re.search(r"(?ms)^##\s+Acceptance criteria\s*$.*?(?=^##\s|\Z)", body)
    if match:
        return body[:match.start()] + section + body[match.end():]
    log = re.search(r"(?m)^##\s+Log\s*$", body)
    if log:
        return body[:log.start()].rstrip() + "\n\n" + section + "\n" + body[log.start():]
    return body.rstrip() + "\n\n" + section
