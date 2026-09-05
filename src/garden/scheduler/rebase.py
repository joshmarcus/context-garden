"""Rebase as its own run mode: bring a PR forward by the cheapest thing that works.

Three rules live here (see docs/architecture.md, beside stacking):

1. A conflict is rebased mechanically first — `git rebase origin/<base>` with no model. A clean
   apply is the whole round: a `rebase` run record (no harness call), a lease push and a re-run
   of the pre-PR checks. Only a textual conflict starts an agent, on the easy tier, with a brief
   that carries the conflicting hunks and the rule "resolve the conflict, change nothing else".
   A rebase round has its own counter (`state[task].rebases`) and never touches `max_revisions`
   or `review.max_rounds`.
2. After any rebase the diff against the new base is compared with `last_diff_hash` from the
   reviewed push; when it is unchanged the last verdict is kept, "rebased; diff unchanged;
   verdict kept" is logged, and no review is dispatched. A textual resolution that changed the
   diff is reviewed as usual.
3. Automerge is a queue: candidates are ordered oldest-approved-first and only the head is
   rebased, checked and merged; the next candidate is taken on the following poll.
"""

from __future__ import annotations

from .. import gitops
from ..checks import failures as check_failures
from ..github import GitHubError, PRInfo, mark_garden_comment
from ..model import Status, Task, now_iso
from ..runs import Run
from .report import TickReport


