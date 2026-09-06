"""The automated review round: dispatch, reap the verdict, route it; and the orphan sweep for verdict runs."""

from __future__ import annotations

from typing import Any

from .. import gitops
from ..criteria import criteria_counts, required_evidence
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
        requirements = required_evidence(task.body, task.extra.get("requires"))
        evidence = st.setdefault("required_evidence", {})
        for item in requirements:
            evidence.setdefault(f"{item['kind']}:{item['name']}", "queued")
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
        required_personas = [item["name"] for item in requirements if item["kind"] == "persona"]
        for name in dict.fromkeys([*required_personas, *(str(n) for n in list(self.cfg.get("review.personas", []) or []))]):
            if name in required_personas and evidence.get(f"persona:{name}") in ("running", "posted"):
                continue  # required evidence is produced when this PR opens, not once per review round
            wanted.append({"kind": "persona", "name": name, "required": name in required_personas})
        self._dispatch_or_defer_reviews(task, wanted, rep, work_run=work_run)

    def _dispatch_or_defer_reviews(self, task: Task, wanted: list[dict[str, Any]], rep: TickReport,
                                   work_run: Run | None = None) -> None:
        """Start each wanted review/persona run if a `review_parallel` slot is free; anything
        left over is queued in state (`pending_reviews`) and picked up by `_drain_pending_reviews`
        on a later tick, so a full review_parallel does not lose the round — it just waits its
        turn, the same way a full max_parallel makes a work task wait in the ready queue."""
        st = self.state.get(task.id)
        required_personas = {item["name"] for item in required_evidence(task.body, task.extra.get("requires"))
                             if item["kind"] == "persona"}
        evidence = st.setdefault("required_evidence", {})
        # Never dispatch a review under a worker round still in flight (work/revise/resume/rebase):
        # its record would sit beside the worker run and could be mistaken for the task's own run,
        # sending a running task back to ready (CG-177). Defer the whole batch — `_drain_pending_reviews`
        # picks it up once the worker finishes — and log it once per deferral episode.
        if self._worker_holding_reviews(task) is not None:
            self._queue_pending_reviews(st, wanted)
            if not st.get("reviews_deferred_for_worker"):
                st["reviews_deferred_for_worker"] = True
                self.log(f"{task.id}: review deferred while a worker run is in flight")
            return
        st.pop("reviews_deferred_for_worker", None)
        deferred: list[dict[str, Any]] = []
        for item in wanted:
            if item["kind"] == "review" and any(evidence.get(f"persona:{name}") != "posted" for name in required_personas):
                deferred.append(item)
                continue
            if self.review_slots_free() <= 0:
                deferred.append(item)
                continue
            harness_name = (self._review_route(task, work_run)[0] if item["kind"] == "review"
                            else self.resolved_harness_name(task, str(self.cfg.get("review.harness") or "")))
            if self.is_harness_paused(harness_name):
                deferred.append(item)
                continue
            kind = item["kind"]
            try:
                if kind == "review":
                    run = self.dispatch_review(task, work_run, count_round=bool(item.get("count_round", True)))
                    rep.dispatched.append(f"{task.id}(review)")
                    self.log(f"{task.id}: review run {run.run_id} started")
                else:
                    self.dispatch_persona_pr(task, item["name"], required_evidence=bool(item.get("required")))
                    if item.get("required"):
                        evidence[f"persona:{item['name']}"] = "running"
                    rep.dispatched.append(f"{task.id}(persona:{item['name']})")
            except Exception as e:  # noqa: BLE001
                task.log(f"automated {kind} could not start: {e}")
                self.store.save(task)
                rep.errors.append(f"{task.id}: {kind} dispatch failed: {e}")
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
            if run.no_process:
                return "worker", f"its {run.mode} record has no process; the tick that reaps it starts the review"
            return "worker", f"waits for its {run.mode} run to finish"
        harness = self.resolved_harness_name(task, str(self.cfg.get("review.harness") or ""))
        if self.is_harness_paused(harness):
            return "harness", f"{harness} harness paused"
        if self.review_slots_free() <= 0:
            return "slots", f"no review slot ({len(self.review_runs_active())} of {self.review_parallel_limit()} busy)"
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

    def _drain_pending_reviews(self, tasks: dict[str, Task], rep: TickReport) -> None:
        for task in tasks.values():
            if self.review_slots_free() <= 0:
                break
            st = self.state.get(task.id)
            if st.get("needs_human"):
                continue
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
                run.model = str(collected.get("model") or run.model)
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

    def dispatch_review(self, task: Task, work_run: Run | None = None, count_round: bool = True,
                        reask_missing_fixes: bool = False) -> Run:
        ensure_open(task)
        harness_name, ladder_model, writer = self._review_route(task, work_run)
        runner = self.runner_for(task, "local", harness_name)
        self._admit_local_launch("review")
        self._raise_if_harness_paused(runner.harness.name if runner.harness else "")
        self._supersede_running_review(task)
        base = self.base_for(task)
        branch = task.branch or task.default_branch()
        wt = gitops.prepare_worktree(self.repo_for(task), self.worktree_for(task), branch, base)
        diff = gitops.diff(wt, base)
        pr_title, pr_body, pr_comment, verified = task.title, "", "", None
        if work_run is not None:
            pr_title = str(work_run.result.get("pr_title") or task.title)
            pr_body = str(work_run.result.get("pr_body") or "")
            pr_comment = str(work_run.result.get("pr_comment") or "")
            verified = work_run.result.get("verified")
        if verified is None:
            verified = self._last_worker_verified(task)
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available and not pr_body:
            try:
                info = self.github.get_pr(slug, number)
                pr_title, pr_body = info.title or pr_title, info.body
            except GitHubError:
                pass
        capture_paths: list[str] = []
        capture_pages: list[str] = []
        check_results: list[dict[str, Any]] = []
        for check_run in reversed(self.runs.runs_for(task.id)):
            if check_run.mode != "check":
                continue
            if not check_results:
                check_results = list((check_run.result or {}).get("checks", []))
            ui_results = [result for result in (check_run.result or {}).get("checks", [])
                          if result.get("name") == "ui"]
            capture_paths = [str(p) for result in ui_results for p in result.get("captures", [])
                             if str(p).endswith(".png")]
            capture_pages = [str(page) for result in ui_results for page in result.get("pages", [])]
            if capture_paths:
                break
        text = review_brief(self.store, task, branch=branch, base=base, pr_title=pr_title, pr_body=pr_body,
                            diff=diff, max_diff_chars=int(self.cfg.get("review.max_diff_chars", 60000)),
                            pr_comment=pr_comment, verified=verified, captures=capture_paths,
                            checks=check_results, reask_missing_fixes=reask_missing_fixes)
        run = self.runs.new_run(task.id, runner.name, mode="review")
        run.branch, run.base, run.worktree = branch, base, str(wt)
        # Remembered so a quota env_error on this run (reap_review, below) knows whether this
        # dispatch actually counted a round — an after-rebase round is exempt from
        # review.max_rounds and must not be charged for having been retried.
        run.env_snapshot = {"count_round": count_round, "capture_pages": sorted(set(capture_pages)),
                            "reask_missing_fixes": reask_missing_fixes}
        review_difficulty = str(self.effective("review.difficulty") or task.difficulty or "medium")
        if review_difficulty not in DIFFICULTIES:
            review_difficulty = "medium"
        run.difficulty = review_difficulty
        run.model = self.model_for(task, runner, review_difficulty)
        if ladder_model:
            run.model = ladder_model
        elif runner.harness and runner.harness.cfg.get("review_model"):
            run.model = str(runner.harness.cfg["review_model"])
        if ladder_model and writer:
            run.env_snapshot.update({"writer_harness": writer.harness, "writer_model": writer.model,
                                     "review_rung": f"{runner.harness.name if runner.harness else harness_name}:{run.model}"})
        run.brief_tokens = max(1, len(text) // 4)
        run.save()
        runner.start(run, wt, text)
        st = self.state.get(task.id)
        st["review_run"] = run.run_id
        if count_round:
            st["review_rounds"] = int(st.get("review_rounds", 0)) + 1
        if ladder_model and writer:
            task.log(f"reviewed by {run.model}, one above {writer.model}")
            self.store.save(task)
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
        if run is None:
            st["review_run"] = ""
            return False
        if run.status != "running":
            # The run record is already terminal. Usually a prior reap applied its verdict and
            # only the tick that would clear this pointer was lost — but if the process was
            # killed after the run's terminal save and before state.json recorded the verdict's
            # effect (`last_review_run` still points elsewhere), the verdict was never applied.
            # Re-apply it once from the stored result, without re-collecting or re-emitting
            # run_finished (both already happened before the crash); otherwise drop the pointer.
            # This is what lets a restart recover a review the old process reaped but never
            # persisted, instead of needing a fresh review (CG-198).
            if st.get("last_review_run") == run_id or not run.result:
                st["review_run"] = ""
                return False
            return self._apply_review(task, run, run.result, rep, emitted=True)
        runner = self.runner_for(task, run.runner, run.harness)
        if not self._finished_or_timed_out(run, runner):
            return False
        review: dict[str, Any] = {}
        if run.status != "timeout":
            run.exit_code = run.read_exit_code()
            run.finished_at = now_iso()
            collected = runner.collect(run)
            if collected.get("env_error"):
                # The reviewer's own account, not the PR: pause the harness, give back the
                # round this dispatch counted (see dispatch_review's count_round, snapshotted
                # on the run since an after-rebase round is exempt and must not be charged),
                # and rejoin the review queue so it is retried once the harness resumes
                # instead of the PR silently never getting a verdict for this round.
                st["review_run"] = ""
                pending_triage = bool(st.pop("pending_triage_notify", False)) and task.status == Status.AWAITING_TRIAGE
                counted = bool((run.env_snapshot or {}).get("count_round", True))
                self._pause_for_env_error(run, collected)
                run.status = "env_error"
                run.save()
                self.events.emit("run_finished", task.id, run=run.run_id, mode="review", status="env_error",
                                 cost_usd=collected.get("cost_usd"), usage=collected.get("usage") or {})
                if counted:
                    st["review_rounds"] = max(0, int(st.get("review_rounds", 0)) - 1)
                self._queue_pending_reviews(st, [{"kind": "review", "count_round": counted}])
                note = (f"automated review paused ({collected.get('env_kind') or 'quota'} limit hit on "
                       f"{run.harness or 'the harness'}); will retry once it resumes")
                task.log(note)
                self.store.save(task)
                if pending_triage:
                    notify(self.cfg.data, task.id, "awaiting_triage", note, task.pr or "")
                rep.transitions.append(f"{task.id} review paused (env_error)")
                return True
            run.usage = collected.get("usage") or {}
            run.cost_usd = collected.get("cost_usd")
            run.model = str(collected.get("model") or run.model)
            run.error = collected.get("error") or ""
            final = collected.get("final_text") or ""
            if final and not (run.path / "final.md").exists():
                (run.path / "final.md").write_text(final)
            review = parse_review(final)
            expected = set((run.env_snapshot or {}).get("capture_pages") or [])
            seen = set(review.get("pages_seen") or [])
            missing = sorted(expected - seen)
            if review and missing:
                review["verdict"] = "request_changes"
                review.setdefault("findings", []).append({"severity": "blocking", "file": "", "line": None,
                                                          "summary": "UI captures not read for: " + ", ".join(missing)})
            run.result = review
            run.status = "done" if review else "failed"
            run.save()
        return self._apply_review(task, run, review, rep, emitted=False)

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
        st = self.state.get(task.id)
        st["review_run"] = ""
        pending_triage = bool(st.pop("pending_triage_notify", False)) and task.status == Status.AWAITING_TRIAGE
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        if not emitted:
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
        missing_fixes = self._blocking_findings_without_fix(review)
        # A replay restores a verdict whose worker run had already been reaped before the
        # scheduler crashed. Older reviews legitimately lack `fix`, and recovery must put
        # their original changes_requested state back rather than replacing it with a new
        # review round. Freshly reaped reviews still get the one actionable re-ask.
        if missing_fixes and not emitted and not st.get("review_fix_reasked"):
            st["review_fix_reasked"] = True
            self.dispatch_review(task, count_round=False, reask_missing_fixes=True)
            rep.transitions.append(f"{task.id} review re-asked for blocking fixes")
            return True
        if not missing_fixes:
            st.pop("review_fix_reasked", None)
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
                    st.pop("review_fix_reasked", None)
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

    @staticmethod
    def _blocking_findings_without_fix(review: dict[str, Any]) -> list[dict[str, Any]]:
        """Blocking findings need actionable advice; older reviewers can omit new fields."""
        return [finding for finding in review.get("findings") or []
                if isinstance(finding, dict) and finding.get("severity") == "blocking"
                and not str(finding.get("fix") or "").strip()]

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
                run.model = str(collected.get("model") or run.model)
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
