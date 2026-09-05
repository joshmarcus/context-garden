"""Poll: what GitHub says about an open PR (merged, closed, feedback, CI), automerge, stacking and restacks."""

from __future__ import annotations

from typing import Any

from .. import gitops
from ..checks import failures as check_failures
from ..checks import run_checks, to_feedback
from ..github import GitHubError, PRInfo, mark_garden_comment
from ..model import Status, Task, now_iso
from ..notify import notify
from .report import TickReport


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
        since = task.last_dispatched_at
        fb = self.github.feedback_since(slug, number, since)
        if fb.ignored:
            for note in fb.ignored:
                task.log(f"bot notice ignored: {note['author']}: {note['body'].strip()[:200]}")
            self.store.save(task)
        st["head_sha"] = pr.head_sha
        ci_note = ""
        if pr.checks == "FAILURE" and st.get("ci_failed_at") != pr.updated_at:
            st["ci_failed_at"] = pr.updated_at
            names = ", ".join(pr.failed_checks) or "unknown"
            ci_note = f"- **CI** is failing on this branch (failed checks: {names}). Investigate the failing checks and fix them."
            specs = list(self.cfg.get("checks.ci", []) or [])
            if specs:
                results = run_checks(specs, self.check_ctx(task, task.branch, self.base_for(task)),
                                     cwd=self.worktree_for(task) if self.worktree_for(task).exists() else None,
                                     timeout=int(self.cfg.get("checks.timeout_seconds", 600)))
                for r in results:
                    self.events.emit("check", task.id, stage="ci", name=r.get("name"), status=r.get("status"), summary=r.get("summary", ""))
                flaky = [r for r in results if r.get("status") == "flaky"]
                if flaky and len(flaky) == len([r for r in results if r.get("status") != "pass"]) and int(st.get("ci_reruns", 0)) < 1:
                    st["ci_reruns"] = int(st.get("ci_reruns", 0)) + 1
                    for r in flaky:
                        if r.get("retry_command"):
                            import subprocess

                            subprocess.run(str(r["retry_command"]), shell=True, check=False, capture_output=True, timeout=120)
                    task.log("CI failure judged flaky by checks; reran instead of dispatching a revise run")
                    self.store.save(task)
                    self.events.emit("ci_rerun", task.id, checks=[r.get("name") for r in flaky])
                    ci_note = ""
                elif check_failures(results):
                    ci_note += "\n\n" + to_feedback(results, "CI check")
        if not fb and not ci_note:
            # Feedback processed, nothing actionable: another stable point to consider merging.
            self._maybe_automerge(task, pr, rep)
            return
        pending = fb.to_markdown() + ("\n\n" + ci_note if ci_note else "")
        st["pending_feedback"] = pending
        st.pop("pending_feedback_easy", None)
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

    # ---- automerge ---------------------------------------------------------
    def _github_cfg(self, key: str, product: str, default: Any) -> Any:
        """A `github.<key>` setting, with a per-product override under `products.<product>.<key>`."""
        prod = self.cfg.product(product)
        if key in prod:
            return prod[key]
        return self.cfg.get(f"github.{key}", default)

    def _automerge_enabled(self, task: Task) -> bool:
        if task.extra.get("automerge") is False:
            return False  # a task-level opt-out
        return bool(self._github_cfg("automerge", task.product, False))

    def _automerge_gate(self, task: Task, pr: PRInfo) -> tuple[bool, str]:
        """Whether every gate the loop already has is green, and the first reason it is not.
        The task must be `in_review` before this is called (a draft, a stall or a pending
        revise round have already taken it elsewhere)."""
        st = self.state.get(task.id)
        final_base = self.final_base_for(task)
        if pr.base and pr.base != final_base:
            parent = st.get("stack_parent") or pr.base
            return False, f"stacked on {parent}; waits for the restack"
        tiers = [str(x) for x in (self._github_cfg("automerge_tiers", task.product, ["easy", "medium"]) or [])]
        if task.difficulty not in tiers:
            return False, f"tier `{task.difficulty}` is not in automerge_tiers ({', '.join(tiers) or 'none'})"
        rev = st.get("last_review") or {}
        if str(rev.get("verdict") or "") != "approve":
            return False, f"the automated review verdict is {rev.get('verdict') or 'not in yet'}, not approve"
        min_rounds = int(self._github_cfg("automerge_min_review_rounds", task.product, 1) or 0)
        if int(st.get("review_rounds", 0)) < min_rounds:
            return False, f"only {int(st.get('review_rounds', 0))} review round(s) so far, need {min_rounds}"
        if str(st.get("pending_feedback") or "").strip():
            return False, "feedback is pending a revise run"
        if st.get("review_run") or any(r.task_id == task.id for r in self.active_runs()):
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
        return True, ""

    def _maybe_automerge(self, task: Task, pr: PRInfo, rep: TickReport) -> None:
        """When automerge is on and every gate is green, merge the PR the garden opened.
        The next poll sees it MERGED and moves the task to `done`, restacking children."""
        if task.status != Status.IN_REVIEW:
            return  # drafts, changes_requested, etc. are not the garden's to merge
        st = self.state.get(task.id)
        if not self._automerge_enabled(task):
            st.pop("automerge_blocked", None)
            return
        ok, reason = self._automerge_gate(task, pr)
        if not ok:
            if st.get("automerge_blocked") != reason:
                st["automerge_blocked"] = reason
                self.log(f"{task.id}: automerge held: {reason}")
            return
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if not slug or not number:
            return
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

    def _cleanup(self, task: Task) -> None:
        try:
            gitops.remove_worktree(self.repo_for(task), self.worktree_for(task))
        except Exception as e:  # noqa: BLE001
            self.log(f"{task.id}: worktree cleanup failed: {e}")

    # ---- stacking ----------------------------------------------------------
    def stacked_children(self, task: Task) -> list[Task]:
        return [t for t in self.store.tasks().values()
                if self.state.get(t.id).get("stack_parent") == task.id and not t.status.terminal]

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
        wt = self.worktree_for(child)
        branch = child.branch or child.default_branch()
        repo = self.repo_for(child)
        try:
            if not wt.exists():
                gitops.prepare_worktree(repo, wt, branch, new_base)
            # Fold in any commits that only exist on origin/<branch> before rebasing, so the
            # force-push below never discards them (e.g. something merged into this branch).
            ok, files = gitops.sync_remote_branch(wt, branch)
            if ok:
                ok, files = gitops.rebase_onto(wt, gitops.base_ref(wt, new_base))
        except gitops.GitError as e:
            ok, files = False, [str(e)]
        if ok:
            try:
                gitops.push(wt, branch, force=True)
            except gitops.GitError as e:
                self.log(f"{child.id}: push after rebase failed: {e}")
            child.log(f"parent {parent_id} merged; rebased onto {new_base} and retargeted the PR")
            self.store.save(child)
            self.events.emit("restacked", child.id, parent=parent_id, base=new_base, conflict=False)
            rep.transitions.append(f"{child.id} restacked onto {new_base}")
            return
        st["pending_feedback"] = (
            f"- **garden**: the parent task {parent_id} merged into `{new_base}`, but rebasing this branch onto "
            f"`origin/{new_base}` conflicts in: {', '.join(files) or 'unknown files'}. Run `git fetch origin && git rebase origin/{new_base}`, "
            f"resolve the conflicts keeping the intent of both sides, and continue the rebase. The runner will force-push the rebased branch. "
            f"Then drop from the PR description anything `{new_base}` now already contains; the description must cover only what this branch still adds. "
            f"Put the corrected description in `pr_body` (only if it must change)."
        )
        st.pop("pending_feedback_easy", None)
        st["force_push"] = True
        self.events.emit("restacked", child.id, parent=parent_id, base=new_base, conflict=True, files=files)
        if child.status.pr_open:
            self._transition(child, Status.CHANGES_REQUESTED, f"parent {parent_id} merged; rebase onto {new_base} conflicts ({', '.join(files)}); revise run will resolve")
            rep.transitions.append(f"{child.id} -> changes_requested (rebase)")
        else:
            child.log(f"parent {parent_id} merged; rebase onto {new_base} conflicts; next run must resolve")
            self.store.save(child)

    def _handle_pr_conflict(self, task: Task, rep: TickReport) -> None:
        """PR is CONFLICTING with its base: try an automatic rebase; on conflict set feedback for a revise run."""
        st = self.state.get(task.id)
        base = self.base_for(task)
        branch = task.branch or task.default_branch()
        wt = self.worktree_for(task)
        repo = self.repo_for(task)
        try:
            if not wt.exists():
                gitops.prepare_worktree(repo, wt, branch, base)
            ok, files = gitops.rebase_onto(wt, gitops.base_ref(wt, base))
        except gitops.GitError as e:
            ok, files = False, [str(e)]
        self.events.emit("conflict", task.id, base=base, files=files, resolved=ok)
        if ok:
            try:
                gitops.push(wt, branch, force=True)
                task.log(f"PR conflicted with {base}; rebased automatically and force-pushed")
                self.store.save(task)
            except gitops.GitError as e:
                self.log(f"{task.id}: push after conflict rebase failed: {e}")
            return
        st["pending_feedback"] = (
            f"- **garden**: this PR conflicts with `{base}`. "
            f"Run `git fetch origin && git rebase origin/{base}`, "
            f"resolve the conflicts keeping the intent of both sides"
            + (f" (conflicting files: {', '.join(files)})" if files else "")
            + ". The runner will force-push the rebased branch. "
            + f"Then drop from the PR description anything `{base}` now already contains; the description must cover only what this branch still adds. "
            + "Put the corrected description in `pr_body` (only if it must change)."
        )
        st.pop("pending_feedback_easy", None)
        st["force_push"] = True
        max_rev = int(self.cfg.get("max_revisions", 3))
        if int(st.get("revisions", 0)) >= max_rev:
            reason = f"PR conflicts with {base} and {max_rev} revision rounds already used"
            self._set_needs_human(task, "revision_cap", reason)
            self.events.emit("needs_human", task.id, stop_kind="revision_cap", reason=reason)
            self._transition(task, Status.CHANGES_REQUESTED,
                             f"PR conflicts with {base} ({', '.join(files) or 'unknown files'}); revision cap reached; needs a human",
                             needs_human=True)
        else:
            self._transition(task, Status.CHANGES_REQUESTED,
                             f"PR conflicts with {base} ({', '.join(files) or 'unknown files'}); revise run will rebase and resolve")
        rep.transitions.append(f"{task.id} -> changes_requested (conflict)")

    def _on_parent_closed(self, task: Task, rep: TickReport) -> None:
        for child in self.stacked_children(task):
            reason = f"stack parent {task.id} was closed without merging"
            self._set_needs_human(child, "parent_closed", reason)
            self.events.emit("needs_human", child.id, stop_kind="parent_closed", reason=reason)
            notify(self.cfg.data, child.id, "needs_human", reason, child.pr or "")
            child.log(f"stack parent {task.id} closed without merging; this PR targets a dead branch and needs a human")
            self.store.save(child)
            rep.transitions.append(f"{child.id} needs human (parent closed)")
