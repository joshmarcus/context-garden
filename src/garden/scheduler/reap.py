"""Reap: a finished worker run becomes a push, pre-PR checks, a PR, a retry, a question or a stall."""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from .. import gitops
from ..canonical import CanonicalCheckoutError, configured_root
from ..checks import to_feedback
from ..criteria import (
    amend_criteria,
    apply_verification,
    evidence_gap_diagnosis,
    evidence_gaps,
    normalize_verified,
    parse_criteria,
)
from ..github import GitHubError, mark_garden_comment
from ..model import Status, Task, now_iso
from ..notify import notify
from ..preflight import missing_preflight
from ..proctree import pid_alive
from ..runner.base import Runner, run_temp_dir
from ..runs import Run, RunMutationConflict, RunSaveOutcome
from .human import validate_investigation_report
from .report import TickReport
from .resources import ResourcePressureError


class ReapMixin:
    def _preserve_dirty_worktree(self, task: Task, run: Run, worktree: Path) -> None:
        """Set aside uncommitted work without turning it into a task-branch commit."""
        if not gitops.has_uncommitted_changes(worktree):
            return
        files = gitops.status_lines(worktree)
        name = f"garden:{task.id}:{run.run_id}:reap"
        sha = gitops.stash_all(worktree, name)
        if not sha:
            return
        artifact = {"name": name, "sha": sha, "at": now_iso(), "run": run.run_id,
                    "reason": "reap", "files": files,
                    "restore": f"git stash apply {sha}"}
        if not any(item.get("sha") == sha for item in run.recovery_artifacts):
            run.recovery_artifacts.append(artifact)
        st = self.state.get(task.id)
        stashes = list(st.get("stashes") or [])
        if not any(item.get("sha") == sha for item in stashes):
            stashes.append(artifact)
            st["stashes"] = stashes
        run.save()
        self.events.emit("recovery_preserved", task.id, run=run.run_id, sha=sha,
                         name=name, files=files)
        task.log(f"preserved uncommitted worktree changes from run {run.run_id} outside the PR: "
                 f"`git stash apply {sha}` in {worktree} ({name})")
        self.store.save(task)
        self.log(f"{task.id}: preserved dirty worktree changes from {run.run_id} ({sha[:12]})")

    @staticmethod
    def _no_change_changes_outcome(result: dict[str, Any]) -> bool:
        """Whether a no-change report is really asking to narrow or change the task.

        Ordinary no-change reports are evidence claims: checks and a fresh review can decide
        whether the current head already satisfies the task.  A worker that explicitly leaves
        a criterion undone or declines a requested improvement is instead disputing the
        product outcome; only that disagreement needs a person.
        """
        verified = result.get("verified") or []
        return bool(result.get("improvements_declined")) or any(
            isinstance(row, dict) and row.get("not_done") for row in verified
        )

    def _apply_criteria_amendments(self, task: Task, run: Run, result: dict[str, Any]) -> None:
        """Persist a worker's narrowly-scoped criterion correction before routing its result."""
        body, applied = amend_criteria(task.body, result.get("criteria_amended"))
        if not applied:
            return
        recorded = list(task.extra.get("criteria_amended") or [])
        new_amendments = [
            amendment for amendment in applied
            if not any(
                existing.get("run") == run.run_id
                and all(existing.get(field) == amendment[field] for field in ("index", "text", "reason"))
                for existing in recorded if isinstance(existing, dict)
            )
        ]
        if not new_amendments:
            return
        task.body = body
        for amendment in new_amendments:
            recorded.append({**amendment, "run": run.run_id})
            task.log(
                f"acceptance criterion {amendment['index'] + 1} amended: "
                f"{amendment['reason']}"
            )
            self.events.emit("criteria_amended", task.id, run=run.run_id, **amendment)
        task.extra["criteria_amended"] = recorded
        self.store.save(task)

    def _cleanup_reaped_temp_dirs(self) -> None:
        """Remove disk-backed temp directories once their local run is no longer active."""
        work_dir = self.cfg.work_dir
        for run in self.runs.all_runs():
            # A terminal metadata value alone is not enough: prove the wrapper exited (or
            # wrote its exit_code) before deleting files a still-live descendant may use.
            if run.is_local_execution and run.status != "running" and run.process_finished():
                shutil.rmtree(run_temp_dir(work_dir, run), ignore_errors=True)

    # ---- reap --------------------------------------------------------------
    def reap(self, task: Task, rep: TickReport) -> bool:
        # Only ever the task's own worker-mode run (work/revise/resume/trial/rebase). A review
        # or persona run dispatched for the same task — the poll re-reviewing a fresh push while
        # a revise is still in flight (CG-177) — is left to reap_review/reap_orphaned, so its
        # record can never be read as "no active run found" and send the task back to ready.
        run = self.latest_worker_run(task.id)
        # A standalone watch may have retained this object while another process accepted
        # the worker's finish. Refresh before interpreting its source/receipt identity;
        # Run.save additionally merges that monotonic state if finish lands later.
        if run is not None and run.path.joinpath("run.json").exists():
            run = Run.load(run.path)
        # garden finish is the sole finaliser of manual runs.  Skip the task
        # while a manual run is active (status "running") or while finalize()
        # has completed the run record but has not yet written the task
        # transition (run.finished_at set, task still RUNNING).
        if run is not None and run.runner == "manual":
            submitted = bool((run.env_snapshot or {}).get("pushed_completion_submitted"))
            if not (run.completion_mode == "pushed" and submitted and run.process_finished()):
                return False
        if self._is_unreaped(task, run):
            if self._manual_reserved(task):
                return False
            # The run record already reached a terminal status (written by a
            # prior finalize() call) but the task is still RUNNING: an earlier
            # tick was killed after writing the run's final status but before
            # the task transition / push / PR step completed. Resume from
            # there instead of declaring "no active run" and redispatching a
            # second run on top of the first one's finished work.
            dead_run_reason = str((run.env_snapshot or {}).get("parked_dead_run_reason") or "")
            if dead_run_reason:
                self._retry_or_fail(task, run, rep, dead_run_reason)
            elif run.status == "timeout":
                self._preserve_timeout_worktree(task, run)
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
        finished = run.process_finished() if self._manual_reserved(task) else self._finished_or_timed_out(run, runner)
        if not finished:
            return False
        if run.runner == "remote":
            run = Run.load(run.path)
        if run.status == "timeout":
            if run.mode == "investigation":
                self._fail_investigation(task, run, rep, "investigation agent timed out")
                return True
            self._preserve_timeout_worktree(task, run)
            self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, status="timeout", cost_usd=None)
            self._retry_or_fail(task, run, rep, f"worker {run.error}" if run.error else "worker timed out")
            return True
        self.finalize(task, run, runner, rep)
        return True

    def _preserve_timeout_worktree(self, task: Task, run: Run) -> None:
        """Keep interrupted local edits even when no final result is available."""
        if run.runner != "local":
            return
        worktree = Path(run.worktree) if run.worktree else self.worktree_for(task)
        if not worktree.exists():
            return
        try:
            self._preserve_dirty_worktree(task, run, worktree)
        except gitops.GitError as exc:
            self.log(f"{task.id}: could not preserve timed-out worktree changes: {exc}")

    def _finished_or_timed_out(self, run: Run, runner: Runner) -> bool:
        if run.runner == "ssh":
            hold = runner.reconcile(run)
            if hold:
                task = self.store.tasks().get(run.task_id)
                st = self.state.get(run.task_id)
                if task is not None and st.get("ssh_recovery_hold") != hold:
                    st["ssh_recovery_hold"] = hold
                    self._set_needs_human(task, "ssh_recovery", hold)
                    run.error = hold
                    run.save()
                    task.log(f"SSH recovery held: {hold}; remote work and checkout ownership retained")
                    self.store.save(task)
                    self.events.emit("ssh_recovery_held", task.id, run=run.run_id, reason=hold)
                    if (run.path / "ssh-rejected.json").exists():
                        # The remote launcher authoritatively refused this new run before
                        # starting a worker. An explicit retry after repair is safe; an
                        # uncertain or live owner never takes this path.
                        run.status = "failed"
                        run.finished_at = now_iso()
                        run.save()
                        self._transition(task, Status.FAILED, hold, needs_human=True)
                    self.state.save()
                return False
            if run.env_snapshot.get("ssh_tmux_session"):
                # Remote supervision enforces timeout and descendant cleanup. A local
                # timeout must never invent completion while the host is unreachable.
                return run.process_finished()
        if run.process_finished():
            return True
        if not runner.detached:
            return False
        # The dispatch-time value survives config edits and scheduler restarts. Check
        # commands have their own seconds-based timeout and never inherit this budget.
        snapshot = run.env_snapshot or {}
        timeout_min = float(snapshot.get("execution_timeout_minutes", 0 if run.mode == "check"
                            else self.cfg.product_timeout_minutes(
                                str(snapshot.get("product") or ""))) or 0)
        elapsed = run.execution_minutes() if run.runner == "remote" else run.elapsed_minutes()
        if timeout_min and elapsed > timeout_min + 5:
            run.kill()
            run.status = "timeout"
            run.finished_at = now_iso()
            run.error = "timed out"
            if run.runner == "remote":
                # Revoke the accepted generation before a retry can be dispatched.  A late
                # heartbeat/result is then rejected even when its former lease had time left.
                run.lease_token = ""
                run.lease_expires_at = ""
            try:
                run.save()
            except RunMutationConflict:
                return False
            return True
        admission_reason = run.supervisor_waiting_reason()
        if admission_reason is not None:
            admission_wait_min = float(self.cfg.get("resources.admission_wait_minutes", 30) or 0)
            waiting_since = run.supervisor_waiting_since()
            waiting_minutes = (
                max(0.0, (dt.datetime.now(dt.UTC) - waiting_since).total_seconds() / 60)
                if waiting_since is not None else 0.0
            )
            if admission_wait_min and waiting_minutes >= admission_wait_min:
                run.kill()
                run.status = "timeout"
                run.finished_at = now_iso()
                run.error = f"admission wait {round(waiting_minutes)} min ({admission_reason})"
                try:
                    run.save()
                except RunMutationConflict:
                    return False
                return True
            # A waiting supervisor has made its reason visible. It is awaiting operator-owned
            # resource admission, not silently failing to make code progress.
            return False
        idle_kill_min = float(self.cfg.get("idle_kill_minutes", 0) or 0)
        idle_min = run.idle_minutes() if idle_kill_min else 0.0
        if idle_kill_min and idle_min >= idle_kill_min:
            run.kill()
            run.status = "timeout"
            run.finished_at = now_iso()
            run.error = f"idle {round(idle_min)} min (no output or file change)"
            if run.runner == "remote":
                # Idle expiry fences the claim just as the overall deadline does.
                run.lease_token = ""
                run.lease_expires_at = ""
            try:
                run.save()
            except RunMutationConflict:
                return False
            return True
        return False

    def _restore_investigation_task_status(self, task: Task, inv: dict[str, Any], note: str) -> None:
        raw = str(inv.get("task_status") or Status.CHANGES_REQUESTED.value)
        try:
            target = Status(raw)
        except ValueError:
            target = Status.CHANGES_REQUESTED
        self._transition(task, target, note, needs_human=True)

    def _fail_investigation(self, task: Task, run: Run, rep: TickReport, reason: str) -> None:
        st = self.state.get(task.id)
        inv = st.get("investigation") if isinstance(st.get("investigation"), dict) else {}
        inv.update({"status": "failed", "error": reason, "failed_at": now_iso(), "run_id": run.run_id,
                    "transcript": str(run.path / "final.md"), "cost_usd": run.cost_usd})
        st["investigation"] = inv
        self._set_needs_human(task, "investigation", f"investigation agent failed: {reason}; retry or take it as operator")
        self._restore_investigation_task_status(task, inv, f"investigation agent failed: {reason}")
        self.events.emit("investigation_failed", task.id, run=run.run_id, reason=reason, cost_usd=run.cost_usd)
        self.state.save()
        rep.transitions.append(f"{task.id} investigation failed")

    def _finalize_investigation(self, task: Task, run: Run, rep: TickReport,
                                collected: dict[str, Any]) -> None:
        """Persist a read-only diagnosis independently of implementation revisions."""
        result = run.result or {}
        report = result.get("investigation_report")
        if collected.get("env_error"):
            self._fail_investigation(task, run, rep, str(collected.get("error") or "investigation environment unavailable"))
            return
        if result.get("status") != "done":
            self._fail_investigation(task, run, rep, "agent returned no complete investigation report")
            return
        try:
            report = validate_investigation_report(report)
        except RuntimeError as exc:
            self._fail_investigation(task, run, rep, f"agent returned an invalid investigation report: {exc}")
            return
        report.setdefault("source_identities", [run.run_id, task.branch or task.default_branch()])
        report.setdefault("observed_behavior", report["likely_cause"])
        report.setdefault("intended_behavior", "The task should progress through Garden's configured workflow.")
        report.setdefault("impact", "Not separately established by the investigation.")
        report.setdefault("corrective_action", report["recommendation"])
        st = self.state.get(task.id)
        inv = st.get("investigation") if isinstance(st.get("investigation"), dict) else {}
        inv.update({"status": "report_ready", "report": report, "completed_at": now_iso(),
                    "run_id": run.run_id, "transcript": str(run.path / "final.md"),
                    "cost_usd": run.cost_usd, "usage": run.usage})
        discoveries = report.get("discovered") or []
        if isinstance(discoveries, list):
            from ..deepdives import redact_secrets

            diagnosis = "\n\n".join([
                "## Deep dive diagnosis",
                str(report.get("likely_cause") or "Not established."),
                "## Evidence",
                "\n".join(f"- {item}" for item in report.get("evidence") or []),
                "## Required outcome",
                str(report.get("corrective_action") or report.get("recommendation") or ""),
            ])
            diagnosis = redact_secrets(diagnosis)
            for item in discoveries:
                if isinstance(item, dict) and str(item.get("kind") or "task") == "task":
                    item["body"] = (str(item.get("body") or "").rstrip() + "\n\n" + diagnosis).strip()
            run.result["discovered"] = discoveries
            run.result["_discovery_context"] = diagnosis
            self._file_discovered(task, run, run.result)
            linked = list(dict.fromkeys(run.result.get("_linked_tasks") or []))
            for task_id in linked:
                linked_state = self.state.get(task_id)
                linked_state["investigation_handoff"] = {
                    "request_id": inv.get("request_id") or f"{task.id}-{run.run_id}",
                    "origin_task_id": task.id,
                    "origin_pr": task.pr or "",
                    "diagnosis": diagnosis,
                    "fallback_feedback": str(inv.get("feedback_markdown") or ""),
                    "report": f"/investigations/{task.id}/{run.run_id}/report.html",
                }
            report["links"] = [*report.get("links", []), *(f"/tasks/{task_id}" for task_id in linked)]
        from ..deepdives import publish_report, save_report

        md_path, html_path = save_report(run.path, run.run_id, str(inv.get("reason") or ""), report)
        inv["report_paths"] = {"markdown": str(md_path), "html": str(html_path)}
        try:
            inv["publication"] = publish_report(self.store.root, run.run_id, md_path, html_path)
        except Exception as exc:  # publication retries must not rerun or discard the diagnosis
            inv["publication"] = {"status": "failed", "error": str(exc), "failed_at": now_iso()}
        st["investigation"] = inv
        run.status = "done"
        run.save()
        self._set_needs_human(task, "investigation_report", "investigation report ready; choose the next task action")
        self._restore_investigation_task_status(task, inv, "investigation report ready; task remains paused")
        self.events.emit("investigation_reported", task.id, run=run.run_id,
                         recommendation=report["recommendation"], cost_usd=run.cost_usd)
        self.state.save()
        rep.transitions.append(f"{task.id} investigation report ready")

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
        result = run.result
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        # Persist the collected outcome (finished_at, usage, cost, result) before the fence
        # check, push and PR steps below. A kill during any of them then leaves finished_at
        # set, so the restart recognises this as an interrupted finalize (see _is_unreaped) and
        # resumes it with resumed=True rather than finalizing a second time. run_finished is
        # emitted once per run, only after this terminal save — so a kill during the fence check
        # can no longer re-emit it (CG-198). A resumed finalize skips the emit because the first
        # pass already made it, so the run's cost is never counted twice (CG-153).
        if run.save() is RunSaveOutcome.SUPERSEDED:
            # The collector raced a newer worker completion or claim generation. Nothing
            # derived from the stale collection may advance until a later reap finalizes
            # the durable generation.
            return
        if final_text and not (run.path / "final.md").exists():
            (run.path / "final.md").write_text(final_text)
        if not resumed:
            self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, harness=run.harness, model=run.model, pool_member=run.pool_member,
                             status=str(result.get("status") or ("error" if run.error else "no_result")),
                             cost_usd=run.cost_usd, usage=run.usage, exit_code=run.exit_code)

        # Manual mode is a safe-boundary reservation, not a process cancellation. Collect the
        # completed worker so it no longer consumes an active slot, but leave the task RUNNING
        # and its finished_at marker intact. Once the reservation is removed, reap() recognizes
        # this as an unreaped completion and resumes the ordinary finalize path.
        if self._manual_reserved(task):
            run.status = "done" if result and run.exit_code in (0, None) else "failed"
            run.save()
            return
        self._apply_criteria_amendments(task, run, result)

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

        if run.mode == "investigation":
            self._finalize_investigation(task, run, rep, collected)
            return

        # A result only vouches for commits.  Preserve every uncommitted path before any
        # result branch (including missing-result retry) can recover the task, so it cannot
        # later be swept into a PR by a repeat reap or revise round.
        worktree = Path(run.worktree) if run.worktree else self.worktree_for(task)
        if not runner.remote and worktree.exists():
            try:
                self._preserve_dirty_worktree(task, run, worktree)
            except gitops.GitError as exc:
                run.status = "failed"
                run.error = f"could not preserve dirty worktree: {exc}"
                run.save()
                self._retry_or_fail(task, run, rep, run.error)
                return

        if collected.get("env_error"):
            # A quota/spend-limit message from the harness's own account, not the worker's
            # doing: close the run without counting an attempt, pause dispatch for this
            # harness (a cheap probe resumes it later, see QuotaMixin), and put the task
            # straight back to ready instead of burning it toward failed.
            self._handle_quota_env_error(task, run, rep, collected)
            return

        # A successful harness-owned completion breaks the consecutive environment-error
        # streak; ordinary worker failures are handled by the normal attempt cap below.
        self.state.get(task.id).pop("consecutive_env_errors", None)
        self.state.save()
        if run.exit_code not in (0, None) and not result:
            run.status = "failed"
            run.save()
            self._retry_or_fail(task, run, rep, f"worker exited {run.exit_code}: {run.error[:200]}")
            return
        status = str(result.get("status", "")).lower()
        # A done worker may give one clear overall attestation instead of mechanically
        # repeating every frozen criterion. Persist its expansion before the pre-PR gate,
        # PR renderer, or review can read this result; explicit rows (including not_done)
        # remain intact and are never rewritten.
        if status == "done":
            snapshot = run.env_snapshot or {}
            criteria = list(snapshot["criteria"]) if "criteria" in snapshot else parse_criteria(task.body)
            normalized = normalize_verified(criteria, result)
            if normalized is not None and normalized != result.get("verified"):
                result["verified"] = normalized
                run.result = result
                run.save()
        # A headless worker can finish its commits but lose its final result while waiting for
        # an unattended command.  The worktree is the durable record in that case: salvage its
        # commits before treating an optional result/checklist omission as a failed attempt.
        # A reported status still wins, so `blocked` and the other deliberate outcomes below
        # are unchanged. Missing results with no usable commits still follow the ordinary
        # retry/failure path below.
        if not status and run.mode in ("work", "revise") and not runner.remote and worktree.exists():
            try:
                base = run.base or self.base_for(task)
                start = run.start_head or gitops.base_ref(worktree, base)
                ahead = int(gitops.git("rev-list", "--count", f"{start}..HEAD", cwd=worktree).strip() or 0)
            except gitops.GitError:
                ahead = 0
            if ahead:
                plural = "s" if ahead != 1 else ""
                summary = f"result missing; {ahead} commit{plural} reaped from the worktree"
                last_message = " ".join(final_text.split()) or "(no final message captured)"
                task.log(f"{summary}; worker's last message: {last_message!r}")
                self.store.save(task)
                self.events.emit("result_missing_reaped", task.id, run=run.run_id, commits=ahead,
                                 last_message=last_message)
                self.log(f"{task.id}: {summary}")
                result = {"summary": summary}
                run.result = result
                run.save()
        if not result:
            run.status = "failed"
            run.save()
            self._retry_or_fail(task, run, rep, f"no GARDEN_RESULT in worker output ({run.error[:200] or 'see final.md'})")
            return
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
            # An immediate answer needs the question and its resume identity.
            self.state.save()
            self.events.emit("waiting_human", task.id, question=question, run=run.run_id)
            self._transition(task, Status.WAITING_HUMAN, f"worker asks: {question}{cost}")
            rep.transitions.append(f"{task.id} -> waiting_human")
            return

        if (status == "no_change" and run.completion_mode != "pushed"
                and not self._no_change_changes_outcome(result)):
            run.status = status
            run.save()
            self._file_discovered(task, run, result)
            reason = str(result.get("reason") or result.get("summary") or "(no reason given)")
            st = self.state.get(task.id)
            st.pop("decision", None)
            st.pop("needs_human", None)
            # Preserve the finding identities from the review that prompted this round. The
            # fresh review must be able to recognise a rediscovered actionable finding as the
            # same one, so the ordinary bounded repeat/stall policy still applies.
            st["no_change_reconciliation"] = {
                "head": str(st.get("head_sha") or ""),
                "run": run.run_id,
            }
            task.log(f"worker found no change to make: {reason}; reconciling with checks and a fresh review")
            self.store.save(task)
            self.events.emit("no_change_reconciled", task.id, reason=reason, run=run.run_id)
            base = run.base or self.base_for(task)
            branch = run.branch or task.branch or task.default_branch()
            worktree = Path(run.worktree) if run.worktree else self.worktree_for(task)
            task.branch = branch
            self._after_push(task, run, worktree, branch, base, result, rep, cost, check_stall=False)
            rep.transitions.append(f"{task.id} no-change -> verification")
            return

        if (status == "wont_do" or (status == "no_change" and
                (run.completion_mode != "pushed" or self._no_change_changes_outcome(result)))):
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
            # Readers can observe the task status before this tick finishes.
            # Persist its decision before publishing the waiting status.
            self.state.save()
            self.events.emit("decision", task.id, decision=status, reason=reason, run=run.run_id)
            word = "won't do" if status == "wont_do" else "nothing to change"
            self._transition(task, Status.WAITING_HUMAN, f"worker says {word}: {reason}{cost}", needs_human=True)
            rep.transitions.append(f"{task.id} -> waiting_human ({status})")
            return

        self._file_discovered(task, run, result)

        if status == "no_change":
            reason = str(result.get("reason") or result.get("summary") or "(no reason given)")
            st = self.state.get(task.id)
            st["no_change_reconciliation"] = {
                "head": str(result.get("pushed_sha") or st.get("head_sha") or ""),
                "run": run.run_id,
            }
            task.log(f"worker found no change to make: {reason}; reconciling the declared pushed head")
            self.store.save(task)
            self.events.emit("no_change_reconciled", task.id, reason=reason, run=run.run_id)

        missing = missing_preflight(result.get("pre_flight"))
        if missing and bool((run.env_snapshot or {}).get("requires_preflight")):
            # The rubric helps an agent choose checks; its serialization is not itself a
            # product outcome. Preserve the omission for review without turning otherwise
            # usable source into an evidence-only revision.
            st = self.state.get(task.id)
            advisories = st.setdefault("verification_advisories", [])
            if not any(isinstance(item, dict) and item.get("run") == run.run_id
                       and item.get("kind") == "pre_flight" for item in advisories):
                advisories.append({"kind": "pre_flight", "run": run.run_id, "missing": missing})
            task.log("review pre-flight advisory: omitted optional items: " + ", ".join(missing))
            self.store.save(task)
            self.events.emit("verification_advisory", task.id, run=run.run_id,
                             advisory="pre_flight", missing=missing)
            self.state.save()

        base = run.base or self.base_for(task)
        branch = run.branch or task.branch or task.default_branch()
        repo = self.repo_for(task)

        if runner.remote or run.completion_mode == "pushed":
            wt = self.worktree_for(task)
            canonical = None
            try:
                # SSH/pull workers publish their result remotely, but checks may still use
                # an explicitly provisioned local canonical checkout.  Claim and preflight
                # that checkout before even fetching through it: the legacy materialisation
                # path below must never reset operator work or a drifted branch.
                if configured_root(self.cfg.product_checkout(task.product), self.store.root) is not None:
                    local_runner = self.runner_for(task, "local")
                    canonical = self.prepare_canonical_run(task, run, local_runner, branch, base)
                    if canonical is not None:
                        wt = canonical
                gitops.fetch(repo)
            except (gitops.GitError, CanonicalCheckoutError) as e:
                run.status = "failed"
                run.error = f"could not prepare local canonical checkout: {e}"
                run.save()
                self._retry_or_fail(task, run, rep, run.error)
                return
            try:
                if (run.env_snapshot or {}).get("remote_branch_promoted"):
                    # A prior finalize pass promoted the exact accepted result, then
                    # deferred local checkout staging for storage pressure.
                    ahead = 1
                elif run.pushed_ref:
                    staged = f"refs/remotes/origin/{run.pushed_ref.removeprefix('refs/heads/')}"
                    remote_head = gitops.git("rev-parse", "--verify", staged, cwd=repo).strip()
                    if not run.pushed_head or remote_head != run.pushed_head:
                        raise gitops.GitError("the staged commit does not match the head reported by the worker")
                    ahead = int(gitops.git("rev-list", "--count", f"{gitops.base_ref(repo, base)}..{staged}", cwd=repo).strip() or 0)
                    # Empty start_head deliberately means "the branch must not exist".
                    # This lease protects promotion from both stale workers and any other
                    # writer that moved (or created) the task branch during the run.
                    if ahead:
                        gitops.git("push", f"--force-with-lease={branch}:{run.start_head}", "origin",
                                   f"{run.pushed_head}:refs/heads/{branch}", cwd=repo)
                        gitops.fetch(repo)
                else:
                    # SSH runners push the task branch themselves and predate the pull-based
                    # worker's lease-specific staging transport.
                    remote_head = gitops.git("rev-parse", "--verify", f"origin/{branch}", cwd=repo).strip()
                    if run.pushed_head and remote_head != run.pushed_head:
                        raise gitops.GitError("the pushed branch head does not match the head reported by the worker")
                    ahead = int(gitops.git("rev-list", "--count", f"{gitops.base_ref(repo, base)}..origin/{branch}", cwd=repo).strip() or 0)
            except gitops.GitError as e:
                ahead = 0
                if run.pushed_head:
                    run.error = str(e)
            if ahead == 0 and not (run.completion_mode == "pushed" and status == "no_change"):
                run.status = "failed"
                run.error = "no commits pushed"
                run.save()
                self._record_implementation_failure(
                    task, "missing_expected_changes", run.run_id,
                    "remote worker finished without pushing commits",
                )
                self._retry_or_fail(task, run, rep, "remote worker finished without pushing commits")
                return
            run.env_snapshot["remote_branch_promoted"] = True
            run.status = "done"
            run.save()
            try:
                with self._local_staging_admission("remote result checkout materialization"):
                    if wt.exists():
                        gitops.git("fetch", "origin", cwd=wt)
                        gitops.git("reset", "-q", "--hard", f"origin/{branch}", cwd=wt)
                    else:
                        gitops.prepare_worktree(repo, wt, branch, base)
            except ResourcePressureError as e:
                # The result and promoted branch are already durable.  Leave the task and
                # terminal run untouched so the normal interrupted-finalize path retries
                # this controller staging step after storage recovers.
                self.log(f"{task.id}: {e}; completed remote result remains queued for staging")
                return
            except gitops.GitError as e:
                run.status = "failed"
                run.error = f"could not materialise local worktree: {e}"
                run.save()
                self._retry_or_fail(task, run, rep, run.error)
                return
            task.branch = branch
            self._after_push(task, run, wt, branch, base, result, rep, cost,
                             check_stall=status != "no_change")
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
            self._record_implementation_failure(
                task, "missing_expected_changes", run.run_id,
                "worker finished with no commits",
            )
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
            if self.external_stack_owner(task):
                reason = (f"external stack owner changed `{e.branch}` while this run was active "
                          f"(expected {e.expected[:12] or '?'}, now {e.actual[:12] or '?'}); "
                          "garden did not rebase or force-push it")
                self.events.emit("lease_rejected", task.id, run=run.run_id, branch=e.branch,
                                 expected=e.expected, actual=e.actual, owner="external")
                self._set_needs_human(task, "external_head_changed", reason)
                self._transition(task, Status.WAITING_HUMAN, reason)
                rep.transitions.append(f"{task.id} waiting_human (external head changed)")
                return
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

    def _check_settings(self, task: Task, stage: str = "pre_pr") -> dict[str, Any]:
        """Resolve and snapshot the check contract for one product and stage.

        All callers use this instead of reading ``checks`` directly.  The detached runner and
        every continuation receive this returned mapping, preserving the product setup
        environment, a check's own environment overrides, and the timeout selected at start.
        """
        product_checks = self.cfg.product_checks(task.product)
        setup = self.cfg.product_setup(task.product)
        specs = list(product_checks.get(stage, []) or [])
        if stage == "pre_pr" and not specs and not product_checks["pre_pr_overridden"]:
            for name in ("test", "lint"):
                cmd = str(setup.get(name) or "").strip()
                if cmd:
                    specs.append({"name": name, "command": cmd})
        validation = self.cfg.product_validation(task.product)
        from ..checks import is_publishing_ci_helper
        publishing_allowed = setup.get("worker_push") is True and validation["provider"] in ("legacy", "actions")
        if not publishing_allowed:
            specs = [
                {**spec, "requires_worker_push": True}
                if is_publishing_ci_helper(str(spec.get("command") or ""))
                else spec
                for spec in specs
            ]
        # A task can require a configured check by name.  It is deliberately a name, rather
        # than a command from task text: check commands run branch code and stay garden config.
        from ..criteria import required_evidence
        required = {r["name"] for r in required_evidence(task.body, task.extra.get("requires")) if r["kind"] == "check"}
        # Required check evidence names pre-PR gates.  CI analysers are a separate contract
        # that diagnoses an already-failed CI rollup and must not acquire a command merely
        # because a task requires that command before its PR opens.
        if stage == "pre_pr" and required:
            configured = list(product_checks["pre_pr"] or [])
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
        return {"specs": specs, "setup": setup,
                "timeout": product_checks["timeout_seconds"], "config": deepcopy(self.cfg.data)}

    def _pre_pr_specs(self, task: Task) -> list[dict[str, Any]]:
        """Compatibility wrapper for callers that need only the resolved pre-PR specs."""
        return list(self._check_settings(task)["specs"])

    def _run_specs_in(self, task: Task, specs: list[dict[str, Any]], worktree: Path, branch: str, base: str) -> list[dict[str, Any]]:
        """Prepare `worktree`'s environment, then run `specs` there, synchronously — the same
        `run_check_job` the detached check run uses, so there is one check-running implementation.
        Off the tick path now (a check runs as a detached run record; see CheckRunMixin); kept as
        the synchronous entry point exercised by the setup tests."""
        from ..checkrun import run_check_job

        if not specs or not worktree.exists():
            return []
        settings = self._check_settings(task)
        return run_check_job({"specs": specs, "ctx": self.check_ctx(task, branch, base, worktree),
                              "cwd": str(worktree), **settings})

    def _pre_pr_checks(self, task: Task, worktree: Path, branch: str, base: str) -> list[dict[str, Any]]:
        results = self._run_specs_in(task, self._pre_pr_specs(task), worktree, branch, base)
        for r in results:
            self.events.emit("check", task.id, stage="pre_pr", name=r.get("name"), status=r.get("status"), summary=r.get("summary", ""))
        return results

    def _start_check_revise(self, task: Task, failed: list[dict[str, Any]], rep: TickReport, cost: str, note: str = "",
                            is_rebase: bool = False, feedback_note: str = "") -> None:
        """Queue a revise round (or hand off to a human at the cap) for a pre-PR check the branch
        actually owns. Mirrors the historic inline behaviour of `_after_push`. `is_rebase` marks a
        stale-base rebase that failed to apply cleanly (CG-131): mechanical bookkeeping, not a
        revision round, so it skips the cap and never hands the task to a human on its own."""
        st = self.state.get(task.id)
        names = ", ".join(str(f.get("name")) for f in failed)
        feedback = to_feedback(failed, "pre-PR check")
        if feedback_note:
            feedback = f"{feedback}\n\n{feedback_note}" if feedback else feedback_note
        # A pre-PR failure can be the first reason this worker is sent back, before a
        # review run exists to add the frozen-criteria delta. Keep the contract that
        # failed worker received with the mechanical feedback, so a task-file edit made
        # while it worked is actionable in the immediately following revise brief.
        for worker_run in reversed(self.runs.runs_for(task.id)):
            if worker_run.mode not in ("work", "revise", "resume"):
                continue
            criteria_note = self._criteria_changed_note(task, worker_run)
            if criteria_note:
                feedback = f"{feedback}\n\n{criteria_note}" if feedback else criteria_note
            break
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
            if not self.cfg.revision_policy()["enabled"] and int(st.get("revisions", 0)) >= max_rev:
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

    def _last_worker_preflight(self, task: Task) -> list[dict[str, Any]] | None:
        """The most recent complete pre-flight, for reviews re-queued without their worker run.

        A poll or restart can dispatch a review from the PR alone. Keep the same author
        checklist visible in that path as in the normal work-run handoff.
        """
        for r in reversed(self.runs.runs_for(task.id)):
            if r.mode in ("work", "revise", "resume") and isinstance(r.result, dict):
                pre_flight = r.result.get("pre_flight")
                if isinstance(pre_flight, list):
                    return pre_flight
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
                with self._local_staging_admission("base recovery checkout materialization"):
                    gitops.prepare_worktree(repo, wt, branch, base)
            gitops.fetch(wt)
            ref = gitops.base_ref(wt, base)
            tip = gitops.rev_parse(wt, ref)
        except ResourcePressureError as e:
            self.log(f"{task.id}: {e}; still waiting for `{base}`")
            return False
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
        if worktree.exists():
            try:
                run.env_snapshot["review_source_head"] = gitops.head_sha(worktree)
                run.save()
            except gitops.GitError:
                pass
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
            self._stall(
                task,
                rep,
                f"revise run {run.run_id} produced no change to the diff or PR description",
                implementation_failure=bool(
                    (run.env_snapshot or {}).get("implementation_failure_eligible", True)
                ),
            )

    def _open_or_update_pr(self, task: Task, run: Run, branch: str, base: str, result: dict[str, Any],
                           rep: TickReport, cost: str) -> None:
        slug = self.slug_for(task)
        summary = str(result.get("summary") or "")
        # Verification belongs to the contract the worker received.  The task
        # may have been edited while the run was in flight; that delta is a
        # separate revise note, not a retroactive requirement for this PR.
        snapshot = run.env_snapshot or {}
        criteria = list(snapshot["criteria"]) if "criteria" in snapshot else parse_criteria(task.body)
        verified = result.get("verified")
        gaps = evidence_gaps(criteria, verified)
        if gaps:
            self._start_check_revise(task, [{
                "name": "acceptance criteria evidence",
                "status": "fail",
                "summary": "no evidence or reason given for: " + "; ".join(gaps),
                "details": evidence_gap_diagnosis(criteria, verified, run.run_id),
            }], rep, cost)
            return
        st = self.state.get(task.id)
        if run.pool_member:
            st["pr_pool_member"] = run.pool_member
        # The source identity changes when the branch changes, independently of GitHub's
        # PR updated_at. Status providers and queued reviews key exclusively to this head.
        try:
            st["head_sha"] = gitops.head_sha(Path(run.worktree))
        except gitops.GitError:
            st["head_sha"] = ""
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
                defer_triage = nxt == Status.AWAITING_TRIAGE and self._review_round_pending(st, task.product)
                if defer_triage:
                    st["pending_triage_notify"] = True
                self._transition(task, nxt, f"pushed revision to {existing.url}: {summary}{cost}", notify_now=not defer_triage)
                rep.transitions.append(f"{task.id} -> {nxt.value} (revised)")
            else:
                title = str(result.get("pr_title") or f"{task.id}: {task.title}")
                body = apply_verification(str(result.get("pr_body") or summary or task.body), criteria, verified)
                footer = f"\n\n---\nTask `{task.id}` from the context garden ({task.product}/{task.phase})."
                if run.pool_member:
                    footer += f" Produced by pool member `{run.pool_member}`."
                if st.get("stack_parent"):
                    footer += f" Stacked on `{st['stack_parent']}` (targets `{base}` until it merges)."
                if st.get("discovered_ids"):
                    footer += " Discovered work: " + ", ".join(f"`{i}`" for i in st["discovered_ids"]) + "."
                pr = self.github.create_pr(
                    slug, branch, base, title, body + footer,
                    draft=bool(self.effective("github.draft_pr", False, task.product)),
                    reviewers=list(self.cfg.get("github.reviewers", []) or []),
                )
                task.pr = pr.url
                st["pr_number"] = pr.number
                st["revisions"] = 0
                st["review_rounds"] = 0
                st.pop("review_loop_friction", None)
                st.pop("review_heads", None)
                st.pop("review_feedback_history", None)
                st["pr_draft"] = bool(self.effective("github.draft_pr", True, task.product))
                self.events.emit("pr_opened", task.id, pr=pr.url, base=base, stacked_on=st.get("stack_parent", ""),
                                 draft=st["pr_draft"], pool_member=run.pool_member)
                nxt = self._pr_status(task)
                defer_triage = nxt == Status.AWAITING_TRIAGE and self._review_round_pending(st, task.product)
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
        if kind not in {"resource", "materialization"}:
            self._pause_for_env_error(run, collected)
        if kind == "resource":
            note = "environment stop (resource): the host exhausted memory or temporary storage; branch not blamed and attempt not counted"
        elif kind == "materialization":
            note = "environment stop (materialization): the worker could not prepare an isolated checkout; branch not blamed and attempt not counted"
        else:
            note = f"environment stop ({kind}): {kind} limit hit on {harness_name or 'the harness'}; not counted as an attempt"
        if harness_name and kind not in {"resource", "materialization"}:
            note += f"; dispatch paused for {harness_name} until a probe succeeds"

        st = self.state.get(task.id)
        env_errors = int(st.get("consecutive_env_errors", 0)) + 1
        cap = max(1, int(self.cfg.get("max_consecutive_env_errors", 3) or 3))
        st["consecutive_env_errors"] = env_errors
        if env_errors >= cap:
            reason = f"{env_errors} consecutive environment errors; retry cap reached"
            self._set_needs_human(task, "env_error", reason)
            self.state.save()
            self.events.emit("needs_human", task.id, stop_kind="env_error", reason=reason)
            self._transition(task, Status.WAITING_HUMAN, reason)
            notify(self.cfg.data, task.id, "waiting_human", reason, task.pr or "")
            rep.transitions.append(f"{task.id} -> waiting_human (env_error cap)")
            return
        snap = run.env_snapshot or {}
        if run.mode == "revise" and task.pr:
            st["pending_feedback"] = snap.get("pending_feedback", "")
            if "implementation_failure_eligible" in snap:
                st["pending_feedback_implementation_failure"] = bool(
                    snap["implementation_failure_eligible"]
                )
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
        if harness_name and kind != "resource":
            self.state.get(task.id)["harness_hold"] = harness_name
            self.state.save()
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
            note = f"rebase conflict run {run.run_id} did not finish: {reason}; will retry"
            task.log(note)
            self.store.save(task)
            self.events.emit("rebase_retry", task.id, run=run.run_id, cause=reason, retry=retries + 1)
            self.state.save()
            self._transition(task, Status.CHANGES_REQUESTED, note)
            rep.transitions.append(f"{task.id} -> changes_requested (rebase retry)")
            return
        files = list(st.get("rebase_files", [])) or list(st.get("rebase_retry_files", []))
        conflict = ", ".join(str(p) for p in files if p) or "the rebase conflict"
        note = f"rebase conflict run {run.run_id} did not finish: {reason}; retry also failed; needs human to resolve {conflict}"
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

    @staticmethod
    def _accepted_terminal_remote_result(run: Run) -> bool:
        """Whether a remote completion belongs to the run's last accepted generation.

        The authenticated finish endpoint writes ``remote_result.json`` and saves its
        receipt before writing ``exit_code`` last.  New records also retain a hashed claim
        history entry; require it to describe the still-current token/ref/host.  Legacy
        records predate that history, so their durable host, token and staging ref are the
        strongest persisted generation identity.
        """
        if run.runner != "remote" or not (run.path / "remote_result.json").exists():
            return False
        try:
            exit_code_text = (run.path / "exit_code").read_text().strip()
            if not exit_code_text:
                return False
            int(exit_code_text)
        except (OSError, UnicodeError, ValueError):
            return False
        if not (run.host and run.lease_token and run.pushed_ref):
            return False
        if not run.claim_history:
            return True
        latest = run.claim_history[-1]
        return bool(
            run.final_received_at
            and latest.get("host") == run.host
            and latest.get("pushed_ref") == run.pushed_ref
            and latest.get("lease_token_sha256")
            == hashlib.sha256(run.lease_token.encode()).hexdigest()
        )

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
            if run.runner == "ssh":
                # Lost collectors remain owned remote work, including after the task's
                # status changes. They are reconciled, never reaped as vanished workers.
                task = tasks.get(run.task_id)
                if task is None and not run.process_finished():
                    continue
                if run.preparer_pid is not None and pid_alive(run.preparer_pid):
                    continue
                if task is not None and not self._finished_or_timed_out(
                    run, self.runner_for(task, run.runner, run.harness)
                ):
                    continue
            task = tasks.get(run.task_id)
            terminal_remote = bool(
                run.runner == "remote" and task is not None and task.status.terminal
                and self._accepted_terminal_remote_result(run)
            )
            if run.runner == "remote" and not terminal_remote:
                continue
            # Dispatch reserves a record before it prepares a worktree and records the
            # worker pid. A concurrent tick must not interpret that intentional window
            # as a worker that never started. Its owner is persisted, so a later
            # scheduler can still reclaim a reservation abandoned by a crashed preparer.
            if run.pid is None and run.preparer_pid is not None and pid_alive(run.preparer_pid):
                continue
            no_exit_code = not (run.path / "exit_code").exists()
            process_missing = run.pid is None
            process_dead = not process_missing and run.process_finished()
            # The local supervisor writes its completion signal immediately before it
            # exits.  A tick can read the missing signal, then observe the exited process
            # after that write.  Treat that completed run as owned by normal reap rather
            # than turning a successful worker into a vanished process.
            if no_exit_code and not process_missing and process_dead:
                no_exit_code = not (run.path / "exit_code").exists()
            if no_exit_code and (process_missing or process_dead):
                # A recovery operation with no worker pid is durable pending work, not live
                # work.  Its client replays the same key after a server restart, which resumes
                # preparation on this record; never close it or let a tick duplicate it.
                if run.idempotency_key and run.status in ("requested", "preparing"):
                    continue
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
                if task is not None and self._manual_reserved(task):
                    run.env_snapshot = dict(run.env_snapshot or {})
                    run.env_snapshot["parked_dead_run_reason"] = reason
                run.save()
                self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode,
                                 harness=run.harness, model=run.model, status="failed",
                                 cost_usd=None, usage={}, error=reason, orphaned=True)
                rep.transitions.append(f"{run.task_id} {run.mode} run {run.run_id} failed ({reason})")
                if (task is not None and not self._manual_reserved(task)
                        and task.status == Status.RUNNING
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
                if terminal_remote:
                    run.result = collected.get("result") or {}
                    run.session_id = str(collected.get("session_id") or run.session_id or "")
                run.usage = collected.get("usage") or {}
                run.cost_usd = collected.get("cost_usd")
                run.model = str(collected.get("model") or run.model)
                run.error = collected.get("error") or run.error
                if collected.get("missing_price"):
                    self.log(f"{run.task_id}: no price configured for model {collected['missing_price']!r}; cost_usd left null")
            except Exception as e:  # noqa: BLE001
                if terminal_remote:
                    rep.errors.append(
                        f"{run.task_id}: terminal remote result {run.run_id} could not be collected: {e}"
                    )
                    continue
                run.error = run.error or str(e)
            run.status = "done" if run.exit_code in (0, None) else "failed"
            note = (
                "closed: terminal task moved on before its accepted remote result was collected"
                if terminal_remote else
                "closed: no active run pointer references it and its process has exited"
            )
            run.error = f"{run.error} ({note})" if run.error else note
            run.save()
            event_detail = {"terminal_task": True} if terminal_remote else {}
            self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode, cost_usd=run.cost_usd,
                             usage=run.usage, status=run.status, dangling=True, **event_detail)
            self.log(f"{run.task_id}: {run.mode} run {run.run_id} closed ({run.status}); {note}")
            rep.transitions.append(f"{run.task_id} {run.mode} run {run.run_id} closed (dangling)")

    # ---- stall detection ---------------------------------------------------
    def _stall(
        self, task: Task, rep: TickReport, reason: str, *, implementation_failure: bool = True
    ) -> None:
        if implementation_failure:
            self._record_implementation_failure(task, "repeated_unchanged_attempt", reason, reason)
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
