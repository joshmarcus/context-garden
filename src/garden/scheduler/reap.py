"""Reap: a finished worker run becomes a push, pre-PR checks, a PR, a retry, a question or a stall."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .. import gitops
from ..checks import failures as check_failures
from ..checks import run_checks, to_feedback
from ..github import GitHubError, mark_garden_comment
from ..model import Status, Task, now_iso
from ..notify import notify
from ..runner.base import Runner, RunnerError, run_setup, scrubbed_env
from ..runs import Run
from .report import TickReport


class ReapMixin:
    # ---- reap --------------------------------------------------------------
    def reap(self, task: Task, rep: TickReport) -> bool:
        run = self.runs.latest(task.id)
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
        if run is None or run.status != "running" or run.mode == "review":
            # Record what happened to the run we expected to reap: if something else
            # (the orphan sweep, a crash, a manual close) finished it out from under us,
            # its run id and closer belong in the log so the next disappearance is traceable.
            if run is None:
                detail = "no run record found for this task"
            else:
                closer = run.error.strip() or "(no closer recorded)"
                detail = f"expected run {run.run_id} but it is {run.status} (mode {run.mode}): {closer}"
            self.events.emit("no_active_run", task.id, run=(run.run_id if run else ""),
                             status=(run.status if run else ""), closer=(run.error if run else ""))
            self._transition(task, Status.READY, f"no active run found; back to ready — {detail}")
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
        run.error = collected.get("error") or ""
        run.session_id = str(collected.get("session_id") or run.session_id or "")
        final_text = collected.get("final_text") or ""
        if final_text and not (run.path / "final.md").exists():
            (run.path / "final.md").write_text(final_text)
        result = run.result
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        # Emit exactly once per run: a resumed finalize (the tick that first finalized this
        # run was killed after emitting run_finished but before the task transition) skips
        # the emit so the run's cost is not counted twice.
        if not resumed:
            self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, harness=run.harness, model=run.model,
                             status=str(result.get("status") or ("error" if run.error else "no_result")),
                             cost_usd=run.cost_usd, usage=run.usage, exit_code=run.exit_code)

        # The runner's fence, not the brief's: whatever the worker was told, a write to the
        # live garden or the product clone is reverted here and the run fails (see the
        # permission deny rules in Harness.fence_settings for the first line of defence).
        violations = self._fence_check(task, run)
        if violations:
            self._fence_fail(task, run, violations, rep)
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
            run.status = "done"
            run.save()
            pr = str(result.get("pr") or "")
            if pr:
                task.pr = pr
            self._transition(task, Status.IN_REVIEW, f"finished ({run.mode}): {result.get('summary', '')}{cost}")
            rep.transitions.append(f"{task.id} -> in_review")
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
            note = gitops.push(worktree, branch, force=force, base=base)
            if note:
                self.log(f"{task.id}: {note}")
        except gitops.GitError as e:
            self._transition(task, Status.FAILED, f"push failed: {e}{cost}")
            rep.transitions.append(f"{task.id} -> failed (push)")
            return
        task.branch = branch
        self._after_push(task, run, worktree, branch, base, result, rep, cost)

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
        env = dict(setup.get("env") or {})
        if env:
            specs = [{**s, "env": {**env, **(s.get("env") or {})}} for s in specs]
        return specs

    def _run_specs_in(self, task: Task, specs: list[dict[str, Any]], worktree: Path, branch: str, base: str) -> list[dict[str, Any]]:
        """Prepare `worktree`'s environment, then run `specs` there. For a local run setup
        already ran in the branch worktree (the marker short-circuits it); for a remote run
        the branch was just materialised into a fresh local worktree, and a base probe checks
        out a throwaway worktree, so its setup artifacts (e.g. node_modules) are absent — run
        setup now so the default test/lint checks find the same prepared env."""
        if not specs or not worktree.exists():
            return []
        setup = self.cfg.product_setup(task.product)
        try:
            run_setup(worktree, setup, log_path=worktree.parent / f".garden-setup-{worktree.name}.log",
                      env=scrubbed_env(self.cfg.data, setup))
        except RunnerError as e:
            return [{"name": "setup", "status": "fail", "summary": "setup command failed", "details": str(e)}]
        return run_checks(specs, self.check_ctx(task, branch, base, worktree), cwd=worktree,
                          timeout=int(self.cfg.get("checks.timeout_seconds", 600)), config=self.cfg.data)

    def _pre_pr_checks(self, task: Task, worktree: Path, branch: str, base: str) -> list[dict[str, Any]]:
        results = self._run_specs_in(task, self._pre_pr_specs(task), worktree, branch, base)
        for r in results:
            self.events.emit("check", task.id, stage="pre_pr", name=r.get("name"), status=r.get("status"), summary=r.get("summary", ""))
        return results

    def _probe_checks_at_base(self, task: Task, worktree: Path, branch: str, base: str,
                              failed: list[dict[str, Any]]) -> tuple[str, bool, list[dict[str, Any]]]:
        """Run the failed pre-PR checks at the branch's base commit (its merge base with `base`),
        in a throwaway detached worktree. Returns (base_sha, moved, base_failures): `moved` is
        True when the base branch's tip has advanced past that commit since the branch was cut,
        and `base_failures` is the subset of `failed` checks that also fail at the base — empty
        when every failure is the branch's own doing."""
        repo = self.repo_for(task)
        gitops.fetch(worktree)
        ref = gitops.base_ref(worktree, base)
        base_sha = gitops.merge_base(worktree, ref)
        moved = bool(base_sha) and gitops.rev_parse(worktree, ref) != base_sha
        names = {str(f.get("name")) for f in failed}
        specs = [s for s in self._pre_pr_specs(task) if str(s.get("name")) in names]
        probe = worktree.parent / f"{worktree.name}.base-probe"
        gitops.remove_worktree(repo, probe)
        try:
            gitops.add_detached_worktree(repo, probe, base_sha)
            results = self._run_specs_in(task, specs, probe, branch, base)
        finally:
            gitops.remove_worktree(repo, probe)
        for r in results:
            self.events.emit("check", task.id, stage="base_probe", name=r.get("name"), status=r.get("status"), summary=r.get("summary", ""))
        return base_sha, moved, check_failures(results)

    def _handle_failed_checks(self, task: Task, run: Run, worktree: Path, branch: str, base: str,
                              failed: list[dict[str, Any]], rep: TickReport, cost: str) -> str:
        """Route a pre-PR check failure. Probe the branch's base first: if the same check fails
        at the merge base too, the failure is not this branch's. A base that has moved is rebased
        and the checks re-run without a worker (return "pass" if they go green); a base that has
        not moved means the base branch itself is broken — the task parks on a card, no revise
        round, no spend. Only a failure the branch actually owns starts a revise round. Returns
        "pass" when the checks now pass and the caller should open the PR, else "done"."""
        try:
            base_sha, moved, base_failures = self._probe_checks_at_base(task, worktree, branch, base, failed)
        except gitops.GitError as e:
            self.log(f"{task.id}: base probe failed ({e}); treating the failure as this branch's")
            base_failures = []
        if base_failures:
            names = ", ".join(str(f.get("name")) for f in base_failures)
            self.events.emit("check_base", task.id, base=base_sha, checks=names, moved=moved)
            self.log(f"{task.id}: pre-PR check(s) {names} fail at base {base_sha[:12]}; not this branch")
            if moved:
                try:
                    ok, _ = gitops.rebase_onto(worktree, gitops.base_ref(worktree, base))
                except gitops.GitError:
                    ok = False
                if ok:
                    try:
                        gitops.push(worktree, branch, force=True)
                    except gitops.GitError as e:
                        self.log(f"{task.id}: push after base rebase failed: {e}")
                    rerun = check_failures(self._pre_pr_checks(task, worktree, branch, base))
                    if not rerun:
                        task.log(f"pre-PR check(s) {names} failed at the stale base {base_sha[:12]}; the base branch "
                                 f"`{base}` had moved, so rebased onto it and the checks pass now — no revise round")
                        self.store.save(task)
                        self.events.emit("rebased_stale_base", task.id, base=base, base_sha=base_sha, resolved=True)
                        rep.transitions.append(f"{task.id} rebased onto moved {base}; checks green")
                        return "pass"
                    self.events.emit("rebased_stale_base", task.id, base=base, base_sha=base_sha, resolved=False)
                    self._start_check_revise(task, rerun, rep, cost, note=f" (still failing after a rebase onto `{base}`)")
                    return "done"
                # the rebase didn't apply cleanly; let a revise round resolve it. This is a
                # stale-base rebase (CG-131), not a revision round: it must not count toward
                # max_revisions.
                self._start_check_revise(task, failed, rep, cost, is_rebase=True)
                return "done"
            # the base branch has not moved: it is itself broken. Park the task; no revise, no spend.
            # Remember the base and its current tip so a later tick can tell when it has moved and
            # continue on its own (see _reprobe_base_broken), no worker and no person needed.
            reason = (f"base branch `{base}` is itself broken — pre-PR check(s) {names} fail at its own commit "
                      f"{base_sha[:12]}, not because of this branch")
            self._set_needs_human(task, "base_broken", reason, base=base, base_sha=base_sha)
            self.events.emit("needs_human", task.id, stop_kind="base_broken", reason=reason)
            self._transition(task, Status.CHANGES_REQUESTED, f"{reason}; waiting for the base to go green, no revise round{cost}", needs_human=True)
            rep.transitions.append(f"{task.id} -> changes_requested (base broken)")
            return "done"
        # the base is clean: this branch owns the failure.
        self._start_check_revise(task, failed, rep, cost)
        return "done"

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
        # The base moved. Bring the branch onto it mechanically (no worker) and re-check.
        try:
            ok, files = gitops.sync_remote_branch(wt, branch)
            if ok:
                ok, files, _ = gitops.rebase_onto_capture(wt, ref)
        except gitops.GitError as e:
            ok, files = False, [str(e)]
        run = self.runs.new_run(task.id, "local", mode="rebase")
        run.branch, run.base, run.worktree, run.difficulty = branch, base, str(wt), "easy"
        if not ok:
            # the rebase does not apply cleanly: hand it to the normal revise path, and only then.
            run.status = "failed"
            run.error = f"rebase onto {base} conflicts: {', '.join(files) or 'unknown files'}"
            run.finished_at = now_iso()
            run.save()
            self.events.emit("rebased_stale_base", task.id, base=base, base_sha=tip, resolved=False)
            st.pop("needs_human", None)
            failed = check_failures(self._pre_pr_checks(task, wt, branch, base))
            self._start_check_revise(task, failed, rep, "", note=f" (rebase onto `{base}` did not apply cleanly)")
            rep.transitions.append(f"{task.id} base moved but rebase conflicted; revise")
            return True
        try:
            note = gitops.push(wt, branch, force=True)
            if note:
                self.log(f"{task.id}: {note}")
        except gitops.GitError as e:
            run.status = "failed"
            run.error = str(e)
            run.finished_at = now_iso()
            run.save()
            self.log(f"{task.id}: push after base rebase failed: {e}")
            return False
        run.status = "done"
        run.cost_usd = 0.0
        run.finished_at = now_iso()
        run.diff_stat = gitops.diff_stat(wt, base)
        run.save()
        st["rebases"] = int(st.get("rebases", 0)) + 1
        self.events.emit("run_finished", task.id, run=run.run_id, mode="rebase", cost_usd=0.0, usage={}, status="done")
        failed = check_failures(self._pre_pr_checks(task, wt, branch, base))
        if failed:
            # Still red after the rebase. Route through the normal probe: it re-parks the task
            # (with the new base tip) if the moved base is broken too, or starts a revise round
            # for a failure the branch now owns — and only then.
            self.events.emit("rebased_stale_base", task.id, base=base, base_sha=tip, resolved=False)
            st.pop("needs_human", None)
            self._handle_failed_checks(task, run, wt, branch, base, failed, rep, "")
            return True
        # Green: clear the stop and open or update the PR, all without a worker run.
        st.pop("needs_human", None)
        st.pop("automerge_blocked", None)
        self.events.emit("rebased_stale_base", task.id, base=base, base_sha=tip, resolved=True)
        task.log(f"base branch `{base}` recovered (moved to {tip[:12]}); rebased onto it and the pre-PR "
                 f"checks pass now — continuing without a worker run")
        self.store.save(task)
        self.log(f"{task.id}: base `{base}` recovered; rebased and re-checked green, continuing on its own")
        rep.transitions.append(f"{task.id} rebased onto recovered {base}; checks green")
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
        failed = check_failures(self._pre_pr_checks(task, worktree, branch, base))
        if failed and not stalled:
            # A failing check may be the branch's fault, or a stale/broken base. Probe the base
            # before spending a revise round; only "pass" (rebased onto a moved base, now green)
            # falls through to open the PR.
            if self._handle_failed_checks(task, run, worktree, branch, base, failed, rep, cost) != "pass":
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
                self._transition(task, nxt, f"pushed revision to {existing.url}: {summary}{cost}")
                rep.transitions.append(f"{task.id} -> {nxt.value} (revised)")
            else:
                title = str(result.get("pr_title") or f"{task.id}: {task.title}")
                body = str(result.get("pr_body") or summary or task.body)
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
                self._transition(task, nxt, f"opened {'draft ' if st['pr_draft'] else ''}{pr.url} (base {base}): {summary}{cost}")
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

    def _retry_or_fail(self, task: Task, run: Run, rep: TickReport, reason: str) -> None:
        max_attempts = int(self.cfg.get("max_attempts", 2))
        if run.mode == "revise":
            self._transition(task, Status.FAILED, f"revision failed: {reason}")
            rep.transitions.append(f"{task.id} -> failed")
        elif task.attempts < max_attempts:
            self._transition(task, Status.READY, f"attempt {task.attempts} failed: {reason}; will retry")
            rep.transitions.append(f"{task.id} -> ready (retry)")
        else:
            self._transition(task, Status.FAILED, f"attempt {task.attempts} failed: {reason}; giving up")
            rep.transitions.append(f"{task.id} -> failed")

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
            for c in (st.get("trial", {}).get("contenders") or []):
                if isinstance(c, dict) and c.get("run_id"):
                    owned.add(str(c["run_id"]))
            if t.status == Status.RUNNING:
                latest = self.runs.latest(t.id)
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
            if run.runner == "manual" or run.run_id in owned or not run.process_finished():
                continue
            run.exit_code = run.read_exit_code()
            run.finished_at = now_iso()
            probe = tasks.get(run.task_id) or Task(path=self.store.root, id=run.task_id, title="")
            try:
                runner = self.runner_for(probe, run.runner, run.harness)
                collected = runner.collect(run)
                run.usage = collected.get("usage") or {}
                run.cost_usd = collected.get("cost_usd")
                run.error = collected.get("error") or run.error
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
