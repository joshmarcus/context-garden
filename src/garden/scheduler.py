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

import fcntl
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitops
from .brief import build_brief, resume_prompt
from .checks import failures as check_failures
from .checks import run_checks, to_feedback
from .events import EventLog
from .github import GitHub, GitHubError, mark_garden_comment
from .graph import blockers, ready, stack_parents
from .harness import DIFFICULTIES
from .model import Phase, Status, Task, now_iso
from .notify import notify, should_notify
from .personas import parse_persona, phase_brief, pr_brief, report_markdown, report_path, valid_name
from .review import feedback_from_review, parse_review, review_brief, review_to_markdown
from .runner import get_runner
from .runner.base import Runner
from .runs import Run, RunStore
from .store import Store
from .trials import TrialLog, compare_brief, parse_compare, parse_contender, ranking_markdown


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


class _TaskState(dict):
    """dict subclass that records which keys have been written since creation.

    Tracks dirty keys so State.save() can merge only changed keys back to disk,
    letting two concurrent writers update different keys of the same task without
    losing each other's changes.
    """

    def __init__(self, data: dict) -> None:
        super().__init__(data)
        # Store _dirty in the object's __dict__, not in the dict key-value store.
        object.__setattr__(self, "_dirty", set())

    @property
    def dirty(self) -> set:
        return object.__getattribute__(self, "_dirty")

    def __getitem__(self, key: str) -> Any:
        val = super().__getitem__(key)
        # Mark mutable values (dict/list) as dirty immediately: the caller is
        # likely to mutate the nested object in-place, and we have no way to
        # intercept those mutations.  Scalar reads are harmless to leave clean.
        if isinstance(val, (dict, list)):
            self.dirty.add(key)
        return val

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self.dirty.add(key)

    def pop(self, key: str, *args: Any) -> Any:  # type: ignore[override]
        result = super().pop(key, *args)
        self.dirty.add(key)
        return result

    def setdefault(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key not in self:
            self[key] = default  # goes through __setitem__ → marks dirty
        else:
            # Key already present; caller may mutate the value in place (e.g. list.append).
            # Mark it dirty so save() picks up in-place mutations.
            self.dirty.add(key)
        return self[key]


class State:
    """Small JSON side-store for things that don't belong in task frontmatter.

    Concurrency guarantee: save() acquires an exclusive flock on a companion
    lock file, re-reads the on-disk state, and merges only the keys that this
    process actually wrote on top of what is currently on disk.  Two concurrent
    writers that touch different keys of the same task will both survive.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, _TaskState] = {}
        if path.exists():
            try:
                raw: dict[str, Any] = json.loads(path.read_text())
                self.data = {
                    k: _TaskState(v) if isinstance(v, dict) else v
                    for k, v in raw.items()
                }
            except json.JSONDecodeError:
                self.data = {}

    def get(self, task_id: str) -> _TaskState:
        existing = self.data.get(task_id)
        if existing is None:
            ts = _TaskState({})
            self.data[task_id] = ts
            return ts
        if not isinstance(existing, _TaskState):
            ts = _TaskState(existing)
            self.data[task_id] = ts
            return ts
        return existing

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        dirty_by_tid: dict[str, set] = {
            tid: ts.dirty
            for tid, ts in self.data.items()
            if isinstance(ts, _TaskState) and ts.dirty
        }
        if not dirty_by_tid:
            return
        lock_path = self.path.parent / (self.path.name + ".lock")
        with open(lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            disk: dict[str, Any] = {}
            if self.path.exists():
                try:
                    disk = json.loads(self.path.read_text())
                except json.JSONDecodeError:
                    disk = {}
            for tid, dirty_keys in dirty_by_tid.items():
                task_disk = disk.setdefault(tid, {})
                task_mem = self.data[tid]
                for key in dirty_keys:
                    if key in task_mem:
                        task_disk[key] = task_mem[key]
                    else:
                        task_disk.pop(key, None)
            self.path.write_text(json.dumps(disk, indent=2, sort_keys=True))


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
        self.trials = TrialLog(self.cfg.garden_dir / "trials.jsonl")
        self.github = github if github is not None else GitHub(
            use_gh=bool(self.cfg.get("github.use_gh", True)),
            bot_logins=[str(b) for b in (self.cfg.get("github.bot_logins") or [])],
        )
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
        cfg["setup"] = self.cfg.product_setup(task.product)  # how this product prepares its env
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
            return gitops.ensure_repo(repo, self.cfg.repos_dir)
        return gitops.ensure_repo(Path(repo), self.cfg.repos_dir)

    def worktree_for(self, task: Task) -> Path:
        override = self.state.get(task.id).get("worktree")
        if override:
            return Path(override)
        return self.cfg.worktree_path(task.id)

    def check_ctx(self, task: Task, branch: str, base: str, worktree: Path | None = None) -> dict[str, Any]:
        """Context passed to check commands as GARDEN_* env vars. `exec_root` (GARDEN_EXEC_ROOT)
        is the live garden's own root, e.g. for `$GARDEN_EXEC_ROOT/.venv/bin/python` — distinct
        from GARDEN_ROOT, which run_checks always sets to a non-existent sentinel so a check
        command cannot use it to act on the live garden (see find_root)."""
        st = self.state.get(task.id)
        return {"exec_root": str(self.store.root), "task_id": task.id, "product": task.product, "phase": task.phase, "branch": branch, "base": base,
                "repo_slug": self.slug_for(task) or "", "pr": task.pr, "pr_number": st.get("pr_number") or 0,
                "head_sha": st.get("head_sha") or "", "failed_checks": st.get("failed_checks") or [],
                "worktree": str(worktree or self.worktree_for(task))}

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

    def _transition(self, task: Task, status: Status, note: str, needs_human: bool = False) -> None:
        old = task.status.value
        task.status = status
        task.log(note)
        self.store.save(task)
        self.events.emit("transition", task.id, **{"from": old, "to": status.value, "note": note})
        self.log(f"{task.id}: {old} -> {status.value} ({note})")
        if should_notify(status.value, needs_human=needs_human):
            notify(self.cfg.data, task.id, status.value, note, task.pr or "")

    def _pr_status(self, task: Task) -> Status:
        """Where an open PR sits: awaiting a human's triage while it is a draft, else in review."""
        return Status.AWAITING_TRIAGE if self.state.get(task.id).get("pr_draft") else Status.IN_REVIEW

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
            notify(self.cfg.data, task.key, "budget", f"budget ${budget:.2f} exceeded (spent ${spent:.2f})", "")
        return True

    # ---- dispatch pause/resume ---------------------------------------------
    def control(self) -> dict[str, Any]:
        """Return the _control entry; non-empty means dispatch is paused."""
        return self.state.get("_control")

    def is_dispatch_paused(self) -> bool:
        return self.control().get("dispatch") == "paused"

    def pause(self, by: str = "cli", reason: str = "") -> None:
        ctrl = self.control()
        ctrl["dispatch"] = "paused"
        ctrl["by"] = by
        ctrl["at"] = now_iso()
        ctrl["reason"] = reason
        self.state.save()
        self.events.emit("dispatch_paused", "", by=by, reason=reason)
        self.log("dispatch paused by " + by + (f": {reason}" if reason else ""))

    def resume(self, by: str = "cli") -> None:
        ctrl = self.control()
        ctrl.pop("dispatch", None)
        ctrl.pop("by", None)
        ctrl.pop("at", None)
        ctrl.pop("reason", None)
        self.state.save()
        self.events.emit("dispatch_resumed", "", by=by)
        self.log(f"dispatch resumed by {by}")

    # ---- tick --------------------------------------------------------------
    def tick(self, dispatch: bool | None = None) -> TickReport:
        rep = TickReport()
        self.store.invalidate()
        self.state = State(self.state.path)  # the CLI, web UI or TUI may have written state since the last pass
        try:
            self.reap_aux(rep)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"aux reap failed: {e}")
        tasks = self.store.tasks()
        for t in list(tasks.values()):
            try:
                if t.status == Status.RUNNING and self.state.get(t.id).get("trial", {}).get("status") in ("running", "comparing"):
                    if self.reap_trial(t, rep):
                        rep.reaped.append(t.id)
                    continue
                if t.status == Status.RUNNING and self.reap(t, rep):
                    rep.reaped.append(t.id)
                elif t.status.pr_open and self.reap_review(t, rep):
                    rep.reaped.append(t.id)
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                rep.errors.append(f"{t.id}: reap failed: {e}")
                self.log(f"{t.id}: reap failed: {e}")
        self.store.invalidate()
        tasks = self.store.tasks()
        for t in list(tasks.values()):
            if t.status.pr_open and t.pr:
                try:
                    self.poll(t, rep)
                    rep.polled.append(t.id)
                except Exception as e:  # noqa: BLE001
                    rep.errors.append(f"{t.id}: poll failed: {e}")
                    self.log(f"{t.id}: poll failed: {e}")
        if dispatch is None:
            dispatch = bool(self.cfg.get("auto_dispatch", True))
        if self.is_dispatch_paused():
            dispatch = False
        if dispatch:
            self.dispatch_ready(rep)
        self.state.save()
        return rep

    # ---- reap --------------------------------------------------------------
    def reap(self, task: Task, rep: TickReport) -> bool:
        run = self.runs.latest(task.id)
        # garden finish is the sole finaliser of manual runs.  Skip the task
        # while a manual run is active (status "running") or while finalize()
        # has completed the run record but has not yet written the task
        # transition (run.finished_at set, task still RUNNING).
        if run is not None and run.runner == "manual":
            return False
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
            note = gitops.push(worktree, branch, force=force, base=base)
            if note:
                self.log(f"{task.id}: {note}")
        except gitops.GitError as e:
            self._transition(task, Status.FAILED, f"push failed: {e}{cost}")
            rep.transitions.append(f"{task.id} -> failed (push)")
            return
        task.branch = branch
        self._after_push(task, run, worktree, branch, base, result, rep, cost)

    def _pre_pr_specs(self, task: Task) -> list[dict[str, Any]]:
        """The pre-PR checks for this product: the configured `checks.pre_pr`, or — when none
        are configured — the product's own `setup.test` and `setup.lint` commands. Either way
        `setup.env` is added so the checks run in the same prepared environment as the worker,
        so the default no longer reaches into any particular venv."""
        setup = self.cfg.product_setup(task.product)
        specs = list(self.cfg.get("checks.pre_pr", []) or [])
        if not specs:
            for name in ("test", "lint"):
                cmd = str(setup.get(name) or "").strip()
                if cmd:
                    specs.append({"name": name, "command": cmd})
        env = dict(setup.get("env") or {})
        if env:
            specs = [{**s, "env": {**env, **(s.get("env") or {})}} for s in specs]
        return specs

    def _pre_pr_checks(self, task: Task, worktree: Path, branch: str, base: str) -> list[dict[str, Any]]:
        specs = self._pre_pr_specs(task)
        if not specs or not worktree.exists():
            return []
        results = run_checks(specs, self.check_ctx(task, branch, base, worktree), cwd=worktree,
                             timeout=int(self.cfg.get("checks.timeout_seconds", 600)))
        for r in results:
            self.events.emit("check", task.id, stage="pre_pr", name=r.get("name"), status=r.get("status"), summary=r.get("summary", ""))
        return results

    def _after_push(self, task: Task, run: Run, worktree: Path, branch: str, base: str, result: dict[str, Any],
                    rep: TickReport, cost: str) -> None:
        """Stall bookkeeping, token-free pre-PR checks, then PR open/update, then a pending restack."""
        st = self.state.get(task.id)
        stalled = False
        diff_h: str | None = None
        body_h: str | None = None
        if bool(self.cfg.get("stall.enabled", True)) and worktree.exists():
            diff_h = gitops.diff_hash(worktree, base)
            body_h = hashlib.sha1(str(result.get("pr_body") or "").encode("utf-8", "replace")).hexdigest()[:16]
            if run.mode == "revise":
                diff_unchanged = bool(diff_h and diff_h == st.get("last_diff_hash"))
                body_unchanged = body_h == st.get("last_pr_body_hash", "")
                if diff_unchanged and body_unchanged:
                    stalled = True
        failed = check_failures(self._pre_pr_checks(task, worktree, branch, base))
        if failed and not stalled:
            st["pending_feedback"] = to_feedback(failed, "pre-PR check")
            names = ", ".join(str(f.get("name")) for f in failed)
            if task.pr:
                self._transition(task, Status.CHANGES_REQUESTED, f"pre-PR checks failed ({names}); revise run will fix before the PR is updated{cost}")
            else:
                self._transition(task, Status.CHANGES_REQUESTED, f"pre-PR checks failed ({names}); no PR opened yet; revise run will fix{cost}")
            rep.transitions.append(f"{task.id} -> changes_requested (checks)")
            return
        # Save hashes only after the round reaches the PR; failed pre-PR rounds are not recorded.
        if diff_h is not None:
            st["last_diff_hash"] = diff_h
        if body_h is not None:
            st["last_pr_body_hash"] = body_h
        self._open_or_update_pr(task, run, branch, base, result, rep, cost)
        if stalled:
            self._stall(task, rep, f"revise run {run.run_id} produced no change to the diff or PR description")
        if st.pop("restack_pending", False) and task.status in (Status.IN_REVIEW, Status.AWAITING_TRIAGE):
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
                st["pr_draft"] = bool(existing.is_draft)
                if run.mode in ("revise", "resume"):
                    try:
                        self.github.update_pr(slug, existing.number, title=title, body=body)
                        comment_body = mark_garden_comment(f"Pushed a revision round: {summary}", run.run_id)
                        self.github.comment(slug, existing.number, comment_body)
                    except GitHubError as e:
                        self.log(f"{task.id}: could not update PR: {e}")
                nxt = self._pr_status(task)
                self._transition(task, nxt, f"pushed revision to {existing.url}: {summary}{cost}")
                rep.transitions.append(f"{task.id} -> {nxt.value} (revised)")
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
                st["pr_draft"] = bool(self.cfg.get("github.draft_pr", True))
                self.events.emit("pr_opened", task.id, pr=pr.url, base=base, stacked_on=st.get("stack_parent", ""), draft=st["pr_draft"])
                nxt = self._pr_status(task)
                self._transition(task, nxt, f"opened {'draft ' if st['pr_draft'] else ''}{pr.url} (base {base}): {summary}{cost}")
                rep.transitions.append(f"{task.id} -> {nxt.value} ({pr.url})")
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
        action = f'garden triage {task.id} --changes "<feedback>" to unblock'
        if task.status != Status.CHANGES_REQUESTED:
            self._transition(task, Status.CHANGES_REQUESTED, f"stalled: {reason}; run `{action}`", needs_human=True)
        else:
            task.log(f"stalled: {reason}; run `{action}`")
            self.store.save(task)
            notify(self.cfg.data, task.id, "stalled", reason, task.pr or "")
        rep.transitions.append(f"{task.id} stalled")

    # ---- automated review --------------------------------------------------
    def _maybe_review(self, task: Task, work_run: Run, rep: TickReport) -> None:
        if not task.pr:
            return
        st = self.state.get(task.id)
        if bool(self.cfg.get("review.enabled", True)) and int(st.get("review_rounds", 0)) < int(self.cfg.get("review.max_rounds", 2)):
            try:
                run = self.dispatch_review(task, work_run)
                rep.dispatched.append(f"{task.id}(review)")
                self.log(f"{task.id}: review run {run.run_id} started")
            except Exception as e:  # noqa: BLE001
                task.log(f"automated review could not start: {e}")
                self.store.save(task)
                rep.errors.append(f"{task.id}: review dispatch failed: {e}")
        for name in list(self.cfg.get("review.personas", []) or []):
            try:
                self.dispatch_persona_pr(task, str(name))
                rep.dispatched.append(f"{task.id}(persona:{name})")
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{task.id}: persona {name} dispatch failed: {e}")

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
        review_difficulty = str(self.cfg.get("review.difficulty") or task.difficulty or "medium")
        if review_difficulty not in DIFFICULTIES:
            review_difficulty = "medium"
        run.difficulty = review_difficulty
        run.model = self.model_for(task, runner, review_difficulty)
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
                comment_body = mark_garden_comment(review_to_markdown(review), run.run_id)
                self.github.comment(slug, number, comment_body)
            except GitHubError as e:
                self.log(f"{task.id}: could not post review: {e}")
        # repeated blocking findings across rounds = the loop isn't converging
        keys = sorted({f"{f.get('file', '')}|{str(f.get('summary', '')).strip().lower()}"
                       for f in review.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "blocking"})
        repeated = sorted(set(keys) & set(st.get("last_findings", [])))
        st["last_findings"] = keys
        if verdict == "request_changes" and task.status in (Status.IN_REVIEW, Status.AWAITING_TRIAGE):
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
            return  # nothing new on GitHub since last look
        st["pr_updated_at"] = pr.updated_at
        since = task.last_dispatched_at
        fb = self.github.feedback_since(slug, number, since)
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
            return
        pending = fb.to_markdown() + ("\n\n" + ci_note if ci_note else "")
        st["pending_feedback"] = pending
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
            st["needs_human"] = f"{max_rev} revision rounds used"
            self.events.emit("needs_human", task.id, reason=st["needs_human"])
            self._transition(task, Status.CHANGES_REQUESTED, f"{note}, but {max_rev} revision rounds already used; needs a human", needs_human=True)
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
            + ". The runner will force-push the rebased branch."
        )
        st["force_push"] = True
        max_rev = int(self.cfg.get("max_revisions", 3))
        if int(st.get("revisions", 0)) >= max_rev:
            st["needs_human"] = f"PR conflicts with {base} and {max_rev} revision rounds already used"
            self.events.emit("needs_human", task.id, reason=st["needs_human"])
            self._transition(task, Status.CHANGES_REQUESTED,
                             f"PR conflicts with {base} ({', '.join(files) or 'unknown files'}); revision cap reached; needs a human",
                             needs_human=True)
        else:
            self._transition(task, Status.CHANGES_REQUESTED,
                             f"PR conflicts with {base} ({', '.join(files) or 'unknown files'}); revise run will rebase and resolve")
        rep.transitions.append(f"{task.id} -> changes_requested (conflict)")

    def _on_parent_closed(self, task: Task, rep: TickReport) -> None:
        for child in self.stacked_children(task):
            st = self.state.get(child.id)
            reason = f"stack parent {task.id} was closed without merging"
            st["needs_human"] = reason
            self.events.emit("needs_human", child.id, reason=reason)
            notify(self.cfg.data, child.id, "needs_human", reason, child.pr or "")
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
                 session_id: str = "", prompt_override: str = "", branch_override: str = "",
                 worktree_override: Path | None = None, model_override: str | None = None) -> Run:
        runner = runner or self.runner_for(task)
        branch = branch_override or task.branch or task.default_branch()
        st = self.state.get(task.id)
        stack = self._stack_for(task) if mode in ("work", "trial") else None
        base = self.base_for(task)
        feedback = str(st.get("pending_feedback") or "") if mode == "revise" else ""
        qa = list(st.get("qa") or [])
        commits_ahead = None
        wt_path = worktree_override or self.worktree_for(task)
        if wt_path.exists() and (feedback or session_id):
            try:
                commits_log = gitops.log_summary(wt_path, base, n=20)
                if commits_log.strip():
                    commits_ahead = commits_log.strip().split("\n")
            except gitops.GitError:
                pass
        brief = build_brief(self.store, task, branch=branch, base=base, review_feedback=feedback, stack=stack, qa=qa, commits_ahead=commits_ahead)
        text = prompt_override or brief.text
        run = self.runs.new_run(task.id, runner.name, mode=mode)
        run.branch, run.base, run.brief_tokens = branch, base, max(1, len(text) // 4)
        run.model = model_override if model_override is not None else self.model_for(task, runner)
        run.difficulty = task.difficulty
        run.harness = runner.harness.name if runner.harness else ""
        run.session_id = session_id
        if session_id and st.get("session_host"):
            run.host = str(st["session_host"])
        runner.assign(run, self.active_runs())
        wt: Path | None = None
        if worktree and not runner.remote:
            wt = gitops.prepare_worktree(self.repo_for(task), worktree_override or self.worktree_for(task), branch, base)
            run.worktree = str(wt)
        run.save()
        runner.start(run, wt or self.store.root, text)
        if not branch_override:
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

    # ---- auxiliary runs (compare, persona) ---------------------------------
    def _aux_list(self) -> list[dict[str, Any]]:
        return self.state.get("_aux").setdefault("runs", [])

    def dispatch_aux(self, kind: str, task: Task | None, brief_text: str, worktree: Path, meta: dict[str, Any],
                     harness_name: str = "", difficulty: str = "") -> Run:
        probe = task or Task(path=self.store.root, id=str(meta.get("id", "_aux")), title="", product=str(meta.get("product", "")), phase=str(meta.get("phase", "")))
        runner = self.runner_for(probe, "local", harness_name)
        run = self.runs.new_run(probe.id if task else f"_{kind}", runner.name, mode=kind)
        run.worktree = str(worktree)
        run.model = self.model_for(probe, runner, difficulty or "hard")
        run.difficulty = difficulty or "hard"
        run.brief_tokens = max(1, len(brief_text) // 4)
        run.save()
        runner.start(run, worktree, brief_text)
        self._aux_list().append({"run_id": run.run_id, "task": run.task_id, "kind": kind, **meta})
        self.events.emit("dispatch", run.task_id, run=run.run_id, mode=kind, model=run.model, harness=run.harness, **{k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))})
        self.state.save()
        return run

    def reap_aux(self, rep: TickReport) -> None:
        remaining = []
        for entry in list(self._aux_list()):
            run = next((r for r in self.runs.runs_for(entry["task"]) if r.run_id == entry["run_id"]), None)
            if run is None:
                continue
            runner = self.runner_for(self.store.tasks().get(entry["task"]) or Task(path=self.store.root, id=entry["task"], title=""), run.runner, run.harness)
            if not self._finished_or_timed_out(run, runner):
                remaining.append(entry)
                continue
            final = ""
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
                run.status = "done"
                run.save()
            self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode, cost_usd=run.cost_usd, usage=run.usage, status=run.status)
            try:
                if entry["kind"] == "compare":
                    self._finish_trial(entry, run, final, rep)
                elif entry["kind"] == "persona":
                    self._finish_persona(entry, run, final, rep)
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{entry['task']}: {entry['kind']} failed: {e}")
        self.state.get("_aux")["runs"] = remaining

    # ---- model trials ------------------------------------------------------
    def start_trial(self, task: Task, contenders: list[str]) -> list[Run]:
        if task.status not in (Status.READY, Status.DRAFT, Status.FAILED) or task.pr:
            raise RuntimeError(f"{task.id} must be ready/draft/failed without a PR to start a trial (is {task.status.value})")
        if len(contenders) < 2:
            raise RuntimeError("a trial needs at least two contenders")
        default_h = task.harness or self.cfg.product_harness(task.product)
        st = self.state.get(task.id)
        trial: dict[str, Any] = {"id": now_iso(), "status": "running", "contenders": []}
        runs: list[Run] = []
        base_branch = task.branch or task.default_branch()
        for spec in contenders:
            label, harness, model = parse_contender(spec, default_h)
            suffix = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
            branch = f"{base_branch}-trial-{suffix}"
            wt = self.cfg.worktree_path(f"{task.id}-trial-{suffix}")
            runner = self.runner_for(task, "local", harness)
            run = self.dispatch(task, mode="trial", runner=runner, branch_override=branch, worktree_override=wt, model_override=model or None)
            trial["contenders"].append({"label": label, "harness": harness, "model": model, "branch": branch, "worktree": str(wt),
                                        "run_id": run.run_id, "status": "running", "pr": "", "pr_number": 0, "cost": None, "score": None})
            runs.append(run)
        st["trial"] = trial
        task.branch = base_branch
        task.log(f"trial started with {', '.join(c['label'] for c in trial['contenders'])}")
        self.store.save(task)
        self.events.emit("trial_started", task.id, contenders=[c["label"] for c in trial["contenders"]])
        self.state.save()
        return runs

    def reap_trial(self, task: Task, rep: TickReport) -> bool:
        st = self.state.get(task.id)
        trial = st["trial"]
        if trial["status"] == "comparing":
            return False
        changed = False
        for c in trial["contenders"]:
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
        if not changed:
            return False
        if any(c["status"] == "running" for c in trial["contenders"]):
            return True
        with_pr = [c for c in trial["contenders"] if c["status"] == "pr"]
        base = self.base_for(task)
        if len(with_pr) >= 2:
            diffs = {c["label"]: gitops.diff(Path(c["worktree"]), base) for c in with_pr}
            text = compare_brief(self.store, task, with_pr, diffs, base, int(self.cfg.get("review.max_diff_chars", 60000)))
            trial["status"] = "comparing"
            self.dispatch_aux("compare", task, text, Path(with_pr[0]["worktree"]), {"trial_id": trial["id"]},
                              harness_name=str(self.cfg.get("review.harness") or ""), difficulty=str(self.cfg.get("review.difficulty") or "hard"))
            rep.dispatched.append(f"{task.id}(compare)")
            task.log("all contenders finished; comparison run started")
            self.store.save(task)
        elif len(with_pr) == 1:
            self._conclude_trial(task, {"winner": with_pr[0]["label"], "rationale": "only one contender produced a PR", "ranking": []}, rep)
        else:
            trial["status"] = "done"
            self._transition(task, Status.FAILED, "trial: no contender produced a PR")
            rep.transitions.append(f"{task.id} -> failed (trial)")
        return True

    def _finalize_contender(self, task: Task, c: dict[str, Any], run: Run, runner: Runner) -> None:
        run.exit_code = run.read_exit_code()
        run.finished_at = now_iso()
        collected = runner.collect(run) if run.status != "timeout" else {"result": {}, "error": "timed out"}
        run.result = collected.get("result") or {}
        run.usage = collected.get("usage") or {}
        run.cost_usd = collected.get("cost_usd")
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
            c["status"], c["note"] = "failed", (result.get("summary") or run.error or "no result")[:200]
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
            c["status"], c["note"] = "failed", str(e)[:200]
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

    def _conclude_trial(self, task: Task, verdict: dict[str, Any], rep: TickReport, compare_cost: float | None = None, run_id: str = "") -> None:
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
        trial["winner"] = winner["label"]
        trial["rationale"] = str(verdict.get("rationale") or "")
        trial["status"] = "done"
        trial["compare_cost"] = compare_cost
        record = {"task": task.id, "title": task.title, "difficulty": task.difficulty, "winner": winner["label"], "rationale": trial["rationale"],
                  "compare_cost": compare_cost,
                  "contenders": [{k: c.get(k) for k in ("label", "harness", "model", "status", "score", "cost", "input_tokens", "output_tokens", "pr", "summary", "note")} for c in trial["contenders"]]}
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
        task.branch = winner["branch"]
        task.pr = winner.get("pr", "")
        st["pr_number"] = winner.get("pr_number") or 0
        st["worktree"] = winner["worktree"]
        st["revisions"] = 0
        st["review_rounds"] = int(self.cfg.get("review.max_rounds", 2))  # the comparison stands in for the review pass
        self.events.emit("trial_done", task.id, winner=winner["label"],
                         scores={c["label"]: c.get("score") for c in trial["contenders"]})
        st["pr_draft"] = bool(self.cfg.get("github.draft_pr", True)) and bool(winner.get("pr"))
        self._transition(task, self._pr_status(task), f"trial won by {winner['label']} (scores: " +
                         ", ".join(f"{c['label']}={c.get('score') if c.get('score') is not None else '–'}" for c in trial["contenders"]) + f"): {task.pr or 'no PR'}")
        rep.transitions.append(f"{task.id} -> {task.status.value} (trial winner {winner['label']})")

    # ---- persona reviews ---------------------------------------------------
    def phase_prs(self, phase: Phase) -> list[dict[str, Any]]:
        rows = []
        for t in phase.tasks:
            if t.status in (Status.DRAFT, Status.READY, Status.CANCELLED) and not t.pr:
                continue
            body, title = "", t.title
            latest = self.runs.latest(t.id)
            if latest and latest.result:
                body = str(latest.result.get("pr_body") or "")
                title = str(latest.result.get("pr_title") or t.title)
            slug = self.slug_for(t)
            number = self._pr_number(t)
            if not body and slug and number and self.github.available:
                try:
                    info = self.github.get_pr(slug, number)
                    body, title = info.body, info.title or title
                except GitHubError:
                    pass
            rows.append({"id": t.id, "title": title, "status": t.status.value, "pr": t.pr, "body": body})
        return rows

    def dispatch_persona_phase(self, phase: Phase, name: str, file_tasks: bool = False) -> Run:
        valid_name(name)
        product = phase.product
        probe = Task(path=self.store.root, id=f"_{product}-{phase.name}", title="", product=product, phase=phase.name)
        repo = self.repo_for(probe)
        base = self.final_base_for(probe)
        wt = self.cfg.worktree_path(f"_phase-{product}-{phase.name}")
        gitops.fetch(repo)
        if wt.exists():
            gitops.git("checkout", "-q", "--detach", gitops.base_ref(wt, base), cwd=wt)
        else:
            wt.parent.mkdir(parents=True, exist_ok=True)
            gitops.git("worktree", "add", "--detach", str(wt), gitops.base_ref(repo, base), cwd=repo)
        text = phase_brief(self.store, phase, name, base, self.phase_prs(phase))
        return self.dispatch_aux("persona", None, text, wt, {"id": probe.id, "product": product, "phase": phase.name,
                                                             "persona": name, "target": "phase", "file_tasks": file_tasks},
                                 harness_name=str(self.cfg.get("review.harness") or ""), difficulty=str(self.cfg.get("review.difficulty") or "hard"))

    def dispatch_persona_pr(self, task: Task, name: str, request_changes: bool = False) -> Run:
        valid_name(name)
        if not task.pr and not task.branch:
            raise RuntimeError(f"{task.id} has no branch to review")
        base = self.base_for(task)
        branch = task.branch or task.default_branch()
        wt = gitops.prepare_worktree(self.repo_for(task), self.worktree_for(task), branch, base)
        diff = gitops.diff(wt, base)
        pr_title, pr_body = task.title, ""
        latest = self.runs.latest(task.id)
        if latest and latest.result:
            pr_title = str(latest.result.get("pr_title") or task.title)
            pr_body = str(latest.result.get("pr_body") or "")
        text = pr_brief(self.store, task, name, branch, base, pr_title, pr_body, diff, int(self.cfg.get("review.max_diff_chars", 60000)))
        return self.dispatch_aux("persona", task, text, wt, {"persona": name, "target": "pr", "request_changes": request_changes},
                                 harness_name=str(self.cfg.get("review.harness") or ""), difficulty=str(self.cfg.get("review.difficulty") or ""))

    def _finish_persona(self, entry: dict[str, Any], run: Run, final: str, rep: TickReport) -> None:
        rev = parse_persona(final)
        name = str(entry.get("persona"))
        if not rev:
            self.events.emit("persona", entry["task"], persona=name, status="no_verdict", target=entry.get("target"))
            rep.errors.append(f"persona {name}: no verdict ({run.error[:100] or 'see final.md'})")
            return
        self.events.emit("persona", entry["task"], persona=name, target=entry.get("target"), score=rev.get("score"),
                         high=sum(1 for f in rev.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "high"))
        if entry.get("target") == "phase":
            phase = self.store.phase(str(entry["product"]), str(entry["phase"]))
            path = report_path(phase, name)
            path.write_text(report_markdown(rev, f"{name} review of {phase.key}", run.run_id))
            self.log(f"persona {name}: report written to {self.store.rel(path)}")
            rep.transitions.append(f"persona {name} report -> {self.store.rel(path)}")
            if entry.get("file_tasks"):
                for f in rev.get("findings") or []:
                    if isinstance(f, dict) and f.get("severity") == "high" and f.get("summary"):
                        t = self.store.create_task(phase.product, phase.name, str(f["summary"])[:80],
                                                   f"## Goal\n\n{f.get('suggestion') or f['summary']}\n\n## Context\n\nRaised by the {name} persona review ({self.store.rel(path)}), area: {f.get('area', '')}.\n",
                                                   priority=2, status="draft")
                        t.discovered_from = f"persona:{name}"
                        self.store.save(t)
                        self.events.emit("discovered", entry["task"], new_task=t.id, title=t.title, blocking=False, status="draft", persona=name)
                self.store.invalidate()
            return
        task = self.store.task(entry["task"])
        md = report_markdown(rev, f"{name} review of {task.id}", run.run_id)
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available:
            try:
                comment_body = mark_garden_comment(md, run.run_id)
                self.github.comment(slug, number, comment_body)
            except GitHubError as e:
                self.log(f"{task.id}: could not post persona review: {e}")
        (run.path / "report.md").write_text(md)
        task.log(f"persona {name} review: score {rev.get('score', '–')}/10, {len(rev.get('findings') or [])} finding(s)")
        self.store.save(task)
        rep.transitions.append(f"{task.id} persona {name}: {rev.get('score', '–')}/10")
        highs = [f for f in rev.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "high"]
        if entry.get("request_changes") and highs and task.status in (Status.IN_REVIEW, Status.AWAITING_TRIAGE) and bool(self.cfg.get("auto_revise", True)):
            st = self.state.get(task.id)
            st["pending_feedback"] = "\n".join(f"- **{name} persona** ({f.get('area', '')}): {f.get('summary', '')} — {f.get('suggestion', '')}" for f in highs)
            self._transition(task, Status.CHANGES_REQUESTED, f"{name} persona review raised {len(highs)} high finding(s)")
            rep.transitions.append(f"{task.id} -> changes_requested (persona {name})")

    # ---- triage: the human's first look at a draft PR ----------------------
    def triage(self, task: Task, ready: bool = False, changes: str = "", note: str = "") -> None:
        """Record the human's initial review of a draft PR: mark it ready for review, or send
        it back with feedback (a revise run follows)."""
        if not task.pr:
            raise RuntimeError(f"{task.id} has no PR to triage")
        st = self.state.get(task.id)
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if changes:
            st["pending_feedback"] = f"- **triage** (human): {changes.strip()}"
            st.pop("needs_human", None)
            self.events.emit("triaged", task.id, pr=task.pr, by="human", decision="changes", note=changes[:200])
            self._transition(task, Status.CHANGES_REQUESTED, f"triage: changes requested by hand: {changes[:120]}")
            self.state.save()
            return
        if ready:
            if st.get("pr_draft") and slug and number and self.github.available:
                try:
                    self.github.mark_ready(slug, number)
                except GitHubError as e:
                    self.log(f"{task.id}: could not mark PR ready on GitHub: {e}")
            st["pr_draft"] = False
            st.pop("needs_human", None)
            self.events.emit("triaged", task.id, pr=task.pr, by="human", decision="ready", note=note[:200])
            self._transition(task, Status.IN_REVIEW, "triage: marked ready for review" + (f" ({note[:100]})" if note else ""))
            self.state.save()
            return
        raise RuntimeError("triage needs --ready or --changes")

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
        if task.pr and task.status in (Status.CHANGES_REQUESTED, Status.IN_REVIEW, Status.AWAITING_TRIAGE, Status.FAILED):
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
