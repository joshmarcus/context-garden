"""The scheduler: a deterministic state machine over task files. No LLM calls.

    tick():
      1. reap     running tasks whose worker finished -> push, open PR, in_review (or retry/fail)
      2. poll     in_review tasks -> merged? closed? new feedback? CI red? -> done/failed/changes_requested
      3. dispatch ready tasks (deps done) into free slots; revise runs for changes_requested

State that isn't in task files lives in .garden/state.json (per-task PR bookkeeping).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitops
from .brief import build_brief
from .github import GitHub, GitHubError
from .model import Status, Task, now_iso
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
        runner_factory: Callable[[str], Runner] | None = None,
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
    def runner_for(self, task: Task, name: str = "") -> Runner:
        name = name or task.runner or self.cfg.product_runner(task.product)
        if self._runner_factory:
            return self._runner_factory(name)
        cfg = dict(self.cfg.get("claude", {}) or {}) if name == "claude-local" else {}
        return get_runner(name, cfg)

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

    def slots_free(self) -> int:
        active = [r for r in self.runs.active() if r.runner != "manual"]
        return max(0, int(self.cfg.get("max_parallel", 2)) - len(active))

    def _transition(self, task: Task, status: Status, note: str) -> None:
        old = task.status.value
        task.status = status
        task.log(note)
        self.store.save(task)
        self.log(f"{task.id}: {old} -> {status.value} ({note})")

    # ---- tick --------------------------------------------------------------
    def tick(self, dispatch: bool | None = None) -> TickReport:
        rep = TickReport()
        self.store.invalidate()
        tasks = self.store.tasks()
        for t in list(tasks.values()):
            if t.status == Status.RUNNING:
                try:
                    if self.reap(t, rep):
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
        if run is None or run.status != "running":
            # running with no run record: someone edited the file by hand
            self._transition(task, Status.READY, "no active run found; back to ready")
            rep.transitions.append(f"{task.id} running -> ready (no run)")
            return True
        runner = self.runner_for(task, run.runner)
        timeout_min = float(self.cfg.get("claude.timeout_minutes", 90) or 0)
        if not run.process_finished():
            if runner.detached and timeout_min and run.elapsed_minutes() > timeout_min + 5:
                run.kill()
                run.status = "timeout"
                run.finished_at = now_iso()
                run.error = "timed out"
                run.save()
                self._retry_or_fail(task, run, rep, "worker timed out")
                return True
            return False
        self.finalize(task, run, runner, rep)
        return True

    def finalize(self, task: Task, run: Run, runner: Runner, rep: TickReport) -> None:
        run.exit_code = run.read_exit_code()
        run.finished_at = now_iso()
        collected = runner.collect(run)
        run.result = collected.get("result") or {}
        run.usage = collected.get("usage") or {}
        run.cost_usd = collected.get("cost_usd")
        run.error = collected.get("error") or ""
        final_text = collected.get("final_text") or ""
        if final_text:
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

        worktree = Path(run.worktree) if run.worktree else self.worktree_for(task)
        base = run.base or self.base_for(task)
        branch = run.branch or task.branch or task.default_branch()
        if not worktree.exists():
            run.status = "done"
            run.save()
            # manual run without a worktree: trust the reported PR, if any
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
        if not slug or not self.github.available:
            self._transition(task, Status.IN_REVIEW,
                             f"branch {branch} pushed; GitHub unavailable, open the PR by hand and run `garden pr {task.id} <url>`{cost}")
            rep.transitions.append(f"{task.id} -> in_review (no PR)")
            return
        try:
            existing = self.github.find_pr(slug, branch)
            if existing and existing.state == "OPEN":
                task.pr = existing.url
                body = str(result.get("pr_body") or summary)
                if run.mode == "revise" and body:
                    try:
                        self.github.comment(slug, existing.number, f"Pushed a revision round: {summary}\n\n{body}\n\n_garden run {run.run_id}_")
                    except GitHubError:
                        pass
                self.state.get(task.id)["pr_number"] = existing.number
                self._transition(task, Status.IN_REVIEW, f"pushed revision to {existing.url}: {summary}{cost}")
                rep.transitions.append(f"{task.id} -> in_review (revised)")
                return
            title = str(result.get("pr_title") or f"{task.id}: {task.title}")
            body = str(result.get("pr_body") or summary or task.body)
            body += f"\n\n---\nTask `{task.id}` from the context garden ({task.product}/{task.phase}). Run `{run.run_id}`."
            pr = self.github.create_pr(
                slug, branch, base, title, body,
                draft=bool(self.cfg.get("github.draft_pr", False)),
                reviewers=list(self.cfg.get("github.reviewers", []) or []),
            )
            task.pr = pr.url
            st = self.state.get(task.id)
            st["pr_number"] = pr.number
            st["revisions"] = 0
            self._transition(task, Status.IN_REVIEW, f"opened {pr.url}: {summary}{cost}")
            rep.transitions.append(f"{task.id} -> in_review ({pr.url})")
        except GitHubError as e:
            self._transition(task, Status.IN_REVIEW, f"branch pushed but PR failed ({e}); open it by hand and run `garden pr {task.id} <url>`{cost}")
            rep.transitions.append(f"{task.id} -> in_review (PR failed)")

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

    # ---- poll --------------------------------------------------------------
    def poll(self, task: Task, rep: TickReport) -> None:
        if not self.github.available:
            return
        slug = self.slug_for(task)
        if not slug:
            return
        st = self.state.get(task.id)
        number = st.get("pr_number")
        if not number:
            import re

            m = re.search(r"/pull/(\d+)", task.pr)
            if not m:
                return
            number = int(m.group(1))
            st["pr_number"] = number
        pr = self.github.get_pr(slug, number)
        st["pr_state"] = pr.state
        st["review_decision"] = pr.review_decision
        st["checks"] = pr.checks
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
            ci_note = "CI checks are failing on this branch. Investigate the failing checks and fix them."
            st["ci_failed_at"] = pr.updated_at
        if not fb and not ci_note:
            return
        if not bool(self.cfg.get("auto_revise", True)):
            self._transition(task, Status.CHANGES_REQUESTED, "new review feedback (auto_revise off; dispatch by hand)")
            rep.transitions.append(f"{task.id} -> changes_requested")
            st["pending_feedback"] = fb.to_markdown() + ("\n\n" + ci_note if ci_note else "")
            return
        max_rev = int(self.cfg.get("max_revisions", 3))
        if int(st.get("revisions", 0)) >= max_rev:
            self._transition(task, Status.CHANGES_REQUESTED, f"new feedback but {max_rev} revision rounds already used; needs a human")
            rep.transitions.append(f"{task.id} -> changes_requested (cap)")
            st["pending_feedback"] = fb.to_markdown() + ("\n\n" + ci_note if ci_note else "")
            return
        st["pending_feedback"] = fb.to_markdown() + ("\n\n" + ci_note if ci_note else "")
        self._transition(task, Status.CHANGES_REQUESTED, f"{len(fb.items)} new review item(s)" + (" + CI failure" if ci_note else ""))
        rep.transitions.append(f"{task.id} -> changes_requested")

    def _cleanup(self, task: Task) -> None:
        try:
            gitops.remove_worktree(self.repo_for(task), self.worktree_for(task))
        except Exception:  # noqa: BLE001
            pass

    # ---- dispatch ----------------------------------------------------------
    def dispatch_ready(self, rep: TickReport) -> None:
        from .graph import ready

        tasks = self.store.tasks()
        # revise runs first: they unblock humans waiting on PRs
        max_rev = int(self.cfg.get("max_revisions", 3))
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
                self.dispatch(task, mode=mode)
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
        wt: Path | None = None
        if worktree:
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
        self._transition(task, Status.RUNNING, f"dispatched {mode} run {run.run_id} via {runner.name} (brief ~{brief.tokens} tokens)")
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
