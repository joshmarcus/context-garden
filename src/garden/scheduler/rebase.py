"""Rebase as its own run mode: bring a PR forward by the cheapest thing that works.

Three rules live here (see docs/architecture.md, beside stacking):

1. A conflict is rebased mechanically first — `git rebase origin/<base>` with no model. A clean
   apply is the whole round: a `rebase` run record (no harness call), a lease push and a re-run
   of the pre-PR checks. Only a textual conflict starts an agent, on the easy tier, with a brief
   that carries the conflicting hunks and the rule "resolve the conflict, change nothing else".
   A rebase round has its own counter (`state[task].rebases`) and never touches `max_revisions`
   or `review.max_rounds`.
2. After any rebase the branch's `git patch-id --stable` from before the rebase is compared with
   the one from after (CG-210): a hash of the diff's own +/- content, blind to the hunk-header
   line numbers and context that shift whenever an unrelated commit lands on the base near the
   branch's hunks, so a plain hash of the diff text would flag as "changed" a rebase that changed
   nothing of the PR's own patch. When the ids match, the last verdict is kept, "rebased; patch id
   unchanged; verdict kept" is logged, and no review is dispatched. A patch id that changed — a
   textual conflict an agent resolved, or a rebase that folded the branch's own commit away as
   already-applied elsewhere — is reviewed as usual.
3. Automerge is a queue: candidates are ordered oldest-approved-first and only the head is
   processed. The compatibility policy rebases and checks it; an explicit product opt-out
   merges a clean approved exact head as-is after a fresh GitHub gate. Once the queue picks a
   head it keeps it: a head whose rollup
   is still running after the pre-merge rebase is "in flight" (a `merge_head` marker holding
   its `automerge_ready_at`), the queue does not pick another head while one is in flight, and
   it merges the head the moment the rollup goes green. A branch already on the base's tip is
   not rebased or pushed. A head leaves the queue only on a conflict, a failed check, a changed
   diff that needs a review, a closed PR or a human request for changes — the reason is logged
   and the next-oldest candidate becomes head.
"""

from __future__ import annotations

import hashlib
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
    artifacts: dict[str, dict[str, object]] = field(default_factory=dict)


