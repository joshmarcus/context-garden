"""Model trials: contenders on their own branches, a comparison run, a winner."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import gitops
from ..github import GitHubError, mark_garden_comment
from ..model import Status, Task, now_iso
from ..runner.base import Runner
from ..runs import Run
from ..trials import compare_brief, parse_compare, parse_contender, ranking_markdown
from .report import TickReport

# Substrings (case-insensitive) in a finished contender's error, summary or final text that
# mark its run a sandbox denial rather than a model result: the incident behind this task, a
# Codex contender that stopped needing "resume with writable Git metadata, prepared
# dependencies, asset network access" because its sandbox denied all three (CG-229). Checked
# in addition to `env_error`/`env_kind` (Harness.parse's own classification of a login or
# quota stop, e.g. CG-212/CG-217), which flows through unchanged whenever it fires.
SANDBOX_DENIAL_MARKERS = (
    "writable git metadata", "prepared dependencies", "asset network access",
    "sandbox denied", "denied by the sandbox", "not a git repository",
)


def _sandbox_denial(*texts: str) -> bool:
    haystack = " ".join(t for t in texts if t).lower()
    return any(m in haystack for m in SANDBOX_DENIAL_MARKERS)


# Cached PR/review facts an old trial's PR leaves behind on the task, cleared by `--again` so
# the new trial starts as clean as a task that never had one. Queue state (automerge_candidate,
# automerge_ready_at, merge_head, automerge_blocked) goes through `_queue_leave` instead, the
# only writer of those four (CG-232).
AGAIN_RESET_KEYS = (
    "pr_number", "pr_state", "head_sha", "pr_draft", "pr_base", "stack_parent",
    "review_run", "last_review", "review_rounds", "review_decision",
    "checks", "failed_checks", "automerged",
    "needs_human", "decision",
    "pending_feedback", "pending_feedback_easy", "pending_feedback_rebase",
    "worktree", "revisions",
)


class TrialsMixin:
    # ---- model trials ------------------------------------------------------
    def start_trial(self, task: Task, contenders: list[str], again: bool = False, keep_prs: bool = False) -> list[Run]:
        if len(contenders) < 2:
            raise RuntimeError("a trial needs at least two contenders")
        default_h = task.harness or self.cfg.product_harness(task.product)
        parsed = [parse_contender(spec, default_h) for spec in contenders]
        for _label, harness, _model in parsed:
            self._raise_if_harness_paused(harness or default_h)
        st = self.state.get(task.id)
        prior = st.get("trial")
        if task.status not in (Status.READY, Status.DRAFT, Status.FAILED) or task.pr:
            if not again:
                raise RuntimeError(f"{task.id} must be ready/draft/failed without a PR to start a trial (is {task.status.value}); "
                                   "use --again to close its last trial's PRs and run a new one")
            if not isinstance(prior, dict) or prior.get("status") not in ("done", "inconclusive"):
                raise RuntimeError(f"{task.id}'s trial has not concluded yet; wait for it (or its comparison run) to finish before running --again")
            self._reset_trial(task, prior, keep_prs=keep_prs)
            st = self.state.get(task.id)
        default_h = task.harness or self.cfg.product_harness(task.product)
        trial: dict[str, Any] = {"id": now_iso(), "status": "running", "contenders": []}
        runs: list[Run] = []
        base_branch = task.branch or task.default_branch()
        for label, harness, model in parsed:
            suffix = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
            branch = f"{base_branch}-trial-{suffix}"
            wt = self.cfg.worktree_path(f"{task.id}-trial-{suffix}")
            runner = self.runner_for(task, "local", harness)
            contender: dict[str, Any] = {"label": label, "harness": harness, "model": model, "branch": branch, "worktree": str(wt),
                                         "run_id": "", "status": "running", "pr": "", "pr_number": 0, "cost": None, "score": None,
                                         "kind": "", "note": ""}
            try:
                run = self.dispatch(task, mode="trial", runner=runner, branch_override=branch, worktree_override=wt, model_override=model or None)
            except Exception as e:  # noqa: BLE001
                # A contender that never got to run at all (its setup command failed, its
                # worktree could not be prepared) is a harness failure, not a model loss: record
                # it env_failed and keep starting the rest instead of crashing the whole trial
                # and losing every contender that *did* get a chance (CG-229).
                contender["status"], contender["kind"], contender["note"] = "env_failed", "setup", str(e)[:200]
                trial["contenders"].append(contender)
                self.events.emit("trial_contender_env_failed", task.id, label=label, env_kind="setup", note=str(e)[:200])
                continue
            contender["model"], contender["run_id"] = run.model, run.run_id
            trial["contenders"].append(contender)
            runs.append(run)
        if not runs:
            detail = "; ".join(f"{c['label']}: {c.get('note', '')}" for c in trial["contenders"])
            raise RuntimeError(f"no contender could be dispatched for {task.id}: {detail}")
        st["trial"] = trial
        task.branch = base_branch
        task.log(f"trial started with {', '.join(c['label'] for c in trial['contenders'])}")
        self.store.save(task)
        self.events.emit("trial_started", task.id, contenders=[c["label"] for c in trial["contenders"]])
        self.state.save()
        return runs

    def _reset_trial(self, task: Task, prior: dict[str, Any], keep_prs: bool) -> None:
        """`--again`: close (or, with `keep_prs`, leave open) every contender PR the previous
        trial left behind, drop their worktrees and — for the ones being closed — their remote
        branches, then clear the task's cached PR/review facts and put it back to ready. The
        incident behind this (CG-232): relaunching a trial by hand meant closing PRs, removing
        worktrees and branches, and clearing state.json keys one at a time under the lock."""
        slug = self.slug_for(task)
        repo = self.repo_for(task)
        closed_prs: set[str] = set()
        for c in prior.get("contenders", []):
            wt = c.get("worktree")
            if wt:
                try:
                    gitops.remove_worktree(repo, Path(wt))
                except Exception:  # noqa: BLE001
                    pass
            if keep_prs:
                continue
            number = c.get("pr_number")
            branch = c.get("branch")
            if number and slug and self.github.available:
                try:
                    self.github.comment(slug, number, mark_garden_comment(
                        f"Closing this contender: {task.id}'s trial is being run again."))
                    self.github.close_pr(slug, number)
                    if c.get("pr"):
                        closed_prs.add(c["pr"])
                except GitHubError as e:
                    self.log(f"{task.id}: could not close trial contender PR #{number}: {e}")
            if branch and slug and self.github.available:
                try:
                    self.github.delete_branch(slug, branch)
                except GitHubError as e:
                    self.log(f"{task.id}: could not delete trial contender branch {branch}: {e}")
        if closed_prs:
            self.trials.mark_closed(task.id, closed_prs)
        self._queue_leave(task)
        st = self.state.get(task.id)
        for key in AGAIN_RESET_KEYS:
            st.pop(key, None)
        task.pr = ""
        task.branch = ""
        self._transition(task, Status.READY, "trial reset for --again" + (" (previous PRs kept open)" if keep_prs else " (previous PRs closed)"))
        self.events.emit("trial_reset", task.id, keep_prs=keep_prs, contenders=[c.get("label") for c in prior.get("contenders", [])])
        self.state.save()

    def _redispatch_contender(self, task: Task, c: dict[str, Any]) -> None:
        """A contender parked `paused` on a quota env_error tries again once its harness
        resumes: the same branch, worktree and model as the run that hit the limit, so the
        trial continues from where the account trouble interrupted it rather than losing the
        contender."""
        runner = self.runner_for(task, "local", c["harness"])
        run = self.dispatch(task, mode="trial", runner=runner, branch_override=c["branch"],
                            worktree_override=Path(c["worktree"]), model_override=c["model"] or None)
        c["run_id"] = run.run_id
        c["status"] = "running"
        c.pop("note", None)

    def reap_trial(self, task: Task, rep: TickReport) -> bool:
        st = self.state.get(task.id)
        trial = st["trial"]
        if trial["status"] == "comparing":
            return False
        changed = False
        for c in trial["contenders"]:
            if c["status"] == "paused":
                if self.is_harness_paused(c["harness"]):
                    continue  # still down; try again next tick
                self._redispatch_contender(task, c)
                changed = True
                continue
            if c["status"] != "running":
                continue
            run = next((r for r in self.runs.runs_for(task.id) if r.run_id == c["run_id"]), None)
            if run is None:
                c["status"] = "failed"
                c["note"] = "run record missing"
                changed = True
                continue
            runner = self.runner_for(task, run.runner, run.harness)
            if not self._finished_or_timed_out(run, runner):
                continue
            changed = True
            self._finalize_contender(task, c, run, runner)
        if any(c["status"] in ("running", "paused") for c in trial["contenders"]):
            return changed
        if not changed and not trial.get("compare_paused"):
            return False
        with_pr = [c for c in trial["contenders"] if c["status"] == "pr"]
        base = self.base_for(task)
        if len(with_pr) >= 2:
            harness_name = str(self.cfg.get("review.harness") or "")
            if self.is_harness_paused(self.resolved_harness_name(task, harness_name)):
                trial["compare_paused"] = True
                return True  # the contenders are done; the comparison waits for the harness
            trial.pop("compare_paused", None)
            diffs = {c["label"]: gitops.diff(Path(c["worktree"]), base) for c in with_pr}
            text = compare_brief(self.store, task, with_pr, diffs, base, int(self.cfg.get("review.max_diff_chars", 60000)))
            trial["status"] = "comparing"
            self.dispatch_aux("compare", task, text, Path(with_pr[0]["worktree"]), {"trial_id": trial["id"]},
                              harness_name=harness_name, difficulty=str(self.effective("review.difficulty") or "hard"))
            rep.dispatched.append(f"{task.id}(compare)")
            task.log("all contenders finished; comparison run started")
            self.store.save(task)
        elif len(with_pr) == 1:
            # Fewer than two PRs means there was nothing to compare: the survivor's PR still
            # moves the task forward, but the trial is inconclusive, not a win — the other
            # contender's environment failure must not make this one look better than it is
            # (CG-229).
            self._conclude_trial(task, {"winner": with_pr[0]["label"],
                                        "rationale": "only one contender produced a PR; the trial is inconclusive, not a win",
                                        "ranking": []}, rep, inconclusive=True)
        else:
            trial["status"] = "inconclusive"
            trial["rationale"] = "no contender produced a PR"
            detail = "; ".join(f"{c['label']}: {c.get('note') or c['status']}" for c in trial["contenders"])
            self._transition(task, Status.FAILED, f"trial inconclusive: no contender produced a PR ({detail})")
            rep.transitions.append(f"{task.id} -> failed (trial inconclusive)")
        return True

    def _finalize_contender(self, task: Task, c: dict[str, Any], run: Run, runner: Runner) -> None:
        run.exit_code = run.read_exit_code()
        run.finished_at = now_iso()
        collected = runner.collect(run) if run.status != "timeout" else {"result": {}, "error": "timed out"}
        if collected.get("env_error"):
            # The harness's own account, not the contender: pause it and park the contender
            # to retry once it resumes, instead of counting this as the contender's failure.
            self._pause_for_env_error(run, collected)
            run.status = "env_error"
            run.save()
            self.events.emit("run_finished", task.id, run=run.run_id, mode="trial", harness=run.harness, model=run.model,
                             status="env_error", cost_usd=collected.get("cost_usd"), usage=collected.get("usage") or {})
            c["status"] = "paused"
            c["note"] = f"{collected.get('env_kind') or 'quota'} limit hit on {run.harness or 'the harness'}; will retry once it resumes"
            return
        run.result = collected.get("result") or {}
        run.usage = collected.get("usage") or {}
        run.cost_usd = collected.get("cost_usd")
        run.model = str(collected.get("model") or run.model)
        run.error = collected.get("error") or ""
        c["cost"] = run.cost_usd
        c["input_tokens"] = int((run.usage or {}).get("input_tokens", 0) or 0)
        c["output_tokens"] = int((run.usage or {}).get("output_tokens", 0) or 0)
        self.events.emit("run_finished", task.id, run=run.run_id, mode="trial", harness=run.harness, model=run.model,
                         status=str(run.result.get("status") or ("error" if run.error else "no_result")), cost_usd=run.cost_usd, usage=run.usage)
        result = run.result
        wt = Path(c["worktree"])
        if str(result.get("status", "")).lower() != "done":
            run.status = "failed"
            run.save()
            final_text = str(collected.get("final_text") or "").strip()
            note = (result.get("summary") or run.error or final_text or "no result")[:200]
            kind = str(collected.get("env_kind") or "") if collected.get("env_error") else ""
            if not kind and _sandbox_denial(run.error, str(result.get("summary") or ""), final_text):
                kind = "sandbox"
            if kind:
                c["status"], c["kind"], c["note"] = "env_failed", kind, note
            else:
                c["status"], c["note"] = "failed", note
            return
        try:
            if gitops.has_uncommitted_changes(wt):
                gitops.commit_all(wt, f"{task.id}: leftover changes from trial run {run.run_id}")
            if gitops.commits_ahead(wt, self.base_for(task)) == 0:
                raise gitops.GitError("no commits")
            gitops.push(wt, c["branch"])
        except gitops.GitError as e:
            run.status = "failed"
            run.save()
            msg = str(e)[:200]
            if _sandbox_denial(msg):
                c["status"], c["kind"], c["note"] = "env_failed", "sandbox", msg
            else:
                c["status"], c["note"] = "failed", msg
            return
        run.status = "done"
        run.save()
        c["pr_title"] = str(result.get("pr_title") or f"{task.id}: {task.title}")
        c["pr_body"] = str(result.get("pr_body") or result.get("summary") or "")
        slug = self.slug_for(task)
        if slug and self.github.available:
            try:
                pr = self.github.create_pr(slug, c["branch"], self.base_for(task), f"[trial {c['label']}] {c['pr_title']}",
                                           c["pr_body"] + f"\n\n---\nTrial contender `{c['label']}` for task `{task.id}`.",
                                           draft=bool(self.cfg.get("github.draft_pr", False)))
                c["pr"], c["pr_number"] = pr.url, pr.number
            except GitHubError as e:
                c["note"] = f"PR failed: {e}"[:200]
        c["status"] = "pr"

    def _finish_trial(self, entry: dict[str, Any], run: Run, final: str, rep: TickReport) -> None:
        task = self.store.task(entry["task"])
        verdict = parse_compare(final)
        if not verdict:
            st = self.state.get(task.id)
            with_pr = [c for c in st["trial"]["contenders"] if c["status"] == "pr"]
            verdict = {"winner": with_pr[0]["label"], "rationale": "comparison run produced no verdict; first contender kept", "ranking": []}
        self._conclude_trial(task, verdict, rep, compare_cost=run.cost_usd, run_id=run.run_id)

    def _conclude_trial(self, task: Task, verdict: dict[str, Any], rep: TickReport, compare_cost: float | None = None,
                        run_id: str = "", inconclusive: bool = False) -> None:
        st = self.state.get(task.id)
        trial = st["trial"]
        scores = {str(r.get("label")): r for r in verdict.get("ranking") or [] if isinstance(r, dict)}
        for c in trial["contenders"]:
            r = scores.get(c["label"])
            if r:
                c["score"] = r.get("score")
                c["summary"] = r.get("summary", "")
        winner = next((c for c in trial["contenders"] if c["label"] == verdict.get("winner") and c["status"] == "pr"), None)
        if winner is None:
            with_pr = sorted([c for c in trial["contenders"] if c["status"] == "pr"], key=lambda c: -(c.get("score") or 0))
            winner = with_pr[0]
        trial["rationale"] = str(verdict.get("rationale") or "")
        trial["status"] = "inconclusive" if inconclusive else "done"
        trial["compare_cost"] = compare_cost
        # An inconclusive trial (fewer than two PRs) keeps the survivor's branch moving the task
        # forward, but records no `winner`: the other contender's environment failure must not
        # be scored as a loss, so the leaderboard's win credit (`trial.winner == label`) never
        # fires for it either (CG-229).
        if inconclusive:
            trial["winner"] = ""
            trial["kept"] = winner["label"]
        else:
            trial["winner"] = winner["label"]
            trial.pop("kept", None)
        contenders_out = []
        for c in trial["contenders"]:
            entry = {k: c.get(k) for k in ("label", "harness", "model", "status", "kind", "score", "cost",
                                           "input_tokens", "output_tokens", "pr", "summary", "note")}
            entry["closed"] = bool(c.get("pr_number")) and c is not winner  # this trial closes every loser's PR below
            contenders_out.append(entry)
        record = {"task": task.id, "title": task.title, "difficulty": task.difficulty, "winner": trial["winner"],
                  "kept": trial.get("kept", ""), "rationale": trial["rationale"], "compare_cost": compare_cost,
                  "contenders": contenders_out}
        self.trials.record(record)
        md = ranking_markdown({"task": task.id, **record})
        slug = self.slug_for(task)
        for c in trial["contenders"]:
            if c.get("pr_number") and slug and self.github.available:
                try:
                    comment_body = mark_garden_comment(md, run_id)
                    self.github.comment(slug, c["pr_number"], comment_body)
                    if c is not winner:
                        self.github.close_pr(slug, c["pr_number"])
                except GitHubError as e:
                    self.log(f"{task.id}: trial PR update failed: {e}")
            if c is not winner and c.get("worktree"):
                try:
                    gitops.remove_worktree(self.repo_for(task), Path(c["worktree"]))
                except Exception:  # noqa: BLE001
                    pass
        task.harness = winner["harness"]
        task.model = winner["model"]
        task.branch = winner["branch"]
        task.pr = winner.get("pr", "")
        st["pr_number"] = winner.get("pr_number") or 0
        st["worktree"] = winner["worktree"]
        st["revisions"] = 0
        st["review_rounds"] = int(self.cfg.get("review.max_rounds", 2))  # the comparison stands in for the review pass
        self.events.emit("trial_done", task.id, winner=trial["winner"], inconclusive=inconclusive,
                         scores={c["label"]: c.get("score") for c in trial["contenders"]})
        st["pr_draft"] = bool(self.cfg.get("github.draft_pr", True)) and bool(winner.get("pr"))
        if inconclusive:
            msg = f"trial inconclusive ({trial['rationale']}); kept {winner['label']}'s PR: {task.pr or 'no PR'}"
        else:
            msg = (f"trial won by {winner['label']} (scores: " +
                  ", ".join(f"{c['label']}={c.get('score') if c.get('score') is not None else '–'}" for c in trial["contenders"]) +
                  f"): {task.pr or 'no PR'}")
        self._transition(task, self._pr_status(task), msg)
        if inconclusive:
            rep.transitions.append(f"{task.id} -> {task.status.value} (trial inconclusive, kept {winner['label']})")
        else:
            rep.transitions.append(f"{task.id} -> {task.status.value} (trial winner {winner['label']})")
