"""Task actions: one function per button on a task page or Inbox card, registered by name.
`task_action` looks the name up in the table and runs it under the hub's lock."""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ...github import GitHubError
from ...gitops import GitError
from ...model import Status, Task, ensure_open
from ...scheduler import Scheduler
from ...store import Store
from ...trials import parse_contender
from ..common import LOGGER, Site, _flash_url
from . import ACTIONS, action


@action("approve")
def approve(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
    target = note.strip()
    if target:
        product, _, phase = target.partition("/")
        if phase and phase != t.phase:
            sched.move(t, product, phase)
    try:
        ph = s.phase(t.product, t.phase)
    except KeyError:
        ph = None
    sched.approve(t, by="web", phase=ph)


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
    t.status = Status.DRAFT
    t.log("back to draft (web)")
    s.save(t)


@action("dispatch")
def dispatch(s: Store, sched: Scheduler, t: Task, note: str, applies_to: str) -> None:
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
    t.status = Status.DONE
    t.log(note or "marked done (web)")
    s.save(t)


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
    sched.start_trial(t, contenders)


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
                run_action(s, sched, t, note, applies_to)
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
        return RedirectResponse(redirect_to, status_code=303)