class RebaseMixin:
    def _review_approval_is_proven(self, task: Task, st: dict[str, object]) -> bool:
        """Whether the stored approval is backed by its immutable review run and head."""
        review_head = str(st.get("last_review_head") or "")
        review_run_id = str(st.get("last_review_run") or "")
        if (not review_head or not review_run_id
                or str((st.get("last_review") or {}).get("verdict") or "") != "approve"):
            return False
        review_run = next((candidate for candidate in reversed(self.runs.runs_for(task.id))
                           if candidate.run_id == review_run_id and candidate.mode == "review"), None)
        if review_run is None or review_run.status != "done":
            return False
        snapshot_head = str((review_run.env_snapshot or {}).get("review_head") or "")
        result_verdict = str((review_run.result or {}).get("verdict") or "")
        return snapshot_head == review_head and result_verdict == "approve"

    def _derived_approved_head(self, task: Task, st: dict[str, object]) -> str:
        """Validate and return the last mechanically derived approved head, if any.

        The original review head/run remain immutable.  Every hop must name a durable rebase
        run whose recorded before/after heads form one chain and whose patch ids match.
        """
        lineage = st.get("derived_review_approval")
        if not isinstance(lineage, dict) or not self._review_approval_is_proven(task, st):
            return ""
        review_head = str(st.get("last_review_head") or "")
        review_run_id = str(st.get("last_review_run") or "")
        if (str(lineage.get("review_head") or "") != review_head
                or str(lineage.get("review_run") or "") != review_run_id):
            return ""
        hops = lineage.get("rebases")
        if not isinstance(hops, list) or not hops:
            return ""
        runs = {candidate.run_id: candidate for candidate in self.runs.runs_for(task.id)}
        current = review_head
        for hop in hops:
            if not isinstance(hop, dict):
                return ""
            before = str(hop.get("from_head") or "")
            after = str(hop.get("head") or "")
            run_id = str(hop.get("run") or "")
            candidate = runs.get(run_id)
            snapshot = (candidate.env_snapshot or {}) if candidate else {}
            if (not before or not after or before != current or candidate is None
                    or candidate.mode != "rebase" or candidate.status != "done"
                    or not candidate.patch_id_before
                    or candidate.patch_id_before != candidate.patch_id_after
                    or str(snapshot.get("rebase_head_before") or "") != before
                    or str(snapshot.get("rebase_local_head_before") or "") != before
                    or str(snapshot.get("rebase_head_after") or "") != after):
                return ""
            current = after
        return current if current == str(lineage.get("head") or "") else ""

    def _effective_approved_head(self, task: Task, st: dict[str, object]) -> str:
        """The reviewed head, advanced only through a fully proven mechanical lineage."""
        return self._derived_approved_head(task, st) or str(st.get("last_review_head") or "")

    def _extend_approved_head_lineage(self, task: Task, run: Run) -> bool:
        """Bind an unchanged-patch rebase head to the existing approval without rewriting it."""
        st = self.state.get(task.id)
        snapshot = run.env_snapshot or {}
        before = str(snapshot.get("rebase_head_before") or "")
        local_before = str(snapshot.get("rebase_local_head_before") or "")
        after = str(snapshot.get("rebase_head_after") or "")
        if (not before or local_before != before or not after or not run.patch_id_before
                or run.patch_id_before != run.patch_id_after
                or not self._review_approval_is_proven(task, st)):
            return False
        existing = st.get("derived_review_approval")
        if existing is None:
            if before != str(st.get("last_review_head") or ""):
                return False
            hops: list[dict[str, str]] = []
        else:
            if not isinstance(existing, dict) or self._derived_approved_head(task, st) != before:
                return False
            hops = [dict(hop) for hop in existing.get("rebases", [])]
        hops.append({"run": run.run_id, "from_head": before, "head": after,
                     "patch_id": run.patch_id_after})
        st["derived_review_approval"] = {
            "review_run": str(st.get("last_review_run") or ""),
            "review_head": str(st.get("last_review_head") or ""),
            "head": after,
            "rebases": hops,
        }
        return True

    def _reviewed_branch_is_current(self, task: Task, wt: Path, branch: str, base: str) -> bool:
        """Prove a reviewed remote head already contains the latest base without rewriting it."""
        st = self.state.get(task.id)
        run_id = str(st.get("last_review_run") or "")
        review_run = next((run for run in reversed(self.runs.runs_for(task.id))
                           if run.run_id == run_id and run.mode == "review"), None)
        if review_run is None or review_run.status != "done" or review_run.base != base:
            return False
        snapshot = review_run.env_snapshot or {}
        reviewed_head = str(snapshot.get("review_head") or "")
        reviewed_base_head = str(snapshot.get("review_base_head") or "")
        reviewed_diff = str(snapshot.get("review_diff_hash") or "")
        if not reviewed_head or not reviewed_base_head or not reviewed_diff:
            return False
        if not gitops.fetch(wt):
            return False
        try:
            local_head = gitops.rev_parse(wt, "HEAD")
            remote_head = gitops.rev_parse(wt, f"origin/{branch}")
            current_base_head = gitops.rev_parse(wt, gitops.base_ref(wt, base))
            current_diff = gitops.diff_hash(wt, base)
        except gitops.GitError:
            return False
        return (local_head == remote_head == reviewed_head
                and current_base_head == reviewed_base_head
                and gitops.is_ancestor(wt, current_base_head, local_head)
                and current_diff == reviewed_diff)

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
        if self.external_stack_owner(task):
            self.log(f"{task.id}: external stack owner controls {branch}; skipped automatic rebase")
            return RebaseOutcome("error", wt, branch)
        repo = self.repo_for(task)
        canonical_enabled = str(self.cfg.product_checkout(task.product).get("strategy") or "worktree") == "in_place"
        run = self.runs.new_run(task.id, "local", mode="rebase") if canonical_enabled else None
        if run is not None:
            run.branch, run.base, run.worktree, run.difficulty = branch, base, str(wt), "easy"
            runner = self.runner_for(task, "local")
            canonical = self.prepare_canonical_run(task, run, runner, branch, base)
            if canonical is not None:
                wt = canonical
                run.worktree = str(wt)
                run.save()
        patch_before = ""
        rebase_head_before = ""
        rebase_local_head_before = ""
        artifact_dir: Path | None = None
        try:
            if not wt.exists():
                with self._local_staging_admission("rebase checkout materialization"):
                    gitops.prepare_worktree(repo, wt, branch, base)
            if skip_if_current and self._reviewed_branch_is_current(task, wt, branch, base):
                if reason:
                    task.log(f"{reason}; reviewed remote head already contains {base}; not rebased or pushed")
                    self.store.save(task)
                return RebaseOutcome("current", wt, branch)
            # The patch id of the branch's own diff before anything moves — compared against the
            # same id computed after the rebase, this is how rule 2 tells a mechanical shift of
            # line numbers and context (CG-210) apart from a genuine change to the PR's own patch.
            # Approval lineage is allowed only when the local patch being measured starts at
            # the exact remote PR head. A stale worktree may still be rebased safely, but it
            # cannot use that patch comparison to derive approval for the pushed head.
            fetched = gitops.fetch(wt)
            rebase_local_head_before = gitops.rev_parse(wt, "HEAD")
            rebase_head_before = gitops.remote_head(wt, branch) if fetched else ""
            patch_before = gitops.patch_id(wt, base)
            identity = hashlib.sha256(
                f"{branch}\0{base}\0{rebase_local_head_before}".encode()
            ).hexdigest()[:16]
            artifact_dir = self.cfg.garden_dir / "rebase-conflicts" / task.id / identity
            ok, files, hunks = gitops.sync_and_rebase(wt, branch, base, artifact_dir=artifact_dir)
        except gitops.GitError as e:
            ok, files, hunks = False, [str(e)], {}
        if not ok:
            artifacts: dict[str, dict[str, object]] = {}
            manifest = artifact_dir / "manifest.json" if artifact_dir is not None else None
            if manifest is not None and manifest.exists():
                import json

                artifacts = json.loads(manifest.read_text())
                for item in artifacts.values():
                    item["manifest"] = str(manifest)
            if run is not None:
                run.mode = "canonical"
                run.status = "done"
                run.error = "mechanical rebase conflicts; agent resolution required"
                run.finished_at = now_iso()
                run.save()
            return RebaseOutcome("conflict", wt, branch, run=run, files=files, hunks=hunks, artifacts=artifacts)
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
                if run is not None:
                    run.mode = "canonical"
                    run.status = "done"
                    run.cost_usd = 0.0
                    run.finished_at = now_iso()
                    run.save()
                return RebaseOutcome("current", wt, branch, run=run)
        if run is None:
            run = self.runs.new_run(task.id, "local", mode="rebase")
            run.branch, run.base, run.worktree, run.difficulty = branch, base, str(wt), "easy"
        run.env_snapshot = {
            "rebase_head_before": rebase_head_before,
            "rebase_local_head_before": rebase_local_head_before,
            "rebase_head_after": gitops.rev_parse(wt, "HEAD"),
        }
        try:
            # Bind the rewrite to the PR head captured before any rebase work. sync_and_rebase
            # fetches again, so an implicit lease would otherwise accept an author push that
            # landed between those fetches and silently carry it through this approval lineage.
            note = (gitops.push(wt, branch, lease=rebase_head_before)
                    if rebase_head_before else gitops.push(wt, branch, force=True))
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
        run.patch_id_before = patch_before
        run.patch_id_after = gitops.patch_id(wt, base)
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
        if self._manual_reserved(task):
            return "held"
        self._refuse_if_phase_not_admitted(task)
        outcome = self._rebase_and_record(task, base, skip_if_current=skip_if_current, reason=reason)
        if outcome.status == "conflict":
            self.events.emit("rebase", task.id, base=base, files=outcome.files, resolved=False, how="agent")
            self._dispatch_rebase_agent(task, base, outcome.files, outcome.hunks, outcome.artifacts, rep, reason)
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
                               artifacts: dict[str, dict[str, object]], rep: TickReport, reason: str) -> None:
        """A plain rebase conflicted textually: queue an easy-tier agent that resolves it. The
        actual dispatch happens in the dispatch phase (see DispatchMixin.dispatch, mode `rebase`),
        so it waits for a free slot like any other run. The force-push flag is set for the push
        after the agent resolves the conflict."""
        st = self.state.get(task.id)
        st["rebase_pending"] = True
        # A new textual conflict starts a new auxiliary retry budget.  Do not let a retry used
        # by an earlier conflict make this independent rebase park immediately (CG-330).
        st.pop("rebase_run_retries", None)
        st.pop("rebase_retry_files", None)
        st["rebase_base"] = base
        st["rebase_files"] = list(files)
        st["rebase_hunks"] = hunks
        st["rebase_artifacts"] = artifacts
        self._queue_leave(task)  # a conflict takes the task off the merge queue
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
        """After a rebase, compare the patch id of the branch's diff from before the rebase to
        after (CG-210): matching ids mean the rebase only shifted line numbers and context around
        someone else's commits, not the PR's own patch, so the last verdict is kept and no review
        is dispatched. A patch id that changed — a real conflict resolution, or a rebase that
        folded the branch's own commit away as already-applied elsewhere — is reviewed as usual.
        A hash of the raw diff text would flag the former as changed too (the incident this
        fixes); patch-id is blind to it because it hashes only the +/- content, not context."""
        st = self.state.get(task.id)
        verdict_kept = bool(run.patch_id_before) and run.patch_id_before == run.patch_id_after
        require_current_base = bool(
            self._github_cfg("automerge_require_current_base", task.product, True)
        )
        if verdict_kept and not require_current_base and not self._extend_approved_head_lineage(task, run):
            # The patch is unchanged, but the rebase did not start at the approved head (or at
            # the end of a previously proven chain).  A new author push must never inherit an
            # older approval. Queue one exact-head review; pending/review pointers make this
            # idempotent if the continuation is replayed after a restart.
            st.pop("derived_review_approval", None)
            self.events.emit("rebase", task.id, run=run.run_id,
                             patch_id_before=run.patch_id_before,
                             patch_id_after=run.patch_id_after, verdict_kept=False,
                             approval_lineage=False)
            task.log(f"rebased; patch id unchanged but approval lineage was not proven; exact-head review queued{cost}")
            self.store.save(task)
            pending = list(st.get("pending_reviews") or [])
            if not st.get("review_run") and not any(item.get("kind") == "review" for item in pending):
                self._dispatch_or_defer_reviews(
                    task, [{"kind": "review", "count_round": False}], rep, work_run=run
                )
            rep.transitions.append(f"{task.id} rebased; exact-head review queued")
            return
        if verdict_kept:
            self.events.emit("rebase", task.id, run=run.run_id, patch_id_before=run.patch_id_before,
                             patch_id_after=run.patch_id_after, verdict_kept=True)
            task.log(f"rebased; patch id unchanged; verdict kept{cost}")
            self.store.save(task)
            if st.pop("pending_triage_notify", False) and task.status == Status.AWAITING_TRIAGE:
                notify(self.cfg.data, task.id, "awaiting_triage", f"rebased; patch id unchanged; verdict kept{cost}", task.pr or "")
            rep.transitions.append(f"{task.id} rebased; verdict kept")
            return
        # The patch actually changed: keep `last_diff_hash` (used elsewhere for stall detection
        # and the scratch-merge marker) in sync with the diff this rebase produced.
        st.pop("derived_review_approval", None)
        wt = self.worktree_for(task)
        diff_h = gitops.diff_hash(wt, base) if wt.exists() else ""
        if diff_h:
            st["last_diff_hash"] = diff_h
        self.events.emit("rebase", task.id, run=run.run_id, patch_id_before=run.patch_id_before,
                         patch_id_after=run.patch_id_after, verdict_kept=False)
        self._maybe_review(task, run, rep)

    # ---- merge queue (rule 3) ----------------------------------------------
    def _run_merge_queue(self, rep: TickReport) -> None:
        """Automerge as a queue that keeps its head. Once a head is picked and rebased it stays
        the head (an in-flight `merge_head`) until it merges or leaves the queue, so a pending
        rollup after the pre-merge rebase never rotates the head. When nothing is in flight, the
        oldest-approved candidate becomes the head."""
        head = self._current_merge_head()
        if head is not None:
            if self.task_is_authorized(head):
                with self.task_effect(head, f"merge-queue:{head.id}"):
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
            if not self.task_is_authorized(t):
                continue
            if t.status != Status.IN_REVIEW:
                continue
            if self._manual_reserved(t):
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
        candidate = candidates[0][2]
        with self.task_effect(candidate, f"merge-queue:{candidate.id}"):
            self._merge_candidate(candidate, rep)

    def _merge_head_pending(self) -> bool:
        """Whether a pre-merge rebase's check run is in flight for a would-be head. Between the
        tick that dispatches that detached check and the tick that reaps it, the task has no
        `merge_head` marker (the reap sets it) yet is already the queue's chosen head, so the
        queue must not pick another candidate meanwhile."""
        for t in self.store.tasks().values():
            if not self.task_is_authorized(t):
                continue
            info = self.state.get(t.id).get("check_run") or {}
            if info.get("stage") == "merge_rebase" and (info.get("cont") or {}).get("merge_head"):
                return True
        return False

    def _current_merge_head(self) -> Task | None:
        """The task currently in flight (rebased, waiting for its rollup), or None. A stale marker
        left on a task that is no longer an `in_review` automerge candidate is cleared here."""
        head: Task | None = None
        for t in self.store.tasks().values():
            if not self.task_is_authorized(t):
                continue
            st = self.state.get(t.id)
            if not st.get("merge_head"):
                continue
            if head is None and t.status == Status.IN_REVIEW and self._automerge_enabled(t):
                head = t
            else:
                self._queue_drop_head(t)
        return head

    def _merge_candidate(self, task: Task, rep: TickReport) -> None:
        """Pick this candidate as the head. The compatibility policy rebases onto the final base;
        an explicit product opt-out merges a clean approved exact head without rewriting it. The
        queue remains serial and refreshes GitHub before either merge path."""
        if self._manual_reserved(task):
            return
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
            self._queue_hold(task, reason)
            return
        if not bool(self._github_cfg("automerge_require_current_base", task.product, True)):
            # Keep the queue serial, but do not rewrite a clean, approved exact head solely
            # because another PR advanced the base. _advance_merge_head fetches GitHub again;
            # its gate catches a newly conflicting, pending, failed, or changed head.
            self._queue_head(task)
            self._advance_merge_head(task, rep)
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
            self._queue_head(task)
            self._advance_merge_head(task, rep)
            return
        if outcome != "clean":
            return  # checking (a check run holds the head) / conflict / push error handled elsewhere
        # No checks configured: the rebase moved the branch and force-pushed it synchronously.
        if st.get("review_run") or st.get("needs_human"):
            return  # the rebase changed the diff: a new review round (or a human) now owns it
        self._queue_head(task, announce=True)

    def _advance_merge_head(self, task: Task, rep: TickReport) -> None:
        """Refresh and act on the queue head. Merge it (no rebase or push) the moment the gate
        passes; keep it as head while its rollup is still running; drop
        it — logging why — only on a hard reason (a conflict, a failed check, a changed diff now
        in review, a closed PR or a human change request), so the next candidate becomes head."""
        if self._manual_reserved(task):
            return
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
        self._queue_hold(task, reason)

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
        # a retarget is deferred or fails, so no child is orphaned; a later pass deletes it once
        # they are clear.
        delete_branch = self._retarget_children_before_delete(task)
        if not delete_branch:
            self.log(f"{task.id}: keeping the branch on merge; a stacked child PR was not retargeted")
        try:
            self.github.merge_pr(slug, number, method=method, delete_branch=delete_branch,
                                 expected_head=pr.head_sha)
        except GitHubError as e:
            self._queue_hold(task, f"merge call failed: {e}", keep=True)
            rep.errors.append(f"{task.id}: automerge failed: {e}")
            return
        rounds = int(st.get("review_rounds", 0))
        self._queue_leave(task)
        st["automerged"] = {"at": now_iso(), "method": method, "review_run": review_run,
                            "verdict": "approve", "review_rounds": rounds}
        self.events.emit("automerged", task.id, pr=task.pr, method=method, review_run=review_run,
                         actor="automated_scheduler",
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
