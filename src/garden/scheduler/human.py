"""What a person does to a task: answer, accept or reject a decision, triage, cancel, retry, resume, finish."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import gitops
from ..brief import resume_prompt
from ..github import GitHubError, mark_garden_comment
from ..model import Phase, Status, Task, now_iso
from ..runs import Run
from .report import TickReport
from .state import _TaskState


class HumanMixin:
    # ---- human answers -----------------------------------------------------
    def answer(self, task: Task, text: str) -> Run:
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
        if not task.pr:
            raise RuntimeError(f"{task.id} has no PR to triage")
        st = self.state.get(task.id)
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if changes:
            st["pending_feedback"] = f"- **triage** (human): {changes.strip()}"
            st.pop("pending_feedback_easy", None)
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

    # ---- manual controls ---------------------------------------------------
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
        self._cancel_active_run(task)
        self._transition(task, Status.CANCELLED, note)

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
        st = self.state.get(task.id)
        raw = st.get("needs_human")
        if not raw:
            raise RuntimeError(f"{task.id} has no needs-human stop to resume from")
        info = raw if isinstance(raw, dict) else {"reason": str(raw)}
        st.pop("needs_human", None)
        st.pop("pending_feedback", None)
        st.pop("pending_feedback_easy", None)
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
