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
   rebased, checked and merged. Once the queue picks a head it keeps it: a head whose rollup
   is still running after the pre-merge rebase is "in flight" (a `merge_head` marker holding
   its `automerge_ready_at`), the queue does not pick another head while one is in flight, and
   it merges the head the moment the rollup goes green. A branch already on the base's tip is
   not rebased or pushed. A head leaves the queue only on a conflict, a failed check, a changed
   diff that needs a review, a closed PR or a human request for changes — the reason is logged
   and the next-oldest candidate becomes head.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import gitops
from ..github import GitHubError, PRInfo, mark_garden_comment
from ..model import Status, Task, now_iso
from ..notify import notify
from ..runs import Run
from .report import TickReport


@dataclass
class RebaseOutcome:
    """The result of the one mechanical-rebase primitive (`_rebase_and_record`). `status` is
    `clean` (rebased, force-pushed, a `rebase` run recorded in `run`), `conflict` (a textual
    conflict; `files`/`hunks` carry it, nothing pushed, no run), `error` (the force-push failed;
    `run` is the failed record) or `current` (skip_if_current and the branch is already on the
    base's tip with the reviewed diff; nothing pushed, no run)."""

    status: str
    wt: Path
    branch: str
    run: Run | None = None
    files: list[str] = field(default_factory=list)
    hunks: dict[str, str] = field(default_factory=dict)


