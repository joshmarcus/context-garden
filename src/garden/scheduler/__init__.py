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

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import gitops
from ..events import EventLog
from ..github import GitHub
from ..harness import DIFFICULTIES
from ..model import Status, Task
from ..notify import notify, should_notify
from ..runner import get_runner
from ..runner.base import Runner
from ..runs import Run, RunStore
from ..store import Store
from ..trials import TrialLog
from .aux import AuxMixin
from .budget import BudgetMixin
from .discovered import DiscoveredMixin
from .dispatch import DispatchMixin
from .edits import EditsMixin
from .fence import FenceMixin
from .human import HumanMixin
from .persona import PersonaMixin
from .poll import PollMixin
from .reap import ReapMixin
from .rebase import RebaseMixin
from .report import TickReport
from .retro import RetroMixin
from .review import ReviewMixin
from .state import State, _TaskState
from .trials import TrialsMixin
from .upgrades import UpgradeMixin

__all__ = ["REVIEW_MODES", "WORKER_MODES", "Scheduler", "State", "TickReport", "_TaskState"]

WORKER_MODES = frozenset({"work", "revise", "resume", "trial", "rebase"})  # count against max_parallel
REVIEW_MODES = frozenset({"review", "persona", "compare"})       # count against review_parallel


class Scheduler(
    BudgetMixin,
    ReapMixin,
    RebaseMixin,
    FenceMixin,
    DiscoveredMixin,
    ReviewMixin,
    EditsMixin,
    PollMixin,
    UpgradeMixin,
    DispatchMixin,
    HumanMixin,
    AuxMixin,
    TrialsMixin,
    PersonaMixin,
    RetroMixin,
):
    """One class, assembled from a module per tick phase (see the package layout in
    docs/architecture.md). This file holds construction, the shared helpers, `tick()` and
    `_transition()`; everything else lives in the mixin whose phase it belongs to, so two
    features in different parts of the loop edit different files."""

    def __init__(
        self,
        store: Store,
        github: GitHub | None = None,
        runner_factory: Callable[[str, Task], Runner] | None = None,
        log: Callable[[str], None] | None = None,
        upgrader: Any | None = None,
        restarter: Callable[[], None] | None = None,
    ):
        self.store = store
        self.cfg = store.config
        self.runs = RunStore(self.cfg.garden_dir)
        self.state = State(self.cfg.garden_dir / "state.json")
        self.events = EventLog(self.cfg.garden_dir / "events.jsonl")
        self.trials = TrialLog(self.cfg.garden_dir / "trials.jsonl")
        notice_patterns = self.cfg.get("github.bot_notice_patterns")
        # PR feedback becomes a worker prompt only from trusted authors: the login the garden
        # uses, `github.trusted_authors`, and the reviewers it requests on every PR.
        trusted = [*(self.cfg.get("github.trusted_authors") or []), *(self.cfg.get("github.reviewers") or [])]
        self.github = github if github is not None else GitHub(
            use_gh=bool(self.cfg.get("github.use_gh", True)),
            bot_logins=[str(b) for b in (self.cfg.get("github.bot_logins") or [])],
            bot_notice_patterns=[str(p) for p in notice_patterns] if notice_patterns is not None else None,
            trusted_authors=[str(a) for a in trusted],
        )
        self._runner_factory = runner_factory
        self.log = log or (lambda msg: None)
        if upgrader is None:
            from ..upgrade import Upgrader

            upgrader = Upgrader(package=self.cfg.upgrade_package(), pip=self.cfg.upgrade_pip(), exec_root=self.store.root)
        self.upgrader = upgrader
        if restarter is None:
            from ..upgrade import default_restart

            restarter = default_restart
        self._restarter = restarter

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

    def worker_runs_active(self) -> list[Run]:
        """Active runs that occupy a `max_parallel` slot: work, revise, resume, trial."""
        return [r for r in self.active_runs() if r.mode in WORKER_MODES]

    def review_runs_active(self) -> list[Run]:
        """Active runs that occupy a `review_parallel` slot: review, persona, comparison."""
        return [r for r in self.active_runs() if r.mode in REVIEW_MODES]

    def slots_free(self) -> int:
        return max(0, self.effective_max_parallel() - len(self.worker_runs_active()))

    def review_parallel_limit(self) -> int:
        limit = self.cfg.get("review_parallel")
        return int(limit) if limit not in (None, "") else self.effective_max_parallel()

    def review_slots_free(self) -> int:
        return max(0, self.review_parallel_limit() - len(self.review_runs_active()))

    @staticmethod
    def _is_unreaped(task: Task, run: Run | None) -> bool:
        """A run whose record reached a terminal status while its task is still RUNNING:
        an earlier tick wrote the run's final status but was killed before the task
        transition / push / PR step ran. `reap()` resumes these instead of treating them
        as abandoned; `garden runs` labels them "finished, not yet reaped" until then.
        `finished_at` is only ever set by our own finalize()/timeout code, so its presence
        distinguishes a genuinely interrupted reap from a run whose status was flipped out
        from under us by something else (e.g. a stale record with no real completion)."""
        return (run is not None and run.runner != "manual" and run.mode != "review"
                and run.status != "running" and bool(run.finished_at)
                and task.status == Status.RUNNING)

    def unreaped_run_ids(self) -> set[str]:
        out: set[str] = set()
        for t in self.store.tasks().values():
            run = self.runs.latest(t.id)
            if self._is_unreaped(t, run):
                out.add(run.run_id)
        return out

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

    # ---- tick --------------------------------------------------------------
    def tick(self, dispatch: bool | None = None) -> TickReport:
        rep = TickReport()
        self.store.invalidate()
        self.state = State(self.state.path)  # the CLI, web UI or TUI may have written state since the last pass
        try:
            self.reap_aux(rep)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"aux reap failed: {e}")
        try:
            self.reap_retro(rep)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"retro reap failed: {e}")
        tasks = self.store.tasks()
        for t in list(tasks.values()):
            try:
                if self.state.get(t.id).get("edit_run") and self.reap_edit(t, rep):
                    rep.reaped.append(t.id)
                    continue
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
        try:
            self.reap_orphaned(rep)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"orphan reap failed: {e}")
        self.store.invalidate()
        tasks = self.store.tasks()
        for t in list(tasks.values()):
            if t.pr and t.status.pr_pending:
                try:
                    self.poll(t, rep)
                    rep.polled.append(t.id)
                except Exception as e:  # noqa: BLE001
                    rep.errors.append(f"{t.id}: poll failed: {e}")
                    self.log(f"{t.id}: poll failed: {e}")
        try:
            self._run_merge_queue(rep)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"merge queue failed: {e}")
            self.log(f"merge queue failed: {e}")
        if dispatch is None:
            dispatch = bool(self.cfg.get("auto_dispatch", True))
        if self.is_dispatch_paused():
            dispatch = False
        if dispatch:
            self.dispatch_edits(rep)
            self.dispatch_ready(rep)
        try:
            self._audit_stuck(rep)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"stuck audit failed: {e}")
        self.state.save()
        self.maybe_auto_upgrade(rep)
        return rep
