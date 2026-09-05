"""The automated review round: dispatch, reap the verdict, route it; and the orphan sweep for verdict runs."""

from __future__ import annotations

from typing import Any

from .. import gitops
from ..github import GitHubError, mark_garden_comment
from ..harness import DIFFICULTIES
from ..model import Status, Task, ensure_open, now_iso
from ..notify import notify
from ..review import (
    feedback_from_review,
    parse_review,
    review_brief,
    review_is_description_only,
    review_to_markdown,
)
from ..runs import Run
from .report import TickReport


class ReviewMixin:
    # ---- automated review --------------------------------------------------
    def _review_round_pending(self, st: dict[str, Any]) -> bool:
        """True when `_maybe_review` will still dispatch (or queue) an automated review round
        for this push. A fresh draft PR's triage ping waits for that verdict instead of firing
        on PR-open, per the phase-02 retro (triage pings fired before the review verdict was
        known); when review is off or its rounds are already spent, there is no verdict coming
        and the ping fires right away."""
        if not bool(self.cfg.get("review.enabled", True)):
            return False
        return int(st.get("review_rounds", 0)) < int(self.cfg.get("review.max_rounds", 2))

    def _maybe_review(self, task: Task, work_run: Run, rep: TickReport) -> None:
        if not task.pr:
            return
        st = self.state.get(task.id)
        # A review that follows a conflict rebase (or a stale-base rebase, CG-131) re-reads
        # code the reviewer already approved: it runs, but must not count toward review.max_rounds.
        after_rebase = bool(st.pop("last_round_rebase", False))
        wanted: list[dict[str, Any]] = []
        if bool(self.cfg.get("review.enabled", True)):
            max_rounds = int(self.cfg.get("review.max_rounds", 2))
            if int(st.get("review_rounds", 0)) < max_rounds:
                wanted.append({"kind": "review", "count_round": not after_rebase})
            else:
                reason = f"{max_rounds} automated review round(s) used; this PR is yours"
                self._set_needs_human(task, "review_cap", reason)
                self.events.emit("needs_human", task.id, stop_kind="review_cap", reason=reason)
                task.log(f"{reason} — run `garden review {task.id}` for one more round, or review on GitHub")
                self.store.save(task)
                notify(self.cfg.data, task.id, "needs_human", reason, task.pr or "")
                rep.transitions.append(f"{task.id} review cap reached")
        for name in list(self.cfg.get("review.personas", []) or []):
            wanted.append({"kind": "persona", "name": str(name)})
        self._dispatch_or_defer_reviews(task, wanted, rep, work_run=work_run)

    def _dispatch_or_defer_reviews(self, task: Task, wanted: list[dict[str, Any]], rep: TickReport,
                                   work_run: Run | None = None) -> None:
        """Start each wanted review/persona run if a `review_parallel` slot is free; anything
        left over is queued in state (`pending_reviews`) and picked up by `_drain_pending_reviews`
        on a later tick, so a full review_parallel does not lose the round — it just waits its
        turn, the same way a full max_parallel makes a work task wait in the ready queue."""
        st = self.state.get(task.id)
        deferred: list[dict[str, Any]] = []
        for item in wanted:
            if self.review_slots_free() <= 0:
                deferred.append(item)
                continue
            kind = item["kind"]
            try:
                if kind == "review":
                    run = self.dispatch_review(task, work_run, count_round=bool(item.get("count_round", True)))
                    rep.dispatched.append(f"{task.id}(review)")
                    self.log(f"{task.id}: review run {run.run_id} started")
                else:
                    self.dispatch_persona_pr(task, item["name"])
                    rep.dispatched.append(f"{task.id}(persona:{item['name']})")
            except Exception as e:  # noqa: BLE001
                task.log(f"automated {kind} could not start: {e}")
                self.store.save(task)
                rep.errors.append(f"{task.id}: {kind} dispatch failed: {e}")
        if deferred:
            st["pending_reviews"] = list(st.get("pending_reviews") or []) + deferred

    def _drain_pending_reviews(self, tasks: dict[str, Task], rep: TickReport) -> None:
        for task in tasks.values():
            if self.review_slots_free() <= 0:
                break
            st = self.state.get(task.id)
            pending = list(st.get("pending_reviews") or [])
            if not pending:
                continue
            st["pending_reviews"] = []
            self._dispatch_or_defer_reviews(task, pending, rep)

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
            except Exception as e:  # noqa: BLE001
                run.error = str(e)
        run.finished_at = now_iso()
        run.status = "superseded"
        note = "superseded by a newer review dispatch for the same task"
        run.error = f"{run.error} ({note})" if run.error else note
        run.save()
        self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, status="superseded",
                         cost_usd=run.cost_usd, usage=run.usage)
        self.log(f"{task.id}: review run {run.run_id} superseded by a new review dispatch")

    def dispatch_review(self, task: Task, work_run: Run | None = None, count_round: bool = True) -> Run:
        ensure_open(task)
        self._supersede_running_review(task)
        harness_name = str(self.cfg.get("review.harness") or "")
        runner = self.runner_for(task, "local", harness_name)
        base = self.base_for(task)
        branch = task.branch or task.default_branch()
        wt = gitops.prepare_worktree(self.repo_for(task), self.worktree_for(task), branch, base)
        diff = gitops.diff(wt, base)
        pr_title, pr_body, pr_comment = task.title, "", ""
        if work_run is not None:
            pr_title = str(work_run.result.get("pr_title") or task.title)
            pr_body = str(work_run.result.get("pr_body") or "")
            pr_comment = str(work_run.result.get("pr_comment") or "")
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available and not pr_body:
            try:
                info = self.github.get_pr(slug, number)
                pr_title, pr_body = info.title or pr_title, info.body
            except GitHubError:
                pass
        text = review_brief(self.store, task, branch=branch, base=base, pr_title=pr_title, pr_body=pr_body,
                            diff=diff, max_diff_chars=int(self.cfg.get("review.max_diff_chars", 60000)),
                            pr_comment=pr_comment)
        run = self.runs.new_run(task.id, runner.name, mode="review")
        run.branch, run.base, run.worktree = branch, base, str(wt)
        review_difficulty = str(self.cfg.get("review.difficulty") or task.difficulty or "medium")
        if review_difficulty not in DIFFICULTIES:
            review_difficulty = "medium"
        run.difficulty = review_difficulty
        run.model = self.model_for(task, runner, review_difficulty)
        if runner.harness and runner.harness.cfg.get("review_model"):
            run.model = str(runner.harness.cfg["review_model"])
        run.brief_tokens = max(1, len(text) // 4)
        run.save()
        runner.start(run, wt, text)
        st = self.state.get(task.id)
        st["review_run"] = run.run_id
        if count_round:
            st["review_rounds"] = int(st.get("review_rounds", 0)) + 1
        self.events.emit("dispatch", task.id, run=run.run_id, mode="review", model=run.model, harness=run.harness)
        self.state.save()
        return run

    def review_again(self, task: Task) -> Run:
        """The person asked for one more automated review after the cap stopped it: raise
        this task's review cap by one round, clear the stop, and dispatch immediately."""
        ensure_open(task)
        if not task.pr:
            raise RuntimeError(f"{task.id} has no PR to review")
        st = self.state.get(task.id)
        self._grant_one_more_review_round(st)
        st.pop("needs_human", None)
        return self.dispatch_review(task)

    def reap_review(self, task: Task, rep: TickReport) -> bool:
        st = self.state.get(task.id)
        run_id = st.get("review_run")
        if not run_id:
            return False
        run = next((r for r in self.runs.runs_for(task.id) if r.run_id == run_id), None)
        if run is None or run.status != "running":
            st["review_run"] = ""
            return False
        runner = self.runner_for(task, run.runner, run.harness)
        if not self._finished_or_timed_out(run, runner):
            return False
        st["review_run"] = ""
        pending_triage = bool(st.pop("pending_triage_notify", False)) and task.status == Status.AWAITING_TRIAGE
        review: dict[str, Any] = {}
        if run.status != "timeout":
            run.exit_code = run.read_exit_code()
            run.finished_at = now_iso()
            collected = runner.collect(run)
            run.usage = collected.get("usage") or {}
            run.cost_usd = collected.get("cost_usd")
            run.error = collected.get("error") or ""
            final = collected.get("final_text") or ""
            if final and not (run.path / "final.md").exists():
                (run.path / "final.md").write_text(final)
            review = parse_review(final)
            run.result = review
            run.status = "done" if review else "failed"
            run.save()
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        self.events.emit("run_finished", task.id, run=run.run_id, mode="review", cost_usd=run.cost_usd, usage=run.usage,
                         status=str(review.get("verdict") or run.status))
        if not review:
            task.log(f"automated review produced no verdict ({run.error[:120] or run.status}){cost}")
            self.store.save(task)
            if pending_triage:
                notify(self.cfg.data, task.id, "awaiting_triage",
                      f"automated review produced no verdict ({run.error[:120] or run.status}){cost}", task.pr or "")
            rep.transitions.append(f"{task.id} review failed")
            return True
        st["last_review"] = review
        st["last_review_run"] = run.run_id
        verdict = str(review.get("verdict", ""))
        self.events.emit("review", task.id, run=run.run_id, verdict=verdict, summary=str(review.get("summary", "")),
                         blocking=sum(1 for f in review.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "blocking"),
                         description_ok=bool(review.get("description_ok", True)))
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available:
            try:
                comment_body = mark_garden_comment(review_to_markdown(review), run.run_id)
                self.github.comment(slug, number, comment_body)
            except GitHubError as e:
                self.log(f"{task.id}: could not post review: {e}")
        # repeated blocking findings across rounds = the loop isn't converging
        keys = sorted({f"{f.get('file', '')}|{str(f.get('summary', '')).strip().lower()}"
                       for f in review.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "blocking"})
        repeated = sorted(set(keys) & set(st.get("last_findings", [])))
        st["last_findings"] = keys
        if task.status in (Status.IN_REVIEW, Status.AWAITING_TRIAGE):
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
                if repeated and bool(self.cfg.get("stall.enabled", True)):
                    self._stall(task, rep, f"review finding repeated after a revise round: {repeated[0].split('|')[1][:80]}")
                    return True
                fb = feedback_from_review(review)
                if fb and bool(self.cfg.get("auto_revise", True)):
                    st["pending_feedback"] = fb
                    st["pending_feedback_easy"] = review_is_description_only(review)
                    st.pop("pending_feedback_rebase", None)
                    self._transition(task, Status.CHANGES_REQUESTED, f"automated review requested changes: {review.get('summary', '')}{cost}")
                    rep.transitions.append(f"{task.id} -> changes_requested (review)")
                    return True
            elif verdict == "approve" and description_only:
                # Approved, but the description still needs work and the reviewer gave no
                # rewrite to apply directly: dispatch a description-only revise round rather
                # than leaving the flagged description sitting on an in_review task forever.
                fb = feedback_from_review(review)
                if fb and bool(self.cfg.get("auto_revise", True)):
                    st["pending_feedback"] = fb
                    st["pending_feedback_easy"] = True
                    st.pop("pending_feedback_rebase", None)
                    self._transition(task, Status.CHANGES_REQUESTED,
                                      f"automated review approved but flagged the description: {review.get('description_feedback', '') or review.get('summary', '')}{cost}")
                    rep.transitions.append(f"{task.id} -> changes_requested (description round)")
                    return True
        task.log(f"automated review: {verdict} — {review.get('summary', '')}{cost}")
        self.store.save(task)
        if pending_triage:
            notify(self.cfg.data, task.id, "awaiting_triage",
                  f"automated review: {verdict} — {review.get('summary', '')}{cost}", task.pr or "")
        rep.transitions.append(f"{task.id} review: {verdict}")
        return True

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
        """Close a verdict-bearing run (review, persona, compare) still marked `running`
        whose task has moved on before the tick that would have read its verdict: merged,
        closed, failed or otherwise past the point where the verdict can be applied, so
        `state[task].review_run` (or the aux pointer) no longer leads a reap to it. Only
        these modes are swept — a task's own work/revise/resume/trial run is always reaped
        by its task, so one that merely finishes between its task's reap and this sweep in
        the same tick (the CG-098 case) is left for the next tick's reap, not swept out from
        under it. Usage and cost are recorded; nothing is posted, since the task is no longer
        where this run left it."""
        aux_run_ids = {e["run_id"] for e in self._aux_list()}
        tasks = self.store.tasks()
        for run in self.runs.active():
            if run.runner == "manual" or run.run_id in aux_run_ids:
                continue
            if run.mode not in ("review", "persona", "compare"):
                continue
            task = tasks.get(run.task_id)
            if not self._verdict_is_moot(task):
                continue
            runner = self.runner_for(task or Task(path=self.store.root, id=run.task_id, title=""), run.runner, run.harness)
            if not self._finished_or_timed_out(run, runner):
                continue
            if run.status != "timeout":
                run.exit_code = run.read_exit_code()
                run.finished_at = now_iso()
                collected = runner.collect(run)
                run.usage = collected.get("usage") or {}
                run.cost_usd = collected.get("cost_usd")
                run.error = collected.get("error") or ""
                final = collected.get("final_text") or ""
                if final and not (run.path / "final.md").exists():
                    (run.path / "final.md").write_text(final)
                run.status = "done" if run.exit_code in (0, None) else "failed"
            note = "closed by orphan sweep: task moved on before this run's verdict was read"
            run.error = f"{run.error} ({note})" if run.error else note
            run.save()
            self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode, cost_usd=run.cost_usd,
                             usage=run.usage, status=run.status, orphaned=True)
            self.log(f"{run.task_id}: {run.mode} run {run.run_id} closed ({run.status}); {note}")
            rep.transitions.append(f"{run.task_id} {run.mode} run {run.run_id} closed (orphaned)")