class RebaseMixin:
    # ---- the one mechanical-rebase primitive (rule 1) ----------------------
    def _rebase_and_record(self, task: Task, base: str, *, wt: Path | None = None,
                           skip_if_current: bool = False, reason: str = "") -> RebaseOutcome:
        """Bring `task`'s branch onto `base` with no model, and on a clean apply force-push with a
        lease and record a token-free `rebase` run — so every rebase path is counted (CG-197). This
        is the single recorded helper the four rebase sequences share (a plain conflict rebase, the
        pre-merge rebase, a stacked-child restack and a moved-base re-check); each caller acts on the
        returned `RebaseOutcome` and owns its own domain events and continuation. Commits that live
        only on `origin/<branch>` are folded in first so the force-push never discards them. The
        recorded run is emitted with `how="mechanical"` so metrics can tell it apart from an agent
        rebase. `skip_if_current` returns `current` (no push, no run) when the branch already sits on
        the base's tip with the reviewed diff, so the same head is never needlessly re-pushed."""
        st = self.state.get(task.id)
        branch = task.branch or task.default_branch()
        wt = wt or self.worktree_for(task)
        repo = self.repo_for(task)
        try:
            if not wt.exists():
                gitops.prepare_worktree(repo, wt, branch, base)
            ok, files, hunks = gitops.sync_and_rebase(wt, branch, base)
        except gitops.GitError as e:
            ok, files, hunks = False, [str(e)], {}
        if not ok:
            return RebaseOutcome("conflict", wt, branch, files=files, hunks=hunks)
        if skip_if_current:
            # A branch already on the base's tip whose diff is exactly what was reviewed: the
            # rebase above was a no-op, origin already holds this head, and the verdict still
            # applies. Nothing to rebase or push (a force-push would only re-push the same sha and
            # needlessly restart the rollup). A diff that no longer matches the reviewed hash falls
            # through to the push, which re-reviews it.
            try:
                head_now = gitops.rev_parse(wt, "HEAD")
                remote_now = gitops.rev_parse(wt, f"origin/{branch}")
                diff_h = gitops.diff_hash(wt, base)
            except gitops.GitError:
                head_now, remote_now, diff_h = "", "", ""
            if head_now and head_now == remote_now and diff_h and diff_h == st.get("last_diff_hash"):
                if reason:
                    task.log(f"{reason}; already on {base}'s tip; not rebased or pushed")
                    self.store.save(task)
                return RebaseOutcome("current", wt, branch)
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
            return RebaseOutcome("error", wt, branch, run=run)
        run.status = "done"
        run.cost_usd = 0.0
        run.finished_at = now_iso()
        run.diff_stat = gitops.diff_stat(wt, base)
        run.save()
        st["rebases"] = int(st.get("rebases", 0)) + 1
        self.events.emit("run_finished", task.id, run=run.run_id, mode="rebase", cost_usd=0.0, usage={}, status="done", how="mechanical")
        return RebaseOutcome("clean", wt, branch, run=run)

    def mechanical_rebase(self, task: Task, base: str, rep: TickReport, *, reason: str,
                          skip_if_current: bool = False, merge_head: bool = False) -> str:
        """Try to bring `task`'s branch onto `base` with no model. On a clean apply: record a
        `rebase` run (no harness call), force-push with a lease, then start the pre-PR checks as
        a detached check run (reaped on a later tick; CG-182) whose continuation keeps the verdict
        or dispatches a review (rule 2). On a textual conflict: dispatch an easy-tier agent that
        carries only the hunks. Returns one of:
        `checking` (rebased, pushed, a check run started — the continuation finishes the round),
        `clean` (rebased and pushed with no checks configured, verdict kept synchronously),
        `current` (the branch was already on the base's tip, so nothing was rebased or pushed —
        only when `skip_if_current`), `conflict` (an agent was dispatched), `error` (the push
        failed). `merge_head` marks the pre-merge rebase: its continuation holds the head in
        flight until its rollup goes green."""
        outcome = self._rebase_and_record(task, base, skip_if_current=skip_if_current, reason=reason)
        if outcome.status == "conflict":
            self.events.emit("rebase", task.id, base=base, files=outcome.files, resolved=False, how="agent")
            self._dispatch_rebase_agent(task, base, outcome.files, outcome.hunks, rep, reason)
            return "conflict"
        if outcome.status in ("current", "error"):
            return outcome.status
        run, wt, branch = outcome.run, outcome.wt, outcome.branch
        self.events.emit("rebase", task.id, base=base, files=[], resolved=True, how="mechanical", run=run.run_id)
        task.log(f"{reason}; rebased onto {base} mechanically and force-pushed")
        self.store.save(task)
        specs = self._pre_pr_specs(task)
        if specs:
            self._dispatch_check_run(task, worktree=wt, branch=branch, base=base, specs=specs,
                                     stage="merge_rebase", rep=rep,
                                     cont={**self._pre_pr_cont(run, wt, branch, base, ""), "merge_head": merge_head})
            return "checking"
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
        st.pop("merge_head", None)  # a conflict takes the task off the merge queue
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
            if st.pop("pending_triage_notify", False) and task.status == Status.AWAITING_TRIAGE:
                notify(self.cfg.data, task.id, "awaiting_triage", f"rebased; diff unchanged; verdict kept{cost}", task.pr or "")
            rep.transitions.append(f"{task.id} rebased; verdict kept")
            return
        if diff_h:
            st["last_diff_hash"] = diff_h
        self.events.emit("rebase", task.id, run=run.run_id, diff_unchanged=False, verdict_kept=False)
        self._maybe_review(task, run, rep)

    # ---- merge queue (rule 3) ----------------------------------------------
    def _run_merge_queue(self, rep: TickReport) -> None:
        """Automerge as a queue that keeps its head. Once a head is picked and rebased it stays
        the head (an in-flight `merge_head`) until it merges or leaves the queue, so a pending
        rollup after the pre-merge rebase never rotates the head. When nothing is in flight, the
        oldest-approved candidate becomes the head."""
        head = self._current_merge_head()
        if head is not None:
            self._advance_merge_head(head, rep)
            return
        if self._merge_head_pending():
            # A pre-merge check dispatched for a would-be head is still in flight. Its
            # `merge_head` marker is only set when that check reaps (`_after_merge_rebase_check`),
            # so `_current_merge_head` cannot see it yet; picking a second candidate and rebasing
            # it here would put two heads in flight, breaking the one-head invariant (CG-176).
            return
        candidates: list[tuple[str, str, Task]] = []
        for t in self.store.tasks().values():
            if t.status != Status.IN_REVIEW:
                continue
            st = self.state.get(t.id)
            if not st.get("automerge_candidate"):
                continue
            if st.get("check_run"):
                continue  # a pre-merge check run is already in flight for this task (CG-182)
            candidates.append((str(st.get("automerge_ready_at") or ""), t.id, t))
        if not candidates:
            return
        candidates.sort(key=lambda c: (c[0], c[1]))
        self._merge_candidate(candidates[0][2], rep)

    def _merge_head_pending(self) -> bool:
        """Whether a pre-merge rebase's check run is in flight for a would-be head. Between the
        tick that dispatches that detached check and the tick that reaps it, the task has no
        `merge_head` marker (the reap sets it) yet is already the queue's chosen head, so the
        queue must not pick another candidate meanwhile."""
        for t in self.store.tasks().values():
            info = self.state.get(t.id).get("check_run") or {}
            if info.get("stage") == "merge_rebase" and (info.get("cont") or {}).get("merge_head"):
                return True
        return False

    def _current_merge_head(self) -> Task | None:
        """The task currently in flight (rebased, waiting for its rollup), or None. A stale marker
        left on a task that is no longer an `in_review` automerge candidate is cleared here."""
        head: Task | None = None
        for t in self.store.tasks().values():
            st = self.state.get(t.id)
            if not st.get("merge_head"):
                continue
            if head is None and t.status == Status.IN_REVIEW and self._automerge_enabled(t):
                head = t
            else:
                st.pop("merge_head", None)
        return head

    def _merge_candidate(self, task: Task, rep: TickReport) -> None:
        """Pick this candidate as the head: rebase it onto the final base once, right before it
        merges. A branch already on the base's tip is merged as it stands (no rebase, no push);
        a rebase that has to move the branch restarts its rollup, so the head goes in flight and
        merges on a later poll once the rollup is green (see `_advance_merge_head`)."""
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
        # verdict (no re-review); a conflict or a failed check takes the task off the queue. The
        # pre-merge checks run as a detached check run (`merge_head=True`), whose continuation
        # holds the head in flight once they pass (CG-182).
        outcome = self.mechanical_rebase(task, self.final_base_for(task), rep,
                                         reason="rebasing before merge", skip_if_current=True, merge_head=True)
        st = self.state.get(task.id)
        if outcome == "current":
            # Already on the base's tip: no push, so the reported rollup is trustworthy — decide
            # now, on this poll, whether to merge or (a still-running rollup) keep waiting.
            st["merge_head"] = True
            self._advance_merge_head(task, rep)
            return
        if outcome != "clean":
            return  # checking (a check run holds the head) / conflict / push error handled elsewhere
        # No checks configured: the rebase moved the branch and force-pushed it synchronously.
        if st.get("review_run") or st.get("needs_human"):
            return  # the rebase changed the diff: a new review round (or a human) now owns it
        st["merge_head"] = True
        self.events.emit("merge_head", task.id, waiting=True, reason="rebased; awaiting rollup")
        self.log(f"{task.id}: rebased before merge; in flight until its rollup is green")

    def _advance_merge_head(self, task: Task, rep: TickReport) -> None:
        """Act on the in-flight head, which is already on the base's tip. Merge it (no rebase, no
        push) the moment the gate passes; keep it as head while its rollup is still running; drop
        it — logging why — only on a hard reason (a conflict, a failed check, a changed diff now
        in review, a closed PR or a human change request), so the next candidate becomes head."""
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if not slug or not number or not self.github.available:
            return
        try:
            pr = self.github.get_pr(slug, number)
        except (GitHubError, KeyError):
            return
        ok, reason = self._automerge_gate(task, pr)
        if ok:
            self._do_merge(task, pr, rep)
            return
        if self._head_in_flight(task, pr):
            return  # rollup still running (or mergeability still being computed): stay the head
        self.events.emit("merge_head", task.id, left=True, reason=reason)
        self.log(f"{task.id}: merge head left the queue: {reason}")
        self._hold_automerge(task, reason)

    def _head_in_flight(self, task: Task, pr: PRInfo) -> bool:
        """Whether the head should keep waiting rather than leave the queue. It waits only while
        its rollup is still running (or GitHub is still recomputing mergeability after the rebase
        push); a conflict, a failed check, a changed diff now in review, a closed PR or a human
        change request all return False so the head is dropped."""
        if pr.state != "OPEN":
            return False
        if pr.mergeable == "CONFLICTING" or pr.checks == "FAILURE":
            return False
        if pr.review_decision == "CHANGES_REQUESTED":
            return False
        st = self.state.get(task.id)
        if str(st.get("pending_feedback") or "").strip() or st.get("review_run"):
            return False  # the rebase changed the diff; a new review round owns it now
        rev = st.get("last_review") or {}
        if str(rev.get("verdict") or "") != "approve":
            return False
        # What is left is a rollup that has not reported yet, or a mergeability GitHub is still
        # computing after the push: keep waiting.
        return pr.checks == "PENDING" or pr.mergeable != "MERGEABLE"

    def _hold_automerge(self, task: Task, reason: str) -> None:
        st = self.state.get(task.id)
        st.pop("merge_head", None)
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
        # Retarget every open stacked-child PR to the final base first: deleting this branch while
        # a child still targets it makes GitHub close the child's PR (CG-173). Keep the branch when
        # a retarget fails, so no child is orphaned; a later pass deletes it once they are clear.
        delete_branch = self._retarget_children_before_delete(task)
        if not delete_branch:
            self.log(f"{task.id}: keeping the branch on merge; a stacked child PR could not be retargeted")
        try:
            self.github.merge_pr(slug, number, method=method, delete_branch=delete_branch)
        except GitHubError as e:
            st["automerge_blocked"] = f"merge call failed: {e}"
            self.log(f"{task.id}: automerge call failed: {e}")
            rep.errors.append(f"{task.id}: automerge failed: {e}")
            return
        rounds = int(st.get("review_rounds", 0))
        st.pop("automerge_blocked", None)
        st.pop("automerge_candidate", None)
        st.pop("automerge_ready_at", None)
        st.pop("merge_head", None)
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
