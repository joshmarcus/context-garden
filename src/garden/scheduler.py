"""The scheduler: a deterministic state machine over task files. No LLM calls.

    tick():
      1. reap     running tasks whose worker finished -> push, open PR, in_review
                  (or retry / fail / waiting_human); finished review runs -> comment, maybe
                  changes_requested; discovered work -> new task files
      2. poll     in_review tasks -> merged? closed? new feedback? CI red? -> done (restack
                  children) / failed / changes_requested
      3. dispatch ready tasks (deps done, or stacked on an open PR) into free slots, within
                  phase budgets; revise runs for changes_requested; skip stalled tasks

State that isn't in task files lives in .garden/state.json; history in .garden/events.jsonl.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitops
from .brief import build_brief, resume_prompt
from .events import EventLog
from .github import GitHub, GitHubError
from .graph import blockers, ready, stack_parents
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
        self.events = EventLog(self.cfg.garden_dir / "events.jsonl")
        self.github = github if github is not None else GitHub(use_gh=bool(self.cfg.get("github.use_gh", True)))
        self._runner_factory = runner_factory
        self.log = log or (lambda msg: None)

    # ---- helpers -----------------------------------------------------------
    @property
    def stack_enabled(self) -> bool:
        return bool(self.cfg.get("stack", True))

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
        """The branch this task's PR currently targets (a stack parent's branch, or the product base)."""
        pb = self.state.get(task.id).get("pr_base")
        return str(pb) if pb else self.final_base_for(task)

    def final_base_for(self, task: Task) -> str:
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
        self.events.emit("transition", task.id, **{"from": old, "to": status.value, "note": note})
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

    # ---- budgets -----------------------------------------------------------
    def budget_for(self, task_or_key: Task | str) -> float:
        key = task_or_key if isinstance(task_or_key, str) else task_or_key.key
        product = key.split("/", 1)[0]
        b = (self.cfg.get("budgets", {}) or {}).get(key)
        if b is None:
            b = self.cfg.product(product).get("budget_usd")
        return float(b or 0.0)

    def spent_for(self, key: str) -> float:
        ids = {t.id for t in self.store.tasks().values() if t.key == key}
        return round(sum(r.cost_usd or 0.0 for r in self.runs.all_runs() if r.task_id in ids), 4)

    def budget_exceeded(self, task: Task) -> bool:
        budget = self.budget_for(task)
        if not budget:
            return False
        spent = self.spent_for(task.key)
        if spent < budget:
            return False
        marker = self.state.get(f"_phase:{task.key}")
        if not marker.get("budget_hit"):
            marker["budget_hit"] = now_iso()
            self.events.emit("budget", "", phase=task.key, spent=spent, budget=budget)
            self.log(f"{task.key}: budget ${budget:.2f} exceeded (spent ${spent:.2f}); dispatch paused")
        return True

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
            self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, status="timeout", cost_usd=None)
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
        run.session_id = str(collected.get("session_id") or run.session_id or "")
        final_text = collected.get("final_text") or ""
        if final_text and not (run.path / "final.md").exists():
            (run.path / "final.md").write_text(final_text)
        result = run.result
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        self.events.emit("run_finished", task.id, run=run.run_id, mode=run.mode, harness=run.harness, model=run.model,
                         status=str(result.get("status") or ("error" if run.error else "no_result")),
                         cost_usd=run.cost_usd, usage=run.usage, exit_code=run.exit_code)

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
        status = str(result.get("status", "")).lower()
        if status == "blocked":
            run.status = "blocked"
            run.save()
            self._transition(task, Status.FAILED, f"worker blocked: {result.get('summary') or result.get('notes') or '?'}{cost}")
            rep.transitions.append(f"{task.id} -> failed (blocked)")
            return
        if status == "needs_input":
            run.status = "waiting"
            run.save()
            question = str(result.get("question") or result.get("summary") or "(no question given)")
            st = self.state.get(task.id)
            st["question"] = question
            st["session_id"] = run.session_id
            st["session_host"] = run.host
            st["session_harness"] = run.harness
            st["question_run"] = run.run_id
            self.events.emit("waiting_human", task.id, question=question, run=run.run_id)
            self._transition(task, Status.WAITING_HUMAN, f"worker asks: {question}{cost}")
            rep.transitions.append(f"{task.id} -> waiting_human")
            return

        self._file_discovered(task, run, result)

        base = run.base or self.base_for(task)
        branch = run.branch or task.branch or task.default_branch()
        repo = self.repo_for(task)

        if runner.remote:
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
            self._after_push(task, run, wt, branch, base, result, rep, cost)
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
        st = self.state.get(task.id)
        force = bool(st.pop("force_push", False))
        try:
            gitops.push(worktree, branch, force=force)
        except gitops.GitError as e:
            self._transition(task, Status.FAILED, f"push failed: {e}{cost}")
            rep.transitions.append(f"{task.id} -> failed (push)")
            return
        task.branch = branch
        self._after_push(task, run, worktree, branch, base, result, rep, cost)

    def _after_push(self, task: Task, run: Run, worktree: Path, branch: str, base: str, result: dict[str, Any],
                    rep: TickReport, cost: str) -> None:
        """Stall bookkeeping, then PR open/update, then a pending restack if the parent merged meanwhile."""
        st = self.state.get(task.id)
        stalled = False
        if bool(self.cfg.get("stall.enabled", True)) and worktree.exists():
            h = gitops.diff_hash(worktree, base)
            if run.mode == "revise" and h and h == st.get("last_diff_hash"):
                stalled = True
            st["last_diff_hash"] = h
        self._open_or_update_pr(task, run, branch, base, result, rep, cost)
        if stalled:
            self._stall(task, rep, f"revise run {run.run_id} produced no change to the diff")
        if st.pop("restack_pending", False) and task.status == Status.IN_REVIEW:
            self._restack(task, rep)

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
                if run.mode in ("revise", "resume"):
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
                footer = f"\n\n---\nTask `{task.id}` from the context garden ({task.product}/{task.phase})."
                if st.get("stack_parent"):
                    footer += f" Stacked on `{st['stack_parent']}` (targets `{base}` until it merges)."
                if st.get("discovered_ids"):
                    footer += " Discovered work: " + ", ".join(f"`{i}`" for i in st["discovered_ids"]) + "."
                pr = self.github.create_pr(
                    slug, branch, base, title, body + footer,
                    draft=bool(self.cfg.get("github.draft_pr", False)),
                    reviewers=list(self.cfg.get("github.reviewers", []) or []),
                )
                task.pr = pr.url
                st["pr_number"] = pr.number
                st["revisions"] = 0
                st["review_rounds"] = 0
                self.events.emit("pr_opened", task.id, pr=pr.url, base=base, stacked_on=st.get("stack_parent", ""))
                self._transition(task, Status.IN_REVIEW, f"opened {pr.url} (base {base}): {summary}{cost}")
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

    # ---- discovered work ---------------------------------------------------
    def _file_discovered(self, task: Task, run: Run, result: dict[str, Any]) -> list[Task]:
        items = result.get("discovered") or []
        if not isinstance(items, list) or not items:
            return []
        auto_blocking = bool(self.cfg.get("discovered.auto_approve_blocking", True))
        existing = {t.title.strip().lower() for t in self.store.tasks().values()}
        created: list[Task] = []
        for item in items:
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                continue
            title = str(item["title"]).strip()
            if title.lower() in existing:
                continue
            blocking = bool(item.get("blocking"))
            body = str(item.get("body") or "").strip() or f"## Goal\n\n{title}\n"
            body += f"\n\n## Provenance\n\nDiscovered by {task.id} ({task.title}) during run `{run.run_id}`."
            diff = str(item.get("difficulty") or "medium")
            t = self.store.create_task(
                task.product, task.phase, title, body,
                priority=int(item.get("priority") or task.priority), reading=[str(r) for r in (item.get("reading") or [])] or list(task.reading),
                status="ready" if (blocking and auto_blocking) else "draft",
                difficulty=diff if diff in DIFFICULTIES else "medium",
            )
            t.discovered_from = task.id
            t.log(f"discovered by {task.id}" + (" (blocking)" if blocking else ""))
            self.store.save(t)
            existing.add(title.lower())
            created.append(t)
            self.events.emit("discovered", task.id, new_task=t.id, title=title, blocking=blocking, status=t.status.value)
            self.log(f"{task.id}: discovered {t.id} {title!r}" + (" [blocking, ready]" if blocking and auto_blocking else ""))
        if created:
            st = self.state.get(task.id)
            st["discovered_ids"] = sorted(set(st.get("discovered_ids", []) + [t.id for t in created]))
            task.log("discovered work filed: " + ", ".join(t.id for t in created))
            self.store.invalidate()
        return created

    # ---- stall detection ---------------------------------------------------
    def _stall(self, task: Task, rep: TickReport, reason: str) -> None:
        st = self.state.get(task.id)
        st["needs_human"] = reason
        self.events.emit("stall", task.id, reason=reason)
        if task.status != Status.CHANGES_REQUESTED:
            self._transition(task, Status.CHANGES_REQUESTED, f"stalled: {reason}; needs a human (garden retry to resume)")
        else:
            task.log(f"stalled: {reason}; needs a human")
            self.store.save(task)
        rep.transitions.append(f"{task.id} stalled")

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
        self.events.emit("dispatch", task.id, run=run.run_id, mode="review", model=run.model, harness=run.harness)
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
        review: dict[str, Any] = {}
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
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        self.events.emit("run_finished", task.id, run=run.run_id, mode="review", cost_usd=run.cost_usd, usage=run.usage,
                         status=str(review.get("verdict") or run.status))
        if not review:
            task.log(f"automated review produced no verdict ({run.error[:120] or run.status}){cost}")
            self.store.save(task)
            rep.transitions.append(f"{task.id} review failed")
            return True
        st["last_review"] = review
        verdict = str(review.get("verdict", ""))
        self.events.emit("review", task.id, run=run.run_id, verdict=verdict, summary=str(review.get("summary", "")),
                         blocking=sum(1 for f in review.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "blocking"),
                         description_ok=bool(review.get("description_ok", True)))
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available:
            try:
                self.github.comment(slug, number, review_to_markdown(review, run.run_id))
            except GitHubError as e:
                self.log(f"{task.id}: could not post review: {e}")
        # repeated blocking findings across rounds = the loop isn't converging
        keys = sorted({f"{f.get('file', '')}|{str(f.get('summary', '')).strip().lower()}"
                       for f in review.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "blocking"})
        repeated = sorted(set(keys) & set(st.get("last_findings", [])))
        st["last_findings"] = keys
        if verdict == "request_changes" and task.status == Status.IN_REVIEW:
            if repeated and bool(self.cfg.get("stall.enabled", True)):
                self._stall(task, rep, f"review finding repeated after a revise round: {repeated[0].split('|')[1][:80]}")
                return True
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
            self._on_merged(task, rep)
            self._cleanup(task)
            return
        if pr.state == "CLOSED":
            self.events.emit("pr_closed", task.id, pr=task.pr)
            self._transition(task, Status.FAILED, f"PR closed without merging: {task.pr}")
            rep.transitions.append(f"{task.id} -> failed (PR closed)")
            self._on_parent_closed(task, rep)
            return
        if task.status == Status.CHANGES_REQUESTED:
            return  # already waiting for a revise slot (or a human)
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
        self.events.emit("feedback", task.id, items=n, ci=bool(ci_note))
        if not bool(self.cfg.get("auto_revise", True)):
            self._transition(task, Status.CHANGES_REQUESTED, f"{note} (auto_revise off; dispatch by hand)")
            rep.transitions.append(f"{task.id} -> changes_requested")
            return
        max_rev = int(self.cfg.get("max_revisions", 3))
        if int(st.get("revisions", 0)) >= max_rev:
            st["needs_human"] = f"{max_rev} revision rounds used"
            self.events.emit("needs_human", task.id, reason=st["needs_human"])
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

    # ---- stacking ----------------------------------------------------------
    def stacked_children(self, task: Task) -> list[Task]:
        return [t for t in self.store.tasks().values()
                if self.state.get(t.id).get("stack_parent") == task.id and not t.status.terminal]

    def _on_merged(self, task: Task, rep: TickReport) -> None:
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
            f"resolve the conflicts keeping the intent of both sides, and continue the rebase. The runner will force-push the rebased branch."
        )
        st["force_push"] = True
        self.events.emit("restacked", child.id, parent=parent_id, base=new_base, conflict=True, files=files)
        if child.status in (Status.IN_REVIEW, Status.CHANGES_REQUESTED):
            self._transition(child, Status.CHANGES_REQUESTED, f"parent {parent_id} merged; rebase onto {new_base} conflicts ({', '.join(files)}); revise run will resolve")
            rep.transitions.append(f"{child.id} -> changes_requested (rebase)")
        else:
            child.log(f"parent {parent_id} merged; rebase onto {new_base} conflicts; next run must resolve")
            self.store.save(child)

    def _on_parent_closed(self, task: Task, rep: TickReport) -> None:
        for child in self.stacked_children(task):
            st = self.state.get(child.id)
            st["needs_human"] = f"stack parent {task.id} was closed without merging"
            self.events.emit("needs_human", child.id, reason=st["needs_human"])
            child.log(f"stack parent {task.id} closed without merging; this PR targets a dead branch and needs a human")
            self.store.save(child)
            rep.transitions.append(f"{child.id} needs human (parent closed)")

    # ---- dispatch ----------------------------------------------------------
    def dispatch_ready(self, rep: TickReport) -> None:
        tasks = self.store.tasks()
        max_rev = int(self.cfg.get("max_revisions", 3))
        queue: list[tuple[Task, str]] = [
            (t, "revise") for t in tasks.values()
            if t.status == Status.CHANGES_REQUESTED
            and self.state.get(t.id).get("pending_feedback")
            and not self.state.get(t.id).get("needs_human")
            and int(self.state.get(t.id).get("revisions", 0)) < max_rev
        ]
        queue += [(t, "work") for t in ready(tasks, stack=self.stack_enabled)]
        for task, mode in queue:
            if self.slots_free() <= 0:
                break
            if self.budget_exceeded(task):
                continue
            runner = self.runner_for(task)
            if not runner.detached:
                continue  # manual tasks are taken by a human, not auto-dispatched
            try:
                self.dispatch(task, mode=mode, runner=runner)
                rep.dispatched.append(f"{task.id}({mode})")
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{task.id}: dispatch failed: {e}")
                self._transition(task, Status.FAILED, f"dispatch failed: {e}")

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
                 session_id: str = "", prompt_override: str = "") -> Run:
        runner = runner or self.runner_for(task)
        branch = task.branch or task.default_branch()
        st = self.state.get(task.id)
        stack = self._stack_for(task) if mode == "work" else None
        base = self.base_for(task)
        feedback = str(st.get("pending_feedback") or "") if mode == "revise" else ""
        qa = list(st.get("qa") or [])
        brief = build_brief(self.store, task, branch=branch, base=base, review_feedback=feedback, stack=stack, qa=qa)
        text = prompt_override or brief.text
        run = self.runs.new_run(task.id, runner.name, mode=mode)
        run.branch, run.base, run.brief_tokens = branch, base, max(1, len(text) // 4)
        run.model = self.model_for(task, runner)
        run.harness = runner.harness.name if runner.harness else ""
        run.session_id = session_id
        if session_id and st.get("session_host"):
            run.host = str(st["session_host"])
        runner.assign(run, self.active_runs())
        wt: Path | None = None
        if worktree and not runner.remote:
            wt = gitops.prepare_worktree(self.repo_for(task), self.worktree_for(task), branch, base)
            run.worktree = str(wt)
        run.save()
        runner.start(run, wt or self.store.root, text)
        task.branch = branch
        task.attempts += 1 if mode == "work" else 0
        task.last_dispatched_at = now_iso()
        if mode == "revise":
            st["revisions"] = int(st.get("revisions", 0)) + 1
            st["pending_feedback"] = ""
        where = f" on {run.host}" if run.host else ""
        model = f" model={run.model}" if run.model else ""
        how = "resumed session" if session_id else "fresh session"
        stacked = f" stacked on {stack['parent_id']}" if stack else ""
        self.events.emit("dispatch", task.id, run=run.run_id, mode=mode, model=run.model, harness=run.harness,
                         host=run.host, base=base, brief_tokens=run.brief_tokens, resumed=bool(session_id))
        self._transition(task, Status.RUNNING, f"dispatched {mode} run {run.run_id} via {runner.name}{where} [{run.harness or 'human'}{model}] ({how}, base {base}{stacked}, ~{run.brief_tokens} tokens)")
        self.state.save()
        return run

    # ---- human answers -----------------------------------------------------
    def answer(self, task: Task, text: str) -> Run:
        if task.status != Status.WAITING_HUMAN:
            raise RuntimeError(f"{task.id} is {task.status.value}, not waiting_human")
        st = self.state.get(task.id)
        question = str(st.get("question") or "")
        st.setdefault("qa", []).append({"q": question, "a": text, "at": now_iso()})
        self.events.emit("answer", task.id, question=question, answer=text)
        runner = self.runner_for(task, "", str(st.get("session_harness") or ""))
        sid = str(st.get("session_id") or "")
        st["question"] = ""
        st["session_id"] = ""
        if sid and runner.harness is not None and runner.harness.can_resume:
            return self.dispatch(task, mode="resume", runner=runner, session_id=sid, prompt_override=resume_prompt(question, text))
        # harness can't resume: a fresh run with the Q&A in its brief
        return self.dispatch(task, mode="resume", runner=runner)

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
        st = self.state.get(task.id)
        st.pop("needs_human", None)
        if task.pr and task.status in (Status.CHANGES_REQUESTED, Status.IN_REVIEW, Status.FAILED):
            # keep the PR; let the revise loop continue
            if not st.get("pending_feedback"):
                st["pending_feedback"] = "- **human**: please re-check the open review comments and CI on this PR and address what is still outstanding."
            self._transition(task, Status.CHANGES_REQUESTED, "re-enabled by hand; revise run will follow")
            self.state.save()
            return
        task.attempts = 0
        self._transition(task, Status.READY, "reset to ready by hand")
        self.state.save()

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