class RebaseMixin:
    # ---- mechanical rebase (rule 1) ----------------------------------------
    def mechanical_rebase(self, task: Task, base: str, rep: TickReport, *, reason: str) -> str:
        """Try to bring `task`'s branch onto `base` with no model. On a clean apply: record a
        `rebase` run (no harness call), force-push with a lease, re-run the pre-PR checks, then
        keep the verdict or dispatch a review (rule 2). On a textual conflict: dispatch an
        easy-tier agent that carries only the hunks. Returns one of:
        `clean` (rebased, pushed, checks green), `checks` (a check revise was started),
        `conflict` (an agent was dispatched), `error` (the push failed)."""
        st = self.state.get(task.id)
        branch = task.branch or task.default_branch()
        wt = self.worktree_for(task)
        repo = self.repo_for(task)
        try:
            if not wt.exists():
                gitops.prepare_worktree(repo, wt, branch, base)
            # Fold in commits that exist only on origin/<branch> first, so the force-push below
            # never discards them (mirrors the restack path).
            ok, files = gitops.sync_remote_branch(wt, branch)
            hunks: dict[str, str] = {}
            if ok:
                ok, files, hunks = gitops.rebase_onto_capture(wt, gitops.base_ref(wt, base))
        except gitops.GitError as e:
            ok, files, hunks = False, [str(e)], {}
        if not ok:
            self.events.emit("rebase", task.id, base=base, files=files, resolved=False, how="agent")
            self._dispatch_rebase_agent(task, base, files, hunks, rep, reason)
            return "conflict"
        run = self.runs.new_run(task.id, "local", mode="rebase")
        run.branch, run.base, run.worktree, run.difficulty = branch, base, str(wt), "easy"
        try:
            note = gitops.push(wt, branch, force=True)
            if note:
                self.log(f"{task.id}: {note}")
        except gitops.GitError as e:
            run.status = "failed"
            run.error = str(e)
            run.finished_at = now_iso()
            run.save()
            self.log(f"{task.id}: rebase push failed: {e}")
            return "error"
        run.status = "done"
        run.cost_usd = 0.0
        run.finished_at = now_iso()
        run.diff_stat = gitops.diff_stat(wt, base)
        run.save()
        st["rebases"] = int(st.get("rebases", 0)) + 1
        self.events.emit("run_finished", task.id, run=run.run_id, mode="rebase", cost_usd=0.0, usage={}, status="done")
        self.events.emit("rebase", task.id, base=base, files=[], resolved=True, how="mechanical", run=run.run_id)
        task.log(f"{reason}; rebased onto {base} mechanically and force-pushed")
        self.store.save(task)
        failed = check_failures(self._pre_pr_checks(task, wt, branch, base))
        if failed:
            self._start_check_revise(task, failed, rep, "")
            return "checks"
        self._rebase_review_or_keep(task, run, base, rep)
        return "clean"

    def _dispatch_rebase_agent(self, task: Task, base: str, files: list[str], hunks: dict[str, str],
                               rep: TickReport, reason: str) -> None:
        """A plain rebase conflicted textually: queue an easy-tier agent that resolves it. The
        actual dispatch happens in the dispatch phase (see DispatchMixin.dispatch, mode `rebase`),
        so it waits for a free slot like any other run. The force-push flag is set for the push
        after the agent resolves the conflict."""
        st = self.state.get(task.id)
        st["rebase_pending"] = True
        st["rebase_base"] = base
        st["rebase_files"] = list(files)
        st["rebase_hunks"] = hunks
        st.pop("automerge_candidate", None)
        st.pop("automerge_ready_at", None)
        st["force_push"] = True
        if task.status.pr_open:
            self._transition(task, Status.CHANGES_REQUESTED,
                             f"{reason}; rebase onto {base} conflicts ({', '.join(files) or 'unknown files'}); a rebase agent will resolve it")
            rep.transitions.append(f"{task.id} -> changes_requested (rebase)")
        else:
            task.log(f"{reason}; rebase onto {base} conflicts; the next run must resolve it")
            self.store.save(task)

    # ---- verdict keep (rule 2) ---------------------------------------------
    def _rebase_review_or_keep(self, task: Task, run: Run, base: str, rep: TickReport, cost: str = "") -> None:
        """After a rebase, compare the diff against the new base with `last_diff_hash` from the
        reviewed push. When they match, keep the last verdict and dispatch no review; when the
        resolution changed the diff, review it as usual."""
        st = self.state.get(task.id)
        wt = self.worktree_for(task)
        diff_h = gitops.diff_hash(wt, base) if wt.exists() else ""
        if diff_h and diff_h == st.get("last_diff_hash"):
            self.events.emit("rebase", task.id, run=run.run_id, diff_unchanged=True, verdict_kept=True)
            task.log(f"rebased; diff unchanged; verdict kept{cost}")
            self.store.save(task)
            rep.transitions.append(f"{task.id} rebased; verdict kept")
            return
        if diff_h:
            st["last_diff_hash"] = diff_h
        self.events.emit("rebase", task.id, run=run.run_id, diff_unchanged=False, verdict_kept=False)
        self._maybe_review(task, run, rep)

    # ---- merge queue (rule 3) ----------------------------------------------
    def _run_merge_queue(self, rep: TickReport) -> None:
        """Automerge as a queue: order the approved candidates oldest-approved-first and act on
        only the head — rebase it once, right before it merges, then merge it. The next candidate
        is taken on the following poll, so exactly one PR is rebased and merged per tick."""
        candidates: list[tuple[str, str, Task]] = []
        for t in self.store.tasks().values():
            if t.status != Status.IN_REVIEW:
                continue
            st = self.state.get(t.id)
            if not st.get("automerge_candidate"):
                continue
            candidates.append((str(st.get("automerge_ready_at") or ""), t.id, t))
        if not candidates:
            return
        candidates.sort(key=lambda c: (c[0], c[1]))
        self._merge_candidate(candidates[0][2], rep)

    def _merge_candidate(self, task: Task, rep: TickReport) -> None:
        """Rebase the head of the queue once, right before it merges, then merge it."""
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if not slug or not number or not self.github.available:
            return
        try:
            pr = self.github.get_pr(slug, number)
        except (GitHubError, KeyError):
            return
        ok, reason = self._automerge_gate(task, pr)
        if not ok:
            self._hold_automerge(task, reason)
            return
        # Rebase once, right before the merge. A clean rebase whose diff is unchanged keeps the
        # verdict (no re-review); a conflict or a failed check takes the task off the queue.
        outcome = self.mechanical_rebase(task, self.final_base_for(task), rep, reason="rebasing before merge")
        if outcome != "clean":
            return
        st = self.state.get(task.id)
        if st.get("review_run"):
            return  # the rebase changed the diff and a review is now in flight
        try:
            pr = self.github.get_pr(slug, number)
        except (GitHubError, KeyError):
            return
        if pr.state != "OPEN":
            return
        ok, reason = self._automerge_gate(task, pr)
        if not ok:
            self._hold_automerge(task, reason)
            return
        self._do_merge(task, pr, rep)

    def _hold_automerge(self, task: Task, reason: str) -> None:
        st = self.state.get(task.id)
        st.pop("automerge_candidate", None)
        st.pop("automerge_ready_at", None)
        if st.get("automerge_blocked") != reason:
            st["automerge_blocked"] = reason
            self.log(f"{task.id}: automerge held: {reason}")

    def _do_merge(self, task: Task, pr: PRInfo, rep: TickReport) -> None:
        """Merge the PR the garden opened. The next poll sees it MERGED and moves the task to
        `done`, restacking children."""
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if not slug or not number:
            return
        st = self.state.get(task.id)
        method = str(self._github_cfg("automerge_method", task.product, "squash"))
        review_run = str(st.get("last_review_run") or "")
        try:
            self.github.merge_pr(slug, number, method=method, delete_branch=True)
        except GitHubError as e:
            st["automerge_blocked"] = f"merge call failed: {e}"
            self.log(f"{task.id}: automerge call failed: {e}")
            rep.errors.append(f"{task.id}: automerge failed: {e}")
            return
        rounds = int(st.get("review_rounds", 0))
        st.pop("automerge_blocked", None)
        st.pop("automerge_candidate", None)
        st.pop("automerge_ready_at", None)
        st["automerged"] = {"at": now_iso(), "method": method, "review_run": review_run,
                            "verdict": "approve", "review_rounds": rounds}
        self.events.emit("automerged", task.id, pr=task.pr, method=method, review_run=review_run,
                         verdict="approve", review_rounds=rounds)
        self.log(f"{task.id}: merged by the garden ({method}); all gates green")
        try:
            body = ("Merged by the garden: every gate is green — automated review approved"
                    + (f" (run `{review_run}`)" if review_run else "")
                    + f", checks passing, mergeable, {rounds} review round(s), under budget.")
            self.github.comment(slug, number, mark_garden_comment(body, review_run))
        except GitHubError as e:
            self.log(f"{task.id}: could not post automerge comment: {e}")
        rep.transitions.append(f"{task.id} automerged")
