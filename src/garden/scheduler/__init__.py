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
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .. import gitops
from ..events import EventLog
from ..github import GitHub, GitHubRouter, RepositorySlug, is_git_remote_url, repo_slug_from_remote
from ..harness import DIFFICULTIES
from ..model import Status, Task, now_iso
from ..notify import notify, should_notify
from ..runner import get_runner
from ..runner.base import Runner
from ..runs import Run, RunStore
from ..store import Store
from ..trials import TrialLog
from .aux import AuxMixin
from .browser import BrowserMixin
from .budget import BudgetMixin
from .checkruns import CheckRunMixin
from .cleanup import CleanupMixin
from .discovered import DiscoveredMixin
from .dispatch import DispatchMixin
from .edits import EditsMixin
from .fence import FenceMixin
from .human import HumanMixin
from .kickoff import KickoffMixin
from .persona import PersonaMixin
from .poll import PollMixin
from .queue import QueueMixin
from .quota import QuotaMixin
from .reap import ReapMixin
from .rebase import RebaseMixin
from .report import TickReport
from .resources import ResourceMixin
from .retro import RetroMixin
from .review import ReviewMixin
from .scope import ScopeMixin
from .state import State, _TaskState
from .trials import TrialsMixin
from .upgrades import UpgradeMixin

_TICK_LOCKS: dict[str, threading.Lock] = {}
_TICK_LOCKS_GUARD = threading.Lock()


@contextmanager
def _tick_thread_lock(path: Path) -> Iterator[None]:
    """Also serialise threads: flock alone is process-scoped on some platforms."""
    key = str(path.resolve())
    with _TICK_LOCKS_GUARD:
        lock = _TICK_LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield

__all__ = ["REVIEW_MODES", "WORKER_MODES", "Scheduler", "State", "TickReport", "_TaskState"]

WORKER_MODES = frozenset({"work", "revise", "resume", "trial", "rebase", "investigation"})  # count against max_parallel
REVIEW_MODES = frozenset({"review", "persona", "compare"})       # count against review_parallel
CHECK_MODES = frozenset({"check"})  # detached pre-PR/base-probe/pre-merge checks; no worker slot


