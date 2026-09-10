"""The automated review round: dispatch, reap the verdict, route it; and the orphan sweep for verdict runs."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import shlex
import sys
from pathlib import Path
from typing import Any

from .. import gitops
from ..criteria import criteria_counts, parse_criteria, required_evidence
from ..github import GitHubError, mark_garden_comment
from ..harness import DIFFICULTIES
from ..model import Status, Task, dispatch_sort_key, ensure_open, now_iso
from ..notify import notify
from ..review import (
    ambiguous_unverified,
    enforce_criteria_verdict,
    feedback_from_review,
    interaction_evidence_gaps,
    parse_review,
    review_brief,
    review_is_description_only,
    review_to_markdown,
    validation_plan,
    visual_source_digest,
)
from ..runs import Run
from .feedback import merge_pending_feedback, remember_pending_feedback
from .report import TickReport
from .resources import ResourcePressureError


class ReviewMixin:
    def _review_ci_ready(self, task: Task) -> bool:
        """Required exact-head validation must land before an automated code review."""
        policy = self.cfg.product_ci_policy(task.product)
        provider = str(policy.get("status_provider") or "github")
        required = bool(policy.get("required"))
        if not required:
            return True
        st = self.state.get(task.id)
        status = st.get("ci_status") or {}
        head = str(st.get("head_sha") or "")
        if provider == "worker_check":
            from ..ci_status import worker_check_status
            status = worker_check_status(self.cfg.garden_dir, task.id, head,
                                         dict(policy.get("worker_check", {}) or {})).to_dict()
            st["ci_status"] = status
        return bool(status.get("green") and status.get("queried_sha") == head)

    # ---- automated review --------------------------------------------------
    def _review_round_pending(self, st: dict[str, Any], product: str | None = None) -> bool:
        """True when `_maybe_review` will still dispatch (or queue) an automated review round
        for this push. A fresh draft PR's triage ping waits for that verdict instead of firing
        on PR-open, per the phase-02 retro (triage pings fired before the review verdict was
        known); when review is off or its rounds are already spent, there is no verdict coming
        and the ping fires right away."""
        if not bool(self.effective("review.enabled", True, product)):
            return False
        max_rounds = self.effective("review.max_rounds", 2, product)
        return max_rounds is None or int(st.get("review_rounds", 0)) < max_rounds

    def _maybe_review(self, task: Task, work_run: Run, rep: TickReport) -> None:
        if not task.pr:
            return
        st = self.state.get(task.id)
        source_head = (self._review_source_head(work_run) if work_run is not None else "")
        source_head = source_head or str(st.get("head_sha") or "")
        if source_head and work_run is not None:
            work_run.env_snapshot["review_source_head"] = source_head
            work_run.save()
        # A review that follows a conflict rebase (or a stale-base rebase, CG-131) re-reads
        # code the reviewer already approved: it runs, but must not count toward review.max_rounds.
        after_rebase = bool(st.pop("last_round_rebase", False))
        requirements = required_evidence(task.body, task.extra.get("requires"))
        evidence = st.setdefault("required_evidence", {})
        for item in requirements:
            evidence.setdefault(f"{item['kind']}:{item['name']}", "queued")
        wanted: list[dict[str, Any]] = []
        if bool(self.effective("review.enabled", True, task.product)):
            max_rounds = self.effective("review.max_rounds", 2, task.product)
            rounds = int(st.get("review_rounds", 0))
            if max_rounds is None or rounds < max_rounds:
                wanted.append({"kind": "review", "count_round": not after_rebase})
            else:
                reason = f"{max_rounds} automated review round(s) used; this PR is yours"
                self._set_needs_human(task, "review_cap", reason)
                self.events.emit("needs_human", task.id, stop_kind="review_cap", reason=reason)
                task.log(f"{reason} — run `garden review {task.id}` for one more round, or review on GitHub")
                self.store.save(task)
                notify(self.cfg.data, task.id, "needs_human", reason, task.pr or "")
                rep.transitions.append(f"{task.id} review cap reached")
            self._record_review_loop_friction(task, st)
        required_personas = [item["name"] for item in requirements if item["kind"] == "persona"]
        for name in dict.fromkeys([*required_personas, *(str(n) for n in list(self.cfg.get("review.personas", []) or []))]):
            if name in required_personas and evidence.get(f"persona:{name}") in ("running", "posted"):
                continue  # required evidence is produced when this PR opens, not once per review round
            wanted.append({"kind": "persona", "name": name, "required": name in required_personas})
        self._dispatch_or_defer_reviews(task, wanted, rep, work_run=work_run)

    def _record_review_loop_friction(self, task: Task, st: dict[str, Any]) -> None:
        """Record one observable, non-blocking signal for a long review episode.

        This deliberately has no needs-human stop: ordinary stall handling remains the
        protection against identical paid retries, while this gives the retro enough context
        to distinguish churn from a genuine new defect.
        """
        threshold = self.cfg.review_friction_after()
        rounds = int(st.get("review_rounds", 0))
        if threshold is None or rounds < threshold or st.get("review_loop_friction"):
            return
        runs = [run for run in self.runs.runs_for(task.id) if run.mode in ("work", "revise", "review")]
        cost = sum(float(run.cost_usd or 0) for run in runs)
        heads = list(dict.fromkeys(str(head) for head in st.get("review_heads", []) if head))
        feedback = str(st.get("pending_feedback") or "").strip()
        cause = self._review_loop_cause(st, feedback)
        evidence = feedback or str((st.get("last_review") or {}).get("summary") or "unknown")
        item = (
            f"Review loop: {rounds} rounds, ${cost:.2f} cumulative work/revise/review cost; "
            f"head lineage {', '.join(heads) or 'unknown'}; cause: {cause}; "
            f"actionable evidence: {evidence[:500]}. "
            "Prevention work: CG-374 (routine recovery), CG-372 (review admission), "
            "CG-323 (worker preflight), CG-339 (proportional application evidence)."
        )
        from ..friction import friction_comment, record_friction

        try:
            phase = self.store.phase(task.product, task.phase)
            record_friction(phase.path / "docs" / "friction.md", [item],
                            f"review loop for {task.id} ({task.title})", now_iso()[:10])
        except KeyError:
            self.log(f"{task.id}: cannot record review-loop friction; phase {task.key} not found")
        slug, number = self.slug_for(task), self._pr_number(task)
        if slug and number and self.github.available:
            try:
                self.github.comment(slug, number, mark_garden_comment(friction_comment([item]), "review-loop"))
            except GitHubError as e:
                self.log(f"{task.id}: could not post review-loop friction: {e}")
        st["review_loop_friction"] = {"rounds": rounds, "cause": cause, "heads": heads, "cost_usd": cost}
        self.events.emit("review_loop_friction", task.id, rounds=rounds, cost_usd=cost,
                         heads=heads, cause=cause, evidence=evidence[:500])

    @staticmethod
    def _review_loop_cause(st: dict[str, Any], feedback: str) -> str:
        """Classify only evidenced loop causes; unexplained loops remain explicitly unknown."""
        text = feedback.lower()
        if st.get("pending_feedback_rebase") or st.get("last_round_rebase"):
            return "mechanical rebase/head change"
        if any(word in text for word in ("capture", "screenshot", "evidence", "infrastructure")):
            return "stale/missing infrastructure evidence"
        if st.get("pending_feedback_easy"):
            return "description-only correction"
        if not feedback and not st.get("last_review"):
            return "lost feedback/state transition"
        if st.get("review_feedback_history", []).count(feedback) > 1:
            return "repeated unaddressed finding"
        if feedback:
            return "newly discovered defect"
        return "unknown"

    def _dispatch_or_defer_reviews(self, task: Task, wanted: list[dict[str, Any]], rep: TickReport,
                                   work_run: Run | None = None, from_pending: bool = False) -> None:
        """Start each wanted review/persona run if a `review_parallel` slot is free; anything
        left over is queued in state (`pending_reviews`) and picked up by `_drain_pending_reviews`
        on a later tick, so a full review_parallel does not lose the round — it just waits its
        turn, the same way a full max_parallel makes a work task wait in the ready queue."""
        st = self.state.get(task.id)
        if self._manual_reserved(task):
            self._queue_pending_reviews(st, wanted)
            return
        required_personas = {item["name"] for item in required_evidence(task.body, task.extra.get("requires"))
                             if item["kind"] == "persona"}
        evidence = st.setdefault("required_evidence", {})
        # Never dispatch a review under a worker round still in flight (work/revise/resume/rebase):
        # its record would sit beside the worker run and could be mistaken for the task's own run,
        # sending a running task back to ready (CG-177). Defer the whole batch — `_drain_pending_reviews`
        # picks it up once the worker finishes — and log it once per deferral episode.
        if self._worker_holding_reviews(task) is not None or st.get("check_run"):
            self._queue_pending_reviews(st, wanted)
            if not st.get("reviews_deferred_for_worker"):
                st["reviews_deferred_for_worker"] = True
                reason = "a validation check is in flight" if st.get("check_run") else "a worker run is in flight"
                self.log(f"{task.id}: review deferred while {reason}")
            return
        st.pop("reviews_deferred_for_worker", None)
        # A review requested while an earlier queued review is eligible waits for the
        # queue drain.  In particular, a just-finished low-priority worker cannot take
        # the local slot before an already waiting critical review.  The drain passes
        # the queued task back here only after clearing its own entry, so it still starts.
        if not from_pending and self._queued_review_precedes(task):
            self._queue_pending_reviews(st, wanted)
            return
        deferred: list[dict[str, Any]] = []
        for item in wanted:
            if item["kind"] == "review" and any(evidence.get(f"persona:{name}") != "posted" for name in required_personas):
                deferred.append(item)
                continue
            if item["kind"] == "review" and not self._review_ci_ready(task):
                deferred.append(item)
                continue
            if self.review_slots_free_for(task) <= 0:
                deferred.append(item)
                continue
            runner_name, harness_name = self._review_item_route(task, item, work_run)
            if runner_name == "local" and self.local_slots_free() <= 0:
                deferred.append(item)
                continue
            tier = str(self.effective("review.difficulty", None, task.product)
                       or task.difficulty or "medium")
            member = self.select_pool_member(task, tier, review=True)
            review_harness = (member or {}).get("harness") or harness_name
            if self.pool_members(tier, review=True) and member is None:
                deferred.append(item)
                continue
            if self.is_harness_paused(review_harness):
                deferred.append(item)
                continue
            kind = item["kind"]
            try:
                if kind == "review":
                    run = self.dispatch_review(task, work_run, count_round=bool(item.get("count_round", True)), member=member)
                    if run.mode == "review":
                        rep.dispatched.append(f"{task.id}(review)")
                        self.log(f"{task.id}: review run {run.run_id} started")
                    else:
                        rep.dispatched.append(f"{task.id}(check:interaction_replay)")
                else:
                    self.dispatch_persona_pr(task, item["name"], required_evidence=bool(item.get("required")), member=member)
                    if item.get("required"):
                        evidence[f"persona:{item['name']}"] = "running"
                    rep.dispatched.append(f"{task.id}(persona:{item['name']})")
            except ResourcePressureError:
                # The preflight above is advisory; the atomic local launch gate may lose
                # its final slot to another scheduler process. Keep the item queued without
                # charging a review round or presenting ordinary occupancy as a failed run.
                deferred.append(item)
            except Exception as e:  # noqa: BLE001
                task.log(f"automated {kind} could not start: {e}")
                self.store.save(task)
                rep.errors.append(f"{task.id}: {kind} dispatch failed: {e}")
                if kind == "review" and not self._verdict_is_moot(task):
                    self._queue_review_recovery(task, None, f"startup failed: {e}", rep, started=False,
                                                count_round=bool(item.get("count_round", True)))
                if kind == "persona" and item.get("required"):
                    self._required_persona_failed(task, str(item["name"]), f"could not start: {e}", rep)
        if deferred:
            self._queue_pending_reviews(st, deferred)

    def _review_route(self, task: Task, work_run: Run | None = None) -> tuple[str, str, Run | None]:
        """Resolve a PR reviewer's harness and model from the live review ladder.

        The last work or revise run is the PR's author.  A writer absent from the ladder
        deliberately retains the existing tier/review_model route.
        """
        writer = work_run if work_run and work_run.mode in ("work", "revise") else None
        if writer is None:
            writer = next((r for r in reversed(self.runs.runs_for(task.id))
                           if r.mode in ("work", "revise")), None)
        writer_key = f"{writer.harness}:{writer.model}" if writer and writer.harness and writer.model else ""
        ladder = [str(entry).strip() for entry in (self.cfg.get("review.ladder") or [])]
        try:
            index = ladder.index(writer_key)
        except ValueError:
            return self.resolved_harness_name(task, str(self.cfg.get("review.harness") or "")), "", writer
        reviewer_key = ladder[min(index + 1, len(ladder) - 1)]
        harness, separator, model = reviewer_key.partition(":")
        if not separator or not harness or not model:
            return self.resolved_harness_name(task, str(self.cfg.get("review.harness") or "")), "", writer
        return harness, model, writer

    def _review_item_route(self, task: Task, item: dict[str, Any],
                           work_run: Run | None = None) -> tuple[str, str]:
        """Return the execution backend and harness used by one pending review item."""
        harness = (self._review_route(task, work_run)[0] if item.get("kind") == "review"
                   else self.resolved_harness_name(task, str(self.cfg.get("review.harness") or "")))
        backend = "remote" if self.runner_for(task).name == "remote" else "local"
        return backend, harness

    def _review_item_wait_reason(self, task: Task, item: dict[str, Any]) -> tuple[str, str] | None:
        """Backend-aware item gate shared by queue admission and its user explanation."""
        current_phase = self.sequential_phase(task.product)
        phase_hold = self.phase_admission_refusal(task)
        if current_phase is not None and current_phase.name != task.phase and phase_hold:
            return "phase", phase_hold
        backend, harness = self._review_item_route(task, item)
        if self.is_harness_paused(harness):
            return "harness", f"{harness} harness paused"
        if backend == "local" and self.local_slots_free() <= 0:
            status = self.resource_status()
            reason = "; ".join(status.reasons) or "local execution capacity is unavailable"
            return "local", reason
        return None

    def _worker_holding_reviews(self, task: Task) -> Run | None:
        """The worker run a review for this task waits behind (a review never runs beside a
        worker round for the same task, CG-177): the newest one, or None."""
        mine = [r for r in self.worker_runs_active() if r.task_id == task.id]
        return mine[-1] if mine else None

    def review_wait_reason(self, task: Task, last_tick: str = "", last_moved: str = "") -> tuple[str, str]:
        """Why a queued review (`pending_reviews`) has not started: the first of the gates the
        tick applies, in the tick's own order, as a gate word and a sentence. The Now page
        shows it, and it reads the predicates `_drain_pending_reviews` and
        `_dispatch_or_defer_reviews` apply, so the page and the tick cannot disagree: the
        drain runs inside dispatch (so a pause holds it), then the worker gate, the review
        harness, the review slots. When none holds it the next tick starts it; when none holds
        it and a tick (`last_tick`, the hub's) has passed since the task last moved
        (`last_moved`, its newest event), something this cannot see is in the way, and the
        sentence sends the person to the task's log rather than promising a recovery."""
        if self.is_dispatch_paused():
            return "paused", "dispatch paused: reviews start again with dispatch"
        run = self._worker_holding_reviews(task)
        if run is not None:
            lifecycle = run.presentation_lifecycle
            if lifecycle == "finished; awaiting collection":
                return "worker", f"its {run.mode} run finished and awaits collection"
            if run.no_process:
                return "worker", f"its {run.mode} local launch was not recorded; the tick that reaps it starts the review"
            if not run.is_local_execution and not (run.claimed_at or run.host):
                return "worker", f"its {run.mode} run is queued for a remote worker to claim"
            if not run.is_local_execution:
                return "worker", f"waits for its {run.mode} remote run; a claim is recorded but liveness is not known"
            return "worker", f"waits for its {run.mode} run to finish"
        if self.state.get(task.id).get("check_run"):
            return "check", "waits for its validation check to finish"
        pending = list(self.state.get(task.id).get("pending_reviews") or [{"kind": "review"}])
        item_reasons = [reason for item in pending if (reason := self._review_item_wait_reason(task, item))]
        harness_reason = next((reason for reason in item_reasons if reason[0] == "harness"), None)
        if harness_reason is not None:
            return harness_reason
        if any(item.get("kind") == "review" for item in pending) and not self._review_ci_ready(task):
            return "ci", "waits for required exact-head CI evidence"
        predecessor = self._queued_review_predecessor(task)
        if predecessor is not None:
            return "queue", (f"queued behind {predecessor.id} (priority {predecessor.priority}; "
                             "reviews use priority, order, then id)")
        if self.review_slots_free() <= 0:
            return "slots", f"no review slot ({len(self.review_runs_active())} of {self.review_parallel_limit()} busy)"
        if item_reasons and len(item_reasons) == len(pending):
            return item_reasons[0]
        if last_tick and last_tick > last_moved:
            return "overdue", "still queued after a tick and no gate explains it: see the task's log"
        return "tick", "queued: the next tick starts it"

    @staticmethod
    def _queue_pending_reviews(st: dict[str, Any], items: list[dict[str, Any]]) -> None:
        """Merge `items` into `st["pending_reviews"]`, keyed by (kind, persona name): a round
        already queued for this task is not queued again, so a review deferred on one tick and
        re-offered on the next (the same worker still in flight) does not pile up duplicate
        entries for the same round (CG-203)."""
        pending = list(st.get("pending_reviews") or [])
        seen = {(i.get("kind"), i.get("name", "")) for i in pending}
        for item in items:
            key = (item.get("kind"), item.get("name", ""))
            if key in seen:
                continue
            seen.add(key)
            pending.append(item)
        st["pending_reviews"] = pending

    def _queued_review_tasks(self) -> list[Task]:
        """Queued review owners in the same deterministic order as ready work.

        Priority is strict across tasks. Within a band, an already queued task keeps
        its turn ahead of a newly requested round; the drain orders simultaneous queued
        tasks with ``dispatch_sort_key`` (explicit order, then id).
        """
        return sorted(
            (task for task in self.store.tasks().values()
             if not task.status.terminal
             and not self.state.get(task.id).get("needs_human")
             and self.state.get(task.id).get("pending_reviews")),
            key=dispatch_sort_key,
        )

    def _queued_review_predecessor(self, task: Task) -> Task | None:
        """The eligible queued task that must be admitted before ``task``, if any."""
        already_queued = bool(self.state.get(task.id).get("pending_reviews"))
        for candidate in self._queued_review_tasks():
            if candidate.id == task.id:
                continue
            if candidate.priority > task.priority:
                break
            if already_queued and dispatch_sort_key(candidate) >= dispatch_sort_key(task):
                continue
            if self._worker_holding_reviews(candidate) is not None:
                continue
            if not self._queued_review_can_start(candidate):
                continue
            # An established queue member wins its priority band over a new request.
            # This is what lets equal-priority reviews take turns instead of allowing
            # the lower sort key to reclaim every newly available slot.
            return candidate
        return None

    def _queued_review_can_start(self, task: Task) -> bool:
        """Whether one of a queued task's items can use a newly free review slot."""
        st = self.state.get(task.id)
        retry_at = str((st.get("review_recovery") or {}).get("retry_at") or "")
        if retry_at:
            try:
                if dt.datetime.now(dt.UTC) < dt.datetime.fromisoformat(retry_at):
                    return False
            except ValueError:
                pass
        if st.get("check_run"):
            return False
        required_personas = {item["name"] for item in required_evidence(task.body, task.extra.get("requires"))
                             if item["kind"] == "persona"}
        evidence = st.get("required_evidence") or {}
        for item in st.get("pending_reviews") or []:
            if item.get("kind") == "review":
                if any(evidence.get(f"persona:{name}") != "posted" for name in required_personas):
                    continue
                if not self._review_ci_ready(task):
                    continue
            if self._review_item_wait_reason(task, item) is None:
                return True
        return False

    def _queued_review_precedes(self, task: Task) -> bool:
        return self._queued_review_predecessor(task) is not None

    def _drain_pending_reviews(self, tasks: dict[str, Task], rep: TickReport) -> None:
        # Local reviews share host admission with workers and checks; remote reviews do
        # not. Drain before ready work starts, strict by task priority among eligible
        # entries. A backend-held task is requeued without preventing the next eligible
        # task from using the global slot; equal-priority tasks are deterministic by
        # order then id.
        self._audit_review_continuations(tasks, rep)
        for task in sorted(tasks.values(), key=dispatch_sort_key):
            if self.review_slots_free() <= 0:
                break
            st = self.state.get(task.id)
            if task.status.terminal:
                self._retire_terminal_review_recovery(task)
                continue
            if st.get("needs_human"):
                continue
            pending = list(st.get("pending_reviews") or [])
            if not pending:
                continue
            if not self._queued_review_can_start(task):
                continue
            st["pending_reviews"] = []
            self._dispatch_or_defer_reviews(task, pending, rep, from_pending=True)

    def _retire_terminal_review_recovery(self, task: Task) -> bool:
        """Discard queued review intent once its task has reached a terminal state.

        ``_transition`` is the normal boundary, while the pending-review drain also calls
        this helper to repair state left by an older controller or an interrupted write.
        The event retains why the continuation disappeared without allowing it to revive a
        completed or cancelled task.
        """
        st = self.state.get(task.id)
        pending = list(st.get("pending_reviews") or [])
        recovery = st.get("review_recovery") or {}
        if not pending and not recovery:
            return False
        st.pop("pending_reviews", None)
        st.pop("review_recovery", None)
        self.state.save()
        reason = f"automatic review recovery retired because task is {task.status.value}"
        task.log(reason)
        self.store.save(task)
        self.events.emit(
            "review_recovery_retired",
            task.id,
            status=task.status.value,
            reason=reason,
            pending=len(pending),
            head=str(recovery.get("head") or ""),
        )
        return True

    def _audit_review_continuations(self, tasks: dict[str, Task], rep: TickReport) -> None:
        """Restore a reviewable current head that has neither a verdict nor a continuation."""
        for task in tasks.values():
            st = self.state.get(task.id)
            head = str(st.get("head_sha") or "")
            approved_head = self._effective_approved_head(task, st) if head else ""
            approval_is_current = (
                self._review_approval_is_proven(task, st) and approved_head == head
            )
            if approval_is_current:
                pending = list(st.get("pending_reviews") or [])
                retained = [
                    item for item in pending
                    if item.get("kind") != "review" or item.get("count_round") is False
                ]
                if retained != pending:
                    if retained:
                        st["pending_reviews"] = retained
                    else:
                        st.pop("pending_reviews", None)
                    if not any(item.get("kind") == "review" for item in retained):
                        recovery = st.get("review_recovery") or {}
                        if str(recovery.get("head") or "") == head:
                            st.pop("review_recovery", None)
                    self.state.save()
            blocked = str(st.get("automerge_blocked") or "")
            if (approval_is_current
                    and (re.search(r"review round\(s\).*need [2-9]\d*", blocked)
                         or "second review" in blocked)):
                self._queue_clear_block(task)
                self.state.save()
            if task.status not in (Status.AWAITING_TRIAGE, Status.IN_REVIEW):
                continue
            if not bool(self.effective("review.enabled", True, task.product)):
                continue
            recovery = st.get("review_recovery") or {}
            recovery_head = str(recovery.get("head") or "")
            if recovery_head and head and recovery_head != head:
                pending = [item for item in (st.get("pending_reviews") or [])
                           if item.get("kind") != "review"]
                if pending:
                    st["pending_reviews"] = pending
                else:
                    st.pop("pending_reviews", None)
                st.pop("review_recovery", None)
                reason = f"review recovery for {recovery_head} discarded after head moved to {head}"
                task.log(reason)
                self.store.save(task)
                self.events.emit("review_recovery_obsolete", task.id,
                                 recovery_head=recovery_head, head=head)
                rep.transitions.append(f"{task.id} stale review recovery discarded")
                self.state.save()
            if st.get("review_run") or st.get("pending_reviews") or st.get("needs_human"):
                continue
            review_runs = [run for run in self.runs.runs_for(task.id) if run.mode == "review"]
            applied_run = str(st.get("last_review_run") or "")
            current_verdict = (bool(st.get("last_review")) and any(
                run.run_id == applied_run
                and (
                    str((run.env_snapshot or {}).get("review_head") or "") == head
                    or (
                        str((run.env_snapshot or {}).get("review_head") or "")
                        == str(st.get("last_review_head") or "")
                        and self._review_approval_is_proven(task, st)
                        and approved_head == head
                    )
                )
                for run in review_runs
            )) if head else bool(st.get("last_review"))
            minimum = self._automerge_min_review_rounds(task)
            missing_additional = current_verdict and int(st.get("review_rounds", 0)) < minimum
            if current_verdict and not missing_additional:
                continue
            available = next((run for run in reversed(review_runs)
                              if str((run.env_snapshot or {}).get("review_head") or "") == head
                              and run.status not in ("running", "superseded")
                              and bool(run.result)), None) if head and not current_verdict else None
            if available is not None:
                st["review_run"] = available.run_id
                self.state.save()
                self.reap_review(task, rep)
                continue
            lost = next((run for run in reversed(self.runs.runs_for(task.id))
                         if run.mode == "review"
                         and str((run.env_snapshot or {}).get("review_head") or "") == head
                         and run.status not in ("running", "superseded")
                         and not run.result), None) if head and not current_verdict else None
            if lost is not None:
                self._queue_review_recovery(
                    task, lost, lost.error or f"terminal {lost.status} review has no verdict", rep,
                    started=self._review_execution_started(lost),
                    count_round=bool((lost.env_snapshot or {}).get("count_round", True)),
                )
                continue
            if not self._review_round_pending(st, task.product):
                continue
            self._queue_pending_reviews(st, [{"kind": "review", "count_round": True}])
            st["review_recovery"] = {"head": head, "attempts": 0,
                                     "limit": int(self.cfg.get("review.recovery_attempts", 2) or 0),
                                     "retry_at": "", "reason": "reviewable head lost every continuation",
                                     "owner": "scheduler", "started": False, "last_run": ""}
            self.events.emit("review_recovery", task.id, head=head, attempt=0,
                             reason="reviewable head lost every continuation")
            rep.transitions.append(f"{task.id} missing review continuation restored")

    def _supersede_running_review(self, task: Task) -> None:
        """A second review dispatched for this task (a person pressed "one more review"
        after a push, or the poll re-reviewed a fresh push) leaves the previous round's
        run pointed at by nothing once `review_run` is overwritten below — closing it
        first means its process is stopped and its eventual verdict is never read, rather
        than the CG-079 bug where the stale record stayed `running` forever and held
        automerge on "a run is in flight" (CG-144)."""
        st = self.state.get(task.id)
        run_id = st.get("review_run")
        if not run_id:
            return
        run = next((r for r in self.runs.runs_for(task.id) if r.run_id == run_id), None)
        if run is None or run.status != "running":
            return
        run.kill()
        run.exit_code = run.read_exit_code()
        if run.process_finished():
            try:
                runner = self.runner_for(task, run.runner, run.harness)
                collected = runner.collect(run)
                run.usage = collected.get("usage") or {}
                run.cost_usd = collected.get("cost_usd")
                run.model = str(collected.get("model") or run.model)
            except Exception as e:  # noqa: BLE001
                run.error = str(e)
        run.finished_at = now_iso()
        run.status = "superseded"
        note = "superseded by a newer review dispatch for the same task"
        run.error = f"{run.error} ({note})" if run.error else note
        run.save()
        self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, harness=run.harness,
                         model=run.model, pool_member=run.pool_member, status="superseded",
                         cost_usd=run.cost_usd, usage=run.usage)
        self.log(f"{task.id}: review run {run.run_id} superseded by a new review dispatch")

    @staticmethod
    def _review_source_head(run: Run) -> str:
        """Return immutable output-head provenance recorded by an author run."""
        snapshot = run.env_snapshot or {}
        result = run.result or {}
        interaction = result.get("interaction")
        return str(
            snapshot.get("review_source_head")
            or run.pushed_head
            or result.get("pushed_sha")
            or result.get("head")
            or (interaction.get("head") if isinstance(interaction, dict) else "")
            or ""
        )

    def _review_source_for_head(self, task: Task, head: str,
                                explicit: Run | None = None) -> Run | None:
        """Find the completed author result that produced exactly the reviewed head."""
        candidates = ([explicit] if explicit is not None else []) + list(
            reversed(self.runs.runs_for(task.id))
        )
        seen: set[str] = set()
        for candidate in candidates:
            if candidate is None or candidate.run_id in seen:
                continue
            seen.add(candidate.run_id)
            if candidate.mode not in ("work", "revise", "resume") or candidate.status != "done":
                continue
            if self._review_source_head(candidate) == head:
                return candidate
        return None

    def dispatch_review(self, task: Task, work_run: Run | None = None, count_round: bool = True,
                        reask_missing_fixes: bool = False,
                        clarify_unverified: list[str] | None = None,
                        clarifies_review_run: str = "",
                        member: dict[str, Any] | None = None) -> Run:
        if self._manual_reserved(task):
            raise RuntimeError(f"{task.id} is reserved in Manual mode")
        self.require_maintenance_running()
        ensure_open(task)
        investigation = self.state.get(task.id).get("investigation") or {}
        if investigation.get("status") in ("requested", "draining", "active", "report_ready"):
            raise RuntimeError(f"{task.id} is paused for investigation ({investigation.get('status')})")
        self._refuse_if_phase_not_admitted(task)
        harness_name, ladder_model, writer = self._review_route(task, work_run)
        review_tier = str(self.effective("review.difficulty", None, task.product)
                          or task.difficulty or "medium")
        member = member if member is not None else self.select_pool_member(task, review_tier, review=True)
        if self.pool_members(review_tier, review=True) and member is None:
            raise RuntimeError("every review pool member is paused")
        if member is not None:
            harness_name = str(member.get("harness") or "")
            ladder_model = None
        runner_name = "remote" if self.runner_for(task).name == "remote" else "local"
        runner = self.runner_for(task, runner_name, harness_name)
        self._raise_if_harness_paused(runner.harness.name if runner.harness else "")
        self._supersede_running_review(task)
        base = self.base_for(task)
        branch = task.branch or task.default_branch()
        canonical_enabled = str(self.cfg.product_checkout(task.product).get("strategy") or "worktree") == "in_place"
        run: Run | None = None
        canonical = None
        if canonical_enabled:
            run = (self.runs.new_run(task.id, "remote", mode="review")
                   if runner_name == "remote" else self._new_local_run(task.id, "review", "review"))
            run.branch, run.base = branch, base
            canonical = self.prepare_canonical_run(task, run, runner, branch, base)
        wt = canonical or gitops.prepare_worktree(self.repo_for(task), self.worktree_for(task), branch, base)
        if run is not None:
            run.worktree = str(wt)
            run.save()
        diff = gitops.diff(wt, base)
        review_head = gitops.head_sha(wt)
        review_base_head = gitops.rev_parse(wt, gitops.base_ref(wt, base))
        review_diff_hash = gitops.diff_hash(wt, base)
        changed = gitops.diff_names(wt, base)
        work_run = self._review_source_for_head(task, review_head, work_run)
        source_run = work_run.run_id if work_run is not None else ""
        source_head = self._review_source_head(work_run) if work_run is not None else ""
        pr_title, pr_body, pr_comment, verified, pre_flight = task.title, "", "", None, None
        author_interaction: dict[str, Any] | None = None
        if work_run is not None:
            pr_title = str(work_run.result.get("pr_title") or task.title)
            pr_body = str(work_run.result.get("pr_body") or "")
            pr_comment = str(work_run.result.get("pr_comment") or "")
            verified = work_run.result.get("verified")
            pre_flight = work_run.result.get("pre_flight")
            candidate = work_run.result.get("interaction")
            author_interaction = candidate if isinstance(candidate, dict) else None
        criteria_snapshot: list[str] | None = None
        if work_run is not None and "criteria" in (work_run.env_snapshot or {}):
            criteria_snapshot = list((work_run.env_snapshot or {}).get("criteria") or [])
        if criteria_snapshot is None:
            for prior in reversed(self.runs.runs_for(task.id)):
                if prior.mode in ("work", "revise", "resume") and "criteria" in (prior.env_snapshot or {}):
                    criteria_snapshot = list((prior.env_snapshot or {}).get("criteria") or [])
                    if criteria_snapshot is not None:
                        break
        if criteria_snapshot is None:
            criteria_snapshot = parse_criteria(task.body)
        if verified is None:
            verified = self._last_worker_verified(task)
        if pre_flight is None:
            pre_flight = self._last_worker_preflight(task)
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available and not pr_body:
            try:
                info = self.github.get_pr(slug, number)
                pr_title, pr_body = info.title or pr_title, info.body
            except GitHubError:
                pass
        plan = validation_plan(changed, task.title, task.body, pr_title, pr_body, head=review_head,
                               check_specs=self._pre_pr_specs(task), visual_scope=task.extra.get("visual_scope"),
                               capture_infrastructure_policy=self.cfg.capture_infrastructure_policy())
        plan["visual_source"] = visual_source_digest(wt, plan)
        # Stored plans and broad path classifiers from older releases may say a generic
        # replay is required. Review admission now leaves the verification method to the
        # agent; the plan is context, never a prerequisite.
        plan["evidence_policy"] = "reviewer_judgment"
        needs_interaction = False
        needs_scalability = False
        interaction_reason = next((row["reason"] for row in plan["reasons"]
                                   if row["item"] == "served interaction"), "non-UI change")
        capture_paths: list[str] = []
        capture_pages: list[str] = []
        capture_advisories: list[dict[str, Any]] = []
        check_results: list[dict[str, Any]] = []
        current_check = None
        reusable_capture_check = None
        stale_validation_check = False
        for check_run in reversed(self.runs.runs_for(task.id)):
            checked_plan = (check_run.env_snapshot or {}).get("validation_plan")
            if check_run.mode == "check" and isinstance(checked_plan, dict):
                stale_validation_check = stale_validation_check or checked_plan.get("head") != review_head
            if (check_run.mode == "check" and check_run.status == "done"
                    and isinstance(checked_plan, dict) and checked_plan.get("head") == review_head):
                current_check = check_run
                plan = checked_plan
                break
            if (check_run.mode == "check" and check_run.status == "done"
                    and isinstance(checked_plan, dict) and plan["pages"]
                    and checked_plan.get("pages") == plan["pages"]
                    and checked_plan.get("visual_source") == plan["visual_source"]):
                reusable_capture_check = check_run
        if current_check is not None:
            check_results = list((current_check.result or {}).get("checks", []))
        capture_check = current_check or reusable_capture_check
        if capture_check is not None:
            indexed_ui_results = [
                (index, result)
                for index, result in enumerate((capture_check.result or {}).get("checks", []))
                if result.get("name") == "ui"
            ]
            ui_results = [
                result for index, result in indexed_ui_results
                if result.get("status") == "pass"
                and self._trusted_generated_ui_result(capture_check, index)
            ]
            capture_paths = [str(p) for result in ui_results for p in result.get("captures", [])
                             if str(p).endswith(".png")]
            capture_pages = [str(page) for result in ui_results for page in result.get("pages", [])]
            policy = str(plan.get("capture_infrastructure_policy") or "require")
            for index, result in indexed_ui_results:
                reason = self._capture_infrastructure_advisory(
                    capture_check, result, index, policy=policy)
                if reason:
                    capture_advisories.append({
                        "diagnostic": reason,
                        "artifacts": [str(path) for path in result.get("captures", [])
                                      if not str(path).endswith(".png")],
                    })
        plan["evidence_policy"] = "reviewer_judgment"
        needs_interaction = False
        needs_scalability = False
        interaction_reason = next((row["reason"] for row in plan["reasons"]
                                   if row["item"] == "served interaction"), "non-UI change")
        replay = self.state.get(task.id).get("interaction_replay") or {}
        replay_config = task.extra.get("interaction_replay")
        replay_config = replay_config if isinstance(replay_config, dict) else {}
        affected_flow = str(replay_config.get("affected_flow") or "").strip()
        author_gaps = interaction_evidence_gaps(
            {"interaction": author_interaction}, required=True, scalability=needs_scalability,
            expected_head=review_head, affected_flow=affected_flow,
            expected_criteria=criteria_snapshot,
        ) if needs_interaction and author_interaction is not None else ["not reported"]
        reusable_author_interaction = needs_interaction and not author_gaps
        replay_matches = (replay.get("head") == review_head
                          and str(replay.get("affected_flow") or "") == affected_flow)
        if needs_interaction and not reusable_author_interaction and not replay_matches:
            # The replay is an ordinary detached check, with the same scrubbed
            # environment, heavy-work lease and resource limits as other validation.
            # A later tick collects its digest before the reviewer process can start.
            self._queue_pending_reviews(self.state.get(task.id), [
                {"kind": "review", "count_round": count_round}])
            current = self.state.get(task.id).get("check_run") or {}
            if current.get("run_id"):
                existing = self._run_by_id(task, current["run_id"])
                if existing is not None:
                    if run is not None:
                        run.status = "superseded"
                        run.finished_at = now_iso()
                        run.save()
                    if canonical is not None:
                        from ..canonical import release

                        release(canonical, run.run_id)
                    return existing
            nonce = secrets.token_urlsafe(24)
            out = self.cfg.garden_dir / "interaction-replays" / task.id / nonce
            module = str(replay_config.get("module") or (
                "garden.invalid_interaction_replay_selection" if affected_flow
                else "garden.interaction_replay"
            ))
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
                module = "garden.invalid_interaction_replay_selection"
            command = shlex.join([
                "env", f"PYTHONPATH={wt / 'src'}", sys.executable,
                "-m", module, "--out", str(out),
                "--head", review_head, f"--nonce={nonce}",
            ])
            if run is not None:
                run.status = "superseded"
                run.finished_at = now_iso()
                run.save()
            if canonical is not None:
                from ..canonical import release

                release(canonical, run.run_id)
            replay_run = self._dispatch_check_run(
                task, worktree=wt, branch=branch, base=base,
                specs=[{"name": "interaction replay", "command": command}],
                stage="interaction_replay", extra={"timeout": 180},
                cont={"head": review_head, "nonce": nonce, "affected_flow": affected_flow,
                      "manifest": str(out / "interaction-manifest.json"),
                      "worktree": str(wt), "branch": branch, "base": base}, rep=TickReport(),
            )
            replay_run.env_snapshot["validation_plan"] = plan
            replay_run.save()
            return replay_run
        replay_nonce = str(replay.get("nonce") or "") if needs_interaction and not reusable_author_interaction else ""
        replay_manifest = Path(str(replay.get("manifest") or "."))
        replay_digest = str(replay.get("digest") or "") if needs_interaction else ""
        # Controller-owned captures and replay manifests are local paths. Keep their
        # reviewer local rather than handing a remote worker evidence it cannot inspect.
        controller_evidence = bool(capture_paths or needs_interaction)
        runner_name = ("remote" if self.runner_for(task).name == "remote" and not controller_evidence
                       else "local")
        runner = self.runner_for(task, runner_name, harness_name)
        self._raise_if_harness_paused(runner.harness.name if runner.harness else "")
        if run is None:
            run = (self.runs.new_run(task.id, "remote", mode="review")
                   if runner_name == "remote" else self._new_local_run(task.id, "review", "review"))
        reference_files: dict[str, str] = {}
        text = review_brief(self.store, task, branch=branch, base=base, pr_title=pr_title, pr_body=pr_body,
                            diff=diff, max_diff_chars=int(self.cfg.get("review.max_diff_chars", 60000)),
                            pr_comment=pr_comment, verified=verified, captures=capture_paths,
                            checks=check_results, capture_advisories=capture_advisories,
                            reask_missing_fixes=reask_missing_fixes,
                            interaction_required=needs_interaction, scalability_required=needs_scalability,
                            review_head=review_head, interaction_reason=interaction_reason,
                            interaction_manifest=(str(replay_manifest) if needs_interaction
                                                  and not reusable_author_interaction else ""),
                            criteria_snapshot=criteria_snapshot, pre_flight=pre_flight, plan=plan,
                            author_interaction=author_interaction,
                            clarify_unverified=clarify_unverified,
                            author_source_run=source_run, author_source_head=source_head,
                            reference_files=reference_files)
        from ..reference_snapshot import write_reference_files

        write_reference_files(run.path, reference_files, self.cfg.data)
        ci_status = self.state.get(task.id).get("ci_status") or {}
        if ci_status:
            text += ("\n\n## Exact-head CI evidence\n\nThe controller admitted this review with "
                     f"`{ci_status.get('provider', 'unknown')}` status `{ci_status.get('state', 'unknown')}` "
                     f"for `{ci_status.get('queried_sha', '')}`. Evidence: "
                     f"{ci_status.get('evidence_url') or 'not available'}. Treat a different review head "
                     "as stale and do not approve it.\n")
        run.branch, run.base, run.worktree = branch, base, str(wt)
        # Remembered so a quota env_error on this run (reap_review, below) knows whether this
        # dispatch actually counted a round — an after-rebase round is exempt from
        # review.max_rounds and must not be charged for having been retried.
        required_pages = set(plan["pages"])
        if capture_advisories:
            required_pages.clear()
        if "*" in required_pages:
            required_pages = set(capture_pages)
        run.env_snapshot.update({"count_round": count_round, "capture_pages": sorted(required_pages),
                            "review_head": review_head, "interaction_required": needs_interaction,
                            "review_base_head": review_base_head, "review_diff_hash": review_diff_hash,
                            "scalability_required": needs_scalability,
                            "validation_check_current": current_check is not None or (not stale_validation_check and not plan["pages"]),
                            "interaction_replay_manifest": str(replay_manifest) if needs_interaction else "",
                            "interaction_replay_nonce": replay_nonce,
                            "interaction_replay_digest": replay_digest,
                            "affected_flow": affected_flow,
                            "author_interaction_reused": reusable_author_interaction,
                            "author_source_run": source_run, "author_source_head": source_head,
                            "reask_missing_fixes": reask_missing_fixes,
                            "clarify_unverified": bool(clarify_unverified),
                            "criteria": criteria_snapshot, "validation_plan": plan})
        if clarifies_review_run:
            run.env_snapshot["clarifies_review_run"] = clarifies_review_run
        run.env_snapshot.update({"product": task.product,
                                 "execution_timeout_minutes": self.cfg.product_timeout_minutes(task.product),
                                 "resource_weight": self.cfg.product_resource_weight(task.product)})
        review_difficulty = str(self.effective("review.difficulty", None, task.product) or task.difficulty or "medium")
        if review_difficulty not in DIFFICULTIES:
            review_difficulty = "medium"
        run.difficulty = review_difficulty
        run.harness = runner.harness.name if runner.harness else ""
        run.model = self.model_for(task, runner, review_difficulty)
        if ladder_model:
            run.model = ladder_model
        elif member is not None:
            run.model = str(member.get("model") or "")
        elif runner.harness and runner.harness.cfg.get("review_model"):
            run.model = str(runner.harness.cfg["review_model"])
        run.pool_member = str((member or {}).get("label") or "")
        if ladder_model and writer:
            run.env_snapshot.update({"writer_harness": writer.harness, "writer_model": writer.model,
                                     "review_rung": f"{runner.harness.name if runner.harness else harness_name}:{run.model}"})
        run.brief_tokens = max(1, len(text) // 4)
        run.save()
        try:
            runner.start(run, wt, text)
        except Exception as exc:
            run.status = "failed"
            run.finished_at = now_iso()
            run.error = f"startup failed before execution was confirmed: {exc}"
            run.save()
            raise
        st = self.state.get(task.id)
        st["review_run"] = run.run_id
        st.setdefault("review_heads", []).append(run.env_snapshot["review_head"])
        if count_round:
            st["review_rounds"] = int(st.get("review_rounds", 0)) + 1
        if ladder_model and writer:
            task.log(f"reviewed by {run.model}, one above {writer.model}")
            self.store.save(task)
        self.events.emit("dispatch", task.id, run=run.run_id, mode="review", model=run.model, harness=run.harness,
                         pool_member=run.pool_member)
        self.state.save()
        return run

    def _after_interaction_replay_check(self, task: Task, run: Run,
                                        results: list[dict[str, Any]], cont: dict[str, Any],
                                        rep: TickReport) -> None:
        """Record completed replay provenance before admitting any model reviewer."""
        path = Path(str(cont["manifest"]))
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = ""
        self.state.get(task.id)["interaction_replay"] = {
            "head": cont["head"], "nonce": cont["nonce"], "manifest": str(path),
            "digest": digest, "run_id": run.run_id,
            "affected_flow": str(cont.get("affected_flow") or ""),
        }
        self.state.save()

    def review_again(self, task: Task) -> Run:
        """The person asked for one more automated review after the cap stopped it: raise
        this task's review cap by one round, clear the stop, and dispatch immediately."""
        ensure_open(task)
        if not task.pr:
            raise RuntimeError(f"{task.id} has no PR to review")
        self._refuse_if_phase_not_admitted(task)
        st = self.state.get(task.id)
        self._grant_one_more_review_round(st)
        st.pop("needs_human", None)
        return self.dispatch_review(task)

    def _resume_review_clarification(self, task: Task, source: Run, entries: list[str],
                                     rep: TickReport) -> bool:
        """Restore or create the one reviewer-only clarification for a terminal result."""
        st = self.state.get(task.id)
        existing = next((candidate for candidate in reversed(self.runs.runs_for(task.id))
                         if candidate.mode == "review"
                         and str((candidate.env_snapshot or {}).get("clarifies_review_run") or "")
                         == source.run_id
                         and candidate.status != "superseded"), None)
        if existing is not None:
            st["review_run"] = existing.run_id
            self.state.save()
            rep.transitions.append(f"{task.id} review clarification continuation restored")
            return True
        self.dispatch_review(
            task, count_round=False, clarify_unverified=entries,
            clarifies_review_run=source.run_id,
        )
        rep.transitions.append(f"{task.id} review re-asked to classify unverified observations")
        return True

    @staticmethod
    def _review_execution_started(run: Run) -> bool:
        """Classify execution from claim/process/output evidence, never worktree age."""
        if run.runner == "remote":
            return bool(run.claimed_at or run.host or (run.path / "remote_result.json").exists()
                        or (run.path / "stdout.json").exists())
        return bool(run.pid is not None or (run.path / "stdout.json").exists()
                    or (run.path / "final.md").exists())

    def _queue_review_recovery(self, task: Task, run: Run | None, reason: str, rep: TickReport,
                               *, started: bool, count_round: bool,
                               refund_round: bool = False, backoff: bool = True) -> bool:
        """Retain one head-bound review continuation, or stop after bounded retries."""
        st = self.state.get(task.id)
        old = st.get("review_recovery") or {}
        head = str(((run.env_snapshot if run else {}) or {}).get("review_head") or
                   old.get("head") or st.get("head_sha") or "")
        attempts = int(old.get("attempts", 0)) + 1 if old.get("head") == head else 1
        limit = int(self.cfg.get("review.recovery_attempts", 2) or 0)
        st["review_run"] = ""
        if run is not None and count_round and (not started or refund_round):
            st["review_rounds"] = max(0, int(st.get("review_rounds", 0)) - 1)
        if attempts > limit:
            st.pop("pending_reviews", None)
            message = f"automatic review recovery exhausted after {limit} attempt(s): {reason}"
            self._set_needs_human(task, "review_recovery_exhausted", message)
            task.log(message)
            self.store.save(task)
            rep.transitions.append(f"{task.id} review recovery exhausted")
            self.state.save()
            return True
        delay = float(self.cfg.get("review.recovery_backoff_seconds", 30) or 0) * attempts
        retry_at = ((dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)).isoformat()
                    if backoff else "")
        st["review_recovery"] = {"head": head, "attempts": attempts, "limit": limit,
                                 "retry_at": retry_at, "reason": reason, "owner": "scheduler",
                                 "started": started, "last_run": run.run_id if run else ""}
        self._queue_pending_reviews(st, [{
            "kind": "review",
            "count_round": count_round and (not started or refund_round),
        }])
        task.log(f"automatic review recovery {attempts}/{limit} queued for the current head: {reason}")
        self.store.save(task)
        self.events.emit("review_recovery", task.id, run=run.run_id if run else "", head=head,
                         attempt=attempts, limit=limit, started=started, reason=reason)
        rep.transitions.append(f"{task.id} review recovery queued ({attempts}/{limit})")
        self.state.save()
        return True

    def reap_review(self, task: Task, rep: TickReport) -> bool:
        st = self.state.get(task.id)
        run_id = st.get("review_run")
        if not run_id:
            return False
        run = next((r for r in self.runs.runs_for(task.id) if r.run_id == run_id), None)
        if run is None:
            return self._queue_review_recovery(task, None, "review run record is missing", rep,
                                               started=True, count_round=False)
        if self._verdict_is_moot(task) or not self._review_evidence_is_current(task, run):
            return self._close_obsolete_review(task, run, rep)
        if run.status != "running":
            # The run record is already terminal. Usually a prior reap applied its verdict and
            # only the tick that would clear this pointer was lost — but if the process was
            # killed after the run's terminal save and before state.json recorded the verdict's
            # effect (`last_review_run` still points elsewhere), the verdict was never applied.
            # Re-apply it once from the stored result, without re-collecting or re-emitting
            # run_finished (both already happened before the crash); otherwise drop the pointer.
            # This is what lets a restart recover a review the old process reaped but never
            # persisted, instead of needing a fresh review (CG-198).
            if self._manual_reserved(task):
                return False
            if st.get("last_review_run") == run_id:
                st["review_run"] = ""
                return False
            pending_clarification = (run.env_snapshot or {}).get("clarification_pending")
            if isinstance(pending_clarification, list) and pending_clarification:
                return self._resume_review_clarification(
                    task, run, [str(entry) for entry in pending_clarification], rep)
            if not run.result:
                return self._queue_review_recovery(
                    task, run, run.error or run.status, rep,
                    started=self._review_execution_started(run),
                    count_round=bool((run.env_snapshot or {}).get("count_round", True)),
                )
            emitted = any(event.get("run") == run.run_id for event in self.events.read(
                task_id=task.id, kinds=["run_finished"]))
            return self._apply_review(task, run, run.result, rep, emitted=emitted)
        runner = self.runner_for(task, run.runner, run.harness)
        finished = run.process_finished() if self._manual_reserved(task) else self._finished_or_timed_out(run, runner)
        if not finished:
            return False
        review: dict[str, Any] = {}
        collected: dict[str, Any]
        if run.status == "timeout":
            if not self._review_execution_started(run):
                return self._queue_review_recovery(
                    task, run, run.error or "timed out", rep, started=False,
                    count_round=bool((run.env_snapshot or {}).get("count_round", True)),
                )
            collected = runner.collect(run)
        else:
            run.exit_code = run.read_exit_code()
            run.finished_at = now_iso()
            collected = runner.collect(run)
        run.usage = collected.get("usage") or {}
        run.cost_usd = collected.get("cost_usd")
        run.model = str(collected.get("model") or run.model)
        run.error = ((collected.get("error") or run.error) if run.status == "timeout"
                     else (collected.get("error") or ""))
        if collected.get("env_error"):
            # The reviewer's own account, not the PR: pause the harness, give back the
            # round this dispatch counted (see dispatch_review's count_round, snapshotted
            # on the run since an after-rebase round is exempt and must not be charged),
            # and route the continuation through the same bounded recovery policy as
            # other missing verdicts. The harness pause remains the admission gate, so
            # the queued retry cannot start until the environment can progress; its
            # successful probe supplies the delay, so no second recovery timer is needed.
            pending_triage = bool(st.pop("pending_triage_notify", False)) and task.status == Status.AWAITING_TRIAGE
            counted = bool((run.env_snapshot or {}).get("count_round", True))
            self._pause_for_env_error(run, collected)
            run.status = "env_error"
            run.save()
            self.events.emit("run_finished", task.id, run=run.run_id, mode="review", harness=run.harness,
                             model=run.model, pool_member=run.pool_member, status="env_error",
                             cost_usd=collected.get("cost_usd"), usage=collected.get("usage") or {})
            note = (f"automated review paused ({collected.get('env_kind') or 'quota'} limit hit on "
                   f"{run.harness or 'the harness'}); will retry once it resumes")
            if pending_triage:
                notify(self.cfg.data, task.id, "awaiting_triage", note, task.pr or "")
            rep.transitions.append(f"{task.id} review paused (env_error)")
            return self._queue_review_recovery(
                task, run, note, rep, started=True, count_round=counted,
                refund_round=True, backoff=False,
            )
        final = collected.get("final_text") or ""
        if final and not (run.path / "final.md").exists():
            (run.path / "final.md").write_text(final)
        review = enforce_criteria_verdict(parse_review(final))
        if run.status == "timeout" and not review:
            run.save()
            return self._queue_review_recovery(
                task, run, run.error or "timed out", rep, started=True,
                count_round=bool((run.env_snapshot or {}).get("count_round", True)),
            )
        if review:
            expansions = review.get("scope_expansions") if isinstance(review, dict) else None
            if isinstance(expansions, list) and not self._manual_reserved(task):
                for expansion in expansions:
                    if not isinstance(expansion, dict):
                        continue
                    item = str(expansion.get("item") or "").strip()
                    reason = str(expansion.get("reason") or "").strip()
                    if item and reason:
                        task.log(f"review validation scope expansion: {item} — {reason}")
                        self.store.save(task)
            metadata_warnings: list[str] = []
            expected = set((run.env_snapshot or {}).get("capture_pages") or [])
            seen = set(review.get("pages_seen") or [])
            missing = sorted(expected - seen)
            if review and missing:
                metadata_warnings.append("Optional UI captures not read for: " + ", ".join(missing))
            if review and not bool((run.env_snapshot or {}).get("validation_check_current")):
                metadata_warnings.append(
                    "Current-head pre-review check result was not available; reviewer attestation used"
                )
            unknown = list(((run.env_snapshot or {}).get("validation_plan") or {}).get("unknown_ui") or [])
            mappings = review.get("ui_scope") if isinstance(review.get("ui_scope"), list) else []
            mapped = {str(row.get("path") or "") for row in mappings if isinstance(row, dict)
                      and isinstance(row.get("consumers"), list) and row.get("consumers")}
            expanded = {str(row.get("item") or "") for row in (expansions or []) if isinstance(row, dict)
                        and str(row.get("reason") or "").strip()}
            unresolved = sorted(path for path in unknown if path not in mapped and path not in expanded)
            if review and unresolved:
                metadata_warnings.append(
                    "Optional UI scope mapping omitted for: " + ", ".join(unresolved)
                )
            frozen_criteria = (list((run.env_snapshot or {})["criteria"])
                               if "criteria" in (run.env_snapshot or {}) else None)
            affected_flow = str((run.env_snapshot or {}).get("affected_flow") or "")
            ambiguous = ambiguous_unverified(
                review, expected_criteria=frozen_criteria, affected_flow=affected_flow)
            if self._manual_reserved(task):
                run.result = review
                run.status = "done" if review else "failed"
                run.save()
                return True
            if ambiguous and not bool((run.env_snapshot or {}).get("clarify_unverified")):
                # Preserve the original report, but spend one reviewer continuation to
                # classify legacy prose. Persist the continuation on this result first so a
                # restart cannot apply the malformed verdict or launch two clarifications.
                run.result = review
                run.status = "done"
                run.env_snapshot["clarification_pending"] = ambiguous
                run.save()
                task.log("automated review clarification requested for ambiguous unverified observations")
                self.store.save(task)
                return self._resume_review_clarification(task, run, ambiguous, rep)
            if ambiguous:
                reason = ("reviewer clarification remained malformed or targeted requirements "
                          "outside the frozen criteria and declared affected flow")
                run.result = review
                run.status = "failed"
                run.error = reason
                run.save()
                st["review_run"] = ""
                self._set_needs_human(task, "review_clarification", reason,
                                      run=run.run_id, entries=ambiguous, owner="reviewer")
                task.log(reason + "; operator review is required and no author revision was queued")
                self.store.save(task)
                self.events.emit("run_finished", task.id, run=run.run_id, mode="review",
                                 cost_usd=run.cost_usd, usage=run.usage, status="failed")
                self.events.emit("needs_human", task.id, stop_kind="review_clarification",
                                 reason=reason, run=run.run_id)
                rep.transitions.append(f"{task.id} reviewer clarification needs operator attention")
                self.state.save()
                return True
            gaps = interaction_evidence_gaps(
                review, required=bool((run.env_snapshot or {}).get("interaction_required")),
                scalability=bool((run.env_snapshot or {}).get("scalability_required")),
                expected_head=str((run.env_snapshot or {}).get("review_head") or ""),
                replay_manifest=Path(str((run.env_snapshot or {}).get("interaction_replay_manifest") or "")),
                replay_nonce=str((run.env_snapshot or {}).get("interaction_replay_nonce") or ""),
                replay_digest=str((run.env_snapshot or {}).get("interaction_replay_digest") or ""),
                affected_flow=affected_flow,
                expected_criteria=frozen_criteria,
                metadata_warnings=metadata_warnings,
            ) if review else []
            if metadata_warnings:
                # Keep packaging diagnostics for operators without turning omitted
                # attachments or optional metadata into a posted review finding.
                run.env_snapshot["evidence_metadata_warnings"] = metadata_warnings
            if gaps:
                review["verdict"] = "request_changes"
                review.setdefault("findings", []).append({
                    "severity": "blocking", "file": "", "line": None,
                    "summary": "Verification contradicts the reviewed source or leaves an outcome unmet: " + "; ".join(gaps),
                    "fix": "Resolve the concrete contradiction or unmet outcome and verify it proportionately.",
                })
            run.result = review
            run.status = "done" if review else "failed"
            run.save()
        if self._manual_reserved(task):
            return True
        return self._apply_review(task, run, review, rep, emitted=False)

    def _review_evidence_is_current(self, task: Task, run: Run) -> bool:
        """Whether this review inspected the branch head that is still current."""
        reviewed = str((run.env_snapshot or {}).get("review_head") or "")
        if not reviewed:
            return False
        current = gitops.head_sha(self.worktree_for(task))
        return current == reviewed

    def _close_obsolete_review(self, task: Task, run: Run, rep: TickReport) -> bool:
        """Collect a completed review for accounting without applying an obsolete verdict."""
        st = self.state.get(task.id)
        if run.status == "running":
            runner = self.runner_for(task, run.runner, run.harness)
            if not self._finished_or_timed_out(run, runner):
                return False
            if run.status != "timeout":
                run.exit_code = run.read_exit_code()
                run.finished_at = now_iso()
                collected = runner.collect(run)
                run.usage = collected.get("usage") or {}
                run.cost_usd = collected.get("cost_usd")
                run.model = str(collected.get("model") or run.model)
                run.error = collected.get("error") or ""
                run.status = "done" if run.exit_code in (0, None) else "failed"
            self.events.emit("run_finished", task.id, run=run.run_id, mode="review",
                             cost_usd=run.cost_usd, usage=run.usage, status=run.status,
                             obsolete=True)
        st["review_run"] = ""
        st.pop("review_recovery", None)
        st.pop("pending_reviews", None)
        note = "review verdict discarded because the task or reviewed head moved on"
        run.error = f"{run.error} ({note})" if run.error else note
        run.save()
        self.state.save()
        self.log(f"{task.id}: review run {run.run_id} closed; {note}")
        rep.transitions.append(f"{task.id} review run {run.run_id} closed (obsolete)")
        return True

    def _review_comment_posted(self, slug: str, number: int, run_id: str) -> bool:
        """True if a comment carrying this run's marker (see `mark_garden_comment`) is already
        on the PR — the backstop for the narrow window `_apply_review` still leaves open (a kill
        between posting the comment and saving state.json): a genuinely-interrupted apply that
        gets replayed on restart still must not post the same review twice."""
        marker = f"run `{run_id}`"
        return any(marker in c for c in self.github.issue_comments(slug, number))

    def _apply_review(self, task: Task, run: Run, review: dict[str, Any], rep: TickReport, emitted: bool) -> bool:
        """Route a finished review run's verdict, then save state.json immediately — not just at
        the tick's end-of-pass save. Without this, a crash any time between a normal apply
        finishing (comment posted, task transitioned) and the tick's own save left `last_review_run`
        stale on disk; a restart then read that staleness as "never applied" and replayed the whole
        thing, posting a second GitHub comment and re-logging, re-transitioning and re-notifying for
        a verdict already fully handled. Saving here shrinks that window to the few lines below,
        the same residual risk already accepted elsewhere (e.g. finalize's own save-then-postprocess
        gap) — narrow enough that `_review_comment_posted` below is left as the backstop."""
        try:
            return self._apply_review_once(task, run, review, rep, emitted)
        finally:
            self.state.save()

    def _apply_review_once(self, task: Task, run: Run, review: dict[str, Any], rep: TickReport, emitted: bool) -> bool:
        """Route a finished review run's verdict: post the comment, apply a description rewrite,
        queue a revise round, or record the verdict. Split out of `reap_review` so a restart can
        re-apply a verdict the previous process reaped but never persisted (`emitted=True` then
        skips the run_finished emit, which the first pass already made)."""
        review = enforce_criteria_verdict(review)
        st = self.state.get(task.id)
        st.pop("review_recovery", None)
        st["review_run"] = ""
        pending_triage = bool(st.pop("pending_triage_notify", False)) and task.status == Status.AWAITING_TRIAGE
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        if not emitted:
            self.events.emit("run_finished", task.id, run=run.run_id, mode="review", harness=run.harness,
                             model=run.model, pool_member=run.pool_member, cost_usd=run.cost_usd, usage=run.usage,
                             status=str(review.get("verdict") or run.status))
        if not review:
            task.log(f"automated review produced no verdict ({run.error[:120] or run.status}){cost}")
            self.store.save(task)
            if pending_triage:
                notify(self.cfg.data, task.id, "awaiting_triage",
                      f"automated review produced no verdict ({run.error[:120] or run.status}){cost}", task.pr or "")
            rep.transitions.append(f"{task.id} review failed")
            return True
        review_head = str((run.env_snapshot or {}).get("review_head") or "")
        parts = remember_pending_feedback(st, review_head)
        st["pending_feedback_sources"] = {
            "head": review_head, "parts": parts,
            "rendered": str(st.get("pending_feedback") or "").strip(),
        }
        st["last_review"] = review
        st["last_review_run"] = run.run_id
        # Inbox ownership is tied to the exact revision an automated reviewer inspected.
        # Keep this separately from GitHub's latest head so a subsequent push cannot inherit
        # an old approval.
        st["last_review_head"] = str((run.env_snapshot or {}).get("review_head") or "")
        # A fresh review supersedes any approval head derived from an older review through
        # patch-identical mechanical rebases. Its immutable run/head become the new root.
        st.pop("derived_review_approval", None)
        st["last_review_base_head"] = str((run.env_snapshot or {}).get("review_base_head") or "")
        reviewed_diff = str((run.env_snapshot or {}).get("review_diff_hash") or "")
        if reviewed_diff:
            st["last_diff_hash"] = reviewed_diff
        verdict = str(review.get("verdict", ""))
        criteria_met, criteria_total = criteria_counts(review.get("criteria"))
        self.events.emit("review", task.id, run=run.run_id, verdict=verdict, summary=str(review.get("summary", "")),
                         blocking=sum(1 for f in review.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "blocking"),
                         description_ok=bool(review.get("description_ok", True)),
                         criteria_met=criteria_met, criteria_total=criteria_total)
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available:
            try:
                if not self._review_comment_posted(slug, number, run.run_id):
                    comment_body = mark_garden_comment(review_to_markdown(review), run.run_id)
                    self.github.comment(slug, number, comment_body)
            except GitHubError as e:
                self.log(f"{task.id}: could not post review: {e}")
        # A missing ``fix`` field is presentation metadata. The concrete blocking summary
        # still reaches the author; do not spend a reviewer-only round to repackage it.
        st.pop("review_fix_reasked", None)
        # repeated blocking findings across rounds = the loop isn't converging
        keys = sorted({f"{f.get('file', '')}|{str(f.get('summary', '')).strip().lower()}"
                       for f in review.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "blocking"})
        repeated = sorted(set(keys) & set(st.get("last_findings", [])))
        st["last_findings"] = keys
        reconciliation = st.get("no_change_reconciliation")
        if isinstance(reconciliation, dict):
            reconciled_head = str(reconciliation.get("head") or "")
            current_head = str(st.get("head_sha") or "")
            if not reconciled_head or not current_head or reconciled_head == current_head:
                st.pop("no_change_reconciliation", None)
        if verdict == "approve":
            merge_pending_feedback(st, review_head, "review", "")
            if (task.status == Status.CHANGES_REQUESTED and not st.get("pending_feedback")
                    and not st.get("needs_human")):
                self._transition(task, Status.IN_REVIEW, "current review resolved the pending review findings")
        if task.status in (Status.IN_REVIEW, Status.AWAITING_TRIAGE, Status.CHANGES_REQUESTED):
            # CI can already have queued this revision while its review was still running.
            already_queued = task.status == Status.CHANGES_REQUESTED
            # Only the description is wrong (no blocking finding) and the reviewer supplied the
            # corrected body: apply it directly instead of spending a revise round on wording.
            # This applies whether the code itself was approved or sent back.
            rewrite = str(review.get("description_rewrite") or "").strip()
            description_only = review_is_description_only(review)
            if description_only and rewrite:
                self._apply_description_rewrite(task, run, rewrite, rep, cost)
                if pending_triage:
                    notify(self.cfg.data, task.id, "awaiting_triage",
                          f"automated review: {verdict} (description rewritten){cost}", task.pr or "")
                return True
            if verdict == "request_changes":
                fb = feedback_from_review(
                    review, run_id=run.run_id,
                    source_head=str(run.env_snapshot.get("review_head") or ""),
                )
                changed = self._criteria_changed_note(task, run)
                if changed:
                    fb = (fb + "\n\n" + changed).strip()
                if fb:
                    st.setdefault("review_feedback_history", []).append(fb)
                    merge_pending_feedback(st, str(run.env_snapshot.get("review_head") or ""), "review", fb)
                    st["pending_feedback_easy"] = review_is_description_only(review) and not already_queued
                    st.pop("pending_feedback_rebase", None)
                    st.pop("review_fix_reasked", None)
                    if repeated and bool(self.cfg.get("stall.enabled", True)):
                        self._stall(task, rep, f"review finding repeated after a revise round: {repeated[0].split('|')[1][:80]}")
                        return True
                    manual_handoff = not bool(self.effective("auto_revise", True, task.product))
                    if manual_handoff and not st.get("needs_human"):
                        self._set_needs_human(task, "manual_revision", "automatic revisions are disabled; full feedback is ready for manual handoff")
                    if already_queued and (not manual_handoff or st.get("needs_human")):
                        return True
                    self._transition(task, Status.CHANGES_REQUESTED,
                                     f"automated review requested changes: {review.get('summary', '')}{cost}",
                                     needs_human=manual_handoff)
                    rep.transitions.append(f"{task.id} -> changes_requested (review)")
                    return True
            elif verdict == "approve" and description_only:
                # Approved, but the description still needs work and the reviewer gave no
                # rewrite to apply directly: dispatch a description-only revise round rather
                # than leaving the flagged description sitting on an in_review task forever.
                fb = feedback_from_review(
                    review, run_id=run.run_id,
                    source_head=str(run.env_snapshot.get("review_head") or ""),
                )
                changed = self._criteria_changed_note(task, run)
                if changed:
                    fb = (fb + "\n\n" + changed).strip()
                if fb:
                    merge_pending_feedback(st, str(run.env_snapshot.get("review_head") or ""), "review", fb)
                    st["pending_feedback_easy"] = not already_queued
                    st.pop("pending_feedback_rebase", None)
                    manual_handoff = not bool(self.effective("auto_revise", True, task.product))
                    if manual_handoff and not st.get("needs_human"):
                        self._set_needs_human(task, "manual_revision", "automatic revisions are disabled; full feedback is ready for manual handoff")
                    if already_queued and (not manual_handoff or st.get("needs_human")):
                        return True
                    self._transition(task, Status.CHANGES_REQUESTED,
                                      f"automated review approved but flagged the description: {review.get('description_feedback', '') or review.get('summary', '')}{cost}",
                                      needs_human=manual_handoff)
                    rep.transitions.append(f"{task.id} -> changes_requested (description round)")
                    return True
        task.log(f"automated review: {verdict} — {review.get('summary', '')}{cost}")
        self.store.save(task)
        if pending_triage:
            notify(self.cfg.data, task.id, "awaiting_triage",
                  f"automated review: {verdict} — {review.get('summary', '')}{cost}", task.pr or "")
        rep.transitions.append(f"{task.id} review: {verdict}")
        return True

    @staticmethod
    def _blocking_findings_without_fix(review: dict[str, Any]) -> list[dict[str, Any]]:
        """Blocking findings need actionable advice; older reviewers can omit new fields."""
        return [finding for finding in review.get("findings") or []
                if isinstance(finding, dict) and finding.get("severity") == "blocking"
                and not str(finding.get("fix") or "").strip()]
    def _criteria_changed_note(self, task: Task, review_run: Run) -> str:
        """Add task edits made after dispatch to the next revise brief."""
        snapshot = review_run.env_snapshot or {}
        if "criteria" not in snapshot:
            return ""
        frozen = list(snapshot.get("criteria") or [])
        current = parse_criteria(task.body)
        if frozen == current:
            return ""
        added = [item for item in current if item not in frozen]
        removed = [item for item in frozen if item not in current]
        lines = ["### Criteria changed after dispatch", "", "The review judged the frozen criteria in your prior brief. The task was edited while you worked; address this delta now:"]
        lines += [f"- Added: {item}" for item in added]
        lines += [f"- Removed: {item}" for item in removed]
        return "\n".join(lines)

    def _apply_description_rewrite(self, task: Task, run: Run, rewrite: str, rep: TickReport, cost: str) -> None:
        """The reviewer found nothing blocking but the description, and returned the corrected
        body: update the PR through the GitHub API and stay in review. No revise round runs."""
        slug = self.slug_for(task)
        number = self._pr_number(task)
        applied = False
        if slug and number and self.github.available:
            try:
                self.github.update_pr(slug, number, body=rewrite)
                applied = True
            except GitHubError as e:
                self.log(f"{task.id}: could not apply the reviewer's description rewrite: {e}")
        self.events.emit("description_rewritten", task.id, run=run.run_id, applied=applied)
        note = "description rewritten by the reviewer" + ("" if applied else " (GitHub update failed)")
        task.log(f"{note}{cost}")
        self.store.save(task)
        self.log(f"{task.id}: {note}")
        rep.transitions.append(f"{task.id} {note}")

    def _verdict_is_moot(self, task: Task | None) -> bool:
        """True when a verdict-bearing run (review/persona/compare) can no longer be
        applied to its task: the task is gone, has reached a terminal status (done,
        cancelled, wont_do) or failed, or its PR is closed or merged. A task that is
        still running, changes_requested, in_review (or awaiting a human/triage) can
        still receive the verdict, so its finished run is reaped by the normal path —
        never swept."""
        if task is None:
            return True
        if task.status.terminal or task.status == Status.FAILED:
            return True
        pr_state = str(self.state.get(task.id).get("pr_state") or "").upper()
        return pr_state in ("CLOSED", "MERGED")

    def reap_orphaned(self, rep: TickReport) -> None:
        """Close a moot verdict run, or a terminal task's pid-less ghost record.

        Verdict runs are moot once their task has moved on. Worker-mode records otherwise
        remain their task's reaper's responsibility, except a terminal task cannot have a
        live pid-less worker that was launched into a worktree; that record has no process
        which could ever report an outcome. A reservation not yet bound to a worktree stays
        active, because the dispatcher may still be completing its launch transaction.
        Usage and cost are recorded; nothing is posted, since the task is no longer where the
        run left it.
        """
        aux_run_ids = {entry["run_id"] for entry in self._aux_list()}
        tasks = self.store.tasks()
        for run in self.runs.active():
            task = tasks.get(run.task_id)
            # A terminal task cannot own an active pid-less record.  This is distinct from a
            # live worker which happens to have no verdict yet: without a pid there is no
            # process to reap, so leaving the record active permanently consumes a slot.
            ghost = bool(run.runner != "remote" and task and task.status.terminal and run.worktree and run.pid is None
                          and not run.process_finished())
            if run.runner == "manual":
                continue
            if not ghost and run.run_id in aux_run_ids:
                continue
            if not ghost and run.mode not in ("review", "persona", "compare"):
                continue
            if not ghost and not self._verdict_is_moot(task):
                continue
            runner = self.runner_for(task or Task(path=self.store.root, id=run.task_id, title=""), run.runner, run.harness)
            if not ghost and not self._finished_or_timed_out(run, runner):
                continue
            if ghost:
                run.finished_at = now_iso()
                run.status = "failed"
            elif run.status != "timeout":
                run.exit_code = run.read_exit_code()
                run.finished_at = now_iso()
                collected = runner.collect(run)
                run.usage = collected.get("usage") or {}
                run.cost_usd = collected.get("cost_usd")
                run.model = str(collected.get("model") or run.model)
                run.error = collected.get("error") or ""
                final = collected.get("final_text") or ""
                if final and not (run.path / "final.md").exists():
                    (run.path / "final.md").write_text(final)
                run.status = "done" if run.exit_code in (0, None) else "failed"
            note = "closed by orphan sweep: task moved on before this run's verdict was read"
            run.error = f"{run.error} ({note})" if run.error else note
            run.save()
            self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode, harness=run.harness,
                             model=run.model, pool_member=run.pool_member, cost_usd=run.cost_usd,
                             usage=run.usage, status=run.status, orphaned=True)
            self.log(f"{run.task_id}: {run.mode} run {run.run_id} closed ({run.status}); {note}")
            rep.transitions.append(f"{run.task_id} {run.mode} run {run.run_id} closed (orphaned)")
