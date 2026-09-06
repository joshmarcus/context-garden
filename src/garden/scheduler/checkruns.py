"""Checks as run records: the tick starts a check run and reaps it on a later tick.

A pre-PR check, a base probe or a pre-merge rebase-and-check used to run the product's test
suite in-process inside `tick()`, which held the web lock for a minute a pass (CG-182). Now
each is a `check` run — its own directory, started by one tick and reaped by a later one,
exactly like a review — so the tick only starts and reaps and never runs a product's suite
itself. The chain (pre-PR → base probe → rebase re-check) is a small state machine: each
stage stores the continuation the reap needs, and `reap_check` routes the results to it.

The git scaffolding a check needs (a mechanical rebase, a throwaway probe worktree) is cheap
and stays in the tick; only the check commands — the slow part — move to the run record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import gitops
from ..checks import failures as check_failures
from ..model import Status, Task, now_iso
from ..preflight import mechanical_results
from ..runs import Run
from .report import TickReport

# Which stages report their results under which `check` event stage: the base probe and the CI
# analyser keep their own labels; every pre-PR-style re-check (a fresh push, a stale-base rebase,
# a pre-merge rebase) reports as "pre_pr", matching the historic synchronous events.
_EVENT_STAGE = {"base_probe": "base_probe", "ci": "ci"}


def _is_ui_path(path: str) -> bool:
    """Files whose rendered result must be inspected before a PR opens."""
    return (path.startswith(("src/garden/web/", "templates/", "static/")) or "/templates/" in path
            or path.endswith((".css", ".scss")))


class CheckRunMixin:
    # ---- dispatch / reap ---------------------------------------------------
    def _run_by_id(self, task: Task, run_id: str) -> Run | None:
        return next((r for r in self.runs.runs_for(task.id) if r.run_id == run_id), None)

    def _pre_pr_cont(self, worker_run: Run | None, worktree: Path, branch: str, base: str, cost: str,
                     diff_h: str | None = None, body_h: str | None = None, stalled: bool = False) -> dict[str, Any]:
        """The continuation context every pre-PR-style check stage shares: which worker run's
        result opens the PR, where the branch is, and the hashes computed before the checks ran."""
        return {"worker_run_id": worker_run.run_id if worker_run else "", "worktree": str(worktree),
                "branch": branch, "base": base, "cost": cost, "diff_h": diff_h, "body_h": body_h,
                "stalled": stalled}

    def _dispatch_check_run(self, task: Task, *, worktree: Path, branch: str, base: str,
                            specs: list[dict[str, Any]], stage: str, cont: dict[str, Any], rep: TickReport,
                            extra: dict[str, Any] | None = None) -> Run:
        """Start a detached check run for `specs` in `worktree` and record the continuation the
        reap resumes. The slot accounting counts it; the task shows it on its page. `extra` adds
        keys to the job payload (e.g. a CI check's flaky-rerun budget)."""
        runner = self.runner_for(task, "local")
        run = self.runs.new_run(task.id, "local", mode="check")
        run.branch, run.base, run.worktree, run.difficulty = branch, base, str(worktree), "easy"
        run.save()
        if stage in {"pre_pr", "rebase_recheck", "merge_rebase", "scratch_merge"}:
            changed = gitops.diff_names(worktree, base)
            if any(_is_ui_path(path) for path in changed) and not any(s.get("name") == "ui" for s in specs):
                specs = [*specs, {"name": "ui", "python": "garden.walkthrough:ui_check",
                                  "out_dir": str(run.path / "ui"), "worktree": str(worktree),
                                  "changed": changed}]
        payload = {"specs": specs, "ctx": self.check_ctx(task, branch, base, worktree),
                   "cwd": str(worktree), "setup": self.cfg.product_setup(task.product),
                   "timeout": int(self.cfg.get("checks.timeout_seconds", 600)), "config": self.cfg.data,
                   **(extra or {})}
        # A CI analyser may have no worktree; launch the process somewhere that exists.
        launch_cwd = worktree if worktree.exists() else run.path
        runner.start_checks(run, launch_cwd, payload)
        st = self.state.get(task.id)
        st["check_run"] = {"run_id": run.run_id, "stage": stage, "cont": cont}
        self.events.emit("dispatch", task.id, run=run.run_id, mode="check", stage=stage)
        self.state.save()
        rep.dispatched.append(f"{task.id}(check:{stage})")
        return run

    def reap_check(self, task: Task, rep: TickReport) -> bool:
        st = self.state.get(task.id)
        info = dict(st.get("check_run") or {})
        run_id = info.get("run_id")
        if not run_id:
            return False
        run = self._run_by_id(task, run_id)
        if run is None or run.status != "running":
            st["check_run"] = {}
            return False
        runner = self.runner_for(task, run.runner, run.harness)
        if not self._finished_or_timed_out(run, runner):
            return False
        st["check_run"] = {}
        results = self._collect_check_results(run)
        run.exit_code = run.read_exit_code()
        run.finished_at = now_iso()
        run.cost_usd = 0.0
        run.result = {"checks": results}
        run.status = "done" if run.status != "timeout" else "timeout"
        run.save()
        stage = str(info.get("stage") or "pre_pr")
        self.events.emit("run_finished", task.id, run=run.run_id, mode="check", status=run.status, cost_usd=0.0, usage={})
        for r in results:
            self.events.emit("check", task.id, stage=_EVENT_STAGE.get(stage, "pre_pr"),
                             name=r.get("name"), status=r.get("status"), summary=r.get("summary", ""))
        cont = dict(info.get("cont") or {})
        handler = {
            "pre_pr": self._after_pre_pr_check,
            "base_probe": self._after_base_probe_check,
            "rebase_recheck": self._after_rebase_recheck,
            "reprobe": self._after_reprobe_check,
            "reprobe_conflict": self._after_reprobe_conflict_check,
            "merge_rebase": self._after_merge_rebase_check,
            "scratch_merge": self._after_scratch_merge_check,
            "ci": self._after_ci_check,
        }.get(stage)
        if handler is None:
            self.log(f"{task.id}: unknown check stage {stage!r}; results dropped")
            return True
        handler(task, run, results, cont, rep)
        return True

    def _collect_check_results(self, run: Run) -> list[dict[str, Any]]:
        path = run.path / "checks.json"
        if not path.exists():
            return [{"name": "checks", "status": "error", "summary": "check run produced no results", "details": run.stderr_text()[-2000:]}]
        try:
            data = json.loads(path.read_text())
            return list(data) if isinstance(data, list) else []
        except (ValueError, OSError) as e:
            return [{"name": "checks", "status": "error", "summary": f"unreadable check results: {e}", "details": ""}]

    # ---- pre-PR checks after a worker push (rule: gate the PR) --------------
    def _after_pre_pr_check(self, task: Task, run: Run, results: list[dict[str, Any]], cont: dict[str, Any], rep: TickReport) -> None:
        worker_run = self._run_by_id(task, cont.get("worker_run_id", ""))
        worktree = Path(cont["worktree"])
        branch, base = cont["branch"], cont["base"]
        stalled = bool(cont.get("stalled"))
        failed = check_failures(results)
        worker_result = worker_run.result if worker_run is not None else self._last_worker_result(task)
        changed = gitops.diff_names(worktree, base)
        ui = [item for item in results if item.get("name") == "ui"]
        captures = [str(path) for item in ui for path in item.get("captures", [])]
        mechanical = mechanical_results(
            worktree, base, str(worker_result.get("pr_body") or ""),
            require_description=not bool(task.pr), ui_changed=any(_is_ui_path(path) for path in changed),
            captures=captures,
        )
        results.extend(mechanical)
        run.result = {"checks": results}
        run.save()
        failed = check_failures(results)
        if failed and not stalled:
            self._handle_failed_checks(task, worker_run, worktree, branch, base, failed, rep, cont)
            return
        self._open_pr_after_checks(task, worker_run, branch, base, cont, rep)
        if stalled and worker_run is not None:
            self._stall(task, rep, f"revise run {worker_run.run_id} produced no change to the diff or PR description")

    def _open_pr_after_checks(self, task: Task, worker_run: Run | None, branch: str, base: str,
                              cont: dict[str, Any], rep: TickReport) -> None:
        """The green path once the checks are in: save the hashes computed before the checks and
        open or update the PR with the worker's result. Mirrors the tail of `_after_push`."""
        st = self.state.get(task.id)
        diff_h, body_h = cont.get("diff_h"), cont.get("body_h")
        if diff_h is not None and (worker_run is None or worker_run.mode != "rebase"):
            st["last_diff_hash"] = diff_h
        if body_h is not None:
            st["last_pr_body_hash"] = body_h
        result = worker_run.result if worker_run else self._last_worker_result(task)
        self._open_or_update_pr(task, worker_run, branch, base, result, rep, cont["cost"])

    def _handle_failed_checks(self, task: Task, worker_run: Run | None, worktree: Path, branch: str, base: str,
                              failed: list[dict[str, Any]], rep: TickReport, cont: dict[str, Any]) -> None:
        """A pre-PR check the branch may or may not own. Probe the branch's base first, as a
        second check run in a throwaway worktree at the merge base: if the same check fails there,
        the failure is not this branch's. The git scaffolding (fetch, merge base, the probe
        worktree) is cheap and runs here; only the check commands go to the probe run."""
        cost = cont["cost"]
        repo = self.repo_for(task)
        try:
            gitops.fetch(worktree)
            ref = gitops.base_ref(worktree, base)
            base_sha = gitops.merge_base(worktree, ref)
            moved = bool(base_sha) and gitops.rev_parse(worktree, ref) != base_sha
            names = {str(f.get("name")) for f in failed}
            specs = [s for s in self._pre_pr_specs(task) if str(s.get("name")) in names]
            probe = worktree.parent / f"{worktree.name}.base-probe"
            gitops.remove_worktree(repo, probe)
            gitops.add_detached_worktree(repo, probe, base_sha)
        except gitops.GitError as e:
            self.log(f"{task.id}: base probe failed ({e}); treating the failure as this branch's")
            self._start_check_revise(task, failed, rep, cost)
            return
        self._dispatch_check_run(
            task, worktree=probe, branch=branch, base=base, specs=specs, stage="base_probe", rep=rep,
            cont={**self._pre_pr_cont(worker_run, worktree, branch, base, cost, cont.get("diff_h"), cont.get("body_h")),
                  "probe": str(probe), "base_sha": base_sha, "moved": moved, "failed": failed})

    def _after_base_probe_check(self, task: Task, run: Run, results: list[dict[str, Any]], cont: dict[str, Any], rep: TickReport) -> None:
        base_failures = check_failures(results)
        probe = Path(cont["probe"])
        try:
            gitops.remove_worktree(self.repo_for(task), probe)
        except gitops.GitError:
            pass
        worker_run = self._run_by_id(task, cont.get("worker_run_id", ""))
        worktree = Path(cont["worktree"])
        branch, base, cost = cont["branch"], cont["base"], cont["cost"]
        failed, base_sha, moved = cont["failed"], cont["base_sha"], cont["moved"]
        if not base_failures:
            # The base is clean: this branch owns the failure.
            self._start_check_revise(task, failed, rep, cost)
            return
        names = ", ".join(str(f.get("name")) for f in base_failures)
        self.events.emit("check_base", task.id, base=base_sha, checks=names, moved=moved)
        self.log(f"{task.id}: pre-PR check(s) {names} fail at base {base_sha[:12]}; not this branch")
        if not moved:
            # The base branch has not moved: it is itself broken. Park the task; no revise, no spend.
            reason = (f"base branch `{base}` is itself broken — pre-PR check(s) {names} fail at its own commit "
                      f"{base_sha[:12]}, not because of this branch")
            self._set_needs_human(task, "base_broken", reason, base=base, base_sha=base_sha)
            self.events.emit("needs_human", task.id, stop_kind="base_broken", reason=reason)
            self._transition(task, Status.CHANGES_REQUESTED, f"{reason}; waiting for the base to go green, no revise round{cost}", needs_human=True)
            rep.transitions.append(f"{task.id} -> changes_requested (base broken)")
            return
        # The base moved: rebase onto it (git, cheap) through the one rebase-and-record helper
        # (CG-197, so the rebase is counted) and re-run the checks as a fresh check run.
        outcome = self._rebase_and_record(task, base, wt=worktree)
        if outcome.status == "conflict":
            # the rebase didn't apply cleanly; let a revise round resolve it (not a revision, CG-131).
            self._start_check_revise(task, failed, rep, cost, is_rebase=True)
            return
        if outcome.status == "error":
            return  # push failure already logged by the helper
        self._dispatch_check_run(
            task, worktree=worktree, branch=branch, base=base, specs=self._pre_pr_specs(task),
            stage="rebase_recheck", rep=rep,
            cont={**self._pre_pr_cont(worker_run, worktree, branch, base, cost, cont.get("diff_h"), cont.get("body_h")),
                  "base_sha": base_sha, "names": names, "failed": failed})

    def _after_rebase_recheck(self, task: Task, run: Run, results: list[dict[str, Any]], cont: dict[str, Any], rep: TickReport) -> None:
        rerun = check_failures(results)
        worker_run = self._run_by_id(task, cont.get("worker_run_id", ""))
        branch, base, cost = cont["branch"], cont["base"], cont["cost"]
        base_sha, names = cont["base_sha"], cont["names"]
        if not rerun:
            task.log(f"pre-PR check(s) {names} failed at the stale base {base_sha[:12]}; the base branch "
                     f"`{base}` had moved, so rebased onto it and the checks pass now — no revise round")
            self.store.save(task)
            self.events.emit("rebased_stale_base", task.id, base=base, base_sha=base_sha, resolved=True)
            rep.transitions.append(f"{task.id} rebased onto moved {base}; checks green")
            self._open_pr_after_checks(task, worker_run, branch, base, cont, rep)
            return
        self.events.emit("rebased_stale_base", task.id, base=base, base_sha=base_sha, resolved=False)
        self._start_check_revise(task, rerun, rep, cost, note=f" (still failing after a rebase onto `{base}`)")

    # ---- base_broken re-probe (a parked task continues on its own) ----------
    def _after_reprobe_check(self, task: Task, run: Run, results: list[dict[str, Any]], cont: dict[str, Any], rep: TickReport) -> None:
        """The re-check after a parked `base_broken` task rebased onto its recovered base. Green:
        clear the stop and open/update the PR, no worker run. Red: route through the base probe,
        which re-parks it (if the moved base is broken too) or starts a revise round."""
        worker_run = self._run_by_id(task, cont.get("worker_run_id", ""))
        worktree = Path(cont["worktree"])
        branch, base = cont["branch"], cont["base"]
        tip = cont.get("base_sha", "")
        st = self.state.get(task.id)
        failed = check_failures(results)
        if failed:
            self.events.emit("rebased_stale_base", task.id, base=base, base_sha=tip, resolved=False)
            st.pop("needs_human", None)
            self._handle_failed_checks(task, worker_run, worktree, branch, base, failed, rep, cont)
            return
        st.pop("needs_human", None)
        self._queue_leave(task)
        self.events.emit("rebased_stale_base", task.id, base=base, base_sha=tip, resolved=True)
        task.log(f"base branch `{base}` recovered (moved to {tip[:12]}); rebased onto it and the pre-PR "
                 f"checks pass now — continuing without a worker run")
        self.store.save(task)
        self.log(f"{task.id}: base `{base}` recovered; rebased and re-checked green, continuing on its own")
        rep.transitions.append(f"{task.id} rebased onto recovered {base}; checks green")
        self._open_or_update_pr(task, worker_run or run, branch, base, self._last_worker_result(task), rep, "")

    def _after_reprobe_conflict_check(self, task: Task, run: Run, results: list[dict[str, Any]], cont: dict[str, Any], rep: TickReport) -> None:
        """The recovered base moved but the rebase conflicted: hand the branch's own failures to
        the normal revise path (and only then)."""
        base = cont["base"]
        st = self.state.get(task.id)
        st.pop("needs_human", None)
        failed = check_failures(results)
        self._start_check_revise(task, failed, rep, "", note=f" (rebase onto `{base}` did not apply cleanly)")
        rep.transitions.append(f"{task.id} base moved but rebase conflicted; revise")

    # ---- hard-tier scratch-merge check (CG-191) ----------------------------
    def _dispatch_scratch_merge(self, task: Task, rep: TickReport) -> None:
        """Build the scratch merge — the branch rebased onto the base tip in a throwaway worktree,
        never touching the branch itself — and run the pre-PR checks on it as a detached check run
        (stage `scratch_merge`). With no checks configured there is nothing to run, so the revision
        is recorded verified at once; a scratch merge that does not apply cleanly holds the merge."""
        st = self.state.get(task.id)
        base = self.final_base_for(task)
        branch = task.branch or task.default_branch()
        diff_h = str(st.get("last_diff_hash") or "")
        specs = self._pre_pr_specs(task)
        if not specs:
            st["scratch_merge"] = {"diff": diff_h, "ok": True}
            self.events.emit("scratch_merge", task.id, resolved=True, checks=0)
            self.store.save(task)
            return
        repo = self.repo_for(task)
        wt = self.worktree_for(task)
        scratch = wt.parent / f"{wt.name}.scratch-merge"
        try:
            gitops.fetch(repo)
            # The branch is checked out in the task's own worktree, so the scratch worktree takes
            # the branch tip detached (the pushed head under review) and rebases it onto the base.
            head_ref = branch
            if gitops.remote_url(repo):
                try:
                    gitops.rev_parse(repo, f"origin/{branch}")
                    head_ref = f"origin/{branch}"
                except gitops.GitError:
                    pass
            gitops.remove_worktree(repo, scratch)
            gitops.add_detached_worktree(repo, scratch, head_ref)
            ok, files, _ = gitops.rebase_onto_capture(scratch, gitops.base_ref(scratch, base))
        except gitops.GitError as e:
            ok, files = False, [str(e)]
        if not ok:
            gitops.remove_worktree(repo, scratch)
            st["scratch_merge"] = {"diff": diff_h, "ok": False, "checks": f"does not merge onto {base}"}
            self.events.emit("scratch_merge", task.id, resolved=False, base=base, files=files)
            self._queue_hold(task, f"the scratch merge onto `{base}` does not apply cleanly ({', '.join(files) or 'unknown'})")
            return
        self._dispatch_check_run(
            task, worktree=scratch, branch=branch, base=base, specs=specs, stage="scratch_merge", rep=rep,
            cont={"scratch": str(scratch), "diff_h": diff_h})

    def _after_scratch_merge_check(self, task: Task, run: Run, results: list[dict[str, Any]], cont: dict[str, Any], rep: TickReport) -> None:
        """Reap the hard-tier scratch-merge check. Green: record this revision as verified (keyed
        to the reviewed diff) so the automerge gate clears and the queue can merge it. Red: hold
        the merge with the failing checks. Either way the throwaway worktree is removed."""
        scratch = cont.get("scratch")
        if scratch:
            try:
                gitops.remove_worktree(self.repo_for(task), Path(scratch))
            except gitops.GitError:
                pass
        st = self.state.get(task.id)
        diff_h = str(cont.get("diff_h") or "")
        failed = check_failures(results)
        if failed:
            names = ", ".join(str(f.get("name")) for f in failed) or "checks"
            st["scratch_merge"] = {"diff": diff_h, "ok": False, "checks": names}
            self.events.emit("scratch_merge", task.id, resolved=False, checks=len(failed))
            self._queue_hold(task, f"the hard-tier scratch-merge check failed ({names})")
            return
        st["scratch_merge"] = {"diff": diff_h, "ok": True}
        self.events.emit("scratch_merge", task.id, resolved=True, checks=len(results))
        task.log("hard-tier scratch-merge check passed; ready to merge once the queue reaches it")
        self.store.save(task)
        rep.transitions.append(f"{task.id} scratch-merge check green")

    # ---- pre-merge / conflict rebase re-check ------------------------------
    def _after_merge_rebase_check(self, task: Task, run: Run, results: list[dict[str, Any]], cont: dict[str, Any], rep: TickReport) -> None:
        """The pre-PR check after a mechanical rebase (a conflict rebase, or the pre-merge rebase
        that moved the head). Red: a revise round. Green: keep the verdict or re-review; and, when
        this was a pre-merge rebase that force-pushed a new head, hold the head in flight until its
        rollup goes green (see RebaseMixin._merge_candidate)."""
        worker_run = self._run_by_id(task, cont.get("worker_run_id", ""))
        base = cont["base"]
        failed = check_failures(results)
        if failed:
            self._start_check_revise(task, failed, rep, "")
            return
        self._rebase_review_or_keep(task, worker_run or run, base, rep)
        if cont.get("merge_head"):
            st = self.state.get(task.id)
            if st.get("review_run") or st.get("needs_human"):
                return  # the rebase changed the diff: a new review round (or a human) now owns it
            self._queue_head(task, announce=True)
