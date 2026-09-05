"""What a person does to a task: answer, accept or reject a decision, triage, cancel, move, retry, resume, finish."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import gitops
from ..brief import brief_gaps, resume_prompt
from ..github import GitHubError, mark_garden_comment
from ..model import (
    Phase,
    Status,
    Task,
    dispatch_sort_key,
    ensure_open,
    now_iso,
    phase_refusal,
    priority_label,
)
from ..runs import Run
from .report import TickReport
from .state import _TaskState


class HumanMixin:
    # ---- approving a draft --------------------------------------------------
    def approve(self, task: Task, by: str = "", phase: Phase | None = None) -> None:
        """Draft -> ready. The one approve gate the CLI, the web and the TUI share: it refuses a
        task that is not a draft, a closed or frozen phase without a freeze exception
        (`phase_refusal`), and a brief that would cost a run without being ready to work —
        placeholder acceptance criteria or a reading-list path that names no file
        (`brief_gaps`) — then logs and saves. `by` names the surface ("cli"/"web"/"tui"),
        recorded in the log line. Raises RuntimeError on a refusal so each surface reports it in
        its own idiom (a skipped line, a flash, a status message)."""
        if task.status != Status.DRAFT:
            raise RuntimeError(f"{task.id} is {task.status.value}, not draft; nothing to approve")
        if phase is not None:
            refusal = phase_refusal(phase, task)
            if refusal:
                raise RuntimeError(refusal)
        gaps = brief_gaps(self.store, task)
        if gaps:
            raise RuntimeError(
                f"{task.id} has an incomplete brief; fix it before approving: " + "; ".join(gaps)
            )
        task.status = Status.READY
        task.log(f"approved ({by})" if by else "approved")
        self.store.save(task)

    # ---- human answers -----------------------------------------------------
    def answer(self, task: Task, text: str) -> Run:
        ensure_open(task)
        if task.status != Status.WAITING_HUMAN:
            raise RuntimeError(f"{task.id} is {task.status.value}, not waiting_human")
        st = self.state.get(task.id)
        question = str(st.get("question") or "")
        st.setdefault("qa", []).append({"q": question, "a": text, "at": now_iso()})
        self.events.emit("answer", task.id, question=question, answer=text)
        runner = self.runner_for(task, "", str(st.get("session_harness") or ""))
        sid = str(st.get("session_id") or "")
        st["question"] = ""
        st["session_id"] = ""
        if sid and runner.harness is not None and runner.harness.can_resume:
            return self.dispatch(task, mode="resume", runner=runner, session_id=sid, prompt_override=resume_prompt(question, text))
        # harness can't resume: a fresh run with the Q&A in its brief
        return self.dispatch(task, mode="resume", runner=runner)

    # ---- worker decisions: wont_do / no_change -----------------------------
    def pending_decision(self, task: Task) -> dict[str, Any] | None:
        """A worker's `wont_do` / `no_change` call awaiting the person, or None."""
        dec = self.state.get(task.id).get("decision")
        return dict(dec) if isinstance(dec, dict) and dec.get("kind") else None

    def accept_decision(self, task: Task, note: str = "") -> None:
        """The person agrees with the worker's call. `wont_do` ends the task; `no_change` resumes the round."""
        ensure_open(task)
        dec = self.pending_decision(task)
        if not dec:
            raise RuntimeError(f"{task.id} has no pending worker decision to accept")
        self.state.get(task.id).pop("decision", None)
        if dec["kind"] == "wont_do":
            self.mark_wont_do(task, reason=str(dec.get("reason") or ""), note=note, run_id=str(dec.get("run") or ""))
        else:
            self._resume_no_change(task, dec, note)

    def reject_decision(self, task: Task, note: str) -> None:
        """The person disagrees: the worker's reasoning goes back into a revise round with the note."""
        ensure_open(task)
        dec = self.pending_decision(task)
        if not dec:
            raise RuntimeError(f"{task.id} has no pending worker decision to reject")
        st = self.state.get(task.id)
        st.pop("decision", None)
        st.pop("needs_human", None)
        kind, reason = str(dec.get("kind")), str(dec.get("reason") or "")
        st["pending_feedback"] = (
            f"### The person disagrees\n\n"
            f"You reported `{kind}` with this reasoning:\n\n> {reason or '(none given)'}\n\n"
            f"The person does not accept that. Their note:\n\n{note.strip() or '(no note)'}\n\n"
            f"Carry out the task as originally asked: make the change and, if there is no open PR yet, leave the branch ready for one."
        )
        st.pop("pending_feedback_easy", None)
        st.pop("pending_feedback_rebase", None)
        self.events.emit("decision_rejected", task.id, decision=kind, note=note[:200])
        self._transition(task, Status.CHANGES_REQUESTED, f"decision rejected by the person; revise run will follow: {note.strip()[:100]}")
        self.state.save()

    def mark_wont_do(self, task: Task, reason: str = "", note: str = "", run_id: str = "") -> None:
        """End the task in `wont_do`: close any open PR with a comment carrying the reason, record it in the log.
        Used by `accept_decision`, `garden set-status ID wont_do` and the web Accept button."""
        st = self.state.get(task.id)
        st.pop("decision", None)
        st.pop("needs_human", None)
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if task.pr and slug and number and self.github.available and task.status != Status.DONE:
            body = f"Closing without merging: this task will not be done.\n\n**Reason:** {reason or '(none given)'}"
            try:
                self.github.comment(slug, number, mark_garden_comment(body, run_id))
                self.github.close_pr(slug, number)
                self.events.emit("pr_closed", task.id, pr=task.pr, wont_do=True)
            except GitHubError as e:
                self.log(f"{task.id}: could not close PR for wont_do: {e}")
        detail = reason or "(no reason given)"
        if note.strip():
            detail += f" — accepted by the person: {note.strip()}"
        self._transition(task, Status.WONT_DO, f"won't do: {detail}")
        self.state.save()

    def _resume_no_change(self, task: Task, dec: dict[str, Any], note: str) -> None:
        """Accepted `no_change`: proceed as if the (unchanged) round had pushed — run the pre-PR checks
        and continue to the PR or the review, without dispatching a new work run."""
        st = self.state.get(task.id)
        st.pop("needs_human", None)
        run = next((r for r in self.runs.runs_for(task.id) if r.run_id == dec.get("run")), None) or self.runs.latest(task.id)
        if run is None:
            raise RuntimeError(f"{task.id} has no run to resume for no_change")
        result = dict(dec.get("result") or {})
        base = run.base or self.base_for(task)
        branch = run.branch or task.branch or task.default_branch()
        worktree = Path(run.worktree) if run.worktree else self.worktree_for(task)
        note_txt = f" ({note.strip()})" if note.strip() else ""
        task.log(f"no-change accepted by the person{note_txt}; resuming the round without a new work run")
        self.store.save(task)
        if worktree.exists():
            try:
                if gitops.has_uncommitted_changes(worktree):
                    gitops.commit_all(worktree, f"{task.id}: leftover changes from worker run {run.run_id}")
                if gitops.commits_ahead(worktree, base) > 0:
                    gitops.push(worktree, branch, base=base)
            except gitops.GitError as e:
                self.log(f"{task.id}: no-change resume git step failed: {e}")
        task.branch = branch
        self.events.emit("decision_accepted", task.id, decision="no_change", note=note[:200])
        self._after_push(task, run, worktree, branch, base, result, TickReport(), "", check_stall=False)
        self.state.save()

    # ---- triage: the human's first look at a draft PR ----------------------
    def triage(self, task: Task, ready: bool = False, changes: str = "", note: str = "") -> None:
        """Record the human's initial review of a draft PR: mark it ready for review, or send
        it back with feedback (a revise run follows)."""
        ensure_open(task)
        if not task.pr:
            raise RuntimeError(f"{task.id} has no PR to triage")
        st = self.state.get(task.id)
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if changes:
            st["pending_feedback"] = f"- **triage** (human): {changes.strip()}"
            st.pop("pending_feedback_easy", None)
            st.pop("pending_feedback_rebase", None)
            st.pop("needs_human", None)
            self._grant_one_more_round(st)
            self.events.emit("triaged", task.id, pr=task.pr, by="human", decision="changes", note=changes[:200])
            self._transition(task, Status.CHANGES_REQUESTED, f"triage: changes requested by hand: {changes[:120]}")
            self.state.save()
            return
        if ready:
            if st.get("pr_draft") and slug and number and self.github.available:
                try:
                    self.github.mark_ready(slug, number)
                except GitHubError as e:
                    self.log(f"{task.id}: could not mark PR ready on GitHub: {e}")
            st["pr_draft"] = False
            st.pop("needs_human", None)
            self.events.emit("triaged", task.id, pr=task.pr, by="human", decision="ready", note=note[:200])
            self._transition(task, Status.IN_REVIEW, "triage: marked ready for review" + (f" ({note[:100]})" if note else ""))
            self.state.save()
            return
        raise RuntimeError("triage needs --ready or --changes")

    # ---- attaching a PR by hand ---------------------------------------------
    def attach_pr(self, task: Task, url: str) -> None:
        """Point this task at a PR opened (or reopened) by hand -- e.g. a stacked PR GitHub
        closed when its base branch went away, reopened under a new number. Resets every
        cached PR fact so the next poll follows the new PR instead of stale state left over
        from the old one: a stale `pr_number` would keep polling the old PR, and a stale
        `review_run` would hold automerge on a run that belongs to a PR this task no longer
        has (CG-174). Used by `garden pr` and its web equivalent, if one exists."""
        st = self.state.get(task.id)
        old_number = st.get("pr_number")
        m = re.search(r"/pull/(\d+)", url)
        new_number = int(m.group(1)) if m else None
        task.pr = url
        for key in ("pr_number", "pr_state", "head_sha", "review_run"):
            st.pop(key, None)
        self._queue_leave(task)
        if new_number:
            st["pr_number"] = new_number
        if task.status in (Status.RUNNING, Status.READY, Status.DRAFT, Status.FAILED):
            task.status = Status.IN_REVIEW
        task.log(f"PR attached: {url} (pr_number {old_number or 'none'} -> {new_number or 'none'})")
        self.events.emit("pr_attached", task.id, pr=url, old_pr_number=old_number or 0, new_pr_number=new_number or 0)
        self.store.save(task)
        self.state.save()

    # ---- manual controls -----------------------------------------------------
    def _cancel_active_run(self, task: Task) -> None:
        """Kill the task's active run and mark it cancelled so it stops occupying a slot.
        Used when a task is pulled out from under a live run (cancel, or a hand retry that
        abandons the current run for a fresh one)."""
        run = self.runs.latest(task.id)
        if run and run.status == "running":
            run.kill()
            run.status = "cancelled"
            run.finished_at = now_iso()
            run.save()

    def cancel(self, task: Task, note: str = "cancelled") -> None:
        ensure_open(task)
        self._cancel_active_run(task)
        self._transition(task, Status.CANCELLED, note)

    def move(self, task: Task, product: str, phase: str) -> None:
        """Move a task to another phase of the same product, keeping its id, run history,
        state.json entry and dependencies: only the file location and `phase:` field change.
        Refuses a task with a run in flight and a closed phase; a frozen phase takes drafts
        only. Emits a `moved` event and logs the move on both phases' task history."""
        if product != task.product:
            raise RuntimeError(f"{task.id} is in {task.product}; a task can only move between phases of its own product")
        try:
            ph = self.store.phase(product, phase)
        except KeyError:
            raise RuntimeError(f"no phase {product}/{phase}") from None
        if ph.key == task.key:
            raise RuntimeError(f"{task.id} is already in {ph.key}")
        if task.status == Status.RUNNING or any(r.task_id == task.id for r in self.runs.active()):
            raise RuntimeError(f"{task.id} has a run in flight; cancel or let it finish before moving")
        if ph.closed:
            raise RuntimeError(f"{ph.key} is closed ({ph.closed}); reopen it first (`garden reopen-phase {ph.key}`)")
        if ph.frozen and task.status != Status.DRAFT:
            raise RuntimeError(f"{ph.key} is frozen ({ph.frozen}); only a draft can move into a frozen phase")
        old_key, old_path = task.key, task.path
        task.phase = phase
        task.path = ph.path / "tasks" / old_path.name
        task.log(f"moved from {old_key} to {ph.key}")
        self.store.save(task)
        if old_path != task.path and old_path.exists():
            old_path.unlink()
        self.events.emit("moved", task.id, **{"from": old_key, "to": ph.key})
        self.log(f"{task.id}: moved {old_key} -> {ph.key}")
        self.store.invalidate()

    def reorder(self, task: Task, after: str | None = None, direction: str = "") -> None:
        """Reorder a task within its own phase section (the backlog). `after` is the id the task
        should follow, "" for the top of the section; `direction` ('up'/'down') is the no-JS
        equivalent, resolved against the section's current order. Writes `order` on every row
        whose rank changes and, when the task crosses a priority band, sets its `priority` to the
        band it landed in. Unlike `move`, a running or in-review task may be reordered. A drop
        that leaves the arrangement unchanged is a no-op."""
        ensure_open(task)
        tasks = self.store.tasks()
        moved = tasks.get(task.id)
        if moved is None:
            raise RuntimeError(f"no task {task.id}")
        section = sorted(
            (t for t in tasks.values()
             if t.product == moved.product and t.phase == moved.phase and not t.status.terminal),
            key=dispatch_sort_key,
        )
        ids = [t.id for t in section]
        i = ids.index(moved.id)
        if direction == "up":
            if i == 0:
                return
            after = ids[i - 2] if i >= 2 else ""
        elif direction == "down":
            if i >= len(ids) - 1:
                return
            after = ids[i + 1]
        after = (after or "").strip()
        if after and after not in ids:
            raise RuntimeError(f"cannot reorder {moved.id}: {after} is not an open task in {moved.key}")
        rest = [tid for tid in ids if tid != moved.id]
        idx = (rest.index(after) + 1) if after else 0
        rest.insert(idx, moved.id)
        if rest == ids:
            return  # dropped where it already was
        # The band the row landed in. The section is sorted ascending by priority, so a valid
        # arrangement needs prev.priority <= band <= next.priority: keep the row's own priority,
        # clamped into that range, so it only changes when the drop lands it among another band.
        pos = rest.index(moved.id)
        prev = tasks[rest[pos - 1]] if pos > 0 else None
        nxt = tasks[rest[pos + 1]] if pos + 1 < len(rest) else None
        lo = prev.priority if prev is not None else -(10**9)
        hi = nxt.priority if nxt is not None else 10**9
        band = min(max(moved.priority, lo), hi)
        original = {t.id: (t.priority, t.order) for t in section}
        for rank, tid in enumerate(rest):
            tasks[tid].order = rank
        old_pri, old_order = original[moved.id]
        moved.priority = band
        for tid in rest:
            t = tasks[tid]
            if t.id != moved.id and (t.priority, t.order) != original[t.id]:
                self.store.save(t)
        band_note = f", priority {priority_label(old_pri)} -> {priority_label(band)}" if band != old_pri else ""
        moved.log(f"reordered in {moved.key} (order {old_order} -> {moved.order}{band_note}) (web)")
        self.store.save(moved)
        self.events.emit("reordered", moved.id, order=moved.order, priority=moved.priority)
        self.store.invalidate()

    def _grant_one_more_review_round(self, st: _TaskState) -> bool:
        """When a human asks for one more automated review after the review cap stopped it,
        roll the counter back one so exactly one more review round is dispatchable. Returns
        True if the cap was raised."""
        max_rounds = int(self.cfg.get("review.max_rounds", 2))
        if int(st.get("review_rounds", 0)) >= max_rounds:
            st["review_rounds"] = max_rounds - 1
            return True
        return False

    def _grant_one_more_round(self, st: _TaskState) -> bool:
        """When a human resumes a task that hit the revision cap, roll the counter back one
        so exactly one more revise round is dispatchable. Returns True if the cap was raised."""
        max_rev = int(self.cfg.get("max_revisions", 3))
        if int(st.get("revisions", 0)) >= max_rev:
            st["revisions"] = max_rev - 1
            return True
        return False

    def retry(self, task: Task) -> None:
        ensure_open(task)
        st = self.state.get(task.id)
        st.pop("needs_human", None)
        if task.status == Status.CHANGES_REQUESTED or (task.pr and task.status in (Status.IN_REVIEW, Status.AWAITING_TRIAGE, Status.FAILED)):
            # let the revise loop continue: keep any PR and dispatch a revise run against the
            # pending feedback. A pre-PR check that failed at the cap has no PR yet, but it is
            # still a revise round — a fresh work run would drop the feedback and the counter.
            note = "re-enabled by hand; revise run will follow"
            if self._grant_one_more_round(st):
                note = "re-enabled by hand with one more round past the revision cap; revise run will follow"
            if not st.get("pending_feedback"):
                st["pending_feedback"] = "- **human**: please re-check the open review comments and CI on this PR and address what is still outstanding."
            self._transition(task, Status.CHANGES_REQUESTED, note)
            self.state.save()
            return
        run = self.runs.latest(task.id)
        if run and run.status == "running":
            # The task is being reset out from under its own active run (e.g. a human
            # retries a task whose worker already finished but the next tick has not
            # reaped it yet). Close the run now — once the task leaves RUNNING, nothing
            # else will reap it, and it would otherwise sit "active" and claim a worker
            # slot forever.
            run.kill()
            run.status = "cancelled"
            run.finished_at = now_iso()
            run.save()
        task.attempts = 0
        if task.status == Status.RUNNING:
            # Abandoning a live run for a fresh one: cancel it so its slot frees up. A run
            # that already finished on disk but has not been reaped is still "running" here
            # and would otherwise hold a slot until the next reap, blocking the new dispatch.
            self._cancel_active_run(task)
        self._transition(task, Status.READY, "reset to ready by hand")
        self.state.save()

    def resume_task(self, task: Task) -> None:
        """'Nothing to fix': clear the needs-human stop and return the task to the state it
        held before the stop, without starting a run. Pending feedback is dropped too — the
        human judged there is nothing to act on."""
        ensure_open(task)
        st = self.state.get(task.id)
        raw = st.get("needs_human")
        if not raw:
            raise RuntimeError(f"{task.id} has no needs-human stop to resume from")
        info = raw if isinstance(raw, dict) else {"reason": str(raw)}
        st.pop("needs_human", None)
        st.pop("pending_feedback", None)
        st.pop("pending_feedback_easy", None)
        st.pop("pending_feedback_rebase", None)
        self.events.emit("resumed", task.id, stop_kind=str(info.get("kind", "")), reason=str(info.get("reason", "")))
        prior = str(info.get("prior_status", ""))
        target: Status | None = None
        if prior in (Status.AWAITING_TRIAGE.value, Status.IN_REVIEW.value):
            target = Status(prior)
        elif task.pr and task.status == Status.CHANGES_REQUESTED:
            target = self._pr_status(task)
        if target is not None and task.status != target:
            self._transition(task, target, f"nothing to fix; resumed to {target.value.replace('_', ' ')} by hand")
        else:
            task.log("nothing to fix; needs-human stop cleared by hand")
            self.store.save(task)
        self.state.save()

    # ---- closing a phase ---------------------------------------------------
    def close_phase(self, phase: Phase, force: bool = False, date: str = "") -> str:
        """Close a phase: it leaves the rail and joins the herbarium. Refuses while it has open
        tasks unless `force`. Returns the closing date written to goals.md ('' if it was
        already closed)."""
        import datetime as _dt

        if phase.closed:
            return ""
        open_tasks = [t for t in phase.tasks if not t.status.terminal]
        if open_tasks and not force:
            ids = ", ".join(f"{t.id} ({t.status.value})" for t in open_tasks)
            raise RuntimeError(f"{phase.key} still has {len(open_tasks)} open task(s): {ids}; finish or cancel them first")
        date = date or _dt.date.today().isoformat()
        self.store.set_phase_closed(phase, date)
        self.events.emit("phase_closed", "", phase=phase.key, closed=date)
        self.log(f"{phase.key} closed ({date})")
        return date

    def reopen_phase(self, phase: Phase) -> None:
        if not phase.closed:
            raise RuntimeError(f"{phase.key} is not closed")
        self.store.set_phase_closed(phase, "")
        self.events.emit("phase_reopened", "", phase=phase.key)
        self.log(f"{phase.key} reopened")

    def finish_manual(self, task: Task, result: dict[str, Any]) -> TickReport:
        from ..runner.manual import ManualRunner

        run = self.runs.latest(task.id)
        if run is None or run.status != "running":
            raise RuntimeError(f"{task.id} has no active run to finish")
        ManualRunner.finish(run, result)
        rep = TickReport()
        self.finalize(task, run, self.runner_for(task, run.runner), rep)
        self.state.save()
        return rep
