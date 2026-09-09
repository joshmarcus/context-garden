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
from ..stabilization import ACTORS
from .report import TickReport
from .state import _TaskState

INVESTIGATION_RECOMMENDATIONS = frozenset({
    "resume unchanged", "raise difficulty", "repair environment/verification",
    "change scope/approach", "defer", "cancel",
})


class HumanMixin:
    def set_difficulty(self, task: Task, difficulty: str, *, reason: str = "", actor: str = "") -> None:
        """Set an implementation tier without accidentally lowering an escalation floor.

        A lower tier is reserved for a deliberately simpler follow-up.  Its reason is
        retained in scheduler state as well as the task log so a later dispatch or
        restart cannot mistake the change for an accidental downgrade.
        """
        levels = ("easy", "medium", "hard")
        if difficulty not in levels:
            raise RuntimeError(f"difficulty must be one of {', '.join(levels)}")
        st = self.state.get(task.id)
        floor = str(st.get("difficulty_floor") or "")
        below_floor = floor in levels and levels.index(difficulty) < levels.index(floor)
        if below_floor and not reason.strip():
            raise RuntimeError(
                f"difficulty {difficulty} is below the durable {floor} escalation floor; "
                "give an explicit reason for a deliberately simpler fix"
            )
        old = task.difficulty
        task.difficulty = difficulty
        source = actor.strip() or "operator"
        if below_floor:
            override = {"at": now_iso(), "from": old, "to": difficulty, "floor": floor,
                        "reason": reason.strip(), "actor": source}
            st.setdefault("difficulty_overrides", []).append(override)
            task.log(
                f"difficulty {old} -> {difficulty} ({source}; deliberate override below "
                f"{floor} floor: {reason.strip()})"
            )
            self.events.emit("difficulty_floor_overridden", task.id, **override)
            self.state.save()
        else:
            task.log(f"difficulty {old} -> {difficulty}" + (f" ({source})" if actor else ""))
        self.store.save(task)

    def _apply_revision_policy(self, task: Task, st: _TaskState) -> None:
        """Raise the implementation floor at each durable substantive threshold.

        Explicit task models are never replaced: the conflict becomes an owner decision.
        The operation runs only at a dispatch boundary, so an active run keeps its model.
        """
        policy = self.cfg.revision_policy()
        if not policy["enabled"]:
            return
        count = int(st.get("substantive_revisions", st.get("revisions", 0)))
        every, decision_after = int(policy["every"]), int(policy["decision_after"])
        thresholds = list(st.get("revision_thresholds") or [])
        if st.get("troubled_decisions") and int(st.get("revision_allowance", 0)) <= 0:
            reason = f"the granted revision allowance is exhausted after {count} substantive revisions"
            self._set_needs_human(task, "troubled_task", reason)
            st["troubled"] = {"reason": reason, "counter": count, "at": now_iso(),
                               "owner": "product owner", "recommendation": "investigate before granting more revisions"}
            self.events.emit("troubled_task", task.id, counter=count, reason=reason,
                             difficulty=task.difficulty, model=task.model or "")
            self.state.save()
            raise RuntimeError(f"{task.id} is troubled: {reason}; choose how to continue")
        if count < every or count % every or count in thresholds:
            return
        levels = ("easy", "medium", "hard")
        current = task.difficulty if task.difficulty in levels else "medium"
        if count >= decision_after or current == "hard" or task.model:
            reason = (f"{count} substantive revision rounds reached the decision threshold"
                      if not task.model else
                      f"{count} substantive revision rounds reached an escalation threshold, but explicit model {task.model} is protected")
            self._set_needs_human(task, "troubled_task", reason)
            st["troubled"] = {"reason": reason, "counter": count, "at": now_iso(),
                               "owner": "product owner", "recommendation": "pause and investigate the repeated findings"}
            thresholds.append(count)
            st["revision_thresholds"] = thresholds
            self.events.emit("troubled_task", task.id, counter=count, reason=reason,
                             difficulty=current, model=task.model or "")
            self.state.save()
            raise RuntimeError(f"{task.id} is troubled: {reason}; choose how to continue")
        new = levels[levels.index(current) + 1]
        runner = self.runner_for(task)
        old_model = self.model_for(task, runner)
        task.difficulty = new
        new_model = self.model_for(task, runner)
        at = now_iso()
        event = {"from": current, "to": new, "prior_model": old_model, "model": new_model,
                 "trigger": "substantive_revision_threshold", "reason": f"{count} substantive revisions",
                 "at": at, "counter": count}
        st.setdefault("difficulty_escalations", []).append(event)
        thresholds.append(count)
        st["revision_thresholds"] = thresholds
        st["difficulty_floor"] = new
        task.log(f"difficulty {current} -> {new} after {count} substantive revisions; model {old_model or '(runner default)'} -> {new_model or '(runner default)'}")
        self.store.save(task)
        self.events.emit("difficulty_escalated", task.id, **event)

    def pause_for_investigation(self, task: Task, reason: str, requester: str = "operator",
                                owner: str = "operator", scope: str = "read-only diagnosis",
                                budget: str = "one bounded investigation") -> None:
        """Request an idempotent safe-boundary investigation without touching live work."""
        ensure_open(task)
        st = self.state.get(task.id)
        existing = st.get("investigation")
        if isinstance(existing, dict) and existing.get("status") in ("requested", "draining", "active"):
            return
        if isinstance(existing, dict):
            st.setdefault("investigation_history", []).append(dict(existing))
        active = any(r.status in ("running", "requested", "preparing") for r in self.runs.runs_for(task.id))
        status = "draining" if active else "requested"
        st["investigation"] = {"status": status, "reason": reason.strip() or "troubled task",
            "requester": requester, "owner": owner, "scope": scope, "budget": budget,
            "requested_at": now_iso(), "task": task.id, "task_status": task.status.value,
            "request_id": f"{task.id}-{now_iso()}"}
        self._set_needs_human(task, "investigation", f"investigation {status}: {reason.strip() or 'troubled task'}")
        self.events.emit("investigation_requested", task.id, status=status, owner=owner, scope=scope, budget=budget)
        self.state.save()

    def retry_investigation(self, task: Task, owner: str = "agent") -> None:
        """Retry a failed diagnosis without losing its transcript, cost, or original bounds."""
        ensure_open(task)
        st = self.state.get(task.id)
        inv = st.get("investigation")
        if not isinstance(inv, dict) or inv.get("status") != "failed":
            raise RuntimeError(f"{task.id} has no failed investigation to retry")
        if owner not in ("agent", "operator"):
            raise RuntimeError("investigation owner must be operator or agent")
        st.setdefault("investigation_history", []).append(dict(inv))
        active = any(r.status in ("running", "requested", "preparing") for r in self.runs.runs_for(task.id))
        inv = {key: inv[key] for key in ("reason", "requester", "scope", "budget", "task", "task_status") if key in inv}
        inv.update({"status": "draining" if active else "requested", "owner": owner,
                    "requested_at": now_iso(), "request_id": f"{task.id}-{now_iso()}"})
        st["investigation"] = inv
        self._set_needs_human(task, "investigation", f"investigation retry requested for {owner}")
        self.events.emit("investigation_retried", task.id, owner=owner, request_id=inv["request_id"])
        self.state.save()

    def take_investigation(self, task: Task) -> None:
        """Let the operator claim a ready investigation without changing task work."""
        ensure_open(task)
        inv = self.state.get(task.id).get("investigation")
        if not isinstance(inv, dict) or inv.get("status") not in ("requested", "failed"):
            raise RuntimeError(f"{task.id} has no operator investigation ready to take")
        if inv.get("status") == "failed":
            st = self.state.get(task.id)
            st.setdefault("investigation_history", []).append(dict(inv))
        inv.update({"status": "active", "owner": "operator", "taken_at": now_iso()})
        self._set_needs_human(task, "investigation", "operator investigation active; implementation remains paused")
        self.events.emit("investigation_taken", task.id, owner="operator", request_id=inv["request_id"])
        self.state.save()

    def complete_investigation(self, task: Task, report: dict[str, Any]) -> None:
        ensure_open(task)
        st = self.state.get(task.id)
        inv = st.get("investigation")
        if not isinstance(inv, dict) or inv.get("status") not in ("requested", "active"):
            raise RuntimeError(f"{task.id} has no active investigation")
        required = {"likely_cause", "confidence", "unknowns", "evidence", "attempted_checks",
                    "retain_work", "alternatives", "recommendation"}
        missing = sorted(required - report.keys()) if isinstance(report, dict) else sorted(required)
        if missing:
            raise RuntimeError(f"investigation report is missing: {', '.join(missing)}")
        for field in ("likely_cause", "confidence"):
            if not isinstance(report[field], str) or not report[field].strip():
                raise RuntimeError(f"investigation report {field} is required")
        for field in ("unknowns", "evidence", "attempted_checks", "alternatives"):
            if not isinstance(report[field], list) or not all(isinstance(item, str) for item in report[field]):
                raise RuntimeError(f"investigation report {field} must be a list of text values")
        if not report["evidence"] or not report["attempted_checks"] or not report["alternatives"]:
            raise RuntimeError("investigation report requires evidence, attempted checks, and alternatives")
        if not isinstance(report["retain_work"], bool):
            raise RuntimeError("investigation report retain_work must be true or false")
        if report["recommendation"] not in INVESTIGATION_RECOMMENDATIONS:
            raise RuntimeError("investigation report has an unsupported recommendation")
        links = report.get("links", [])
        if not isinstance(links, list) or not all(isinstance(item, str) for item in links):
            raise RuntimeError("investigation report links must be a list of text values")
        inv.update({"status": "report_ready", "report": report, "completed_at": now_iso()})
        self._set_needs_human(task, "investigation_report", "investigation report ready; choose the next task action")
        self.events.emit("investigation_reported", task.id, owner=inv.get("owner", ""))
        self.state.save()

    def defer_troubled(self, task: Task, reason: str) -> None:
        """Keep preserved work paused with an explicit durable owner reason."""
        ensure_open(task)
        st = self.state.get(task.id)
        info = st.get("needs_human")
        if not isinstance(info, dict) or info.get("kind") not in ("troubled_task", "investigation_report"):
            raise RuntimeError(f"{task.id} has no troubled-task decision to defer")
        if not reason.strip():
            raise RuntimeError("a defer reason is required")
        st["troubled_deferred"] = {"reason": reason.strip(), "at": now_iso(), "counter": int(st.get("substantive_revisions", 0))}
        self._set_needs_human(task, "troubled_task", f"deferred: {reason.strip()}")
        self.events.emit("troubled_deferred", task.id, reason=reason.strip())
        self.state.save()

    def change_troubled_approach(self, task: Task, approach: str, allowance: int = 1) -> None:
        """Queue one preserved revision with the owner's distinct revised approach."""
        if not approach.strip():
            raise RuntimeError("the changed approach is required")
        st = self.state.get(task.id)
        info = st.get("needs_human")
        if not isinstance(info, dict) or info.get("kind") not in ("troubled_task", "investigation_report"):
            raise RuntimeError(f"{task.id} has no troubled-task decision to change")
        old_feedback = str(st.get("pending_feedback") or "").strip()
        st["pending_feedback"] = (old_feedback + "\n\n## Owner-selected change of approach\n\n" + approach.strip()).strip()
        st.setdefault("approach_changes", []).append({"at": now_iso(), "approach": approach.strip()})
        self.continue_troubled(task, allowance=allowance)

    def cancel_troubled(self, task: Task, reason: str) -> None:
        """Cancel from a troubled decision while retaining all branch/run artifacts."""
        ensure_open(task)
        info = self.state.get(task.id).get("needs_human")
        if not isinstance(info, dict) or info.get("kind") not in ("troubled_task", "investigation_report"):
            raise RuntimeError(f"{task.id} has no troubled-task decision to cancel")
        if not reason.strip():
            raise RuntimeError("a cancellation reason is required")
        if any(run.status in ("requested", "preparing", "running") for run in self.runs.runs_for(task.id)):
            raise RuntimeError(f"{task.id} has a run in flight; cancellation decision is stale")
        self._transition(task, Status.CANCELLED, f"cancelled after troubled-task decision: {reason.strip()}")
        self.events.emit("troubled_cancelled", task.id, reason=reason.strip(), preserved_branch=task.branch, preserved_pr=task.pr)

    def continue_troubled(self, task: Task, allowance: int = 1, difficulty: str = "") -> None:
        """Idempotently grant bounded preserved revisions without erasing lifetime history."""
        ensure_open(task)
        if allowance <= 0 or allowance > 3:
            raise RuntimeError("allowance must be between 1 and 3")
        st = self.state.get(task.id)
        raw = st.get("needs_human")
        if not raw or (isinstance(raw, dict) and raw.get("kind") not in ("revision_cap", "troubled_task", "investigation_report")):
            raise RuntimeError(f"{task.id} has no troubled-task decision to continue")
        if difficulty:
            levels = ("easy", "medium", "hard")
            if difficulty not in levels or levels.index(difficulty) < levels.index(task.difficulty):
                raise RuntimeError("difficulty must preserve or raise the current floor")
            task.difficulty = difficulty
        decision = {"at": now_iso(), "allowance": allowance, "difficulty": task.difficulty,
                    "counter": int(st.get("substantive_revisions", st.get("revisions", 0)))}
        st.setdefault("troubled_decisions", []).append(decision)
        st["revision_allowance"] = int(st.get("revision_allowance", 0)) + allowance
        st.pop("needs_human", None)
        st.pop("troubled", None)
        st.pop("investigation", None)
        self._grant_one_more_round(st)
        self._transition(task, Status.CHANGES_REQUESTED, f"troubled task continued with {allowance} bounded revision(s) at {task.difficulty}")
        self.events.emit("troubled_continued", task.id, **decision)
        self.state.save()

    @staticmethod
    def _validate_action_actor(actor: str) -> str:
        """Return a recorded action actor, rejecting ambiguous live provenance."""
        if actor not in ACTORS:
            raise RuntimeError(
                "actor must be one of " + ", ".join(sorted(ACTORS))
            )
        return actor

    # ---- approving a draft --------------------------------------------------
    def approve(self, task: Task, by: str = "", phase: Phase | None = None) -> str:
        """Draft -> ready. The one approve gate the CLI, the web and the TUI share: it refuses a
        task that is not a draft, a closed or frozen phase without a freeze exception
        (`phase_refusal`), and a brief that would cost a run without being ready to work —
        placeholder acceptance criteria or a reading-list path that names no file
        (`brief_gaps`) — then logs and saves. `by` names the surface ("cli"/"web"/"tui"),
        recorded in the log line. Raises RuntimeError on a refusal so each surface reports it in
        its own idiom (a skipped line, a flash, a status message). Returns a warning (never a
        refusal) when this is the phase's first task approved and the phase has no kickoff
        report (CG-224) — the phase is otherwise left free to start."""
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
        warning = ""
        if phase is not None and self._is_phase_start(phase, task) and not self.has_kickoff(phase):
            warning = f"{phase.key} has no kickoff report; run `garden kickoff {phase.key}` before starting work"
        note = f"approved ({by})" if by else "approved"
        if warning:
            note += f"; no kickoff report for {phase.key}"
        self._transition(task, Status.READY, note)
        return warning

    def _is_phase_start(self, phase: Phase, task: Task) -> bool:
        """True when no other task in `phase` has ever left draft: approving `task` would be
        the phase's first task moving into the loop."""
        return not any(t.status != Status.DRAFT for t in phase.tasks if t.id != task.id)

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
        # Snapshot the two fields this dispatch is about to clear from state, before it clears
        # them, so a quota env_error on this very resume run can put them back (see
        # reap._handle_quota_env_error's "resume" branch) instead of losing the question and
        # sending the task to ready, which would also lose whatever PR/feedback led to it.
        snapshot = {"question": question, "session_id": sid}
        st["question"] = ""
        st["session_id"] = ""
        if sid and runner.harness is not None and runner.harness.can_resume:
            run = self.dispatch(task, mode="resume", runner=runner, session_id=sid, prompt_override=resume_prompt(question, text))
        else:
            # harness can't resume: a fresh run with the Q&A in its brief
            run = self.dispatch(task, mode="resume", runner=runner)
        run.env_snapshot = snapshot
        run.save()
        return run

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
        # The decision card is represented by WAITING_HUMAN, but accepting it hands the
        # unchanged branch back to the normal PR/review pipeline. Move out of the human stop
        # before that pipeline can dispatch a detached check; otherwise a check continuation
        # can preserve the waiting status and leave an Inbox question card with no question.
        if task.status == Status.WAITING_HUMAN:
            # A branch without a PR is still in the revise pipeline while its pre-PR check
            # runs. Keep that check's continuation out of the human-stop state too; it records
            # the current status and would otherwise restore waiting_human on the next tick.
            target = self._pr_status(task) if task.pr else Status.CHANGES_REQUESTED
            self._transition(task, target, "no-change accepted; returning to the work pipeline")
        if worktree.exists():
            try:
                self._preserve_dirty_worktree(task, run, worktree)
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
        note = f"PR attached: {url} (pr_number {old_number or 'none'} -> {new_number or 'none'})"
        if task.status in (Status.RUNNING, Status.READY, Status.DRAFT, Status.FAILED):
            self._transition(task, Status.IN_REVIEW, note)
        else:
            task.log(note)
            self.store.save(task)
        self.events.emit("pr_attached", task.id, pr=url, old_pr_number=old_number or 0, new_pr_number=new_number or 0)
        self.state.save()

    def mark_done(self, task: Task, note: str = "", force: bool = False, *, actor: str = "human_owner") -> None:
        """Mark a task done only after its PR's commits reach the final base, unless forced.

        The forced path is the explicit human escape hatch for abandoning an in-review PR.
        """
        if not force:
            ensure_open(task)
        if task.pr and not force and not self._pr_commits_on_base(task):
            raise RuntimeError(
                f"{task.id}'s PR commits are not on its base branch; merge it first or use --force"
            )
        self.events.emit("mark_done", task.id, actor=self._validate_action_actor(actor), reason=note or "marked done")
        self._transition(task, Status.DONE, note or "marked done", base_merged=not force)

    def set_status(self, task: Task, status: Status, note: str, *, actor: str = "human_owner") -> None:
        """Apply an explicit operator status override with durable provenance."""
        actor = self._validate_action_actor(actor)
        self.events.emit("set_status", task.id, actor=actor, reason=note)
        self._transition(task, status, note)

    def _pr_commits_on_base(self, task: Task) -> bool:
        """Whether the recorded PR head is an ancestor of the task's final base branch."""
        head = str(self.state.get(task.id).get("head_sha") or task.branch or "")
        if not head:
            return False
        try:
            repo = self.repo_for(task)
            gitops.fetch(repo)
            return gitops.is_ancestor(repo, head, gitops.base_ref(repo, self.final_base_for(task)))
        except gitops.GitError:
            return False

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
        self.store.invalidate_tasks()

    def reorder(self, task: Task, after: str | None = None, direction: str = "") -> None:
        """Reorder a task within its own phase section (the backlog). `after` is the id the task
        should follow, "" for the top of the section; `direction` ('up'/'down') is the no-JS
        equivalent, resolved against the section's current order. Writes `order` on the moved row
        and, within its destination priority band only, on whichever band-mates now collide with
        its rank; other bands in the section are untouched. When the task crosses a priority
        band, its `priority` is set to the band it landed in. Unlike `move`, a running or
        in-review task may be reordered. A drop that leaves the arrangement unchanged is a
        no-op."""
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
        old_pri, old_order = moved.priority, moved.order
        # `order` only ranks within a priority band (dispatch_sort_key compares priority first),
        # so a drop needs to touch only the destination band: the moved row and whichever of its
        # new band-mates now collide with its rank. Band-mates before the insertion point keep
        # their rank and are left untouched; other priority bands in the section never enter
        # into it at all.
        band_ids = [tid for tid in rest if tid == moved.id or tasks[tid].priority == band]
        moved.priority = band
        for rank, tid in enumerate(band_ids):
            t = tasks[tid]
            if t.order != rank:
                t.order = rank
                if t.id != moved.id:
                    self.store.save(t)
        band_note = f", priority {priority_label(old_pri)} -> {priority_label(band)}" if band != old_pri else ""
        moved.log(f"reordered in {moved.key} (order {old_order} -> {moved.order}{band_note}) (web)")
        self.store.save(moved)
        self.events.emit("reordered", moved.id, order=moved.order, priority=moved.priority)
        self.store.invalidate_tasks()

    def _grant_one_more_review_round(self, st: _TaskState) -> bool:
        """When a human asks for one more automated review after the review cap stopped it,
        roll the counter back one so exactly one more review round is dispatchable. Returns
        True if the cap was raised."""
        max_rounds = self.cfg.review_max_rounds()
        if max_rounds is not None and int(st.get("review_rounds", 0)) >= max_rounds:
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

    def retry(self, task: Task, *, actor: str = "human_owner") -> None:
        ensure_open(task)
        self.events.emit("retry", task.id, actor=self._validate_action_actor(actor), reason="continued loop")
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

    def delegate_recovery(self, task: Task, rep: TickReport | None = None) -> str:
        """Spend one explicitly delegated recovery continuation.

        This is intentionally narrower than ``retry``: it may resume a capped revision
        with its existing feedback, or replay the exact interrupted check continuation.
        A fingerprint is consumed before the continuation is queued, so an unchanged stop
        cannot loop indefinitely under delegated authority.
        """
        ensure_open(task)
        if not bool(self.cfg.get("recovery.delegated", False)):
            raise RuntimeError("delegated recovery is disabled; an owner must choose a retry")
        st = self.state.get(task.id)
        raw = st.get("needs_human")
        info = raw if isinstance(raw, dict) else {}
        kind = str(info.get("kind") or "")
        if kind not in {"revision_cap", "check_did_not_run"}:
            raise RuntimeError(f"{task.id} has no delegated recovery for {kind or 'this stop'}")
        feedback = str(st.get("pending_feedback") or "")
        check = dict(st.get("recovery_check") or {})
        fingerprint = "\x1f".join((kind, feedback, str(check.get("stage") or ""), str(check.get("cause") or "")))
        used = set(str(item) for item in (st.get("delegated_recovery_fingerprints") or []))
        if fingerprint in used:
            raise RuntimeError("this unchanged recovery has already used its delegated continuation")
        rep = rep or TickReport()
        if kind == "revision_cap":
            if not feedback:
                raise RuntimeError("a capped revision has no feedback to retain")
            used.add(fingerprint)
            st["delegated_recovery_fingerprints"] = sorted(used)
            st.pop("needs_human", None)
            self._grant_one_more_round(st)
            self._transition(task, Status.CHANGES_REQUESTED,
                             "delegated operator recovery: one retained-feedback revise round queued")
            self.events.emit("delegated_recovery", task.id, stop_kind=kind, action="revise")
            self.state.save()
            return "one retained-feedback revise round queued"

        if not check.get("specs"):
            raise RuntimeError("the interrupted check has no preserved continuation")
        self._dispatch_check_run(
            task, worktree=Path(str(check.get("cont", {}).get("worktree") or self.worktree_for(task))),
            branch=str(check.get("cont", {}).get("branch") or task.branch),
            base=str(check.get("cont", {}).get("base") or self.base_for(task)),
            specs=list(check["specs"]), stage=str(check["stage"]),
            cont=dict(check["cont"]), rep=rep, retries=int(check.get("retries", 0)) + 1,
            backend=str(check.get("backend") or ""), provenance=str(check.get("provenance") or ""),
        )
        used.add(fingerprint)
        st["delegated_recovery_fingerprints"] = sorted(used)
        st.pop("needs_human", None)
        st.pop("recovery_check", None)
        self.events.emit("delegated_recovery", task.id, stop_kind=kind, action="check")
        self.state.save()
        return "one preserved check continuation queued"

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
        from ..stabilization import gate

        proven, missing = gate(phase)
        if not proven:
            raise RuntimeError(f"{phase.key} stabilization is UNPROVEN: " + "; ".join(missing))
        blocking = [t for t in phase.tasks if t.retro_blocking and not t.status.terminal]
        if blocking and not force:
            ids = ", ".join(f"{t.id} ({t.status.value})" for t in blocking)
            raise RuntimeError(f"{phase.key} has {len(blocking)} open retro-blocking task(s) that must "
                               f"land before it can close: {ids}; finish or cancel them, or close anyway "
                               "with --force")
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
        if run.completion_mode == "pushed":
            return self._finish_pushed_manual(task, run, result)
        if run.completion_mode == "external":
            # A branch-first external session can truthfully end blocked before a PR
            # exists. Its outcome is still guarded and finalized exactly like an
            # ordinary manual run; only successful external work needs PR reconciliation.
            if result.get("status") == "blocked":
                ManualRunner.finish(run, result)
                rep = TickReport()
                self.finalize(task, run, self.runner_for(task, run.runner), rep)
                self.state.save()
                return rep
            return self._finish_external_manual(task, run, result)
        ManualRunner.finish(run, result)
        rep = TickReport()
        self.finalize(task, run, self.runner_for(task, run.runner), rep)
        self.state.save()
        return rep

    def _finish_pushed_manual(self, task: Task, run: Run, result: dict[str, Any]) -> TickReport:
        """Verify a separately-authored remote tip, then use normal remote finalization."""
        from ..runner.manual import ManualRunner

        repository = str(result.get("repository") or "")
        branch = str(result.get("branch") or "")
        pushed_sha = str(result.get("pushed_sha") or "")

        def refuse(reason: str) -> None:
            attempt = {"at": now_iso(), "status": "refused", "reason": reason,
                       "repository": repository, "branch": branch, "pushed_sha": pushed_sha,
                       "cost_usd": None}
            run.completion_attempts.append(attempt)
            run.save()
            self.events.emit("external_completion_refused", task.id, run=run.run_id,
                             reason=reason, repository=repository, branch=branch,
                             pushed_sha=pushed_sha, cost_usd=None, supervised=True)
            raise RuntimeError(reason)

        if result.get("status") == "blocked":
            ManualRunner.finish(run, result)
            rep = TickReport()
            self.finalize(task, run, self.runner_for(task, run.runner), rep)
            self.state.save()
            return rep

        rep = TickReport()
        git_guard_violations = self._git_guard_check(task, run)
        if git_guard_violations:
            refuse("pushed completion refused: clone git internals changed since dispatch")
        violations = self._fence_check(task, run)
        if violations:
            refuse("pushed completion refused: worktree fence violation")

        expected_repository = self.slug_for(task) or ""
        if not repository or repository.lower() != expected_repository.lower():
            refuse(f"pushed completion repository {repository!r} does not match configured repository {expected_repository!r}")
        if not branch or branch != run.branch:
            refuse(f"pushed completion branch {branch!r} does not match claimed branch {run.branch!r}")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", pushed_sha):
            refuse("pushed completion needs an exact full commit SHA")
        repo = self.repo_for(task)
        if not gitops.fetch(repo):
            refuse("could not fetch the configured repository")
        try:
            remote_head = gitops.git("rev-parse", "--verify", f"refs/remotes/origin/{branch}", cwd=repo).strip()
        except gitops.GitError:
            refuse(f"pushed completion branch {branch!r} was not found on the configured repository")
        if remote_head.lower() != pushed_sha.lower():
            refuse(f"pushed completion SHA is stale or does not match origin/{branch}")
        if run.start_head and not gitops.is_ancestor(repo, run.start_head, remote_head):
            refuse(f"origin/{branch} replaced the branch claimed at dispatch; take it again to authorize the new history")

        run.pushed_head = remote_head
        ManualRunner.finish(run, result)
        run.env_snapshot["pushed_completion_submitted"] = True
        run.save()
        self.finalize(task, run, self.runner_for(task, run.runner), rep)
        self.state.save()
        return rep

    def _finish_external_manual(self, task: Task, run: Run, result: dict[str, Any]) -> TickReport:
        """Finalize an operator-owned branch by its PR facts, never a coincidental path."""
        from ..runner.manual import ManualRunner

        url = str(result.get("pr") or run.external_pr or task.pr or "")
        match = re.search(r"/pull/(\d+)", url)
        pr_number = int(match.group(1)) if match else None

        def record_refusal(reason: str) -> None:
            attempt = {"at": now_iso(), "status": "refused", "reason": reason,
                       "cost_usd": None, "pr_url": url, "pr_number": pr_number}
            run.completion_attempts.append(attempt)
            run.save()
            self.events.emit("external_completion_refused", task.id, run=run.run_id, reason=reason,
                             cost_usd=None, supervised=True, pr_url=url, pr_number=pr_number)

        def refuse(reason: str) -> None:
            record_refusal(reason)
            raise RuntimeError(reason)

        # An external claim still shares the dispatch's live-garden and Git-internals
        # protection.  Check Git first: the ordinary fence invokes git against the clone,
        # which must never happen after its metadata has changed.
        rep = TickReport()
        git_guard_violations = self._git_guard_check(task, run)
        if git_guard_violations:
            record_refusal("external completion refused: clone git internals changed since dispatch")
            self._release_fence_bookkeeping(task)
            self._git_guard_fail(task, run, git_guard_violations, rep)
            self.state.save()
            return rep
        violations = self._fence_check(task, run)
        self._release_fence_bookkeeping(task)
        if violations:
            record_refusal("external completion refused: worktree fence violation")
            self._fence_fail(task, run, violations, rep)
            self.state.save()
            return rep

        slug = self.slug_for(task)
        if not match or not slug or not self.github.available:
            refuse("external completion needs an accessible PR URL")
        try:
            pr = self.github.get_pr(slug, pr_number)
        except (GitHubError, KeyError) as e:
            refuse(f"could not read external PR: {e}")
        if not run.branch or pr.head != run.branch:
            refuse(
                f"external PR head {pr.head!r} does not match claimed branch {run.branch!r}; "
                "claim it again with `garden take ID --pr URL`"
            )
        if pr.state == "MERGED":
            head = pr.head_sha or f"origin/{pr.head}"
            try:
                repo = self.repo_for(task)
                gitops.fetch(repo)
                merged = gitops.is_ancestor(repo, head, gitops.base_ref(repo, self.final_base_for(task)))
            except gitops.GitError as e:
                refuse(f"could not verify merged PR ancestry: {e}")
            if not merged:
                refuse(f"merged PR head {head} is not included in final base {self.final_base_for(task)}")
        elif pr.state != "OPEN":
            refuse(f"external PR is {pr.state.lower()}, not open or merged")
        st = self.state.get(task.id)
        task.pr, task.branch = pr.url, pr.head
        st.update({"pr_number": pr.number, "pr_state": pr.state, "pr_base": pr.base,
                   "head_sha": pr.head_sha, "checks": pr.checks,
                   "failed_checks": pr.failed_checks, "review_decision": pr.review_decision})
        ManualRunner.finish(run, {**result, "pr": pr.url})
        run.result = {**result, "pr": pr.url}
        run.finished_at = now_iso()
        run.cost_usd = float(result["cost_usd"]) if isinstance(result.get("cost_usd"), (int, float)) else None
        run.status = "done"
        run.save()
        self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode,
                         harness="human", status="done", cost_usd=run.cost_usd,
                         external=True, supervised=True)
        if pr.state == "MERGED":
            self._transition(task, Status.DONE, f"external PR merged and verified on {self.final_base_for(task)}")
            rep.transitions.append(f"{task.id} -> done (external merged PR)")
            # This follows the ordinary merged-PR lifecycle, but deliberately leaves the
            # operator-owned checkout alone rather than calling `_cleanup`.
            self._on_merged(task, rep, head_sha=pr.head_sha)
        elif pr.state == "OPEN":
            self._transition(task, Status.IN_REVIEW, f"external PR attached at {pr.head}; existing CI is {pr.checks or 'unknown'}")
            rep.transitions.append(f"{task.id} -> in_review (external PR)")
            self._maybe_review(task, run, rep)
        self.state.save()
        return rep
