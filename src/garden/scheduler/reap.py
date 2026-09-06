"""Reap: a finished worker run becomes a push, pre-PR checks, a PR, a retry, a question or a stall."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from .. import gitops
from ..checks import to_feedback
from ..criteria import apply_verification, parse_criteria
from ..github import GitHubError, mark_garden_comment
from ..model import Status, Task, now_iso
from ..notify import notify
from ..runner.base import Runner, run_temp_dir
from ..runs import Run
from .report import TickReport


class ReapMixin:
    def _cleanup_reaped_temp_dirs(self) -> None:
        """Remove disk-backed temp directories once their local run is no longer active."""
        work_dir = self.cfg.work_dir
        for run in self.runs.all_runs():
            if run.runner == "local" and run.status != "running":
                shutil.rmtree(run_temp_dir(work_dir, run), ignore_errors=True)

    # ---- reap --------------------------------------------------------------
    def reap(self, task: Task, rep: TickReport) -> bool:
        # Only ever the task's own worker-mode run (work/revise/resume/trial/rebase). A review
        # or persona run dispatched for the same task — the poll re-reviewing a fresh push while
        # a revise is still in flight (CG-177) — is left to reap_review/reap_orphaned, so its
        # record can never be read as "no active run found" and send the task back to ready.
        run = self.latest_worker_run(task.id)
        # garden finish is the sole finaliser of manual runs.  Skip the task
        # while a manual run is active (status "running") or while finalize()
        # has completed the run record but has not yet written the task
        # transition (run.finished_at set, task still RUNNING).
        if run is not None and run.runner == "manual":
            return False
        if self._is_unreaped(task, run):
            # The run record already reached a terminal status (written by a
            # prior finalize() call) but the task is still RUNNING: an earlier
            # tick was killed after writing the run's final status but before
            # the task transition / push / PR step completed. Resume from
            # there instead of declaring "no active run" and redispatching a
            # second run on top of the first one's finished work.
            if run.status == "timeout":
                self._retry_or_fail(task, run, rep, "worker timed out")
            else:
                runner = self.runner_for(task, run.runner, run.harness)
                # The interrupted tick already emitted run_finished before it wrote
                # the run's terminal status, so this resumed finalize must not emit it
                # a second time (which would double-count the run's cost).
                self.finalize(task, run, runner, rep, resumed=True)
            return True
        if run is None or run.status != "running":
            # Record what happened to the run we expected to reap: if something else
            # (the orphan sweep, a crash, a manual close) finished it out from under us,
            # its run id and closer belong in the log so the next disappearance is traceable.
            if run is None:
                detail = "no run record found for this task"
            else:
                closer = run.error.strip() or "(no closer recorded)"
                detail = f"expected run {run.run_id} but it is {run.status} (mode {run.mode}): {closer}"
            # Distinguish a prior attempt that made real, unreported progress from a clean
            # restart: if the worktree already has commits ahead of base, say so in the log
            # (and the event) rather than only "back to ready" (CG-125). The commits stay in
            # the worktree; the re-dispatched worker's brief lists them (see dispatch's
            # commits_ahead) so it continues from there instead of reverse-engineering git.
            prior_commits, progress = self._prior_progress(task)
            self.events.emit("no_active_run", task.id, run=(run.run_id if run else ""),
                             status=(run.status if run else ""), closer=(run.error if run else ""),
                             prior_commits=prior_commits)
            self._transition(task, Status.READY, f"no active run found; back to ready — {detail}{progress}")
            rep.transitions.append(f"{task.id} running -> ready (no run)")
            return True
        runner = self.runner_for(task, run.runner, run.harness)
        if not self._finished_or_timed_out(run, runner):
            return False
        if run.status == "timeout":
            self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, status="timeout", cost_usd=None)
            self._retry_or_fail(task, run, rep, f"worker {run.error}" if run.error else "worker timed out")
            return True
        self.finalize(task, run, runner, rep)
        return True

    def _finished_or_timed_out(self, run: Run, runner: Runner) -> bool:
        if run.process_finished():
            return True
        if not runner.detached:
            return False
        timeout_min = float(self.cfg.get("timeout_minutes", 90) or 0)
        if timeout_min and run.elapsed_minutes() > timeout_min + 5:
            run.kill()
            run.status = "timeout"
            run.finished_at = now_iso()
            run.error = "timed out"
            run.save()
            return True
        idle_kill_min = float(self.cfg.get("idle_kill_minutes", 0) or 0)
        idle_min = run.idle_minutes() if idle_kill_min else 0.0
        if idle_kill_min and idle_min >= idle_kill_min:
            run.kill()
            run.status = "timeout"
            run.finished_at = now_iso()
            run.error = f"idle {round(idle_min)} min (no output or file change)"
            run.save()
            return True
        return False

    def finalize(self, task: Task, run: Run, runner: Runner, rep: TickReport, resumed: bool = False) -> None:
        run.exit_code = run.read_exit_code()
        run.finished_at = now_iso()
        collected = runner.collect(run)
        run.result = collected.get("result") or {}
        run.usage = collected.get("usage") or {}
        run.cost_usd = collected.get("cost_usd")
        run.model = str(collected.get("model") or run.model)
        run.error = collected.get("error") or ""
        run.session_id = str(collected.get("session_id") or run.session_id or "")
        if collected.get("missing_price"):
            self.log(f"{task.id}: no price configured for model {collected['missing_price']!r}; cost_usd left null")
        final_text = collected.get("final_text") or ""
        if final_text and not (run.path / "final.md").exists():
            (run.path / "final.md").write_text(final_text)
        result = run.result
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        # Persist the collected outcome (finished_at, usage, cost, result) before the fence
        # check, push and PR steps below. A kill during any of them then leaves finished_at
        # set, so the restart recognises this as an interrupted finalize (see _is_unreaped) and
        # resumes it with resumed=True rather than finalizing a second time. run_finished is
        # emitted once per run, only after this terminal save — so a kill during the fence check
        # can no longer re-emit it (CG-198). A resumed finalize skips the emit because the first
        # pass already made it, so the run's cost is never counted twice (CG-153).
        run.save()
        if not resumed:
            self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, harness=run.harness, model=run.model,
                             status=str(result.get("status") or ("error" if run.error else "no_result")),
                             cost_usd=run.cost_usd, usage=run.usage, exit_code=run.exit_code)

        # Checked before the ordinary fence, and by reading files directly rather than through
        # `gitops.git`: a change to the clone's git internals would otherwise make the fence's
        # own git-based checks below (head_sha, status_lines) run against a clone that may no
        # longer be trustworthy (CG-239).
        git_guard_violations = self._git_guard_check(task, run)
        if git_guard_violations:
            self._release_fence_bookkeeping(task)
            self._git_guard_fail(task, run, git_guard_violations, rep)
            return

        # The runner's fence, not the brief's: whatever the worker was told, a write to the
        # live garden or the product clone is reverted here and the run fails (see the
        # permission deny rules in Harness.fence_settings for the first line of defence).
        violations = self._fence_check(task, run)
        self._release_fence_bookkeeping(task)
        if violations:
            self._fence_fail(task, run, violations, rep)
            return

        if collected.get("env_error"):
            # A quota/spend-limit message from the harness's own account, not the worker's
            # doing: close the run without counting an attempt, pause dispatch for this
            # harness (a cheap probe resumes it later, see QuotaMixin), and put the task
            # straight back to ready instead of burning it toward failed.
            self._handle_quota_env_error(task, run, rep, collected)
            return

        if run.exit_code not in (0, None) and not result:
            run.status = "failed"
            run.save()
            self._retry_or_fail(task, run, rep, f"worker exited {run.exit_code}: {run.error[:200]}")
            return
        if not result:
            run.status = "failed"
            run.save()
            self._retry_or_fail(task, run, rep, f"no GARDEN_RESULT in worker output ({run.error[:200] or 'see final.md'})")
            return
        status = str(result.get("status", "")).lower()
        if status == "blocked":
            run.status = "blocked"
            run.save()
            self._transition(task, Status.FAILED, f"worker blocked: {result.get('summary') or result.get('notes') or '?'}{cost}")
            rep.transitions.append(f"{task.id} -> failed (blocked)")
            return
        if status == "needs_input":
            run.status = "waiting"
            run.save()
            question = str(result.get("question") or result.get("summary") or "(no question given)")
            st = self.state.get(task.id)
            st["question"] = question
            st["session_id"] = run.session_id
            st["session_host"] = run.host
            st["session_harness"] = run.harness
            st["question_run"] = run.run_id
            self.events.emit("waiting_human", task.id, question=question, run=run.run_id)
            self._transition(task, Status.WAITING_HUMAN, f"worker asks: {question}{cost}")
            rep.transitions.append(f"{task.id} -> waiting_human")
            return

        if status in ("wont_do", "no_change"):
            run.status = status
            run.save()
            self._file_discovered(task, run, result)
            reason = str(result.get("reason") or result.get("summary") or "(no reason given)")
            st = self.state.get(task.id)
            decision = {"kind": status, "reason": reason, "run": run.run_id, "final": final_text,
                        "result": {k: result.get(k) for k in ("summary", "pr_title", "pr_body", "pr_comment", "notes") if result.get(k)}}
            decision["result"].setdefault("summary", reason)
            st["decision"] = decision
            st.pop("question", None)
            self.events.emit("decision", task.id, decision=status, reason=reason, run=run.run_id)
            word = "won't do" if status == "wont_do" else "nothing to change"
            self._transition(task, Status.WAITING_HUMAN, f"worker says {word}: {reason}{cost}", needs_human=True)
            rep.transitions.append(f"{task.id} -> waiting_human ({status})")
            return

        self._file_discovered(task, run, result)

        base = run.base or self.base_for(task)
        branch = run.branch or task.branch or task.default_branch()
        repo = self.repo_for(task)

        if runner.remote:
            gitops.fetch(repo)
            try:
                gitops.git("rev-parse", "--verify", f"origin/{branch}", cwd=repo)
                ahead = int(gitops.git("rev-list", "--count", f"{gitops.base_ref(repo, base)}..origin/{branch}", cwd=repo).strip() or 0)
            except gitops.GitError:
                ahead = 0
            if ahead == 0:
                run.status = "failed"
                run.error = "no commits pushed"
                run.save()
                self._retry_or_fail(task, run, rep, "remote worker finished without pushing commits")
                return
            run.status = "done"
            run.save()
            wt = self.worktree_for(task)
            try:
                if wt.exists():
                    gitops.git("fetch", "origin", cwd=wt)
                    gitops.git("reset", "-q", "--hard", f"origin/{branch}", cwd=wt)
                else:
                    gitops.prepare_worktree(repo, wt, branch, base)
            except gitops.GitError as e:
                self.log(f"{task.id}: could not materialise local worktree: {e}")
            task.branch = branch
            self._after_push(task, run, wt, branch, base, result, rep, cost)
            return

        worktree = Path(run.worktree) if run.worktree else self.worktree_for(task)
        if not worktree.exists():
            # A manual run with no garden-managed worktree (the common `garden take`
            # path): the human pushed and opened the PR themselves. There is no local
            # tree to run pre-PR checks against, but the automated reviewer builds its
            # own worktree from the pushed branch, so it still gets a look (CG-158).
            run.status = "done"
            run.save()
            pr = str(result.get("pr") or "")
            if pr:
                task.pr = pr
            self._transition(task, Status.IN_REVIEW, f"finished ({run.mode}): {result.get('summary', '')}{cost}")
            rep.transitions.append(f"{task.id} -> in_review")
            self._maybe_review(task, run, rep)
            return
        try:
            if gitops.has_uncommitted_changes(worktree):
                gitops.commit_all(worktree, f"{task.id}: leftover changes from worker run {run.run_id}")
            ahead = gitops.commits_ahead(worktree, base)
        except gitops.GitError as e:
            run.status = "failed"
            run.error = str(e)
            run.save()
            self._retry_or_fail(task, run, rep, f"git error: {e}")
            return
        if ahead == 0:
            run.status = "failed"
            run.error = "no commits"
            run.save()
            self._retry_or_fail(task, run, rep, "worker finished with no commits")
            return
        run.status = "done"
        run.save()
        st = self.state.get(task.id)
        force = bool(st.pop("force_push", False))
        try:
            note = gitops.push(worktree, branch, force=force, base=base, lease=run.start_head)
            if note:
                self.log(f"{task.id}: {note}")
        except gitops.LeaseRejected as e:
            if not self._retry_lease_push(task, run, worktree, base, e):
                self._transition(task, Status.FAILED, f"push failed: {e}{cost}")
                rep.transitions.append(f"{task.id} -> failed (push)")
                return
        except gitops.GitError as e:
            self._transition(task, Status.FAILED, f"push failed: {e}{cost}")
            rep.transitions.append(f"{task.id} -> failed (push)")
            return
        task.branch = branch
        self._after_push(task, run, worktree, branch, base, result, rep, cost)

    def _retry_lease_push(self, task: Task, run: Run, worktree: Path, base: str, err: gitops.LeaseRejected) -> bool:
        """A push after a worker run was rejected: `origin/<branch>` moved past the head this run
        started from — another writer (an earlier revise round, the merge queue's own rebase)
        pushed to the same branch meanwhile (CG-220). The worker's own commits are real, finished
        work; only the base under them moved, so bring them onto the new head mechanically (no
        model — the same rebase-and-record helper every other mechanical rebase shares) and push
        once more. Returns True when that resolved cleanly and the caller should carry on to the
        PR as usual; False (a genuine conflict, or a second failure) and the caller fails the task
        the way a plain push failure always has."""
        self.events.emit("lease_rejected", task.id, run=run.run_id, branch=err.branch,
                         expected=err.expected, actual=err.actual)
        self.log(f"{task.id}: push rejected on {err.branch} (expected origin at {err.expected[:12] or '?'}, "
                 f"now at {err.actual[:12] or '?'}); rebasing this run's commits onto the new head and retrying once")
        outcome = self._rebase_and_record(task, base, wt=worktree,
                                          reason=f"push rejected; origin/{err.branch} moved during this run")
        return outcome.status == "clean"

    def _pre_pr_specs(self, task: Task) -> list[dict[str, Any]]:
        """The pre-PR checks for this product: the configured `checks.pre_pr`, or — when none
        are configured — the product's own `setup.test` and `setup.lint` commands. Either way
        `setup.env` is added so the checks run in the same prepared environment as the worker,
        so the default no longer reaches into any particular venv."""
        setup = self.cfg.product_setup(task.product)
        specs = list(self.cfg.get("checks.pre_pr", []) or [])
        if not specs:
            for name in ("test", "lint"):
                cmd = str(setup.get(name) or "").strip()
                if cmd:
                    specs.append({"name": name, "command": cmd})
        # A task can require a configured check by name.  It is deliberately a name, rather
        # than a command from task text: check commands run branch code and stay garden config.
        from ..criteria import required_evidence
        required = {r["name"] for r in required_evidence(task.body, task.extra.get("requires")) if r["kind"] == "check"}
        if required:
            configured = list(self.cfg.get("checks.pre_pr", []) or [])
            known = {str(spec.get("name")) for spec in specs}
            for spec in configured:
                if str(spec.get("name")) in required and str(spec.get("name")) not in known:
                    specs.append(spec)
                    known.add(str(spec.get("name")))
            # Keep a misspelled requirement visible: an error result follows the ordinary
            # mechanical changes-requested route instead of silently reviewing without it.
            specs.extend({"name": name} for name in sorted(required - known))
        env = dict(setup.get("env") or {})
        if env:
            specs = [{**s, "env": {**env, **(s.get("env") or {})}} for s in specs]
        return specs

    def _run_specs_in(self, task: Task, specs: list[dict[str, Any]], worktree: Path, branch: str, base: str) -> list[dict[str, Any]]:
        """Prepare `worktree`'s environment, then run `specs` there, synchronously — the same
        `run_check_job` the detached check run uses, so there is one check-running implementation.
        Off the tick path now (a check runs as a detached run record; see CheckRunMixin); kept as
        the synchronous entry point exercised by the setup tests."""
        from ..checkrun import run_check_job

        if not specs or not worktree.exists():
            return []
        return run_check_job({"specs": specs, "ctx": self.check_ctx(task, branch, base, worktree),
                              "cwd": str(worktree), "setup": self.cfg.product_setup(task.product),
                              "timeout": int(self.cfg.get("checks.timeout_seconds", 600)), "config": self.cfg.data})

    def _pre_pr_checks(self, task: Task, worktree: Path, branch: str, base: str) -> list[dict[str, Any]]:
        results = self._run_specs_in(task, self._pre_pr_specs(task), worktree, branch, base)
        for r in results:
            self.events.emit("check", task.id, stage="pre_pr", name=r.get("name"), status=r.get("status"), summary=r.get("summary", ""))
        return results

    def _start_check_revise(self, task: Task, failed: list[dict[str, Any]], rep: TickReport, cost: str, note: str = "",
                            is_rebase: bool = False) -> None:
        """Queue a revise round (or hand off to a human at the cap) for a pre-PR check the branch
        actually owns. Mirrors the historic inline behaviour of `_after_push`. `is_rebase` marks a
        stale-base rebase that failed to apply cleanly (CG-131): mechanical bookkeeping, not a
        revision round, so it skips the cap and never hands the task to a human on its own."""
        st = self.state.get(task.id)
        names = ", ".join(str(f.get("name")) for f in failed)
        feedback = to_feedback(failed, "pre-PR check")
        if not feedback.strip():
            # A killed or empty check leaves nothing to revise against; storing it as
            # empty feedback would make dispatch skip the task forever. Flag it instead.
            reason = f"pre-PR check did not finish ({names}); no output to revise against"
            st["needs_human"] = reason
            self.events.emit("needs_human", task.id, reason=reason)
            self._transition(task, Status.CHANGES_REQUESTED, f"{reason}; needs a human{cost}", needs_human=True)
            rep.transitions.append(f"{task.id} -> changes_requested (check did not finish)")
            return
        st["pending_feedback"] = feedback
        st.pop("pending_feedback_easy", None)
        if is_rebase:
            st["pending_feedback_rebase"] = True
        else:
            max_rev = int(self.cfg.get("max_revisions", 3))
            if int(st.get("revisions", 0)) >= max_rev:
                # Cap reached: hand it to a human like the review path, rather than leaving a
                # task in changes_requested that the dispatch queue skips forever.
                reason = f"pre-PR checks failed ({names}) and {max_rev} revision rounds already used"
                st["needs_human"] = reason
                self.events.emit("needs_human", task.id, reason=reason)
                self._transition(task, Status.CHANGES_REQUESTED, f"{reason}; needs a human{cost}", needs_human=True)
                rep.transitions.append(f"{task.id} -> changes_requested (checks, cap)")
                return
        if task.pr:
            self._transition(task, Status.CHANGES_REQUESTED, f"pre-PR checks failed ({names}){note}; revise run will fix before the PR is updated{cost}")
        else:
            self._transition(task, Status.CHANGES_REQUESTED, f"pre-PR checks failed ({names}){note}; no PR opened yet; revise run will fix{cost}")
        rep.transitions.append(f"{task.id} -> changes_requested (checks)")

    # ---- base_broken: re-probe and continue on its own --------------------
    def _last_worker_result(self, task: Task) -> dict[str, Any]:
        """The most recent worker run's reported result (its `pr_title`/`pr_body`/`summary`),
        so a re-probe can open or update the PR with what the worker actually wrote. Only the
        PR-description fields are kept: any friction or one-off `pr_comment` was already posted
        when the run first finished and must not be replayed on the mechanical continuation."""
        for r in reversed(self.runs.runs_for(task.id)):
            if r.mode in ("work", "revise", "resume") and r.result:
                return {k: r.result[k] for k in ("pr_title", "pr_body", "summary") if r.result.get(k)}
        return {}

    def _last_worker_verified(self, task: Task) -> list[dict[str, Any]] | None:
        """The `verified` per-criterion evidence from the most recent worker round that reported
        it, so a review dispatched without a `work_run` in hand (a re-review, a re-probe) still
        shows the reviewer the author's claims."""
        for r in reversed(self.runs.runs_for(task.id)):
            if r.mode in ("work", "revise", "resume") and isinstance(r.result, dict):
                v = r.result.get("verified")
                if isinstance(v, list) and v:
                    return v
        return None

    def _reprobe_base_broken(self, task: Task, rep: TickReport) -> bool:
        """A task parked with the `base_broken` stop re-probes its base every tick and continues
        by itself once the base goes green: compare the base branch's tip with the commit that was
        broken when the task parked; while it has not moved, wait (no run, no spend). When it has
        moved, rebase the branch onto it mechanically (the CG-141 rebase, no worker), force-push so
        any stale CI on the branch runs again, and re-run the pre-PR checks. On green, clear the
        stop and open or update the PR. A rebase that does not apply, or checks that still fail
        after it, fall through to the normal revise path — and only then. Returns True when the
        task was acted on this tick."""
        st = self.state.get(task.id)
        info = st.get("needs_human")
        if not (isinstance(info, dict) and info.get("kind") == "base_broken"):
            return False
        if task.status.terminal or any(r.task_id == task.id for r in self.active_runs()):
            return False
        base = str(info.get("base") or self.base_for(task))
        parked_sha = str(info.get("base_sha") or "")
        branch = task.branch or task.default_branch()
        wt = self.worktree_for(task)
        repo = self.repo_for(task)
        try:
            if not wt.exists():
                gitops.prepare_worktree(repo, wt, branch, base)
            gitops.fetch(wt)
            ref = gitops.base_ref(wt, base)
            tip = gitops.rev_parse(wt, ref)
        except gitops.GitError as e:
            self.log(f"{task.id}: base re-probe failed ({e}); still waiting for `{base}`")
            return False
        if not parked_sha or tip == parked_sha:
            return False  # the base has not moved: keep waiting, no worker, no spend
        # The base moved. Bring the branch onto it mechanically (no worker) through the one
        # rebase-and-record helper (CG-197) and re-check.
        outcome = self._rebase_and_record(task, base, wt=wt)
        run = outcome.run
        specs = self._pre_pr_specs(task)
        if outcome.status == "conflict":
            # the rebase does not apply cleanly: hand it to the normal revise path, and only then.
            self.events.emit("rebased_stale_base", task.id, base=base, base_sha=tip, resolved=False)
            st.pop("needs_human", None)
            if specs:
                self._dispatch_check_run(task, worktree=wt, branch=branch, base=base, specs=specs,
                                         stage="reprobe_conflict", rep=rep,
                                         cont=self._pre_pr_cont(None, wt, branch, base, ""))
            else:
                self._start_check_revise(task, [], rep, "", note=f" (rebase onto `{base}` did not apply cleanly)")
            rep.transitions.append(f"{task.id} base moved but rebase conflicted; revise")
            return True
        if outcome.status == "error":
            return False  # push failure already logged by the helper
        if specs:
            # Re-run the pre-PR checks as a detached check run; the continuation (`_after_reprobe_check`)
            # opens the PR on green or routes a failure through the base probe.
            self._dispatch_check_run(task, worktree=wt, branch=branch, base=base, specs=specs,
                                     stage="reprobe", rep=rep,
                                     cont={**self._pre_pr_cont(run, wt, branch, base, ""), "base_sha": tip})
            return True
        # No checks configured: the base recovered, so clear the stop and open the PR directly.
        st.pop("needs_human", None)
        self._queue_leave(task)
        self.events.emit("rebased_stale_base", task.id, base=base, base_sha=tip, resolved=True)
        task.log(f"base branch `{base}` recovered (moved to {tip[:12]}); rebased onto it "
                 f"and continuing without a worker run")
        self.store.save(task)
        self.log(f"{task.id}: base `{base}` recovered; rebased, continuing on its own")
        rep.transitions.append(f"{task.id} rebased onto recovered {base}")
        self._open_or_update_pr(task, run, branch, base, self._last_worker_result(task), rep, "")
        return True

    def _after_push(self, task: Task, run: Run, worktree: Path, branch: str, base: str, result: dict[str, Any],
                    rep: TickReport, cost: str, check_stall: bool = True) -> None:
        """Stall bookkeeping, token-free pre-PR checks, then PR open/update, then a pending restack.

        `check_stall=False` skips the "revise round changed nothing" guard: a person who accepted a
        worker's `no_change` decision has already ruled that the unchanged diff is correct, so the
        round must proceed to the PR or review rather than stall."""
        st = self.state.get(task.id)
        # A stack parent that merged while this run was in flight leaves `base` naming the parent's
        # branch, which GitHub may already have deleted. Retarget to the final base and rebase onto
        # it before the pre-PR checks and the PR, so the PR never opens (or updates) against a dead
        # branch (CG-173). A textual conflict hands off to an easy-tier rebase agent; only then does
        # the round stop short of the PR.
        if self._parent_merged(task):
            st.pop("restack_pending", None)
            parent_id = st.get("stack_parent", "")
            self._restack(task, rep)
            if st.get("rebase_pending"):
                if task.status != Status.CHANGES_REQUESTED:
                    self._transition(task, Status.CHANGES_REQUESTED,
                                     f"parent {parent_id} merged; rebase conflicts; a rebase agent will resolve it{cost}")
                    rep.transitions.append(f"{task.id} -> changes_requested (rebase)")
                return
            base = self.base_for(task)
        stalled = False
        diff_h: str | None = None
        body_h: str | None = None
        if worktree.exists():
            run.diff_stat = gitops.diff_stat(worktree, base)
            run.save()
        if check_stall and bool(self.cfg.get("stall.enabled", True)) and worktree.exists():
            diff_h = gitops.diff_hash(worktree, base)
            body_h = hashlib.sha1(str(result.get("pr_body") or "").encode("utf-8", "replace")).hexdigest()[:16]
            if run.mode == "revise":
                diff_unchanged = bool(diff_h and diff_h == st.get("last_diff_hash"))
                body_unchanged = body_h == st.get("last_pr_body_hash", "")
                if diff_unchanged and body_unchanged:
                    stalled = True
        # Pre-PR checks run as a detached check run, started here and reaped on a later tick
        # (CG-182): only the git scaffolding stays in the tick. The continuation (see
        # `_after_pre_pr_check`) probes the base on a failure and opens the PR when green. A
        # stalled revise round skips the checks and goes straight to the PR, then stalls.
        specs = [] if stalled else self._pre_pr_specs(task)
        if specs and worktree.exists():
            self._dispatch_check_run(task, worktree=worktree, branch=branch, base=base, specs=specs,
                                     stage="pre_pr", rep=rep,
                                     cont=self._pre_pr_cont(run, worktree, branch, base, cost, diff_h, body_h, stalled))
            return
        # Save hashes only after the round reaches the PR; failed pre-PR rounds are not recorded.
        # A rebase round is the exception: `_rebase_review_or_keep` must compare the new diff
        # against the reviewed hash before it is overwritten, so it owns `last_diff_hash`.
        if diff_h is not None and run.mode != "rebase":
            st["last_diff_hash"] = diff_h
        if body_h is not None:
            st["last_pr_body_hash"] = body_h
        self._open_or_update_pr(task, run, branch, base, result, rep, cost)
        if stalled:
            self._stall(task, rep, f"revise run {run.run_id} produced no change to the diff or PR description")

    def _open_or_update_pr(self, task: Task, run: Run, branch: str, base: str, result: dict[str, Any],
                           rep: TickReport, cost: str) -> None:
        slug = self.slug_for(task)
        summary = str(result.get("summary") or "")
        criteria = parse_criteria(task.body)
        verified = result.get("verified")
        st = self.state.get(task.id)
        if not slug or not self.github.available:
            self._transition(task, Status.IN_REVIEW,
                             f"branch {branch} pushed; GitHub unavailable, open the PR by hand and run `garden pr {task.id} <url>`{cost}")
            rep.transitions.append(f"{task.id} -> in_review (no PR)")
            return
        try:
            existing = self.github.find_pr(slug, branch)
            if existing and existing.state == "OPEN":
                task.pr = existing.url
                st["pr_number"] = existing.number
                body = str(result.get("pr_body") or "")
                if body:
                    body = apply_verification(body, criteria, verified)
                title = str(result.get("pr_title") or "")
                st["pr_draft"] = bool(existing.is_draft)
                if run.mode in ("revise", "resume", "rebase"):
                    try:
                        self.github.update_pr(slug, existing.number, title=title, body=body)
                        pr_comment = str(result.get("pr_comment") or "").strip()
                        if pr_comment:
                            self.github.comment(slug, existing.number, mark_garden_comment(pr_comment, run.run_id))
                        comment_body = mark_garden_comment(f"Pushed a revision round: {summary}", run.run_id)
                        self.github.comment(slug, existing.number, comment_body)
                    except GitHubError as e:
                        self.log(f"{task.id}: could not update PR: {e}")
                nxt = self._pr_status(task)
                defer_triage = nxt == Status.AWAITING_TRIAGE and self._review_round_pending(st)
                if defer_triage:
                    st["pending_triage_notify"] = True
                self._transition(task, nxt, f"pushed revision to {existing.url}: {summary}{cost}", notify_now=not defer_triage)
                rep.transitions.append(f"{task.id} -> {nxt.value} (revised)")
            else:
                title = str(result.get("pr_title") or f"{task.id}: {task.title}")
                body = apply_verification(str(result.get("pr_body") or summary or task.body), criteria, verified)
                footer = f"\n\n---\nTask `{task.id}` from the context garden ({task.product}/{task.phase})."
                if st.get("stack_parent"):
                    footer += f" Stacked on `{st['stack_parent']}` (targets `{base}` until it merges)."
                if st.get("discovered_ids"):
                    footer += " Discovered work: " + ", ".join(f"`{i}`" for i in st["discovered_ids"]) + "."
                pr = self.github.create_pr(
                    slug, branch, base, title, body + footer,
                    draft=bool(self.cfg.get("github.draft_pr", False)),
                    reviewers=list(self.cfg.get("github.reviewers", []) or []),
                )
                task.pr = pr.url
                st["pr_number"] = pr.number
                st["revisions"] = 0
                st["review_rounds"] = 0
                st["pr_draft"] = bool(self.cfg.get("github.draft_pr", True))
                self.events.emit("pr_opened", task.id, pr=pr.url, base=base, stacked_on=st.get("stack_parent", ""), draft=st["pr_draft"])
                nxt = self._pr_status(task)
                defer_triage = nxt == Status.AWAITING_TRIAGE and self._review_round_pending(st)
                if defer_triage:
                    st["pending_triage_notify"] = True
                self._transition(task, nxt, f"opened {'draft ' if st['pr_draft'] else ''}{pr.url} (base {base}): {summary}{cost}",
                                 notify_now=not defer_triage)
                rep.transitions.append(f"{task.id} -> {nxt.value} ({pr.url})")
        except GitHubError as e:
            self._transition(task, Status.IN_REVIEW, f"branch pushed but PR failed ({e}); open it by hand and run `garden pr {task.id} <url>`{cost}")
            rep.transitions.append(f"{task.id} -> in_review (PR failed)")
            return
        self._record_friction(task, run, result)
        if run.mode == "rebase":
            # A resolved rebase keeps the verdict when it did not change the diff; only a
            # resolution that altered the tree is reviewed again (see rule 2).
            self._rebase_review_or_keep(task, run, base, rep)
        else:
            self._maybe_review(task, run, rep)

    def _prior_progress(self, task: Task) -> tuple[int, str]:
        """Commits a prior, interrupted attempt already left on this task's branch. When a run
        disappears (swept, crashed, closed out from under the reap) the task goes back to ready;
        if its worktree already has commits ahead of base, that is real progress, not a clean
        restart. Returns (count, note): a non-empty note is appended to the "back to ready" log
        line so the two cases are distinguishable, and the count goes on the `no_active_run`
        event. Empty when the worktree is missing or has no commits of its own."""
        wt = self.worktree_for(task)
        if not wt.exists():
            return 0, ""
        try:
            n = gitops.commits_ahead(wt, self.base_for(task))
        except gitops.GitError:
            return 0, ""
        if n <= 0:
            return 0, ""
        branch = task.branch or task.default_branch()
        plural = "s" if n != 1 else ""
        return n, (f"; the prior attempt made real progress — {n} commit{plural} already on `{branch}`, "
                   f"kept in the worktree and listed for the next run to continue from")

    def _handle_quota_env_error(self, task: Task, run: Run, rep: TickReport, collected: dict[str, Any]) -> None:
        """A quota/spend-limit stop: the harness's account, not the task, is at fault. Undo
        the attempt dispatch() counted when this run started (revise/rebase/resume never
        counted one), pause dispatch for the harness, and put the round back where dispatch
        found it. A work round has no PR yet, so it goes back to ready. A revise or rebase
        round always has an open PR and stored feedback (or a pending rebase) that dispatch
        already cleared to start this very run; restore it from the run's `env_snapshot` and
        go back to changes_requested instead, so the PR and the feedback survive the stop. A
        resume round (the continuation `human.answer()` dispatches after a person answers a
        worker's question, whatever round originally asked it) already cleared the pending
        question and session out of state before this run started; restore those from the
        same `env_snapshot` and go back to waiting_human, so the question survives the stop
        instead of the task falling back to ready and losing the PR/feedback that led to it."""
        kind = str(collected.get("env_kind") or "quota")
        run.status = "env_error"
        run.save()
        if run.mode == "work":
            task.attempts = max(0, task.attempts - 1)
        harness_name = run.harness or ""
        self._pause_for_env_error(run, collected)
        note = f"environment stop ({kind}): {kind} limit hit on {harness_name or 'the harness'}; not counted as an attempt"
        if harness_name:
            note += f"; dispatch paused for {harness_name} until a probe succeeds"

        st = self.state.get(task.id)
        snap = run.env_snapshot or {}
        if run.mode == "revise" and task.pr:
            st["pending_feedback"] = snap.get("pending_feedback", "")
            if snap.get("pending_feedback_easy"):
                st["pending_feedback_easy"] = True
            else:
                st.pop("pending_feedback_easy", None)
            if snap.get("pending_feedback_rebase"):
                st["pending_feedback_rebase"] = True
                st["rebases"] = max(0, int(st.get("rebases", 0)) - 1)
            else:
                st["revisions"] = max(0, int(st.get("revisions", 0)) - 1)
            self.state.save()
            self._transition(task, Status.CHANGES_REQUESTED, f"{note}; feedback restored, will retry the revise round")
            rep.transitions.append(f"{task.id} -> changes_requested (env_error: {kind})")
            return
        if run.mode == "rebase" and task.pr:
            st["rebase_pending"] = True
            st["rebases"] = max(0, int(st.get("rebases", 0)) - 1)
            self.state.save()
            self._transition(task, Status.CHANGES_REQUESTED, f"{note}; will retry the rebase round")
            rep.transitions.append(f"{task.id} -> changes_requested (env_error: {kind})")
            return
        if run.mode == "resume":
            st["question"] = snap.get("question", "")
            st["session_id"] = snap.get("session_id", "")
            self.state.save()
            self._transition(task, Status.WAITING_HUMAN, f"{note}; the pending question and session are restored, answer again once it resumes")
            rep.transitions.append(f"{task.id} -> waiting_human (env_error: {kind})")
            return
        self._transition(task, Status.READY, note)
        rep.transitions.append(f"{task.id} -> ready (env_error: {kind})")

    def _retry_or_fail(self, task: Task, run: Run, rep: TickReport, reason: str) -> None:
        max_attempts = int(self.cfg.get("max_attempts", 2))
        if run.mode == "rebase":
            self._retry_or_park_rebase(task, run, rep, reason)
        elif run.mode == "revise":
            self._transition(task, Status.FAILED, f"revision failed: {reason}")
            rep.transitions.append(f"{task.id} -> failed")
        elif run.mode == "work" and task.attempts < max_attempts:
            self._transition(task, Status.READY, f"attempt {task.attempts} failed: {reason}; will retry")
            rep.transitions.append(f"{task.id} -> ready (retry)")
        elif run.mode == "work":
            self._transition(task, Status.FAILED, f"attempt {task.attempts} failed: {reason}; giving up")
            rep.transitions.append(f"{task.id} -> failed")
        else:
            self._transition(task, Status.FAILED, f"{run.mode} run failed: {reason}")
            rep.transitions.append(f"{task.id} -> failed ({run.mode})")

    def _retry_or_park_rebase(self, task: Task, run: Run, rep: TickReport, reason: str) -> None:
        """Retry a conflict-resolution run once without reopening the work round.

        A rebase agent works on an already-open PR.  Treating its missing result as a failed
        work attempt loses that context and lets the ready queue dispatch a new work brief from
        the base branch.  Keep the branch and PR, and put the same rebase continuation back on
        the rebase queue instead.  The second loss is a human decision, not an attempt failure.
        """
        st = self.state.get(task.id)
        retries = int(st.get("rebase_run_retries", 0))
        if retries < 1:
            st["rebase_run_retries"] = retries + 1
            st["rebase_pending"] = True
            st["rebase_retry_files"] = list(st.get("rebase_files", []))
            note = f"rebase run {run.run_id} did not finish: {reason}; will retry"
            task.log(note)
            self.store.save(task)
            self.events.emit("rebase_retry", task.id, run=run.run_id, cause=reason, retry=retries + 1)
            self.state.save()
            self._transition(task, Status.CHANGES_REQUESTED, note)
            rep.transitions.append(f"{task.id} -> changes_requested (rebase retry)")
            return
        files = list(st.get("rebase_files", [])) or list(st.get("rebase_retry_files", []))
        conflict = ", ".join(str(p) for p in files if p) or "the rebase conflict"
        note = f"rebase run {run.run_id} did not finish: {reason}; retry also failed; needs human to resolve {conflict}"
        self._set_needs_human(task, "rebase_failed", note, run=run.run_id, cause=reason,
                              files=files)
        st.pop("rebase_pending", None)
        self.events.emit("needs_human", task.id, stop_kind="rebase_failed", reason=note, run=run.run_id)
        self.state.save()
        self._transition(task, Status.IN_REVIEW if task.pr else Status.CHANGES_REQUESTED, note, needs_human=True)
        rep.transitions.append(f"{task.id} -> {'in_review' if task.pr else 'changes_requested'} (rebase needs human)")

    # ---- dead-run sweep -----------------------------------------------------
    def _owned_run_ids(self) -> set[str]:
        """Every run id some other reap path still follows this tick: the aux list
        (persona/compare), the retro list (phase personas + reconcile), a task's
        `review_run`/`edit_run` pointer, a trial's per-contender runs, and — for a task
        still `running` — whichever run `RunStore.latest` would hand to the ordinary
        `reap()`. Anything left `running` outside this set has no path left that will
        ever visit it again."""
        owned = {e["run_id"] for e in self._aux_list()}
        for entry in self._retro_list():
            owned.update(str(v) for v in (entry.get("persona_runs") or {}).values())
            if entry.get("recon_run_id"):
                owned.add(str(entry["recon_run_id"]))
        for t in self.store.tasks().values():
            st = self.state.get(t.id)
            for key in ("review_run", "edit_run"):
                rid = st.get(key)
                if rid:
                    owned.add(str(rid))
            check_rid = (st.get("check_run") or {}).get("run_id")
            if check_rid:
                owned.add(str(check_rid))
            for c in (st.get("trial", {}).get("contenders") or []):
                if isinstance(c, dict) and c.get("run_id"):
                    owned.add(str(c["run_id"]))
            if t.status == Status.RUNNING:
                latest = self.latest_worker_run(t.id)
                if latest is not None:
                    owned.add(latest.run_id)
        return owned

    def reap_dead_runs(self, rep: TickReport) -> None:
        """Close any `running` run record whose process has already exited and that no
        pointer above (`_owned_run_ids`) still leads a reap to: the generalisation of the
        orphan sweep (CG-116) from "review/persona/compare whose task moved on" to every
        mode and a bare pid check, so a record superseded, forgotten, or left behind by a
        pointer that moved on is closed instead of sitting `running` forever with no
        process behind it (CG-144)."""
        owned = self._owned_run_ids()
        tasks = self.store.tasks()
        for run in self.runs.active():
            if run.runner == "manual":
                continue
            no_exit_code = not (run.path / "exit_code").exists()
            process_missing = run.pid is None
            process_dead = not process_missing and run.process_finished()
            if no_exit_code and (process_missing or process_dead):
                # A recovery operation with no worker pid is durable pending work, not live
                # work.  Its client replays the same key after a server restart, which resumes
                # preparation on this record; never close it or let a tick duplicate it.
                if run.idempotency_key and run.status in ("requested", "preparing"):
                    continue
                task = tasks.get(run.task_id)
                # A terminal task can still have a worktree that the terminal sweep
                # protects with this record (for example while a human finishes a
                # hand-created run record).  Leave that ownership marker in place so
                # cleanup does not remove caches from beneath the worktree on this tick.
                # Non-terminal tasks take the immediate failure path below.
                if task is not None and task.status.terminal and process_missing:
                    continue
                reason = "process never started" if process_missing else "process vanished"
                run.status = "failed"
                run.finished_at = now_iso()
                run.error = reason
                run.save()
                self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode,
                                 harness=run.harness, model=run.model, status="failed",
                                 cost_usd=None, usage={}, error=reason, orphaned=True)
                rep.transitions.append(f"{run.task_id} {run.mode} run {run.run_id} failed ({reason})")
                if (task is not None and task.status == Status.RUNNING
                        and run.mode in ("work", "revise", "resume", "trial", "rebase")
                        and self.latest_worker_run(task.id).run_id == run.run_id):
                    self._retry_or_fail(task, run, rep, reason)
                continue
            if run.run_id in owned:
                continue
            if not run.process_finished():
                continue
            run.exit_code = run.read_exit_code()
            run.finished_at = now_iso()
            probe = tasks.get(run.task_id) or Task(path=self.store.root, id=run.task_id, title="")
            try:
                runner = self.runner_for(probe, run.runner, run.harness)
                collected = runner.collect(run)
                run.usage = collected.get("usage") or {}
                run.cost_usd = collected.get("cost_usd")
                run.model = str(collected.get("model") or run.model)
                run.error = collected.get("error") or run.error
                if collected.get("missing_price"):
                    self.log(f"{run.task_id}: no price configured for model {collected['missing_price']!r}; cost_usd left null")
            except Exception as e:  # noqa: BLE001
                run.error = run.error or str(e)
            run.status = "done" if run.exit_code in (0, None) else "failed"
            note = "closed: no active run pointer references it and its process has exited"
            run.error = f"{run.error} ({note})" if run.error else note
            run.save()
            self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode, cost_usd=run.cost_usd,
                             usage=run.usage, status=run.status, dangling=True)
            self.log(f"{run.task_id}: {run.mode} run {run.run_id} closed ({run.status}); {note}")
            rep.transitions.append(f"{run.task_id} {run.mode} run {run.run_id} closed (dangling)")

    # ---- stall detection ---------------------------------------------------
    def _stall(self, task: Task, rep: TickReport, reason: str) -> None:
        self._set_needs_human(task, "stall", reason)
        self.events.emit("stall", task.id, reason=reason)
        action = f'garden triage {task.id} --changes "<feedback>" to unblock'
        if task.status != Status.CHANGES_REQUESTED:
            self._transition(task, Status.CHANGES_REQUESTED, f"stalled: {reason}; run `{action}`", needs_human=True)
        else:
            task.log(f"stalled: {reason}; run `{action}`")
            self.store.save(task)
            notify(self.cfg.data, task.id, "stalled", reason, task.pr or "")
        rep.transitions.append(f"{task.id} stalled")
