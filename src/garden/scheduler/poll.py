"""Poll: what GitHub says about an open PR (merged, closed, feedback, CI), automerge, stacking and restacks."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from dataclasses import asdict
from typing import Any

from .. import gitops
from ..checks import failures as check_failures
from ..checks import to_feedback
from ..github import Feedback, GitHubError, PRInfo, RepositorySlug
from ..ci_status import CIStatus, resolve_status, status_reason
from ..model import Status, Task, now_iso, phase_refusal
from ..notify import notify
from ..runs import Run
from .feedback import merge_pending_feedback
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
    _PR_OBSERVATIONS = "__open_prs__"

    @staticmethod
    def _ci_failure_identity(pr: PRInfo) -> str:
        return json.dumps({
            "head": pr.head_sha,
            "state": pr.checks,
            "failed_checks": sorted(pr.failed_checks),
        }, sort_keys=True)

    @staticmethod
    def _feedback_key(item: dict[str, Any]) -> str:
        identity = str(item.get("id") or "")
        if identity and not identity.endswith(":"):
            return identity
        stable = "\0".join(str(item.get(k) or "") for k in
                           ("kind", "author", "created", "path", "line", "state", "body"))
        return hashlib.sha256(stable.encode()).hexdigest()

    def _new_feedback(self, record: dict[str, Any], feedback: Feedback) -> Feedback:
        seen = set(record.get("feedback_seen") or [])
        items = [item for item in feedback.items if self._feedback_key(item) not in seen]
        ignored = [item for item in feedback.ignored if self._feedback_key(item) not in seen]
        return Feedback(items=items, ignored=ignored)

    def _remember_feedback(
        self, record: dict[str, Any], feedback: Feedback, *, count_items: bool = True
    ) -> None:
        seen_order = list(record.get("feedback_seen") or [])
        seen = set(seen_order)
        added = 0
        for item in [*feedback.items, *feedback.ignored]:
            key = self._feedback_key(item)
            if key not in seen:
                seen.add(key)
                seen_order.append(key)
                if count_items and item in feedback.items:
                    added += 1
        record["feedback_seen"] = seen_order
        record["feedback_count"] = int(record.get("feedback_count") or 0) + added

    def _remember_pr_feedback(self, product: str, number: int, feedback: Feedback) -> None:
        repository = dict(self.state.get(self._PR_OBSERVATIONS).get(product) or {})
        rows = list(repository.get("prs") or [])
        for row in rows:
            if int(row.get("number") or 0) == number:
                self._remember_feedback(row, feedback)
                break
        repository["prs"] = rows
        self.state.get(self._PR_OBSERVATIONS)[product] = repository

    def refresh_open_prs(
        self, tasks: dict[str, Task], rep: TickReport
    ) -> tuple[dict[tuple[str, int], tuple[PRInfo, Feedback]], set[str]]:
        """Refresh the Board's repository observations in this tick's existing poll phase.

        The returned objects are also consumed by linked-task polling, so an open linked PR
        is fetched once.  The previous snapshot survives provider failures and is marked stale.
        """
        result: dict[tuple[str, int], tuple[PRInfo, Feedback]] = {}
        suppressed: set[str] = set()
        root = self.state.get(self._PR_OBSERVATIONS)
        products = {str(product) for product in (self.cfg.data.get("products") or {})}
        linked = {
            (task.product, self._pr_number(task)): task
            for task in tasks.values()
            if task.pr
        }
        for product in sorted(products):
            route = self.cfg.product_github(product)
            if not route:
                continue
            prior = dict(root.get(product) or {})
            if float(prior.get("retry_at") or 0) > time.time():
                suppressed.add(product)
                continue
            slug = RepositorySlug(route["slug"], route["host"])
            try:
                prs = self.github.list_open_prs(slug, self.cfg.product_project_users(product))
                old_rows = {int(row["number"]): row for row in prior.get("prs", [])}
                rows = []
                for pr in prs:
                    old = dict(old_rows.get(pr.number) or {})
                    # Identity-based deduplication deliberately rereads provider pages. A
                    # timestamp cursor alone can skip a comment sharing the cursor timestamp.
                    # This makes equal timestamps, repeated pages, and restarts safe.
                    fb = self.github.feedback_since(slug, pr.number, "")
                    linked_task = linked.get((product, pr.number))
                    if linked_task is not None and "feedback_seen" not in old and linked_task.last_dispatched_at:
                        # Existing tasks used last_dispatched_at as their feedback cursor before
                        # repository observations persisted identities. Seed those historical
                        # identities during the first upgraded poll so handled comments cannot
                        # consume another revision attempt. Newer feedback remains actionable,
                        # and every later poll uses identity deduplication exclusively.
                        cursor = linked_task.last_dispatched_at
                        historical = Feedback(
                            items=[item for item in fb.items if str(item.get("created") or "") <= cursor],
                            ignored=[item for item in fb.ignored if str(item.get("created") or "") <= cursor],
                        )
                        self._remember_feedback(old, historical, count_items=False)
                    fresh = self._new_feedback(old, fb)
                    if (product, pr.number) not in linked:
                        self._remember_feedback(old, fresh)
                    timestamps = [str(i.get("created") or "") for i in [*fb.items, *fb.ignored]]
                    if timestamps:
                        old["feedback_since"] = max(timestamps)
                    old.update(asdict(pr))
                    old["number"] = pr.number
                    old["new_feedback"] = len(fresh.items)
                    rows.append(old)
                    result[(product, pr.number)] = (pr, fresh)
                root[product] = {"prs": rows, "refreshed_at": now_iso(), "error": "", "stale": False,
                                 "failures": 0, "retry_at": 0}
            except (GitHubError, OSError, ValueError) as exc:
                suppressed.add(product)
                failures = int(prior.get("failures") or 0) + 1
                rate_limited = "rate limit" in str(exc).lower() or "429" in str(exc)
                prior.update({"error": str(exc), "stale": True, "failures": failures,
                              "retry_at": time.time() + min(900, 30 * (2 ** min(failures - 1, 5))) if rate_limited else 0})
                root[product] = prior
                rep.errors.append(f"{product}: open PR refresh failed: {exc}")
        return result, suppressed

    def _ci_status(self, task: Task, pr: PRInfo) -> CIStatus:
        provider = str(self.cfg.get("ci.status_provider", "github") or "github")
        required = bool(self.cfg.get("ci.required", False)
                        or self.cfg.product_setup(task.product).get("worker_push") is True)
        policy = dict(self.cfg.get("ci", {}) or {})
        policy["required"] = required
        return resolve_status(provider, self.cfg.garden_dir, task.id, pr, policy)

    # ---- poll --------------------------------------------------------------
    def poll(self, task: Task, rep: TickReport, observed: tuple[PRInfo, Feedback] | None = None) -> None:
        if not self.github.available:
            return
        slug = self.slug_for(task)
        if not slug:
            return
        st = self.state.get(task.id)
        number = self._pr_number(task)
        if not number:
            return
        pr, observed_feedback = observed or (self.github.get_pr(slug, number), None)
        if not pr.head_sha:
            pr.head_sha = str(st.get("head_sha") or "")
        st["pr_state"] = pr.state
        st["review_decision"] = pr.review_decision
        st["checks"] = pr.checks
        validation = self.cfg.product_validation(task.product)
        provider = validation["provider"]
        expects_rollup = provider in ("actions", "status") or (
            provider == "legacy" and bool(self.cfg.get("checks.ci", []))
        )
        st["ci_missing"] = bool(expects_rollup and not pr.checks)
        if st["ci_missing"]:
            st["ci_diagnostic"] = (
                "GitHub Actions returned no result; confirm Actions is enabled and the workflow triggers for this branch"
                if provider == "actions" else
                "the configured status provider returned no result; confirm its installation and repository permissions"
            )
        else:
            st.pop("ci_diagnostic", None)
        st["failed_checks"] = list(pr.failed_checks)
        st["mergeable"] = pr.mergeable
        st["head_sha"] = pr.head_sha
        ci_status = self._ci_status(task, pr)
        previous_ci = st.get("ci_status") or {}
        st["ci_status"] = ci_status.to_dict()
        if previous_ci != st["ci_status"]:
            self.events.emit("ci_status", task.id, **ci_status.to_dict())
        st["last_polled"] = now_iso()
        if self._manual_reserved(task):
            # Observation continues in Manual mode, including terminal PR state, but every
            # lifecycle consequence stays parked until the reservation is removed. Keep the
            # observation separate from the normal processing cursors so the first ordinary
            # poll still sees feedback and draft/head changes made during the reservation.
            st["manual_observed_pr"] = {
                "draft": bool(pr.is_draft),
                "head_sha": pr.head_sha,
                "updated_at": pr.updated_at,
            }
            return
        if pr.state == "MERGED":
            final_base = self.final_base_for(task)
            if pr.base and pr.base != final_base:
                self._mark_merged_into_parent(task, pr, rep)
                return
            by_garden = bool(st.get("automerged"))
            self._transition(task, Status.DONE, f"PR merged{' by the garden' if by_garden else ''}: {task.pr}",
                             base_merged=True)
            rep.transitions.append(f"{task.id} -> done")
            self._on_merged(task, rep, head_sha=pr.head_sha)
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
        # A frozen child can be left stacked when its parent merges.  Defer the restack
        # without touching its PR or branch, then resume this same reconciliation after
        # the phase is unfrozen. A closed PR is handled above by its base-deletion recovery.
        if self._parent_merged(task):
            self._restack(task, rep)
            return
        was_draft = bool(st.get("pr_draft"))
        st["pr_draft"] = bool(pr.is_draft)
        # A manually assigned task remains an observation only. The person who claimed it
        # owns every source/review/merge transition until they explicitly finish it.
        if (task.runner or self.cfg.product_runner(task.product)) == "manual":
            if observed_feedback is not None:
                self._remember_pr_feedback(task.product, number, observed_feedback)
            return
        if task.status == Status.AWAITING_TRIAGE and not pr.is_draft:
            self.events.emit("triaged", task.id, pr=task.pr, by="github")
            self._transition(task, Status.IN_REVIEW, "marked ready for review on GitHub; triage done")
            rep.transitions.append(f"{task.id} -> in_review (triaged)")
        elif task.status == Status.IN_REVIEW and pr.is_draft and not was_draft:
            self._transition(task, Status.AWAITING_TRIAGE, "converted back to draft on GitHub")
            rep.transitions.append(f"{task.id} -> awaiting_triage")
        if task.status == Status.CHANGES_REQUESTED:
            deferred = st.get("deferred_ci_check")
            if deferred and not phase_refusal(self.store.phase(task.product, task.phase), task):
                st.pop("deferred_ci_check", None)
                st["ci_failed_at"] = pr.updated_at
                self._dispatch_check_run(
                    task, worktree=self.worktree_for(task), branch=task.branch or task.default_branch(),
                    base=self.base_for(task), specs=list(deferred["specs"]), stage="ci", rep=rep,
                    cont={"ci_note": str(deferred["ci_note"]), "head": str(deferred["head"])},
                    extra={"ci_rerun": int(st.get("ci_reruns", 0)) < 1},
                )
                return
            return  # already waiting for a revise slot (or a human)
        if pr.mergeable == "CONFLICTING":
            self._handle_pr_conflict(task, rep)
            return
        ci_note = ""
        # Check rollups change independently of the PR timestamp. Process a new terminal
        # failure before the timestamp shortcut below or an approved PR can sit in review,
        # visibly red but never routed back to revision. Persisting the head and failure
        # shape keeps repeated observations idempotent across ticks and controller restarts;
        # analyser run IDs separately deduplicate the bounded retry attempts they initiate.
        ci_identity = self._ci_failure_identity(pr)
        # Reaping a flaky analyser and polling happen in the same tick. Give the requested
        # external rerun that one observation boundary to replace the old failed rollup;
        # otherwise this poll immediately analyses the same attempt again and spends the
        # bounded retry before the provider can publish its result. A changed failure shape
        # is a new failure and remains immediately actionable.
        waiting_identity = st.get("ci_rerun_waiting_for")
        waiting_for_rerun = waiting_identity == ci_identity
        if waiting_identity:
            st.pop("ci_rerun_waiting_for", None)
        if waiting_for_rerun:
            st.pop("ci_failed_at", None)
        failure_key = f"{ci_status.provider}:{ci_status.queried_sha}:{ci_status.state}"
        github_ci_failure = (provider in ("actions", "status", "legacy")
                             and pr.checks == "FAILURE"
                             and not waiting_for_rerun
                             and st.get("ci_failed_at") != ci_identity)
        exact_ci_failure = (ci_status.state == "failure"
                            and st.get("ci_failed_at") not in (failure_key, pr.updated_at)
                            and not waiting_for_rerun)
        if github_ci_failure or exact_ci_failure:
            failure_identity = failure_key if exact_ci_failure else ci_identity
            names = ", ".join(ci_status.failures if exact_ci_failure else pr.failed_checks) or "unknown"
            ci_note = f"- **CI** is failing on this branch (failed checks: {names}). Investigate the failing checks and fix them."
            specs = list(self.cfg.get("checks.ci", []) or [])
            phase_hold = phase_refusal(self.store.phase(task.product, task.phase), task)
            if specs and phase_hold:
                # Keep the CI facts and route the feedback into the held task, but do not
                # spend a detached analyser run until the phase is released.
                st["deferred_ci_check"] = {"specs": specs, "ci_note": ci_note, "head": pr.head_sha}
            else:
                st["ci_failed_at"] = failure_identity
            if specs:
                # The CI analyser runs as a detached check run, reaped a tick later (CG-182): the
                # tick never runs it in-process. The continuation (`_after_ci_check`) combines its
                # verdict with the GitHub feedback and starts (or reruns instead of) a revise round.
                if not phase_hold:
                    self._dispatch_check_run(task, worktree=self.worktree_for(task), branch=task.branch or task.default_branch(),
                                             base=self.base_for(task), specs=specs, stage="ci", rep=rep, cont={"ci_note": ci_note, "head": pr.head_sha},
                                             extra={"ci_rerun": int(st.get("ci_reruns", 0)) < 1},
                                             source_head=pr.head_sha)
                    return
        if pr.updated_at and pr.updated_at == st.get("pr_updated_at") and not observed_feedback and not ci_note:
            # No PR metadata, feedback, or terminal CI result changed. A check rollup can
            # still flip to green without bumping updated_at, so re-evaluate merge gates.
            self._maybe_automerge(task, pr, rep)
            return
        st["pr_updated_at"] = pr.updated_at
        st["head_sha"] = pr.head_sha
        fb = observed_feedback if observed_feedback is not None else self.github.feedback_since(slug, number, task.last_dispatched_at)
        if fb.ignored:
            self._log_ignored_feedback(task, fb.ignored)
        self._apply_feedback(task, pr, fb, ci_note, rep)
        self._remember_pr_feedback(task.product, number, fb)

    def _apply_feedback(self, task: Task, pr: PRInfo, fb: Any, ci_note: str, rep: TickReport,
                        *, replace_ci: bool = False) -> None:
        """Turn new PR feedback and/or a CI note into a revise round (or a human hand-off at the
        cap). Shared by `poll` and the CI check continuation so both route a failure the same way."""
        st = self.state.get(task.id)
        if not fb and not ci_note and not replace_ci:
            # Feedback processed, nothing actionable: another stable point to consider merging.
            self._maybe_automerge(task, pr, rep)
            return
        before = str(st.get("pending_feedback") or "")
        for item in fb.items:
            # Comments are PR-scoped instructions, not CI attestations for this head.
            # Their original commit is retained in the rendered feedback when available.
            identity = ({"kind": item.get("kind"), "id": item["id"]} if item.get("id") else item)
            key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
            merge_pending_feedback(st, pr.head_sha, "github:" + key, Feedback(items=[item]).to_markdown())
        if ci_note or replace_ci:
            merge_pending_feedback(st, pr.head_sha, "ci", ci_note)
        if not st.get("pending_feedback"):
            if task.status == Status.CHANGES_REQUESTED and not st.get("needs_human"):
                self._transition(task, Status.IN_REVIEW, "current checks resolved the pending CI feedback")
            self.state.save()
            return
        if not bool(self.effective("auto_revise", True, task.product)) and not st.get("needs_human"):
            self._set_needs_human(task, "manual_revision", "automatic revisions are disabled; full feedback is ready for manual handoff")
        if task.status == Status.CHANGES_REQUESTED and st.get("pending_feedback") == before:
            self.state.save()
            return
        st.pop("pending_feedback_easy", None)
        st.pop("pending_feedback_rebase", None)
        # A concurrent producer already queued this revision. Enrich its brief without
        # another transition, notification, or revision-cap decision.
        if task.status == Status.CHANGES_REQUESTED:
            self.state.save()
            return
        n = len(fb.items)
        note = f"{n} new review item(s)" if n else "CI failure"
        if n and ci_note:
            note += " + CI failure"
        self.events.emit("feedback", task.id, items=n, ci=bool(ci_note))
        if not bool(self.effective("auto_revise", True, task.product)):
            self._transition(task, Status.CHANGES_REQUESTED, f"{note} (auto_revise off; dispatch by hand)", needs_human=True)
            rep.transitions.append(f"{task.id} -> changes_requested")
            return
        max_rev = int(self.cfg.get("max_revisions", 3))
        if not self.cfg.revision_policy()["enabled"] and int(st.get("revisions", 0)) >= max_rev:
            reason = f"{max_rev} revision rounds used"
            self._set_needs_human(task, "revision_cap", reason,
                                  delegated_recovery=bool(self.cfg.get("recovery.delegated", False)))
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
        head = str((run.env_snapshot or {}).get("ci_head") or cont.get("head") or "")
        if (run.task_id != task.id or run.mode != "check" or not head
                or pr.state != "OPEN" or not task.status.pr_open
                or (cont.get("head") and cont["head"] != head)
                or pr.head_sha != head or str(st.get("head_sha") or "") != head):
            self.log(f"{task.id}: ignored CI feedback from {run.run_id}: missing or obsolete head")
            return
        applied = st.get("applied_ci_feedback") or {}
        applied_runs = list(applied.get("runs") or []) if applied.get("head") == head else []
        if run.run_id in applied_runs:
            return
        ci_note = str(cont.get("ci_note") or "")
        reran = [r for r in results if r.get("reran")]
        if reran:
            ci_note = ""
        elif pr.checks == "SUCCESS":
            ci_note = ""
        elif check_failures(results):
            ci_note += "\n\n" + to_feedback(results, "CI check")
        repository = self.state.get(self._PR_OBSERVATIONS).get(task.product) or {}
        record = next((row for row in repository.get("prs", [])
                       if int(row.get("number") or 0) == number), {})
        fb = self._new_feedback(record, self.github.feedback_since(slug, number, ""))
        if fb.ignored:
            self._log_ignored_feedback(task, fb.ignored)
        # Save the consumed run and rerun accounting in the same state write as the
        # composed feedback. A crash after that write cannot charge/reapply this run.
        st["applied_ci_feedback"] = {"head": head, "runs": [*applied_runs, run.run_id]}
        if reran:
            st["ci_reruns"] = int(st.get("ci_reruns", 0)) + 1
            # The provider may continue reporting the same head and FAILURE after the
            # requested rerun. Suppress the poll later in this tick, then let a subsequent
            # observation through the analyser once more. The ci_reruns limit prevents
            # another flaky rerun, while the head checks reject obsolete commits.
            st["ci_rerun_waiting_for"] = self._ci_failure_identity(pr)
        self._apply_feedback(task, pr, fb, ci_note, rep, replace_ci=True)
        self.state.save()
        if reran:
            task.log("CI failure judged flaky by checks; reran instead of dispatching a revise run")
            self.store.save(task)
            self.events.emit("ci_rerun", task.id, checks=[r.get("name") for r in reran])
        self._remember_pr_feedback(task.product, number, fb)

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
        if self.external_stack_owner(task):
            return False  # a stack owner, not garden, decides whether its branch may merge
        if task.extra.get("automerge") is False:
            return False  # a task-level opt-out
        return bool(self._github_cfg("automerge", task.product, False))

    def _hard_tier_automerge(self, task: Task) -> bool:
        """Whether this hard-tier PR may merge under the scratch-merge policy.

        Only the hard tier is affected; easy and medium keep following `automerge_tiers`.
        Hard tasks use the configured review minimum and also require the garden's own
        scratch-merge check.
        """
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

    def _automerge_min_review_rounds(self, task: Task) -> int:
        """Return the automated-review minimum for a task's configured product policy."""
        min_rounds = int(self._github_cfg("automerge_min_review_rounds", task.product, 1) or 0)
        self_product_default = (self.cfg.product_self(task.product)
                                and "automerge_min_review_rounds" not in self.cfg.product(task.product))
        if self_product_default:
            # The second opinion is supplied by a current-head persona or a human, so only
            # one automated review round is required by the default self-product policy.
            return max(min_rounds, 1)
        if (self._needs_second_review_round(task.product)
                and "automerge_min_review_rounds" not in self.cfg.product(task.product)):
            return max(min_rounds, 2)
        return min_rounds

    def _automerge_gate(self, task: Task, pr: PRInfo, require_scratch: bool = True) -> tuple[bool, str]:
        """Whether every gate the loop already has is green, and the first reason it is not.
        The task must be `in_review` before this is called (a draft, a stall or a pending
        revise round have already taken it elsewhere)."""
        st = self.state.get(task.id)
        try:
            phase_hold = phase_refusal(self.store.phase(task.product, task.phase), task)
        except KeyError:
            phase_hold = ""
        if phase_hold:
            return False, phase_hold
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
        require_current_base = bool(
            self._github_cfg("automerge_require_current_base", task.product, True)
        )
        reviewed_head = self._effective_approved_head(task, st)
        if not require_current_base and not pr.head_sha:
            return False, "GitHub did not report the current PR head"
        if not require_current_base and reviewed_head != pr.head_sha:
            return False, "the approved review is not for the current PR head"
        min_rounds = self._automerge_min_review_rounds(task)
        self_product_default = (self.cfg.product_self(task.product)
                                and "automerge_min_review_rounds" not in self.cfg.product(task.product))
        if int(st.get("review_rounds", 0)) < min_rounds:
            return False, f"only {int(st.get('review_rounds', 0))} review round(s) so far, need {min_rounds}"
        if (self_product_default and int(st.get("review_rounds", 0)) >= 1
                and pr.review_decision != "APPROVED"
                and not any(str(item.get("head") or "") == str(pr.head_sha or "")
                            for item in st.get("persona_reviews", []) if isinstance(item, dict))):
            return False, "the second review must be a persona review or human approval"
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
        validation = self.cfg.product_validation(task.product)
        provider = validation["provider"]
        if provider in ("actions", "status"):
            if not pr.checks:
                if provider == "actions":
                    return False, "GitHub Actions has no result (disabled, unavailable, or not triggered)"
                return False, "the configured status provider has no result (unavailable or insufficient permission)"
            if pr.checks != "SUCCESS":
                return False, f"the required {provider} validation is {pr.checks.lower()}"
        elif provider == "command":
            if not pr.head_sha or st.get("validation_head") != pr.head_sha:
                return False, "the configured validation command has no passing result for the exact PR head"
        elif provider == "legacy":
            if self.cfg.product_setup(task.product).get("worker_push") is True and not pr.checks:
                return False, "worker CI is enabled but the PR has no CI result yet"
            if pr.checks not in ("SUCCESS", ""):
                return False, f"the PR checks rollup is {pr.checks.lower() or 'pending'}"
            if not require_current_base and pr.checks != "SUCCESS":
                return False, "the exact-head PR checks have not reported success"
        ci_status = self._ci_status(task, pr)
        if ci_status.state != "not_required" and not ci_status.green:
            return False, status_reason(ci_status)
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
        product_patterns = self.cfg.product_protected_paths(task.product)
        return [p for p in gitops.diff_names(worktree, base)
                if _touches_guarded_path(p) or any(fnmatch.fnmatch(p, pattern) for pattern in product_patterns)]

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

    # ---- merged into a non-base branch (CG-228) -----------------------------
    def _mark_merged_into_parent(self, task: Task, pr: PRInfo, rep: TickReport) -> None:
        """The PR merged, but into a stack parent's branch rather than the product's base: the
        task's commits sit on that branch now, not on the base, so `done` would be a lie (the
        CG-225 incident: a dependent was approved against a "done" dependency whose code had
        never reached main). Record the parent branch's tip right after absorbing this PR — not
        this task's own branch tip, which a squash merge would rewrite to a new sha — so a later
        poll of the parent can check whether that content actually made it onto the base."""
        st = self.state.get(task.id)
        parent_id = str(st.get("stack_parent") or "")
        parent_label = parent_id or pr.base
        repo = self.repo_for(task)
        sha = ""
        try:
            gitops.fetch(repo)
            sha = gitops.rev_parse(repo, gitops.base_ref(repo, pr.base))
        except gitops.GitError:
            sha = ""
        st["merged_into_parent"] = {"parent": parent_id, "branch": pr.base, "sha": sha}
        for k in ("needs_human", "pending_feedback"):
            st.pop(k, None)
        self._queue_leave(task)
        final_base = self.final_base_for(task)
        self._transition(task, Status.MERGED_INTO_PARENT,
                         f"PR merged into {parent_label}'s branch (`{pr.base}`), not the base `{final_base}`; "
                         f"will be done once {parent_label} reaches the base")
        rep.transitions.append(f"{task.id} -> merged_into_parent")

    def _promote_if_ancestor(self, child: Task, parent: Task, rep: TickReport, parent_ref: str = "") -> None:
        """`child` already merged into `parent`'s branch (Status.MERGED_INTO_PARENT) and `parent`
        has just reached the base itself: promote `child` to `done`, but only once its commits
        (the parent branch's tip recorded at the time, see `_mark_merged_into_parent`) actually
        show up as an ancestor of `parent_ref` -- the parent PR's own head sha, as GitHub reported
        it right before that PR merged, not the base's new tip. A squash or rebase merge (the
        garden's own default `automerge_method` is squash) folds the parent's whole branch into
        one brand-new commit on the base, so a child's original sha is never literally an ancestor
        of the base tip even though its changes are right there in that commit's tree; the parent
        PR's pre-merge head, by contrast, is the real, unrewritten commit the parent branch was
        sitting on, and it still carries the child's commits as ancestors no matter which merge
        method folded that branch into the base. Falls back to the base's tip (the original,
        squash-unsafe check) only when no head sha was recorded. Not automatic even so: a
        force-push or a rewritten rebase between the two merges could in principle have dropped
        the child's commits before the parent's own merge."""
        st = self.state.get(child.id)
        info = st.get("merged_into_parent") or {}
        sha = str(info.get("sha") or "")
        final_base = self.final_base_for(child)
        repo = self.repo_for(child)
        ancestor = False
        target = ""
        if sha:
            try:
                gitops.fetch(repo)
                target = parent_ref or gitops.base_ref(repo, final_base)
                ancestor = gitops.is_ancestor(repo, sha, target)
                # GitHub normally reports the parent's final head on its merged PR, but a
                # provider can leave that field stale while its branch still exists.  The
                # fetched branch is the authoritative revision in that case: only accept it
                # when it contains the recorded child revision, never merely because it is
                # present.
                if not ancestor and parent.branch:
                    branch_target = gitops.base_ref(repo, parent.branch)
                    if branch_target != target and gitops.is_ancestor(repo, sha, branch_target):
                        target = branch_target
                        ancestor = True
            except gitops.GitError:
                ancestor = False
        if not ancestor:
            child.log(f"parent {parent.id} merged to {final_base}, but this task's commits are not on "
                     f"{final_base} yet; still waiting")
            self.store.save(child)
            return
        st.pop("stack_parent", None)
        self._transition(child, Status.DONE,
                         f"parent {parent.id} merged to {final_base}; this task's commits are now on {final_base}",
                         base_merged=True)
        rep.transitions.append(f"{child.id} -> done")
        self._on_merged(child, rep, head_sha=target)
        self._cleanup(child)

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
        if self.external_stack_owner(task):
            return True
        slug = self.slug_for(task)
        if not (slug and self.github.available):
            return True
        all_ok = True
        parent_branch = task.branch or task.default_branch()
        for child in self.stacked_children(task):
            number = self._pr_number(child)
            new_base = self.final_base_for(child)
            if not number:
                continue
            try:
                pr = self.github.get_pr(slug, number)
            except (GitHubError, KeyError):
                continue
            if pr.state != "OPEN":
                continue
            try:
                self._refuse_if_closed_or_frozen(child)
            except RuntimeError:
                # A held child keeps its PR and stack base unchanged.  Returning False
                # also preserves the parent branch, so GitHub cannot close that PR
                # before the phase is unfrozen and the ordinary restack can resume.
                return False
            if self.external_stack_owner(child):
                if pr.base == parent_branch:
                    reason = (f"external stack owner must retarget its PR from {parent_branch} "
                              f"after stack parent {task.id} merges")
                    self._set_needs_human(child, "external_stack_retarget", reason)
                    self.events.emit("needs_human", child.id,
                                     stop_kind="external_stack_retarget", reason=reason)
                    child.log(reason)
                    self.store.save(child)
                    return False
                continue
            if pr.base == new_base:
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

    def _on_merged(self, task: Task, rep: TickReport, head_sha: str = "") -> None:
        """`task` just reached the base (its PR merged there). `head_sha` is that PR's own head
        sha as GitHub reported it right before the merge -- the real, unrewritten commit the
        branch was sitting on, still valid to check ancestry against however a squash or rebase
        merge folded it into the base (see `_promote_if_ancestor`)."""
        if self.cfg.product(task.product).get("provides_tool"):
            try:
                self._note_tool_upgrade(task)
            except Exception as e:  # noqa: BLE001 - never let this block a merge
                self.log(f"{task.id}: tool upgrade check failed: {e}")
        for child in self.stacked_children(task):
            if self.external_stack_owner(child):
                reason = f"external stack owner must reconcile dependency branch after {task.id} merged"
                self._set_needs_human(child, "external_stack_changed", reason)
                self.events.emit("needs_human", child.id, stop_kind="external_stack_changed", reason=reason)
                child.log(reason)
                self.store.save(child)
                continue
            st = self.state.get(child.id)
            if child.status == Status.MERGED_INTO_PARENT:
                self._promote_if_ancestor(child, task, rep, head_sha)
                continue
            if child.status in (Status.RUNNING, Status.WAITING_HUMAN):
                st["restack_pending"] = True
                child.log(f"parent {task.id} merged; will rebase onto {self.final_base_for(child)} when the current run finishes")
                self.store.save(child)
                continue
            self._restack(child, rep)

    def _restack(self, child: Task, rep: TickReport) -> None:
        """Parent merged: retarget the child's PR to the final base and rebase its branch."""
        if self.external_stack_owner(child):
            return
        st = self.state.get(child.id)
        parent_id = st.get("stack_parent", "")
        try:
            self._refuse_if_closed_or_frozen(child)
        except RuntimeError as refusal:
            # Retain the stack relationship unchanged while the phase is held.  The poll
            # path retries after an unfreeze, rather than losing the reconciliation work.
            if not st.get("restack_pending"):
                st["restack_pending"] = True
                child.log(f"parent {parent_id} merged; restack deferred: {refusal}")
                self.store.save(child)
            return
        new_base = self.final_base_for(child)
        st.pop("restack_pending", None)
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
        self._dispatch_rebase_agent(child, new_base, outcome.files, outcome.hunks, outcome.artifacts,
                                    rep, f"parent {parent_id} merged")

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
        if self.external_stack_owner(task):
            reason = "external stack owner must resolve this PR conflict; garden did not rebase or force-push it"
            self._set_needs_human(task, "external_stack_conflict", reason)
            self.events.emit("needs_human", task.id, stop_kind="external_stack_conflict", reason=reason)
            return
        if self.worker_run_in_flight(task.id):
            return  # a worker is writing this branch right now (CG-220); try again next tick
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
