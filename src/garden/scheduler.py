"""The scheduler: a deterministic state machine over task files. No LLM calls.

    tick():
      1. reap     running tasks whose worker finished -> push, open PR, in_review (or retry/fail);
                  finished review runs -> comment on the PR, maybe changes_requested
      2. poll     in_review tasks -> merged? closed? new feedback? CI red? -> done/failed/changes_requested
      3. dispatch ready tasks (deps done) into free slots; revise runs for changes_requested

State that isn't in task files lives in .garden/state.json (per-task PR bookkeeping).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitops
from .brief import build_brief
from .github import GitHub, GitHubError
from .harness import DIFFICULTIES
from .model import Status, Task, now_iso
from .review import feedback_from_review, parse_review, review_brief, review_to_markdown
from .runner import get_runner
from .runner.base import Runner
from .runs import Run, RunStore
from .store import Store


@dataclass
class TickReport:
    reaped: list[str] = field(default_factory=list)
    polled: list[str] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.dispatched:
            parts.append(f"dispatched {', '.join(self.dispatched)}")
        if self.transitions:
            parts.append("; ".join(self.transitions))
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        return " | ".join(parts) if parts else "nothing to do"

    @property
    def changed(self) -> bool:
        return bool(self.dispatched or self.transitions or self.errors)


class State:
    """Small JSON side-store for things that don't belong in task frontmatter."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                self.data = {}

    def get(self, task_id: str) -> dict[str, Any]:
        return self.data.setdefault(task_id, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))


