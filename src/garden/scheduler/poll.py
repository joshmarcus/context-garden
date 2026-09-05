"""Poll: what GitHub says about an open PR (merged, closed, feedback, CI), automerge, stacking and restacks."""

from __future__ import annotations

import fnmatch
from typing import Any

from .. import gitops
from ..checks import failures as check_failures
from ..checks import to_feedback
from ..github import GitHubError, PRInfo
from ..model import Status, Task, now_iso
from ..notify import notify
from ..runs import Run
from .report import TickReport

# Paths whose change makes a PR too sensitive to merge without a person: the garden's own
# config, task files, CI config and principles. Automerge holds when the diff touches any of
# them, so a self-approved PR cannot quietly rewrite the loop's own rules (CG-194).
_GUARDED_PREFIXES = (".github/", "principles/")


def _touches_guarded_path(rel: str) -> bool:
    parts = rel.split("/")
    if fnmatch.fnmatch(parts[-1], "garden*.yaml"):
        return True
    if "tasks" in parts[:-1]:  # a file under any **/tasks/ directory
        return True
    return rel.startswith(_GUARDED_PREFIXES)


class PollMixin:
    # ---- poll --------------------------------------------------------------
    def poll(self, task: Task, rep: TickReport) -> None:
        if not self.github.available:
            return
        slug = self.slug_for(task)
        if not slug:
            return
        st = self.state.get(task.id)
        number = self._pr_number(task)
        if not number:
            return
        pr = self.github.get_pr(slug, number)
        st["pr_state"] = pr.state
        st["review_decision"] = pr.review_decision
        st["checks"] = pr.checks
        st["failed_checks"] = list(pr.failed_checks)
        st["last_polled"] = now_iso()
        if pr.state == "MERGED":
            by_garden = bool(st.get("automerged"))
            self._transition(task, Status.DONE, f"PR merged{' by the garden' if by_garden else ''}: {task.pr}")
            rep.transitions.append(f"{task.id} -> done")
            self._on_merged(task, rep)
            self._cleanup(task)
            return
        if pr.state == "CLOSED":
            if self._reopen_if_base_deleted(task, slug, pr, rep):
                return
            self.events.emit("pr_closed", task.id, pr=task.pr)
            self._transition(task, Status.FAILED, f"PR closed without merging: {task.pr}")
            rep.transitions.append(f"{task.id} -> failed (PR closed)")
            self._on_parent_closed(task, rep)
            return
        if not task.status.pr_open:
            return  # merged/closed handled above; the rest (triage, CI, feedback) only applies to the active review flow
        was_draft = bool(st.get("pr_draft"))
        st["pr_draft"] = bool(pr.is_draft)
        if task.status == Status.AWAITING_TRIAGE and not pr.is_draft:
            self.events.emit("triaged", task.id, pr=task.pr, by="github")
            self._transition(task, Status.IN_REVIEW, "marked ready for review on GitHub; triage done")
            rep.transitions.append(f"{task.id} -> in_review (triaged)")
        elif task.status == Status.IN_REVIEW and pr.is_draft and not was_draft:
            self._transition(task, Status.AWAITING_TRIAGE, "converted back to draft on GitHub")
            rep.transitions.append(f"{task.id} -> awaiting_triage")
        if task.status == Status.CHANGES_REQUESTED:
            return  # already waiting for a revise slot (or a human)
        if pr.mergeable == "CONFLICTING":
            self._handle_pr_conflict(task, rep)
            return
        if pr.updated_at and pr.updated_at == st.get("pr_updated_at"):
            # Nothing new on GitHub since last look, so any feedback is already processed:
            # a stable point to consider merging on the garden's own gates (a check rollup
            # can flip to green without bumping updated_at, so re-evaluate every poll).
            self._maybe_automerge(task, pr, rep)
            return
        st["pr_updated_at"] = pr.updated_at
        st["head_sha"] = pr.head_sha
        ci_note = ""
        if pr.checks == "FAILURE" and st.get("ci_failed_at") != pr.updated_at:
            st["ci_failed_at"] = pr.updated_at
            names = ", ".join(pr.failed_checks) or "unknown"
            ci_note = f"- **CI** is failing on this branch (failed checks: {names}). Investigate the failing checks and fix them."
            specs = list(self.cfg.get("checks.ci", []) or [])
            if specs:
                # The CI analyser runs as a detached check run, reaped a tick later (CG-182): the
                # tick never runs it in-process. The continuation (`_after_ci_check`) combines its
                # verdict with the GitHub feedback and starts (or reruns instead of) a revise round.
                self._dispatch_check_run(task, worktree=self.worktree_for(task), branch=task.branch or task.default_branch(),
                                         base=self.base_for(task), specs=specs, stage="ci", rep=rep, cont={"ci_note": ci_note},
                                         extra={"ci_rerun": int(st.get("ci_reruns", 0)) < 1})
                return
        fb = self.github.feedback_since(slug, number, task.last_dispatched_at)
        if fb.ignored:
            self._log_ignored_feedback(task, fb.ignored)
        self._apply_feedback(task, pr, fb, ci_note, rep)

    def _apply_feedback(self, task: Task, pr: PRInfo, fb: Any, ci_note: str, rep: TickReport) -> None:
        """Turn new PR feedback and/or a CI note into a revise round (or a human hand-off at the
        cap). Shared by `poll` and the CI check continuation so both route a failure the same way."""
        st = self.state.get(task.id)
        if not fb and not ci_note:
            # Feedback processed, nothing actionable: another stable point to consider merging.
            self._maybe_automerge(task, pr, rep)
            return
        pending = fb.to_markdown() + ("\n\n" + ci_note if ci_note else "")
        st["pending_feedback"] = pending
        st.pop("pending_feedback_easy", None)
        st.pop("pending_feedback_rebase", None)
        n = len(fb.items)
        note = f"{n} new review item(s)" if n else "CI failure"
        if n and ci_note:
            note += " + CI failure"
        self.events.emit("feedback", task.id, items=n, ci=bool(ci_note))
        if not bool(self.cfg.get("auto_revise", True)):
            self._transition(task, Status.CHANGES_REQUESTED, f"{note} (auto_revise off; dispatch by hand)", needs_human=True)
            rep.transitions.append(f"{task.id} -> changes_requested")
            return
        max_rev = int(self.cfg.get("max_revisions", 3))
        if int(st.get("revisions", 0)) >= max_rev:
            reason = f"{max_rev} revision rounds used"
            self._set_needs_human(task, "revision_cap", reason)
            self.events.emit("needs_human", task.id, stop_kind="revision_cap", reason=reason)
            self._transition(task, Status.CHANGES_REQUESTED, f"{note}, but {max_rev} revision rounds already used; needs a human", needs_human=True)
            rep.transitions.append(f"{task.id} -> changes_requested (cap)")
            return
        self._transition(task, Status.CHANGES_REQUESTED, note)
        rep.transitions.append(f"{task.id} -> changes_requested")

    def _after_ci_check(self, task: Task, run: Run, results: list[dict[str, Any]], cont: dict[str, Any], rep: TickReport) -> None:
        """Reap a CI analyser check run: a wholly-flaky verdict reruns CI instead of a revise
        round (once); otherwise the analyser's details join the CI note and the GitHub feedback."""
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if not slug or not number or not self.github.available:
            return
        try:
            pr = self.github.get_pr(slug, number)
        except (GitHubError, KeyError):
            return
        st = self.state.get(task.id)
        ci_note = str(cont.get("ci_note") or "")
        reran = [r for r in results if r.get("reran")]
        if reran:
            # The detached job already reran CI (it held the flaky-rerun budget); record it here.
            st["ci_reruns"] = int(st.get("ci_reruns", 0)) + 1
            task.log("CI failure judged flaky by checks; reran instead of dispatching a revise run")
            self.store.save(task)
            self.events.emit("ci_rerun", task.id, checks=[r.get("name") for r in reran])
            ci_note = ""
        elif check_failures(results):
            ci_note += "\n\n" + to_feedback(results, "CI check")
        fb = self.github.feedback_since(slug, number, task.last_dispatched_at)
        if fb.ignored:
            self._log_ignored_feedback(task, fb.ignored)
        self._apply_feedback(task, pr, fb, ci_note, rep)

    def _log_ignored_feedback(self, task: Task, ignored: list[dict[str, Any]]) -> None:
        """One task-log line and one event per skipped comment (a bot notice, or an author
        the garden does not trust), once: the same comment comes back on every poll until
        the next dispatch, so `state.json` remembers which ones were already logged."""
        st = self.state.get(task.id)
        seen = list(st.get("feedback_ignored") or [])
        logged = False
        for note in ignored:
            key = f"{note.get('author', '')}@{note.get('created', '')}"
            if key in seen:
                continue
            seen.append(key)
            reason = str(note.get("reason") or "notice")
            what = "feedback from an untrusted author ignored" if reason == "untrusted" else "bot notice ignored"
            task.log(f"{what}: {note['author']}: {str(note.get('body') or '').strip()[:200]}")
            self.events.emit("feedback_ignored", task.id, author=note.get("author", ""), reason=reason)
            logged = True
        if logged:
            st["feedback_ignored"] = seen[-50:]
            self.store.save(task)

    # ---- automerge ---------------------------------------------------------
    def _github_cfg(self, key: str, product: str, default: Any) -> Any:
        """A `github.<key>` setting, with a per-product override under `products.<product>.<key>`."""
        prod = self.cfg.product(product)
        if key in prod:
            return prod[key]
        return self.cfg.get(f"github.{key}", default)

    def _needs_second_review_round(self, product: str) -> bool:
        """Whether a PR against `product` needs a second approving round before automerge.

        A product with `self: true` (the garden's own repo) or `provides_tool: true` (the
        product that ships the `garden` binary) can change the loop that merges it, so one LLM
        review is not enough: it takes two approving rounds by default, or a person merging by
        hand. An explicit per-product `automerge_min_review_rounds` overrides this default."""
        p = self.cfg.product(product)
        return bool(self.cfg.product_self(product) or p.get("provides_tool"))

    def _automerge_enabled(self, task: Task) -> bool:
        if task.extra.get("automerge") is False:
            return False  # a task-level opt-out
        return bool(self._github_cfg("automerge", task.product, False))

    def _hard_tier_automerge(self, task: Task) -> bool:
        """Whether this hard-tier PR may merge under the two-round + scratch-merge policy
        (config `github.automerge_hard_tier`, default on). Only the hard tier is affected;
        easy and medium keep following `automerge_tiers`. When on, a hard-tier PR merges after
        two approving review rounds and the garden's own scratch-merge check (CG-191)."""
        if task.difficulty != "hard":
            return False
        return bool(self._github_cfg("automerge_hard_tier", task.product, True))

    def _scratch_merge_verified(self, task: Task) -> bool:
        """Whether the hard-tier scratch-merge check has passed for the current reviewed diff.
        The recorded result is keyed to `last_diff_hash`, so a revise round (a changed diff)
        invalidates it while a clean rebase (unchanged diff) keeps it."""
        st = self.state.get(task.id)
        sm = st.get("scratch_merge") or {}
        return bool(sm.get("ok")) and str(sm.get("diff") or "") == str(st.get("last_diff_hash") or "")

    def _automerge_gate(self, task: Task, pr: PRInfo, require_scratch: bool = True) -> tuple[bool, str]:
        """Whether every gate the loop already has is green, and the first reason it is not.
        The task must be `in_review` before this is called (a draft, a stall or a pending
        revise round have already taken it elsewhere)."""
        st = self.state.get(task.id)
        if st.get("needs_human"):
            # A rebase right before this merge can trigger a fresh review (rule 2 in
            # rebase.py) that hits the review cap: that sets this stop instead of a verdict,
            # so a merge must not proceed on the stale verdict recorded before the rebase.
            return False, "a needs-human stop is set"
        final_base = self.final_base_for(task)
        if pr.base and pr.base != final_base:
            parent = st.get("stack_parent") or pr.base
            return False, f"stacked on {parent}; waits for the restack"
        hard_tier = self._hard_tier_automerge(task)
        tiers = [str(x) for x in (self._github_cfg("automerge_tiers", task.product, ["easy", "medium"]) or [])]
        if task.difficulty not in tiers and not hard_tier:
            return False, f"tier `{task.difficulty}` is not in automerge_tiers ({', '.join(tiers) or 'none'})"
        rev = st.get("last_review") or {}
        if str(rev.get("verdict") or "") != "approve":
            return False, f"the automated review verdict is {rev.get('verdict') or 'not in yet'}, not approve"
        min_rounds = int(self._github_cfg("automerge_min_review_rounds", task.product, 1) or 0)
        if hard_tier:
            min_rounds = max(min_rounds, 2)  # a hard-tier PR merges only after two approving rounds
        if (self._needs_second_review_round(task.product)
                and "automerge_min_review_rounds" not in self.cfg.product(task.product)):
            min_rounds = max(min_rounds, 2)
        if int(st.get("review_rounds", 0)) < min_rounds:
            return False, f"only {int(st.get('review_rounds', 0))} review round(s) so far, need {min_rounds}"
        if str(st.get("pending_feedback") or "").strip():
            return False, "feedback is pending a revise run"
        review_run = st.get("review_run")
        if review_run:
            run = next((r for r in self.runs.runs_for(task.id) if r.run_id == review_run), None)
            # A pointer to a run that has since been superseded or otherwise closed (but
            # never cleared, e.g. by the orphan/dead-run sweeps) must not hold automerge
            # forever; a pointer with no run behind it at all still fails closed (CG-144).
            if run is None or run.status == "running":
                return False, "a run is in flight"
        elif any(r.task_id == task.id for r in self.active_runs()):
            return False, "a run is in flight"
        if pr.checks not in ("SUCCESS", ""):
            return False, f"the PR checks rollup is {pr.checks.lower() or 'pending'}"
        if pr.mergeable != "MERGEABLE":
            return False, f"GitHub reports the PR {pr.mergeable.lower() or 'mergeability unknown'}"
        if pr.review_decision == "CHANGES_REQUESTED":
            return False, "a human review requests changes"
        budget = self.budget_for(task)
        if budget and self.spent_for(task.key) >= budget:
            return False, f"phase {task.key} is over budget"
        guarded = self._guarded_diff_paths(task)
        if guarded:
            shown = ", ".join(guarded[:3]) + (" …" if len(guarded) > 3 else "")
            return False, f"the diff touches guarded paths ({shown}); merge by hand"
        if require_scratch and hard_tier and not self._scratch_merge_verified(task):
            sm = st.get("scratch_merge") or {}
            if str(sm.get("diff") or "") == str(st.get("last_diff_hash") or "") and not sm.get("ok"):
                return False, f"the hard-tier scratch-merge check failed ({sm.get('checks') or 'checks'})"
            return False, "the hard-tier scratch-merge check has not passed for this revision"
        return True, ""

    def _guarded_diff_paths(self, task: Task) -> list[str]:
        """The PR's changed paths that are too sensitive to automerge — garden*.yaml, any
        **/tasks/ file, .github/ or principles/. Empty (so the gate passes) when the worktree
        is absent and the diff cannot be read."""
        worktree = self.worktree_for(task)
        if not worktree.exists():
            return []
        base = self.final_base_for(task)
        return [p for p in gitops.diff_names(worktree, base) if _touches_guarded_path(p)]

    def _maybe_automerge(self, task: Task, pr: PRInfo, rep: TickReport) -> None:
        """Decide whether this PR is a merge candidate. When automerge is on and every gate is
        green, mark it (and record when it first became ready); the merge queue then rebases and
        merges only the head of the queue, one per tick (see RebaseMixin._run_merge_queue)."""
        if task.status != Status.IN_REVIEW:
            return  # drafts, changes_requested, etc. are not the garden's to merge
        st = self.state.get(task.id)
        if not self._automerge_enabled(task):
            self._queue_leave(task)
            return
        ok, reason = self._automerge_gate(task, pr)
        if not ok:
            if st.get("merge_head"):
                # The merge queue owns the in-flight head: a pending rollup after its pre-merge
                # rebase must not drop it here (that would rotate the head). _advance_merge_head
                # decides when the head leaves the queue, and keeps its ready_at until then.
                return
            # A hard-tier PR that clears every other gate gets the garden's own scratch-merge
            # check dispatched here; the recorded pass then clears the gate on a later tick.
            self._maybe_dispatch_scratch_merge(task, pr, rep)
            self._queue_hold(task, reason)
            return
        self._queue_join(task)

    def _maybe_dispatch_scratch_merge(self, task: Task, pr: PRInfo, rep: TickReport) -> None:
        """Hard-tier automerge (CG-191): before a hard-tier PR may merge, the garden runs its own
        scratch-merge check — the pre-PR suite on the branch rebased onto the base tip in a
        throwaway worktree. Dispatch it once every other gate is green and this revision has not
        already been verified, and not while another check for the task is in flight."""
        if not self._hard_tier_automerge(task):
            return
        st = self.state.get(task.id)
        if self._scratch_merge_verified(task):
            return  # this revision is already verified
        sm = st.get("scratch_merge") or {}
        if sm and str(sm.get("diff") or "") == str(st.get("last_diff_hash") or ""):
            return  # a result (a recorded failure) for this revision already stands
        if st.get("check_run"):
            return  # a check run is already in flight for this task
        ok, _ = self._automerge_gate(task, pr, require_scratch=False)
        if not ok:
            return  # something else holds the merge; don't spend a scratch run yet
        self._dispatch_scratch_merge(task, rep)

    def _cleanup(self, task: Task) -> None:
        try:
            gitops.remove_worktree(self.repo_for(task), self.worktree_for(task))
        except Exception as e:  # noqa: BLE001
            self.log(f"{task.id}: worktree cleanup failed: {e}")

    # ---- stacking ----------------------------------------------------------
    def stacked_children(self, task: Task) -> list[Task]:
        return [t for t in self.store.tasks().values()
                if self.state.get(t.id).get("stack_parent") == task.id and not t.status.terminal]

    def _parent_merged(self, task: Task) -> bool:
        """Whether this task's stack parent has reached a terminal status (merged): its branch is
        gone or going, so the child must target the final base, not the parent's branch."""
        st = self.state.get(task.id)
        if st.get("restack_pending"):
            return True
        parent_id = st.get("stack_parent")
        if not parent_id:
            return False
        parent = self.store.tasks().get(parent_id)
        return parent is not None and parent.status.terminal

    def _retarget_children_before_delete(self, task: Task) -> bool:
        """Before a parent's branch is deleted (on merge), point every open stacked-child PR at
        the final base so GitHub does not close it the instant the branch goes. Returns True when
        every child that needed retargeting was retargeted (so the caller may delete the branch),
        False when any retarget failed (so the caller keeps the branch and lets a later pass retry).
        The child's branch is rebased onto the final base later, by `_on_merged`/`_restack`."""
        slug = self.slug_for(task)
        if not (slug and self.github.available):
            return True
        all_ok = True
        for child in self.stacked_children(task):
            number = self._pr_number(child)
            new_base = self.final_base_for(child)
            if not number:
                continue
            try:
                pr = self.github.get_pr(slug, number)
            except (GitHubError, KeyError):
                continue
            if pr.state != "OPEN" or pr.base == new_base:
                continue
            try:
                self.github.update_pr(slug, number, base=new_base)
                self.events.emit("retargeted", child.id, parent=task.id, base=new_base)
                child.log(f"stack parent {task.id} merging; retargeted this PR to {new_base} before the parent branch is deleted")
                self.store.save(child)
            except GitHubError as e:
                all_ok = False
                self.log(f"{child.id}: could not retarget before parent branch delete: {e}")
        return all_ok

    def _on_merged(self, task: Task, rep: TickReport) -> None:
        if self.cfg.product(task.product).get("provides_tool"):
            try:
                self._note_tool_upgrade(task)
            except Exception as e:  # noqa: BLE001 - never let this block a merge
                self.log(f"{task.id}: tool upgrade check failed: {e}")
        for child in self.stacked_children(task):
            st = self.state.get(child.id)
            if child.status in (Status.RUNNING, Status.WAITING_HUMAN):
                st["restack_pending"] = True
                child.log(f"parent {task.id} merged; will rebase onto {self.final_base_for(child)} when the current run finishes")
                self.store.save(child)
                continue
            self._restack(child, rep)

    def _restack(self, child: Task, rep: TickReport) -> None:
        """Parent merged: retarget the child's PR to the final base and rebase its branch."""
        st = self.state.get(child.id)
        parent_id = st.get("stack_parent", "")
        new_base = self.final_base_for(child)
        st["pr_base"] = new_base
        st.pop("stack_parent", None)
        slug = self.slug_for(child)
        number = self._pr_number(child)
        if slug and number and self.github.available:
            try:
                self.github.update_pr(slug, number, base=new_base)
            except GitHubError as e:
                self.log(f"{child.id}: could not retarget PR: {e}")
        # The rebase-and-record helper (CG-197) folds in origin-only commits, rebases, force-pushes
        # and records a `rebase` run so the restack is counted like every other rebase path.
        outcome = self._rebase_and_record(child, new_base, reason=f"parent {parent_id} merged")
        if outcome.status != "conflict":
            child.log(f"parent {parent_id} merged; rebased onto {new_base} and retargeted the PR")
            self.store.save(child)
            self.events.emit("restacked", child.id, parent=parent_id, base=new_base, conflict=False)
            rep.transitions.append(f"{child.id} restacked onto {new_base}")
            return
        self.events.emit("restacked", child.id, parent=parent_id, base=new_base, conflict=True, files=outcome.files)
        # A textual conflict: an easy-tier rebase agent resolves it, not a full revise run.
        self._dispatch_rebase_agent(child, new_base, outcome.files, outcome.hunks, rep, f"parent {parent_id} merged")

    def _reopen_if_base_deleted(self, task: Task, slug: str | None, pr: PRInfo, rep: TickReport) -> bool:
        """A PR GitHub closed because its base branch was deleted (a stack parent that merged with
        `--delete-branch`) is not a task failure: reopen it onto the final base and rebase, or —
        when GitHub refuses to reopen — open a fresh PR from the same head branch. Returns True
        when the PR was recovered, so the caller stops treating the close as a failure."""
        if not task.status.pr_open:
            return False  # a terminal/failed task keeps its close; only the active review flow recovers
        final_base = self.final_base_for(task)
        number = self._pr_number(task)
        if not (slug and number and self.github.available):
            return False
        if not pr.base or pr.base == final_base:
            return False  # a PR already on the final base was not closed by a base deletion
        try:
            deleted = self.github.base_ref_deleted(slug, number)
        except GitHubError:
            deleted = False
        if not deleted:
            try:
                deleted = not self.github.branch_exists(slug, pr.base)
            except GitHubError:
                deleted = False
        if not deleted:
            return False
        st = self.state.get(task.id)
        branch = pr.head or task.branch or task.default_branch()
        try:
            self.github.reopen_pr(slug, number)
            self.events.emit("pr_reopened", task.id, pr=task.pr, base=final_base, how="reopen")
        except GitHubError as reopen_err:
            self.log(f"{task.id}: could not reopen PR #{number} after base deletion: {reopen_err}")
            try:
                new = self.github.create_pr(slug, branch, final_base,
                                            pr.title or f"{task.id}: {task.title}", pr.body or task.body)
            except GitHubError as create_err:
                self.log(f"{task.id}: could not recreate PR after base deletion: {create_err}")
                return False
            task.pr = new.url
            st["pr_number"] = new.number
            self.events.emit("pr_reopened", task.id, pr=new.url, base=final_base, how="recreate")
        st["pr_state"] = "OPEN"
        task.log(f"PR was closed when its base branch `{pr.base}` was deleted; recovered it onto {final_base}")
        self.store.save(task)
        # Retarget the recovered PR to the final base and rebase the branch onto it.
        self._restack(task, rep)
        return True

    def _handle_pr_conflict(self, task: Task, rep: TickReport) -> None:
        """PR is CONFLICTING with its base: run a rebase round. Mechanical first (no model),
        an easy-tier agent only on a real textual conflict — never a full revise run, and never
        against `max_revisions` (see RebaseMixin.mechanical_rebase)."""
        base = self.base_for(task)
        self.mechanical_rebase(task, base, rep, reason=f"PR conflicts with {base}")

    def _on_parent_closed(self, task: Task, rep: TickReport) -> None:
        for child in self.stacked_children(task):
            reason = f"stack parent {task.id} was closed without merging"
            self._set_needs_human(child, "parent_closed", reason)
            self.events.emit("needs_human", child.id, stop_kind="parent_closed", reason=reason)
            notify(self.cfg.data, child.id, "needs_human", reason, child.pr or "")
            child.log(f"stack parent {task.id} closed without merging; this PR targets a dead branch and needs a human")
            self.store.save(child)
            rep.transitions.append(f"{child.id} needs human (parent closed)")
