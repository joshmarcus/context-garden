"""Dispatch: the queue, slots, stacking and the run a worker gets; plus the stuck-task audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import gitops
from ..brief import build_brief
from ..graph import blockers, ready, stack_parents
from ..model import Phase, Status, Task, ensure_open, now_iso, phase_refusal
from ..notify import notify
from ..runner.base import Runner
from ..runs import Run
from .report import TickReport


class DispatchMixin:
    # ---- dispatch ----------------------------------------------------------
    def _refuse_if_closed_or_frozen(self, task: Task) -> None:
        """The single gate every dispatch (tick, retry, revise, trial, `garden dispatch`/`take`,
        the web dispatch button) passes through: a closed phase always refuses; a frozen one
        refuses unless the task carries a freeze exception."""
        try:
            ph: Phase | None = self.store.phase(task.product, task.phase)
        except KeyError:
            return
        refusal = phase_refusal(ph, task)
        if refusal:
            raise RuntimeError(refusal)

    def dispatch_ready(self, rep: TickReport) -> None:
        tasks = self.store.tasks()
        max_rev = int(self.cfg.get("max_revisions", 3))
        # Rebase rounds go first: they are the cheapest work and they unblock a merge. A rebase
        # round has its own counter and is not bounded by max_revisions.
        queue: list[tuple[Task, str]] = [
            (t, "rebase") for t in tasks.values()
            if t.status == Status.CHANGES_REQUESTED
            and self.state.get(t.id).get("rebase_pending")
            and not self.state.get(t.id).get("needs_human")
        ]
        queue += [
            (t, "revise") for t in tasks.values()
            if t.status == Status.CHANGES_REQUESTED
            and self.state.get(t.id).get("pending_feedback")
            and not self.state.get(t.id).get("rebase_pending")
            and not self.state.get(t.id).get("needs_human")
            # a conflict/stale-base rebase round is exempt from the revision cap (CG-139)
            and (self.state.get(t.id).get("pending_feedback_rebase")
                 or int(self.state.get(t.id).get("revisions", 0)) < max_rev)
        ]
        queue += [(t, "work") for t in ready(tasks, stack=self.stack_enabled) if not self._edit_pending(t)]
        phases = {ph.key: ph for p in self.store.products() for ph in p.phases}
        for task, mode in queue:
            ph = phases.get(task.key)
            if ph is not None and phase_refusal(ph, task):
                continue  # the phase is closed or frozen; nothing dispatches into it without an exception
            if self.slots_free() <= 0:
                break
            if self.budget_exceeded(task):
                continue
            runner = self.runner_for(task)
            if not runner.detached:
                continue  # manual tasks are taken by a human, not auto-dispatched
            if runner.harness and self.is_harness_paused(runner.harness.name):
                continue  # the harness hit a quota/spend-limit stop; a probe resumes it on its own
            try:
                self.dispatch(task, mode=mode, runner=runner)
                rep.dispatched.append(f"{task.id}({mode})")
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{task.id}: dispatch failed: {e}")
                self._transition(task, Status.FAILED, f"dispatch failed: {e}")
        self._drain_pending_reviews(tasks, rep)

    def _audit_stuck(self, rep: TickReport) -> None:
        """Backstop: any non-terminal task with no active run and no dispatchable next
        round is stuck — a hand edit, a killed check, or a future bug left it with nothing
        scheduled and nothing on the Inbox. Flag it `needs_human` so it surfaces as a card
        (resume with one more round, or send it back) instead of sitting silent."""
        tasks = self.store.tasks()
        active = {r.task_id for r in self.runs.active()}
        ready_ids = {t.id for t in ready(tasks, stack=self.stack_enabled)}
        max_rev = int(self.cfg.get("max_revisions", 3))
        for t in tasks.values():
            if t.status.terminal or t.status == Status.RUNNING:
                continue  # running/terminal tasks are accounted for (reap handles a lost run)
            st = self.state.get(t.id)
            if st.get("needs_human"):
                continue  # already a card
            if (t.id in active or st.get("review_run") or st.get("edit_run")
                    or st.get("trial", {}).get("status") in ("running", "comparing")):
                continue  # a run is on it
            # A stored pending_feedback always comes with the changes_requested transition
            # (CG-140): nothing dispatches from in_review, so feedback parked there while the
            # task stays in_review would sit forever and hold automerge silently.
            manual_task = False
            if t.status == Status.IN_REVIEW and str(st.get("pending_feedback") or "").strip():
                reason = "pending feedback recorded but the task is in_review, not changes_requested"
            # These statuses wait on a human or GitHub and have their own Inbox handling.
            elif t.status in (Status.WAITING_HUMAN, Status.AWAITING_TRIAGE, Status.IN_REVIEW, Status.FAILED, Status.DRAFT, Status.READY):
                continue  # (a ready task not in the ready set is blocked by deps, i.e. waiting)
            elif t.status == Status.MERGED_INTO_PARENT:
                continue  # waits on the stack parent's own merge to the base (CG-228), not a human
            elif t.id in ready_ids:
                continue  # a work run is dispatchable (slots/pause aside)
            elif t.status == Status.CHANGES_REQUESTED:
                if st.get("rebase_pending"):
                    continue  # a rebase run is dispatchable (its own queue, no feedback needed)
                has_fb = bool(str(st.get("pending_feedback") or "").strip())
                under_cap = bool(st.get("pending_feedback_rebase")) or int(st.get("revisions", 0)) < max_rev
                manual_task = not self.runner_for(t).detached
                if has_fb and under_cap and not manual_task:
                    continue  # a revise run is dispatchable
                if has_fb and under_cap:
                    # A manual-runner task: dispatch_ready never auto-dispatches its revise
                    # round, so without this flag it would sit in changes_requested forever
                    # with no Inbox card telling anyone to take it (CG-158).
                    reason = "manual task has a revise round waiting; take it with `garden take`"
                else:
                    reason = ("no feedback recorded to revise against" if not has_fb
                              else f"{max_rev} revision rounds already used")
            else:
                reason = f"nothing to dispatch from status {t.status.value}"
            note = f"stuck: {reason}"
            st["needs_human"] = note
            self.events.emit("needs_human", t.id, reason=note, stuck=True)
            notify(self.cfg.data, t.id, "needs_human", note, t.pr or "")
            hint = f"take it (`garden take {t.id}`)" if manual_task else f"resume with one more round (`garden retry {t.id}`)"
            t.log(f"{note}; {hint} "
                  f'or send it back (`garden triage {t.id} --changes "..."`)')
            self.store.save(t)
            rep.transitions.append(f"{t.id} stuck ({reason})")

    def _sweep_terminal_state(self, rep: TickReport) -> None:
        """Backstop for a terminal task (done, cancelled, wont_do) still carrying a stale
        needs_human, pending-feedback or automerge stop — left over from before `_transition`
        cleared these on every terminal transition, or from a hand-edited state.json. Runs
        once a tick over every terminal task; a no-op once its state is clean, so a finished
        task never wears a decision on the Inbox, the Board or its own page (CG-195)."""
        for t in self.store.tasks().values():
            if not t.status.terminal:
                continue
            st = self.state.get(t.id)
            cleared = [k for k in ("needs_human", "pending_feedback", "pending_feedback_easy", "pending_feedback_rebase")
                       if st.pop(k, None) is not None]
            if self._queue_leave(t):
                cleared.append("queue state")
            if cleared:
                rep.transitions.append(f"{t.id}: swept stale {', '.join(cleared)} (terminal)")

    def _stash_dirty_worktree(self, task: Task, wt_path: Path) -> None:
        """A killed worker can leave uncommitted edits in its worktree; a fresh dispatch that
        reused it would then fail to reconcile the branch onto its base (`git merge --ff-only`
        refuses to overwrite local changes). Stash the edits under a named stash so the branch
        is clean for the new run, record the stash on the task (id + sha, listed on its page so
        a person can recover it), and continue."""
        if not wt_path.exists() or not gitops.is_repo(wt_path):
            return
        try:
            if not gitops.has_uncommitted_changes(wt_path):
                return
            name = f"garden:{task.id}:{now_iso()}"
            sha = gitops.stash_all(wt_path, name)
        except gitops.GitError as e:
            self.log(f"{task.id}: could not stash the worktree's leftover changes: {e}")
            return
        if not sha:
            return
        st = self.state.get(task.id)
        stashes = list(st.get("stashes") or [])
        stashes.append({"name": name, "sha": sha, "at": now_iso()})
        st["stashes"] = stashes
        self.events.emit("stashed", task.id, sha=sha, name=name)
        task.log(f"stashed leftover changes from a prior run before redispatch: `git stash apply {sha}` "
                 f"in {wt_path} to recover them ({name})")
        self.store.save(task)
        self.log(f"{task.id}: stashed a dirty worktree before dispatch ({sha[:12]})")

    def _stack_for(self, task: Task) -> dict[str, Any] | None:
        """Decide the base for a fresh run: a stack parent's branch, or the product base."""
        st = self.state.get(task.id)
        if st.get("stack_parent"):
            parent = self.store.tasks().get(st["stack_parent"])
            if parent and not parent.status.terminal:
                return {"parent_id": parent.id, "parent_title": parent.title, "parent_pr": parent.pr, "parent_branch": parent.branch,
                        "final_base": self.final_base_for(task)}
            st.pop("stack_parent", None)
            st["pr_base"] = self.final_base_for(task)
            return None
        if not self.stack_enabled or blockers(task, self.store.tasks(), stack=False) == []:
            return None
        parents = stack_parents(task, self.store.tasks())
        if len(parents) != 1:
            return None
        p = parents[0]
        st["stack_parent"] = p.id
        st["pr_base"] = p.branch
        self.events.emit("stacked", task.id, parent=p.id, base=p.branch)
        return {"parent_id": p.id, "parent_title": p.title, "parent_pr": p.pr, "parent_branch": p.branch,
                "final_base": self.final_base_for(task)}

    def dispatch(self, task: Task, mode: str = "work", runner: Runner | None = None, worktree: bool = True,
                 session_id: str = "", prompt_override: str = "", branch_override: str = "",
                 worktree_override: Path | None = None, model_override: str | None = None) -> Run:
        ensure_open(task)
        self._refuse_if_closed_or_frozen(task)
        runner = runner or self.runner_for(task)
        self._raise_if_harness_paused(runner.harness.name if runner.harness else "")
        branch = branch_override or task.branch or task.default_branch()
        st = self.state.get(task.id)
        st.pop("needs_human", None)
        # Reserved early so a revise/rebase/resume run's backup branch (below) and a dirty
        # worktree's stash (further below) can both name themselves after the run about to
        # reuse it; every later mutation just sets attributes on this same object before its
        # final run.save() near the bottom of this method.
        run_id = self.runs.next_run_id(task.id, mode) if mode in ("revise", "rebase", "resume") else ""
        run = self.runs.new_run(task.id, runner.name, mode=mode, run_id=run_id)
        stack = self._stack_for(task) if mode in ("work", "trial") else None
        base = self.base_for(task)
        feedback = str(st.get("pending_feedback") or "") if mode == "revise" else ""
        revise_easy = mode == "revise" and bool(st.get("pending_feedback_easy"))
        easy_tier = revise_easy or mode == "rebase"
        # Snapshot what this dispatch is about to clear from state, before it clears it, so a
        # quota env_error on this very run can put it back (see reap._handle_quota_env_error):
        # the point is not to burn the round's context on the harness's own account trouble.
        if mode == "revise":
            run.env_snapshot = {"pending_feedback": feedback, "pending_feedback_easy": revise_easy,
                                "pending_feedback_rebase": bool(st.get("pending_feedback_rebase"))}
            from ..suggestions import pending_suggestions

            pend = pending_suggestions(task.body)
            if pend:
                sug_fb = ("### Suggestions on this task (the spec moved)\n\nThe task's spec has these pending "
                          "suggestions; take them into account in this round:\n"
                          + "\n".join(f"- {s.text}" for s in pend))
                feedback = f"{feedback}\n\n{sug_fb}".strip() if feedback else sug_fb
        elif mode == "rebase":
            run.env_snapshot = {"rebase_pending": True}
        qa = list(st.get("qa") or [])
        # List any commits already on the branch in the brief, so a re-dispatched worker
        # builds on the prior attempt instead of reverse-engineering it from git. This
        # covers a revise/resume round (whose branch has an open PR to build on) and, just
        # as importantly, a fresh `work` round that lands on a worktree an interrupted prior
        # attempt left with real, unreported progress — the "back to ready" case in reap
        # (CG-125). A truly clean start has no commits ahead of base, so the section is
        # omitted and nothing changes.
        wt_path = worktree_override or self.worktree_for(task)
        # A killed worker's leftover uncommitted edits are stashed (not swept into the sync
        # below as a commit) before anything else touches the worktree, so they are recovered
        # by `git stash apply`, not buried in a backup branch's synthetic commit.
        if worktree and not runner.remote:
            self._stash_dirty_worktree(task, wt_path)
        # A revise, rebase or resume run writes to a branch another writer may have just moved
        # (a prior revise round's push, the merge queue's own rebase): sync the worktree to
        # origin's head first so this run starts from the same head, instead of racing a stale
        # local copy toward a rejected push (CG-220). Any commits sitting only in the worktree —
        # a killed prior run's progress — are kept on `backup/<run-id>`, never silently dropped.
        # run_id was reserved above, alongside the run itself (see RunStore.next_run_id).
        if run_id and worktree and not runner.remote:
            backed_up = gitops.sync_to_origin_head(wt_path, branch, f"backup/{run_id}")
            if backed_up:
                note = (f"kept {len(backed_up)} local-only commit(s) on `backup/{run_id}` before "
                        f"syncing to origin/{branch}'s head: " + "; ".join(backed_up))
                task.log(note)
                self.store.save(task)
                self.log(f"{task.id}: {note}")
        commits_ahead = None
        if wt_path.exists():
            try:
                commits_log = gitops.log_summary(wt_path, base, n=20)
                if commits_log.strip():
                    commits_ahead = commits_log.strip().split("\n")
            except gitops.GitError:
                pass
        # Prepare the worktree before building the brief so reading-list snippets are inlined
        # from the *target checkout* — the branch the worker will actually edit, including a
        # stacked parent's changes and files a dependency created — not the stale base repo.
        # build_brief's product_dirs prefers this worktree once it exists.
        wt: Path | None = None
        if worktree and not runner.remote:
            wt = gitops.prepare_worktree(self.repo_for(task), wt_path, branch, base)
        # The head this run starts from, for a lease-protected push once it finishes (CG-220):
        # empty for a branch never pushed to origin yet (a fresh `work`/`trial` round), in which
        # case the push falls back to its previous, non-leased behaviour.
        start_head = gitops.remote_head(wt, branch) if wt is not None else ""
        if mode == "rebase":
            from ..brief import rebase_brief

            text = prompt_override or rebase_brief(
                self.store, task, branch=branch, base=base,
                hunks=dict(st.get("rebase_hunks") or {}), files=list(st.get("rebase_files") or []))
        else:
            brief = build_brief(self.store, task, branch=branch, base=base, review_feedback=feedback, stack=stack, qa=qa, commits_ahead=commits_ahead)
            text = prompt_override or brief.text
        run.branch, run.base, run.brief_tokens = branch, base, max(1, len(text) // 4)
        run.start_head = start_head
        run.model = model_override if model_override is not None else self.model_for(task, runner, "easy" if easy_tier else "")
        run.difficulty = "easy" if easy_tier else task.difficulty
        run.harness = runner.harness.name if runner.harness else ""
        run.session_id = session_id
        if session_id and st.get("session_host"):
            run.host = str(st["session_host"])
        runner.assign(run, self.active_runs())
        if wt is not None:
            run.worktree = str(wt)
        if mode in ("work", "revise", "resume", "rebase"):
            fence = self._fence_repos(task)
            run.fence_paths = [str(p) for _, p in fence]
            self._fence_snapshot(task, run)
            self._git_guard_snapshot(task, run)
        run.save()
        try:
            runner.start(run, wt or self.store.root, text)
        except Exception as e:  # setup/start failed: mark this run failed so it stops
            run.status = "failed"    # counting against active() (a leaked slot) and re-raise
            run.error = str(e)
            run.save()
            raise
        if not branch_override:
            task.branch = branch
        task.attempts += 1 if mode == "work" else 0
        task.last_dispatched_at = now_iso()
        # A stale-base rebase that a worker had to resolve by hand (CG-131) is mechanical, not a
        # fix the worker was asked to make: it shares the `rebases` counter with the mechanical/
        # agent rebase mode above and never counts toward max_revisions, so a PR that waits out
        # several merges under it does not burn through the revision cap for having been rebased.
        is_rebase = bool(st.pop("pending_feedback_rebase", False))
        rebase_note = ""
        if mode == "revise":
            if is_rebase:
                st["rebases"] = int(st.get("rebases", 0)) + 1
                rebase_note = f", rebase round {st['rebases']} (not counted)"
            else:
                st["revisions"] = int(st.get("revisions", 0)) + 1
            st["pending_feedback"] = ""
            st.pop("pending_feedback_easy", None)
        elif mode == "rebase":
            # A rebase round has its own counter and never touches max_revisions.
            st["rebases"] = int(st.get("rebases", 0)) + 1
            st.pop("rebase_pending", None)
        st["last_round_rebase"] = is_rebase
        where = f" on {run.host}" if run.host else ""
        model = f" model={run.model}" if run.model else ""
        how = "resumed session" if session_id else "fresh session"
        stacked = f" stacked on {stack['parent_id']}" if stack else ""
        tier_note = ", description only; easy tier" if revise_easy else (", conflict only; easy tier" if mode == "rebase" else "")
        self.events.emit("dispatch", task.id, run=run.run_id, mode=mode, model=run.model, harness=run.harness,
                         host=run.host, base=base, brief_tokens=run.brief_tokens, resumed=bool(session_id))
        self._transition(task, Status.RUNNING, f"dispatched {mode} run {run.run_id} via {runner.name}{where} [{run.harness or 'human'}{model}] ({how}, base {base}{stacked}{tier_note}{rebase_note}, ~{run.brief_tokens} tokens)")
        self.state.save()
        return run