class Scheduler(
    BudgetMixin,
    BrowserMixin,
    ResourceMixin,
    ReapMixin,
    CheckRunMixin,
    CleanupMixin,
    QueueMixin,
    RebaseMixin,
    FenceMixin,
    DiscoveredMixin,
    ReviewMixin,
    EditsMixin,
    PollMixin,
    UpgradeMixin,
    ScopeMixin,
    DispatchMixin,
    HumanMixin,
    AuxMixin,
    QuotaMixin,
    TrialsMixin,
    PersonaMixin,
    RetroMixin,
    KickoffMixin,
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
        read_only: bool = False,
    ):
        self.store = store
        self.cfg = store.config
        self.runs = RunStore(self.cfg.garden_dir)
        self.state = State(self.cfg.garden_dir / "state.json")
        self.events = EventLog(self.cfg.garden_dir / "events.jsonl")
        self.trials = TrialLog(self.cfg.garden_dir / "trials.jsonl")
        self.log = log or (lambda msg: None)
        if not read_only:
            self._migrate_fence_bookkeeping()
            # A new CLI process has no old Store instance to compare against.  Check active
            # dispatch manifests before constructing anything that could use garden.yaml.
            self._hold_startup_config_against_fences()
        notice_patterns = self.cfg.get("github.bot_notice_patterns")
        # PR feedback becomes a worker prompt only from trusted authors: the login the garden
        # uses, `github.trusted_authors`, and the reviewers it requests on every PR.
        trusted = [*(self.cfg.get("github.trusted_authors") or []), *(self.cfg.get("github.reviewers") or [])]
        if github is not None:
            self.github = github
        else:
            common = {
                "use_gh": bool(self.cfg.get("github.use_gh", True)),
                "bot_logins": [str(b) for b in (self.cfg.get("github.bot_logins") or [])],
                "bot_notice_patterns": [str(p) for p in notice_patterns] if notice_patterns is not None else None,
                "trusted_authors": [str(a) for a in trusted],
                "trusted_bots": [str(b) for b in (self.cfg.get("github.trusted_bots") or [])],
            }
            default = GitHub(**common)
            routes = {}
            for product in (self.cfg.data.get("products") or {}):
                route = self.cfg.product_github(str(product))
                if isinstance(self.cfg.product(str(product)).get("github"), dict):
                    routes[(route["host"], route["slug"])] = GitHub(
                        **common, host=route["host"], api_base=route.get("api_base", ""),
                        token_env=route.get("token_env", ""),
                    )
            self.github = GitHubRouter(default, routes)
        self._runner_factory = runner_factory
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

    def external_stack_owner(self, task: Task) -> bool:
        """Whether another tool, rather than garden, owns this product's stack history."""
        return self.cfg.product_stack_owner(task.product) == "external"

    def stack_enabled_for(self, task: Task) -> bool:
        return self.stack_enabled and not self.external_stack_owner(task)

    def runner_for(self, task: Task, name: str = "", harness_name: str = "") -> Runner:
        name = name or task.runner or self.cfg.product_runner(task.product)
        if name == "claude-local":
            name = "local"
        if self._runner_factory:
            return self._runner_factory(name, task)
        harness = self.cfg.harness(harness_name or task.harness or self.cfg.product_harness(task.product))
        if name in {"ssh", "remote"}:
            cfg = dict(self.cfg.get("ssh" if name == "ssh" else "workers", {}) or {})
            cfg["_product"] = task.product
        else:
            cfg = {}
        cfg["timeout_minutes"] = self.cfg.product_timeout_minutes(task.product)
        cfg["work_dir"] = str(self.cfg.work_dir)
        cfg["setup"] = self.cfg.product_setup(task.product)  # how this product prepares its env
        cfg["checkout"] = self.cfg.product_checkout(task.product)
        cfg["worker_env"] = dict(self.cfg.get("worker_env") or {})  # what of the scheduler's env it keeps
        cfg["resources"] = dict(self.cfg.get("resources") or {})  # supervisor lease and cgroup boundary
        # A private class may be selected only from operator configuration.  Preserve the
        # entire registration map so its configured alias continues to resolve at reap.
        cfg["_runner_adapters"] = dict(self.cfg.get("runner_adapters") or {})
        return get_runner(name, cfg, harness)

    def resolved_harness_name(self, task: Task, harness_name: str = "") -> str:
        """The harness `runner_for(task, ..., harness_name)` would resolve to, without
        building a runner: an explicit name wins, else the task's own, else the product's
        default. Used to check `is_harness_paused` before a dispatch that does not need a
        runner yet (a review/persona batch still queued)."""
        return harness_name or task.harness or self.cfg.product_harness(task.product)

    def pool_members(self, tier: str, review: bool = False) -> list[dict[str, Any]]:
        """Normalise a configured tier/review pool, leaving string tier maps to model_for."""
        raw: Any = self.cfg.get("review.pool") if review else self.effective("models")
        if review:
            entries = raw if isinstance(raw, list) else []
        else:
            entries = (raw or {}).get(tier) if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return []
        members: list[dict[str, Any]] = []
        for entry in entries:
            if review and isinstance(entry, str):
                harness, _, model = entry.partition(":")
                entry = {"harness": harness, "model": model}
            if not isinstance(entry, dict) or not entry.get("harness"):
                continue
            harness, model = str(entry["harness"]), str(entry.get("model") or "")
            weight = max(1, int(entry.get("weight") or 1))
            members.append({"harness": harness, "model": model, "weight": weight,
                            "label": f"{harness}:{model}"})
        return members

    def select_pool_member(self, task: Task, tier: str, review: bool = False,
                           *, advance: bool = True) -> dict[str, Any] | None:
        """Choose an available pool member, preserving work pins and quota-aware weighting.

        A review pool is deliberately independent of the task's worker route: a task pinned
        to one worker account must still be reviewable by every configured reviewer account.
        Callers doing admission before launch may peek with ``advance=False`` and advance
        only after launch succeeds.
        """
        members = self.pool_members(tier, review=review)
        if not members:
            return None
        if not review and task.harness:
            members = [m for m in members if m["harness"] == task.harness]
        if not review and task.model:
            members = [m for m in members if m["model"] == task.model]
        if not members:
            # A pin is allowed to name a model outside the pool; it remains an explicit route.
            if not review and (task.harness or task.model):
                return {"harness": task.harness or self.cfg.product_harness(task.product),
                        "model": task.model, "weight": 1,
                        "label": f"{task.harness or self.cfg.product_harness(task.product)}:{task.model}"}
            return None
        available = [m for m in members if not self.is_harness_paused(m["harness"])]
        if not available:
            return None
        policy = str(self.cfg.get("dispatch.spread") or "quota_aware")
        if policy == "round_robin":
            slots = available
        elif policy == "quota_aware":
            limited = [m for m in available if self.pool_member_near_quota(m)]
            # Keep ordinary weighted rotation byte-for-byte stable when nobody is near a
            # limit. Once there is a limited member, double healthy slots and retain limited
            # slots at their ordinary weight: this represents a real half even for weight-one
            # members, which cannot be halved in an integer slot list.
            slots = [m for m in available for _ in range(m["weight"] * (1 if limited and m in limited else (2 if limited else 1)))]
        else:
            slots = [m for m in available for _ in range(m["weight"])]
        key = f"{'review' if review else 'work'}:{tier}:" + ",".join(m["label"] for m in members)
        rotation = self.state.get("_pool_rotation")
        index = int(rotation.get(key, 0)) % len(slots)
        if advance:
            rotation[key] = index + 1
            self.state.save()
        return dict(slots[index])

    def model_for(self, task: Task, runner: Runner, difficulty: str = "") -> str:
        if runner.harness is None:
            return ""
        d = difficulty or task.difficulty
        if d not in DIFFICULTIES:
            d = "medium"
        override = task.model if not difficulty else ""
        if override:
            return override
        # a more specific live override on this exact harness/tier still wins over the stop
        key = f"harnesses.{runner.harness.name}.models.{d}"
        ov = self.overrides()
        if key in ov:
            return str(ov[key])
        models = self.operating_profile().get("models") or {}
        if isinstance(models.get(d), str) and models.get(d):
            return str(models[d])
        # Before pools, ``models.<tier>`` was the garden-wide tier override.  Keep that
        # scalar form working; a list is a pool and has already been selected by dispatch.
        models = self.effective("models") or {}
        if isinstance(models, dict) and isinstance(models.get(d), str) and models.get(d):
            return str(models[d])
        return runner.harness.model_for(d)

    def retro_model_for(self, runner: Runner) -> str:
        """The model that names the judge for a retro reconciliation, a persona review or a
        trial comparison, independent of the tier map that prices work: this harness's
        `harnesses.<h>.retro_model`, or — when this harness is the garden's own default
        (`harness:`) — the top-level `retro.model`. Empty means neither is set, so the
        caller falls back to `model_for`'s tier resolution (retro.difficulty)."""
        if runner.harness is None:
            return ""
        override = str(runner.harness.cfg.get("retro_model") or "")
        if override:
            return override
        if runner.harness.name == str(self.cfg.get("harness") or "claude"):
            return str(self.cfg.get("retro.model") or "")
        return ""

    def git_identity(self) -> tuple[str, str]:
        """The identity written into a fresh product clone (see CG-147): `git.user_name` /
        `git.user_email` in garden.yaml, else the garden checkout's own git config, else the
        authenticated `gh` login with a GitHub noreply email. Either half may still come back
        blank if none of those resolve; `doctor` is what catches that, not this method."""
        name = str(self.cfg.get("git.user_name") or "")
        email = str(self.cfg.get("git.user_email") or "")
        if not name or not email:
            own_name, own_email = gitops.identity(self.store.root)
            name = name or own_name
            email = email or own_email
        if (not name or not email) and self.github.available:
            login = self.github.me()
            if login:
                name = name or login
                email = email or f"{login}@users.noreply.github.com"
        return name, email

    def repo_for(self, task: Task) -> Path:
        repo = task.repo or self.cfg.product_repo(task.product)
        if isinstance(repo, str) and is_git_remote_url(repo):
            git_name, git_email = self.git_identity()
            return gitops.ensure_repo(repo, self.cfg.repos_dir, git_name, git_email)
        # An existing path is never cloned, so ensure_repo cannot use an identity here.
        # Avoid two process launches on every scheduler lookup just to discard the result.
        return gitops.ensure_repo(Path(repo), self.cfg.repos_dir)

    def worktree_for(self, task: Task) -> Path:
        from ..canonical import configured_root

        canonical = configured_root(self.cfg.product_checkout(task.product), self.store.root)
        if canonical is not None:
            return canonical
        override = self.state.get(task.id).get("worktree")
        if override:
            return Path(override)
        return self.cfg.worktree_path(task.id)

    def prepare_canonical_run(self, task: Task, run: Run, runner: Runner, branch: str, base: str) -> Path | None:
        """Claim and reconcile an in-place checkout for any execution mode."""
        from ..canonical import claim, configured_root, preflight, reconcile
        from ..runner.base import scrubbed_env

        checkout = self.cfg.product_checkout(task.product)
        if runner.remote and str(checkout.get("strategy") or "worktree") == "in_place":
            # A process that exited is still an owner until its result has passed collection
            # and fence/preservation processing.  ``active`` covers the former interval;
            # ``unreaped`` covers a restart after finalize's terminal save but before the task
            # transition completed.  The next remote claim may recover the durable lease only
            # after neither source names its owner.
            if runner.name == "ssh" and not run.host:
                runner.assign(run, [item for item in self.active_runs() if item.run_id != run.run_id])
                run.save()
            protected_runs = {item.run_id: item for item in self.runs.active()}
            unreaped_ids = self.unreaped_run_ids()
            protected_runs.update(
                (item.run_id, item) for item in self.runs.all_runs()
                if item.run_id in unreaped_ids
            )
            identity = runner.canonical_checkout_identity(run)
            if identity is not None:
                run.env_snapshot["canonical_checkout_identity"] = identity
                run.save()
            run.env_snapshot["canonical_active_run_ids"] = sorted(
                item.run_id for item in protected_runs.values()
                if item.run_id != run.run_id
                and (identity is None
                     or item.env_snapshot.get("canonical_checkout_identity") == identity)
            )
            run.save()
            return None
        root = configured_root(checkout, self.store.root) if not runner.remote else None
        if root is None:
            return None
        # Publish the concrete checkout identity before consulting the durable run store.
        # Another scheduler may be preparing a run concurrently; without this save it sees
        # an active owner with no path, mistakes the lease for stale, and reclaims it.
        run.worktree = str(root)
        run.save()
        active_ids = {
            item.run_id for item in self.runs.active()
            if item.run_id != run.run_id and item.worktree
            and Path(item.worktree).resolve() == root.resolve()
        }
        try:
            claim(root, run.run_id, active_ids)
            preflight(root, branch, base)
            reconcile(root, checkout, scrubbed_env(runner.config, self.cfg.product_setup(task.product), worktree=root),
                      run.path / "reconcile.log")
            preflight(root, branch, base)
        except Exception:
            from ..canonical import release

            release(root, run.run_id)
            run.status = "failed"
            run.finished_at = now_iso()
            run.save()
            raise
        return root

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
        route = self.cfg.product_github(task.product)
        if route:
            configured = self.cfg.product(task.product).get("github")
            if isinstance(configured, dict):
                remote = gitops.remote_url(self.repo_for(task))
                if remote and is_git_remote_url(remote):
                    actual = repo_slug_from_remote(remote, route["host"])
                    if actual is None or actual.casefold() != route["slug"].casefold():
                        raise gitops.GitError(
                            f"product {task.product} remote does not match configured GitHub host and repository"
                        )
            return RepositorySlug(route["slug"], route["host"])
        return gitops.slug(self.repo_for(task))

    def active_runs(self) -> list[Run]:
        return [r for r in self.runs.active() if r.runner != "manual"]

    def worker_runs_active(self) -> list[Run]:
        """Active runs that occupy a `max_parallel` slot: worker modes only."""
        return [r for r in self.active_runs() if r.mode in WORKER_MODES]

    def worker_run_in_flight(self, task_id: str) -> bool:
        """Whether a worker-mode run (work/revise/resume/trial/rebase) is active for this task —
        the branch is being written to right now. `_handle_pr_conflict` (the conflict rebase)
        checks this before touching a branch, the same way CG-182 fences a check run, so a
        mechanical rebase never races a worker writing the same branch (CG-220). The merge
        queue's pre-merge rebase and the stale-base probe already refuse a task with any run in
        flight — `_automerge_gate`'s "a run is in flight" gate and the base-broken stop being
        cleared by `dispatch()` before any run starts — so they do not need this helper too."""
        return any(r.task_id == task_id and r.mode in WORKER_MODES for r in self.active_runs())

    def latest_worker_run(self, task_id: str) -> Run | None:
        """The run the ordinary `reap()` should reap for a RUNNING task: the latest run whose
        mode occupies a `max_parallel` slot (work/revise/resume/trial/rebase), preferring one
        still running. A review or persona run dispatched for the same task — e.g. the poll
        re-reviewing a fresh push while a revise is still in flight — is left to
        `reap_review`/`reap_orphaned` and never mistaken for the task's own run, so a review
        record can no longer send a running task back to `ready` (CG-177)."""
        worker = [r for r in self.runs.runs_for(task_id) if r.mode in WORKER_MODES]
        if not worker:
            return None
        active = [r for r in worker if r.status == "running"]
        return active[-1] if active else worker[-1]

    def review_runs_active(self) -> list[Run]:
        """Active runs that occupy a `review_parallel` slot: review, persona, comparison."""
        return [r for r in self.active_runs() if r.mode in REVIEW_MODES]

    def check_runs_active(self) -> list[Run]:
        """Active check runs (pre-PR, base probe, pre-merge).

        Checks are deliberately visible as runs but do not occupy worker slots: they are
        short, machine-bound work and have no worker-mode concurrency cap.
        """
        return [r for r in self.active_runs() if r.mode in CHECK_MODES]

    def slots_free(self) -> int:
        """Worker slots available to dispatch; checks and edit runs are excluded."""
        return max(0, self.effective_max_parallel() - len(self.worker_runs_active()))

    def review_parallel_limit(self) -> int:
        limit = self.effective("review_parallel")
        return int(limit) if limit not in (None, "") else self.effective_max_parallel()

    def review_slots_free(self) -> int:
        """Free slots in the global review/persona/comparison pool.

        Physical capacity is backend-specific and is checked by each launch path.  Folding
        local capacity into this global count prevents remote reviews from reaching their
        independently admitted execution queue.
        """
        return max(0, self.review_parallel_limit() - len(self.review_runs_active()))

    @staticmethod
    def _is_unreaped(task: Task, run: Run | None) -> bool:
        """A run whose finalize started (`finished_at` set) but did not complete before its task
        left RUNNING: an earlier tick collected the run's outcome but was killed during the fence
        check, push, PR or task transition that follows. `reap()` resumes these instead of
        treating them as abandoned; `garden runs` labels them "finished, not yet reaped" until
        then. `finished_at` is only ever set by our own finalize()/timeout code, so its presence
        — whatever the record's status — distinguishes a genuinely interrupted reap from a live
        run (finished_at still empty) or one whose status was flipped out from under us."""
        resumable_manual = bool(
            run is not None and run.runner == "manual" and run.completion_mode == "pushed"
            and (run.env_snapshot or {}).get("pushed_completion_submitted")
        )
        return (run is not None and (run.runner != "manual" or resumable_manual)
                and run.mode != "review" and bool(run.finished_at)
                and task.status == Status.RUNNING)

    def unreaped_run_ids(self) -> set[str]:
        out: set[str] = set()
        for t in self.store.tasks().values():
            if self.state.get(t.id).get("check_run"):
                continue  # its worker run is done and the check run owns the continuation (CG-182)
            run = self.latest_worker_run(t.id)
            if self._is_unreaped(t, run):
                out.add(run.run_id)
            review_id = str(self.state.get(t.id).get("review_run") or "")
            review = next((r for r in self.runs.runs_for(t.id) if r.run_id == review_id), None)
            if review is not None and review.status == "running" and review.process_finished():
                out.add(review.run_id)
        return out

    def _transition(self, task: Task, status: Status, note: str, needs_human: bool = False,
                    notify_now: bool = True, base_merged: bool | None = None) -> None:
        old = task.status.value
        task.status = status
        task.log(note)
        self.store.save(task)
        st = self.state.get(task.id)
        changed = False
        if status.terminal:
            # A task that reached a terminal status (done, cancelled, wont_do) is finished;
            # any stop recorded while it was still active (a review-cap card, feedback
            # waiting for a revise run, an automerge hold) must not linger and be counted as
            # a decision on the Inbox, the Board or the task page.
            for k in ("needs_human", "pending_feedback"):
                changed = st.pop(k, None) is not None or changed
            changed = self._retire_terminal_review_recovery(task) or changed
            changed = self._retire_terminal_check(task) or changed
            changed = self._queue_leave(task) or changed
        elif status != Status.IN_REVIEW:
            # A task that left in_review is no longer the merge queue's head.
            changed = self._queue_drop_head(task) or changed
        if changed:
            self.state.save()
        transition = {"from": old, "to": status.value, "note": note}
        if status == Status.DONE and base_merged is not None:
            transition["base_merged"] = base_merged
        self.events.emit("transition", task.id, **transition)
        self.log(f"{task.id}: {old} -> {status.value} ({note})")
        if notify_now and should_notify(status.value, needs_human=needs_human):
            notify(self.cfg.data, task.id, status.value, note, task.pr or "")

    def _pr_status(self, task: Task) -> Status:
        """Where an open PR sits: awaiting a human's triage while it is a draft, else in review."""
        return Status.AWAITING_TRIAGE if self.state.get(task.id).get("pr_draft") else Status.IN_REVIEW

    def _pr_number(self, task: Task) -> int | None:
        """The PR number to poll: the task's `pr` URL is the source of truth (a hand-attached
        PR replaces it directly), the cache is just there to avoid re-parsing every time. When
        the two disagree — e.g. `garden pr` attached a new URL but something left the old
        cached number in place — repair the cache instead of following the stale number."""
        st = self.state.get(task.id)
        m = re.search(r"/pull/(\d+)", task.pr or "")
        url_number = int(m.group(1)) if m else None
        cached = int(st["pr_number"]) if st.get("pr_number") else None
        if url_number and cached != url_number:
            if cached:
                self.log(f"{task.id}: pr_number cache #{cached} disagreed with pr url #{url_number}; repaired")
            st["pr_number"] = url_number
            return url_number
        if cached:
            return cached
        return None

    def manual_reservation(self, task: Task | str) -> dict[str, Any] | None:
        """Return a lifecycle reservation, kept separate from manual runner selection."""
        task_id = task if isinstance(task, str) else task.id
        value = self.state.get(task_id).get("manual_reservation")
        return dict(value) if isinstance(value, dict) and value.get("id") else None

    def _manual_reserved(self, task: Task | str) -> bool:
        return self.manual_reservation(task) is not None

    # ---- tick --------------------------------------------------------------
    @contextmanager
    def _step(self, rep: TickReport, name: str) -> Iterator[None]:
        """Time one phase of the tick into the report, so a slow pass can name the step that
        cost it. Errors inside the block still propagate; the caller wraps what it wants to
        keep the loop alive."""
        t0 = time.monotonic()
        try:
            yield
        finally:
            rep.record_step(name, time.monotonic() - t0)

    def _guard(self, rep: TickReport, name: str, fn: Callable[[], None]) -> None:
        """The one guard every tick phase runs under: an exception is logged with the phase
        name and recorded on the report instead of aborting the rest of the tick (CG-203)."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"{name} failed: {e}")
            self.log(f"{name} failed: {e}")

    def _reap_all(self, rep: TickReport) -> None:
        """Walk the run records of every mode and reap each finished run through its own path:
        a worker's push/PR (or retry/fail/waiting_human), a finished review or persona verdict,
        an edit, a check, a trial, and finally the orphan and dead-run sweeps. Shared by `tick`
        and `reap_on_start` so a restart reaps exactly as a tick would."""
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
                review_pending = bool(self.state.get(t.id).get("review_run"))
                if self.state.get(t.id).get("edit_run") and self.reap_edit(t, rep):
                    rep.reaped.append(t.id)
                    continue
                if self.state.get(t.id).get("check_run"):
                    # A check run in flight owns this task's continuation: reap it when it
                    # finishes, and never let the worker reaper touch the task meanwhile.
                    if self.reap_check(t, rep):
                        rep.reaped.append(t.id)
                    continue
                if t.status == Status.RUNNING and self.state.get(t.id).get("trial", {}).get("status") in ("running", "comparing"):
                    if self.reap_trial(t, rep):
                        rep.reaped.append(t.id)
                    continue
                if t.status == Status.RUNNING and self.reap(t, rep):
                    rep.reaped.append(t.id)
                # A review is its own run, not part of the task's status machine. A
                # failed rebase can put the task back in READY before its already-finished
                # review is collected.
                if review_pending and self.reap_review(t, rep):
                    rep.reaped.append(t.id)
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                rep.errors.append(f"{t.id}: reap failed: {e}")
                self.log(f"{t.id}: reap failed: {e}")
        try:
            self.reap_orphaned(rep)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"orphan reap failed: {e}")
        try:
            self.reap_dead_runs(rep)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"dead-run reap failed: {e}")
        self._cleanup_reaped_temp_dirs()

    def reap_on_start(self) -> TickReport:
        """Before a freshly started process ticks, walk the run records of every mode and reap
        those whose worker finished but whose reap the previous process never completed: a
        verdict a review produced, a worker's push and PR, a persona, trial, edit or check
        result. Each is applied exactly as a normal reap would, so a restart never loses finished
        work (a review the old process reaped in its last tick but died before persisting) nor
        re-runs it, and only then does the caller tick. Safe to call more than once: an
        already-reaped run is skipped (CG-198)."""
        with self._controller_lock():
            return self._reap_on_start_locked()

    def _reap_on_start_locked(self) -> TickReport:
        """Apply startup recovery while the caller holds the controller lock."""
        if self.maintenance_requested():
            return TickReport()
        rep = TickReport()
        started = time.monotonic()
        self.store.invalidate_tasks()
        self.state = State(self.state.path)
        self._migrate_fence_bookkeeping()
        self.confirm_restarted_upgrade()
        with self._step(rep, "reap"):
            self._reload_config_if_safe()  # CG-192 / CG-242: see tick()
            self._reap_all(rep)
        self.state.save()
        rep.duration_s = time.monotonic() - started
        if rep.reaped or rep.transitions:
            self.log(f"start-up reap {rep.summary()}")
        return rep

    def tick(self, dispatch: bool | None = None) -> TickReport:
        """Run one controller-owned pass, serialised across processes for this garden."""
        with self._controller_lock():
            return self._tick_locked(dispatch)

    @contextmanager
    def _controller_lock(self) -> Iterator[None]:
        """Serialize a state transition with scheduler ticks across threads and processes."""
        lock_path = self.cfg.garden_dir / "tick.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _tick_thread_lock(lock_path), open(lock_path, "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            yield

    @contextmanager
    def tick_lock(self) -> Iterator[None]:
        """Serialize a state-changing operator action with scheduler passes."""
        with self._controller_lock():
            yield

    def _tick_locked(self, dispatch: bool | None = None) -> TickReport:
        rep = TickReport()
        started = time.monotonic()
        self.store.invalidate_tasks()  # re-reads task files; garden.yaml goes through the reload gate below
        self.state = State(self.state.path)  # the CLI, web UI or TUI may have written state since the last pass
        self._migrate_fence_bookkeeping()
        self.confirm_restarted_upgrade()
        if self.maintenance_requested():
            # A prior transaction has completed before this locked pass observes the
            # request.  Do no collection or mutation in the acknowledgement pass.
            self._quiesce_for_maintenance()
            self.state.save()
            return rep
        try:
            # Re-reads garden.yaml when it changed on disk (CG-192), holding an executable-field
            # change against an in-flight run's fence manifest until it's safe or an operator
            # confirms it (CG-242) — before anything else in this pass can act on it.
            self._reload_config_if_safe()
            with gitops.tick_read_cache():
                self._tick_body(rep, dispatch)
        finally:
            # Saved here, not just at the end of the happy path, so a phase that raises past
            # its own guard (or a bug in a guard itself) cannot lose a transition an earlier
            # phase already made (CG-203).
            self.state.save()
        # A `garden pin` command may have written its request while this pass was in
        # flight.  Its state was not part of this Scheduler instance's snapshot, so reload
        # at the boundary before deciding whether this controller should consume it.
        self.state = State(self.state.path)
        self.maybe_auto_upgrade(rep)
        rep.duration_s = time.monotonic() - started
        self.events.emit("tick", "", duration_s=rep.duration_s, steps=rep.steps,
                         summary=rep.summary())
        budget = float(self.cfg.get("tick.warn_seconds", 10) or 0)
        if budget and rep.duration_s > budget:
            self.log(f"tick pass {rep.timing()} exceeded {budget:.0f}s budget")
        return rep

    def _tick_body(self, rep: TickReport, dispatch: bool | None) -> None:
        with self._step(rep, "reap"):
            self._reap_all(rep)
        self.refresh_resource_pressure()
        self.store.invalidate_tasks()
        tasks = self.store.tasks()
        with self._step(rep, "poll"):
            observed, suppressed_products = (
                self.refresh_open_prs(tasks, rep) if self.github.available else ({}, set())
            )
            for t in list(tasks.values()):
                if self.state.get(t.id).get("check_run"):
                    continue  # a check run in flight owns this task; don't re-poll it (CG-182)
                if t.pr and t.status.pr_pending:
                    if t.product in suppressed_products:
                        continue
                    try:
                        number = self._pr_number(t)
                        self.poll(t, rep, observed.get((t.product, number)) if number else None)
                        rep.polled.append(t.id)
                    except Exception as e:  # noqa: BLE001
                        rep.errors.append(f"{t.id}: poll failed: {e}")
                        self.log(f"{t.id}: poll failed: {e}")
        with self._step(rep, "base_reprobe"):
            for t in list(tasks.values()):
                # A task parked because its base branch was broken re-probes the base and continues
                # on its own once it goes green — a mechanical rebase and re-check, no worker.
                if self._manual_reserved(t):
                    continue
                try:
                    self._reprobe_base_broken(t, rep)
                except Exception as e:  # noqa: BLE001
                    rep.errors.append(f"{t.id}: base re-probe failed: {e}")
                    self.log(f"{t.id}: base re-probe failed: {e}")
        with self._step(rep, "merge_queue"):
            self._guard(rep, "merge queue", lambda: self._run_merge_queue(rep))
        with self._step(rep, "retro_close"):
            self._guard(rep, "retro close", lambda: self.close_accepted_reopens(rep))
        with self._step(rep, "harness_probe"):
            self._guard(rep, "harness probe", lambda: self.probe_paused_harnesses(rep))
        with self._step(rep, "tool_update"):
            self._guard(rep, "tool update detection", self.detect_tool_upgrade)
        if dispatch is None:
            dispatch = bool(self.cfg.get("auto_dispatch", True))
        if self.is_dispatch_paused():
            dispatch = False
        pending_upgrade = self.upgrade_available()
        if (self.cfg.upgrade_auto() and pending_upgrade
                and pending_upgrade.get("status") not in {"failed", "restart_pending", "installed"}):
            # Once an authorized update is known, stop admitting new work. Existing workers,
            # reviews and checks keep running and are reaped above; this prevents a busy queue
            # from starving the safe install boundary indefinitely.
            dispatch = False
        if dispatch:
            with self._step(rep, "dispatch"):
                self._guard(rep, "dispatch edits", lambda: self.dispatch_edits(rep))
                self._guard(rep, "dispatch ready", lambda: self.dispatch_ready(rep))
        with self._step(rep, "audit"):
            self._guard(rep, "worktree sweep", lambda: self._sweep_terminal_worktrees(rep))
            self._guard(rep, "branch sweep", lambda: self.sweep_worker_branches(rep))
            self._guard(rep, "stuck audit", lambda: self._audit_stuck(rep))
            self._guard(rep, "terminal sweep", lambda: self._sweep_terminal_state(rep))
            self._guard(rep, "id audit", lambda: self._audit_ids(rep))