class Scheduler:
    def __init__(
        self,
        store: Store,
        github: GitHub | None = None,
        runner_factory: Callable[[str, Task], Runner] | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.cfg = store.config
        self.runs = RunStore(self.cfg.garden_dir)
        self.state = State(self.cfg.garden_dir / "state.json")
        self.github = github if github is not None else GitHub(use_gh=bool(self.cfg.get("github.use_gh", True)))
        self._runner_factory = runner_factory
        self.log = log or (lambda msg: None)

    # ---- helpers -----------------------------------------------------------
    def runner_for(self, task: Task, name: str = "", harness_name: str = "") -> Runner:
        name = name or task.runner or self.cfg.product_runner(task.product)
        if name == "claude-local":
            name = "local"
        if self._runner_factory:
            return self._runner_factory(name, task)
        harness = self.cfg.harness(harness_name or task.harness or self.cfg.product_harness(task.product))
        if name == "ssh":
            cfg = dict(self.cfg.get("ssh", {}) or {})
            cfg["_product"] = task.product
        else:
            cfg = {}
        cfg.setdefault("timeout_minutes", self.cfg.get("timeout_minutes", 90))
        return get_runner(name, cfg, harness)

    def model_for(self, task: Task, runner: Runner, difficulty: str = "") -> str:
        if runner.harness is None:
            return ""
        d = difficulty or task.difficulty
        if d not in DIFFICULTIES:
            d = "medium"
        return runner.harness.model_for(d, task.model if not difficulty else "")

    def repo_for(self, task: Task) -> Path:
        repo = task.repo or self.cfg.product_repo(task.product)
        if isinstance(repo, str) and ("://" in repo or repo.startswith("git@")):
            return gitops.ensure_repo(repo, self.cfg.garden_dir / "repos")
        return gitops.ensure_repo(Path(repo), self.cfg.garden_dir / "repos")

    def worktree_for(self, task: Task) -> Path:
        return self.cfg.garden_dir / "worktrees" / task.id

    def base_for(self, task: Task) -> str:
        return self.cfg.product_base_branch(task.product)

    def slug_for(self, task: Task) -> str | None:
        override = self.cfg.product(task.product).get("github")
        if override:
            return str(override)
        return gitops.slug(self.repo_for(task))

    def active_runs(self) -> list[Run]:
        return [r for r in self.runs.active() if r.runner != "manual"]

    def slots_free(self) -> int:
        return max(0, int(self.cfg.get("max_parallel", 10)) - len(self.active_runs()))

    def _transition(self, task: Task, status: Status, note: str) -> None:
        old = task.status.value
        task.status = status
        task.log(note)
        self.store.save(task)
        self.log(f"{task.id}: {old} -> {status.value} ({note})")

    def _pr_number(self, task: Task) -> int | None:
        st = self.state.get(task.id)
        if st.get("pr_number"):
            return int(st["pr_number"])
        m = re.search(r"/pull/(\d+)", task.pr or "")
        if m:
            st["pr_number"] = int(m.group(1))
            return int(m.group(1))
        return None

    # ---- tick --------------------------------------------------------------
    def tick(self, dispatch: bool | None = None) -> TickReport:
        rep = TickReport()
        self.store.invalidate()
        tasks = self.store.tasks()
        for t in list(tasks.values()):
            try:
                if t.status == Status.RUNNING and self.reap(t, rep):
                    rep.reaped.append(t.id)
                elif t.status in (Status.IN_REVIEW, Status.CHANGES_REQUESTED) and self.reap_review(t, rep):
                    rep.reaped.append(t.id)
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                rep.errors.append(f"{t.id}: reap failed: {e}")
                self.log(f"{t.id}: reap failed: {e}")
        self.store.invalidate()
        tasks = self.store.tasks()
        for t in list(tasks.values()):
            if t.status in (Status.IN_REVIEW, Status.CHANGES_REQUESTED) and t.pr:
                try:
                    self.poll(t, rep)
                    rep.polled.append(t.id)
                except Exception as e:  # noqa: BLE001
                    rep.errors.append(f"{t.id}: poll failed: {e}")
                    self.log(f"{t.id}: poll failed: {e}")
        if dispatch is None:
            dispatch = bool(self.cfg.get("auto_dispatch", True))
        if dispatch:
            self.dispatch_ready(rep)
        self.state.save()
        return rep

    # ---- reap --------------------------------------------------------------
    def reap(self, task: Task, rep: TickReport) -> bool:
        run = self.runs.latest(task.id)
        if run is None or run.status != "running" or run.mode == "review":
            self._transition(task, Status.READY, "no active run found; back to ready")
            rep.transitions.append(f"{task.id} running -> ready (no run)")
            return True
        runner = self.runner_for(task, run.runner, run.harness)
        if not self._finished_or_timed_out(run, runner):
            return False
        if run.status == "timeout":
            self._retry_or_fail(task, run, rep, "worker timed out")
            return True
        self.finalize(task, run, runner, rep)
        return True

    def _finished_or_timed_out(self, run: Run, runner: Runner) -> bool:
        if run.process_finished():
            return True
        timeout_min = float(self.cfg.get("timeout_minutes", 90) or 0)
        if runner.detached and timeout_min and run.elapsed_minutes() > timeout_min + 5:
            run.kill()
            run.status = "timeout"
            run.finished_at = now_iso()
            run.error = "timed out"
            run.save()
            return True
        return False

    def finalize(self, task: Task, run: Run, runner: Runner, rep: TickReport) -> None:
        run.exit_code = run.read_exit_code()
        run.finished_at = now_iso()
        collected = runner.collect(run)
        run.result = collected.get("result") or {}
        run.usage = collected.get("usage") or {}
        run.cost_usd = collected.get("cost_usd")
        run.error = collected.get("error") or ""
        final_text = collected.get("final_text") or ""
        if final_text and not (run.path / "final.md").exists():
            (run.path / "final.md").write_text(final_text)
        result = run.result
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""

        if run.exit_code not in (0, None) and not result:
            run.status = "failed"
            run.save()
            self._retry_or_fail(task, run, rep, f"worker exited {run.exit_code}: {run.error[:200]}")
            return
        if not result:
            run.status = "failed"
            run.save()
            self._retry_or_fail(task, run, rep, f"no GARDEN_RESULT in worker output ({run.error[:200] or 'see final.md'})")
            return
        if str(result.get("status", "")).lower() == "blocked":
            run.status = "blocked"
            run.save()
            self._transition(task, Status.FAILED, f"worker blocked: {result.get('summary') or result.get('notes') or '?'}{cost}")
            rep.transitions.append(f"{task.id} -> failed (blocked)")
            return

        base = run.base or self.base_for(task)
        branch = run.branch or task.branch or task.default_branch()
        repo = self.repo_for(task)

        if runner.remote:
            # the worker pushed the branch itself; fetch it and materialise a local worktree
            gitops.fetch(repo)
            try:
                gitops.git("rev-parse", "--verify", f"origin/{branch}", cwd=repo)
                ahead = int(gitops.git("rev-list", "--count", f"{gitops.base_ref(repo, base)}..origin/{branch}", cwd=repo).strip() or 0)
            except gitops.GitError:
                ahead = 0
            if ahead == 0:
                run.status = "failed"
                run.error = "no commits pushed"
                run.save()
                self._retry_or_fail(task, run, rep, "remote worker finished without pushing commits")
                return
            run.status = "done"
            run.save()
            wt = self.worktree_for(task)
            try:
                if wt.exists():
                    gitops.git("fetch", "origin", cwd=wt)
                    gitops.git("reset", "-q", "--hard", f"origin/{branch}", cwd=wt)
                else:
                    gitops.prepare_worktree(repo, wt, branch, base)
            except gitops.GitError as e:
                self.log(f"{task.id}: could not materialise local worktree: {e}")
            task.branch = branch
            self._open_or_update_pr(task, run, branch, base, result, rep, cost)
            return

        worktree = Path(run.worktree) if run.worktree else self.worktree_for(task)
        if not worktree.exists():
            run.status = "done"
            run.save()
            pr = str(result.get("pr") or "")
            if pr:
                task.pr = pr
            self._transition(task, Status.IN_REVIEW, f"finished ({run.mode}): {result.get('summary', '')}{cost}")
            rep.transitions.append(f"{task.id} -> in_review")
            return
        try:
            if gitops.has_uncommitted_changes(worktree):
                gitops.commit_all(worktree, f"{task.id}: leftover changes from worker run {run.run_id}")
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
            self._retry_or_fail(task, run, rep, "worker finished with no commits")
            return
        run.status = "done"
        run.save()
        try:
            gitops.push(worktree, branch)
        except gitops.GitError as e:
            self._transition(task, Status.FAILED, f"push failed: {e}{cost}")
            rep.transitions.append(f"{task.id} -> failed (push)")
            return
        task.branch = branch
        self._open_or_update_pr(task, run, branch, base, result, rep, cost)

    def _open_or_update_pr(self, task: Task, run: Run, branch: str, base: str, result: dict[str, Any],
                           rep: TickReport, cost: str) -> None:
        slug = self.slug_for(task)
        summary = str(result.get("summary") or "")
        st = self.state.get(task.id)
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
                title = str(result.get("pr_title") or "")
                if run.mode == "revise":
                    try:
                        self.github.update_pr(slug, existing.number, title=title, body=body)
                        self.github.comment(slug, existing.number, f"Pushed a revision round: {summary}\n\n_garden run {run.run_id}_")
                    except GitHubError as e:
                        self.log(f"{task.id}: could not update PR: {e}")
                self._transition(task, Status.IN_REVIEW, f"pushed revision to {existing.url}: {summary}{cost}")
                rep.transitions.append(f"{task.id} -> in_review (revised)")
            else:
                title = str(result.get("pr_title") or f"{task.id}: {task.title}")
                body = str(result.get("pr_body") or summary or task.body)
                body += f"\n\n---\nTask `{task.id}` from the context garden ({task.product}/{task.phase})."
                pr = self.github.create_pr(
                    slug, branch, base, title, body,
                    draft=bool(self.cfg.get("github.draft_pr", False)),
                    reviewers=list(self.cfg.get("github.reviewers", []) or []),
                )
                task.pr = pr.url
                st["pr_number"] = pr.number
                st["revisions"] = 0
                st["review_rounds"] = 0
                self._transition(task, Status.IN_REVIEW, f"opened {pr.url}: {summary}{cost}")
                rep.transitions.append(f"{task.id} -> in_review ({pr.url})")
        except GitHubError as e:
            self._transition(task, Status.IN_REVIEW, f"branch pushed but PR failed ({e}); open it by hand and run `garden pr {task.id} <url>`{cost}")
            rep.transitions.append(f"{task.id} -> in_review (PR failed)")
            return
        self._maybe_review(task, run, rep)

    def _retry_or_fail(self, task: Task, run: Run, rep: TickReport, reason: str) -> None:
        max_attempts = int(self.cfg.get("max_attempts", 2))
        if run.mode == "revise":
            self._transition(task, Status.FAILED, f"revision failed: {reason}")
            rep.transitions.append(f"{task.id} -> failed")
        elif task.attempts < max_attempts:
            self._transition(task, Status.READY, f"attempt {task.attempts} failed: {reason}; will retry")
            rep.transitions.append(f"{task.id} -> ready (retry)")
        else:
            self._transition(task, Status.FAILED, f"attempt {task.attempts} failed: {reason}; giving up")
            rep.transitions.append(f"{task.id} -> failed")

    # ---- automated review --------------------------------------------------
    def _maybe_review(self, task: Task, work_run: Run, rep: TickReport) -> None:
        if not bool(self.cfg.get("review.enabled", True)) or not task.pr:
            return
        st = self.state.get(task.id)
        if int(st.get("review_rounds", 0)) >= int(self.cfg.get("review.max_rounds", 2)):
            return
        try:
            run = self.dispatch_review(task, work_run)
            rep.dispatched.append(f"{task.id}(review)")
            self.log(f"{task.id}: review run {run.run_id} started")
        except Exception as e:  # noqa: BLE001
            task.log(f"automated review could not start: {e}")
            self.store.save(task)
            rep.errors.append(f"{task.id}: review dispatch failed: {e}")

    def dispatch_review(self, task: Task, work_run: Run | None = None) -> Run:
        harness_name = str(self.cfg.get("review.harness") or "")
        runner = self.runner_for(task, "local", harness_name)
        base = self.base_for(task)
        branch = task.branch or task.default_branch()
        wt = gitops.prepare_worktree(self.repo_for(task), self.worktree_for(task), branch, base)
        diff = gitops.diff(wt, base)
        pr_title, pr_body = task.title, ""
        if work_run is not None:
            pr_title = str(work_run.result.get("pr_title") or task.title)
            pr_body = str(work_run.result.get("pr_body") or "")
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available and not pr_body:
            try:
                info = self.github.get_pr(slug, number)
                pr_title, pr_body = info.title or pr_title, info.body
            except GitHubError:
                pass
        text = review_brief(self.store, task, branch=branch, base=base, pr_title=pr_title, pr_body=pr_body,
                            diff=diff, max_diff_chars=int(self.cfg.get("review.max_diff_chars", 60000)))
        run = self.runs.new_run(task.id, runner.name, mode="review")
        run.branch, run.base, run.worktree = branch, base, str(wt)
        run.model = self.model_for(task, runner, str(self.cfg.get("review.difficulty") or ""))
        if runner.harness and runner.harness.cfg.get("review_model"):
            run.model = str(runner.harness.cfg["review_model"])
        run.brief_tokens = max(1, len(text) // 4)
        run.save()
        runner.start(run, wt, text)
        st = self.state.get(task.id)
        st["review_run"] = run.run_id
        st["review_rounds"] = int(st.get("review_rounds", 0)) + 1
        self.state.save()
        return run

    def reap_review(self, task: Task, rep: TickReport) -> bool:
        st = self.state.get(task.id)
        run_id = st.get("review_run")
        if not run_id:
            return False
        run = next((r for r in self.runs.runs_for(task.id) if r.run_id == run_id), None)
        if run is None or run.status != "running":
            st["review_run"] = ""
            return False
        runner = self.runner_for(task, run.runner, run.harness)
        if not self._finished_or_timed_out(run, runner):
            return False
        st["review_run"] = ""
        if run.status != "timeout":
            run.exit_code = run.read_exit_code()
            run.finished_at = now_iso()
            collected = runner.collect(run)
            run.usage = collected.get("usage") or {}
            run.cost_usd = collected.get("cost_usd")
            run.error = collected.get("error") or ""
            final = collected.get("final_text") or ""
            if final and not (run.path / "final.md").exists():
                (run.path / "final.md").write_text(final)
            review = parse_review(final)
            run.result = review
            run.status = "done" if review else "failed"
            run.save()
        else:
            review = {}
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        if not review:
            task.log(f"automated review produced no verdict ({run.error[:120] or run.status}){cost}")
            self.store.save(task)
            rep.transitions.append(f"{task.id} review failed")
            return True
        st["last_review"] = review
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available:
            try:
                self.github.comment(slug, number, review_to_markdown(review, run.run_id))
            except GitHubError as e:
                self.log(f"{task.id}: could not post review: {e}")
        verdict = str(review.get("verdict", ""))
        if verdict == "request_changes" and task.status == Status.IN_REVIEW:
            fb = feedback_from_review(review)
            if fb and bool(self.cfg.get("auto_revise", True)):
                st["pending_feedback"] = fb
                self._transition(task, Status.CHANGES_REQUESTED, f"automated review requested changes: {review.get('summary', '')}{cost}")
                rep.transitions.append(f"{task.id} -> changes_requested (review)")
                return True
        task.log(f"automated review: {verdict} — {review.get('summary', '')}{cost}")
        self.store.save(task)
        rep.transitions.append(f"{task.id} review: {verdict}")
        return True

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
            self._transition(task, Status.DONE, f"PR merged: {task.pr}")
            rep.transitions.append(f"{task.id} -> done")
            self._cleanup(task)
            return
        if pr.state == "CLOSED":
            self._transition(task, Status.FAILED, f"PR closed without merging: {task.pr}")
            rep.transitions.append(f"{task.id} -> failed (PR closed)")
            return
        if task.status == Status.CHANGES_REQUESTED:
            return  # already waiting for a revise slot
        if pr.updated_at and pr.updated_at == st.get("pr_updated_at"):
            return  # nothing new on GitHub since last look
        st["pr_updated_at"] = pr.updated_at
        since = task.last_dispatched_at
        fb = self.github.feedback_since(slug, number, since)
        ci_note = ""
        if pr.checks == "FAILURE" and st.get("ci_failed_at") != pr.updated_at:
            names = ", ".join(pr.failed_checks) or "unknown"
            ci_note = f"- **CI** is failing on this branch (failed checks: {names}). Investigate the failing checks and fix them."
            st["ci_failed_at"] = pr.updated_at
        if not fb and not ci_note:
            return
        pending = fb.to_markdown() + ("\n\n" + ci_note if ci_note else "")
        st["pending_feedback"] = pending
        n = len(fb.items)
        note = f"{n} new review item(s)" if n else "CI failure"
        if n and ci_note:
            note += " + CI failure"
        if not bool(self.cfg.get("auto_revise", True)):
            self._transition(task, Status.CHANGES_REQUESTED, f"{note} (auto_revise off; dispatch by hand)")
            rep.transitions.append(f"{task.id} -> changes_requested")
            return
        max_rev = int(self.cfg.get("max_revisions", 3))
        if int(st.get("revisions", 0)) >= max_rev:
            self._transition(task, Status.CHANGES_REQUESTED, f"{note}, but {max_rev} revision rounds already used; needs a human")
            rep.transitions.append(f"{task.id} -> changes_requested (cap)")
            return
        self._transition(task, Status.CHANGES_REQUESTED, note)
        rep.transitions.append(f"{task.id} -> changes_requested")

    def _cleanup(self, task: Task) -> None:
        try:
            gitops.remove_worktree(self.repo_for(task), self.worktree_for(task))
        except Exception as e:  # noqa: BLE001
            self.log(f"{task.id}: worktree cleanup failed: {e}")

    # ---- dispatch ----------------------------------------------------------
    def dispatch_ready(self, rep: TickReport) -> None:
        from .graph import ready

        tasks = self.store.tasks()
        max_rev = int(self.cfg.get("max_revisions", 3))
        # revise runs first: they unblock humans waiting on PRs
        queue: list[tuple[Task, str]] = [
            (t, "revise") for t in tasks.values()
            if t.status == Status.CHANGES_REQUESTED
            and self.state.get(t.id).get("pending_feedback")
            and int(self.state.get(t.id).get("revisions", 0)) < max_rev
        ]
        queue += [(t, "work") for t in ready(tasks)]
        for task, mode in queue:
            if self.slots_free() <= 0:
                break
            runner = self.runner_for(task)
            if not runner.detached:
                continue  # manual tasks are taken by a human, not auto-dispatched
            try:
                self.dispatch(task, mode=mode, runner=runner)
                rep.dispatched.append(f"{task.id}({mode})")
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{task.id}: dispatch failed: {e}")
                self._transition(task, Status.FAILED, f"dispatch failed: {e}")

    def dispatch(self, task: Task, mode: str = "work", runner: Runner | None = None, worktree: bool = True) -> Run:
        runner = runner or self.runner_for(task)
        branch = task.branch or task.default_branch()
        base = self.base_for(task)
        st = self.state.get(task.id)
        feedback = str(st.get("pending_feedback") or "") if mode == "revise" else ""
        brief = build_brief(self.store, task, branch=branch, base=base, review_feedback=feedback)
        run = self.runs.new_run(task.id, runner.name, mode=mode)
        run.branch, run.base, run.brief_tokens = branch, base, brief.tokens
        run.model = self.model_for(task, runner)
        run.harness = runner.harness.name if runner.harness else ""
        runner.assign(run, self.active_runs())
        wt: Path | None = None
        if worktree and not runner.remote:
            wt = gitops.prepare_worktree(self.repo_for(task), self.worktree_for(task), branch, base)
            run.worktree = str(wt)
        run.save()
        runner.start(run, wt or self.store.root, brief.text)
        task.branch = branch
        task.attempts += 1 if mode == "work" else 0
        task.last_dispatched_at = now_iso()
        if mode == "revise":
            st["revisions"] = int(st.get("revisions", 0)) + 1
            st["pending_feedback"] = ""
        where = f" on {run.host}" if run.host else ""
        model = f" model={run.model}" if run.model else ""
        self._transition(task, Status.RUNNING, f"dispatched {mode} run {run.run_id} via {runner.name}{where} [{run.harness or 'human'}{model}] (brief ~{brief.tokens} tokens)")
        self.state.save()
        return run

    # ---- manual controls ---------------------------------------------------
    def cancel(self, task: Task, note: str = "cancelled") -> None:
        run = self.runs.latest(task.id)
        if run and run.status == "running":
            run.kill()
            run.status = "cancelled"
            run.finished_at = now_iso()
            run.save()
        self._transition(task, Status.CANCELLED, note)

    def retry(self, task: Task) -> None:
        task.attempts = 0
        self._transition(task, Status.READY, "reset to ready by hand")

    def finish_manual(self, task: Task, result: dict[str, Any]) -> TickReport:
        from .runner.manual import ManualRunner

        run = self.runs.latest(task.id)
        if run is None or run.status != "running":
            raise RuntimeError(f"{task.id} has no active run to finish")
        ManualRunner.finish(run, result)
        rep = TickReport()
        self.finalize(task, run, self.runner_for(task, run.runner), rep)
        self.state.save()
        return rep
