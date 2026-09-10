"""Auxiliary runs (comparison, persona) tracked under `_aux` in state rather than on a task."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model import Task, now_iso
from ..runs import Run
from .report import TickReport


class AuxMixin:
    # ---- auxiliary runs (compare, persona) ---------------------------------
    def _aux_list(self) -> list[dict[str, Any]]:
        return self.state.get("_aux").setdefault("runs", [])

    def dispatch_aux(self, kind: str, task: Task | None, brief_text: str, worktree: Path, meta: dict[str, Any],
                     harness_name: str = "", difficulty: str = "", prepared_run: Run | None = None,
                     model_override: str | None = None, pool_member: str = "") -> Run:
        self.require_maintenance_running()
        probe = task or Task(path=self.store.root, id=str(meta.get("id", "_aux")), title="", product=str(meta.get("product", "")), phase=str(meta.get("phase", "")))
        runner_name = "remote" if self.runner_for(probe).name == "remote" else "local"
        runner = self.runner_for(probe, runner_name, harness_name)
        self._raise_if_harness_paused(runner.harness.name if runner.harness else "")
        # Phase persona reports must retain their phase identity.  Unlike PR personas, they
        # have no task, and the old shared `_persona` bucket let reports from two phases
        # overwrite one another.
        run_task_id = probe.id if task or kind == "persona" else f"_{kind}"
        resource_weight = self.cfg.product_resource_weight(probe.product)
        run = prepared_run or (self.runs.new_run(run_task_id, runner_name, mode=kind)
                               if runner_name == "remote" else self._new_local_run(
                                   run_task_id, kind, kind, resource_weight=resource_weight))
        run.branch = task.branch or task.default_branch() if task else self.final_base_for(probe)
        run.base = self.base_for(task) if task else self.final_base_for(probe)
        run.env_snapshot.update({"product": probe.product,
                                 "execution_timeout_minutes": self.cfg.product_timeout_minutes(probe.product),
                                 "resource_weight": resource_weight})
        run.worktree = str(worktree)
        run.model = self.model_for(probe, runner, difficulty or "hard")
        if kind in ("persona", "compare"):
            # The judge, not the work: named by retro_model independent of the tier map
            # above, so a garden can price work cheaply and still hand the verdict to its
            # best model (CG-235). kickoff runs also land here but are not a judge call.
            override = self.retro_model_for(runner)
            if override:
                run.model = override
        if model_override is not None:
            run.model = model_override
        run.difficulty = difficulty or "hard"
        run.harness = runner.harness.name if runner.harness else ""
        run.pool_member = pool_member
        run.brief_tokens = max(1, len(brief_text) // 4)
        canonical = self.prepare_canonical_run(probe, run, runner, run.branch, run.base)
        if canonical is not None:
            worktree = canonical
            run.worktree = str(canonical)
        run.save()
        runner.start(run, worktree, brief_text)
        self._aux_list().append({"run_id": run.run_id, "task": run.task_id, "kind": kind, **meta})
        self.events.emit("dispatch", run.task_id, run=run.run_id, mode=kind, model=run.model,
                         harness=run.harness, pool_member=run.pool_member,
                         **{k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))})
        self.state.save()
        return run

    def reap_aux(self, rep: TickReport) -> None:
        remaining = []
        for entry in list(self._aux_list()):
            run = next((r for r in self.runs.runs_for(entry["task"]) if r.run_id == entry["run_id"]), None)
            if run is None:
                continue
            task = self.store.tasks().get(entry["task"])
            reserved = task is not None and self._manual_reserved(task)
            runner = self.runner_for(self.store.tasks().get(entry["task"]) or Task(path=self.store.root, id=entry["task"], title=""), run.runner, run.harness)
            finished = (run.process_finished() if reserved else self._finished_or_timed_out(run, runner))
            if run.status == "running" and not finished:
                remaining.append(entry)
                continue
            final_path = run.path / "final.md"
            final = final_path.read_text() if run.status != "running" and final_path.exists() else ""
            collected: dict[str, Any] = {}
            if run.status != "timeout":
                run.exit_code = run.read_exit_code()
                run.finished_at = now_iso()
                collected = runner.collect(run)
                run.usage = collected.get("usage") or {}
                run.cost_usd = collected.get("cost_usd")
                run.model = str(collected.get("model") or run.model)
                run.error = collected.get("error") or ""
                final = collected.get("final_text") or ""
                if final and not (run.path / "final.md").exists():
                    (run.path / "final.md").write_text(final)
                run.status = "env_error" if collected.get("env_error") else "done"
                run.save()
            if reserved:
                remaining.append(entry)
                continue
            if collected.get("env_error"):
                # The harness's own account, not this round: pause it and put the request
                # back where it can try again once it resumes, instead of a broken verdict.
                self._pause_for_env_error(run, collected)
                self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode, cost_usd=run.cost_usd,
                                 usage=run.usage, status="env_error", harness=run.harness,
                                 model=run.model, pool_member=run.pool_member)
                self._requeue_aux_env_error(entry, run, rep)
                continue
            self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode,
                             cost_usd=run.cost_usd, usage=run.usage, status=run.status,
                             harness=run.harness, model=run.model,
                             pool_member=run.pool_member)
            try:
                if entry["kind"] == "compare":
                    self._finish_trial(entry, run, final, rep)
                elif entry["kind"] == "persona":
                    self._finish_persona(entry, run, final, rep)
                elif entry["kind"] == "kickoff":
                    self._finish_kickoff(entry, run, final, rep)
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{entry['task']}: {entry['kind']} failed: {e}")
        self.state.get("_aux")["runs"] = remaining

    def _requeue_aux_env_error(self, entry: dict[str, Any], run: Run, rep: TickReport) -> None:
        """Put a persona/compare aux request back where it can be tried again once its harness
        resumes, instead of losing it or reporting a broken verdict for what was really the
        harness's own account trouble. A PR-targeted persona round rejoins the review queue
        (`_dispatch_or_defer_reviews` already gates a fresh dispatch on `is_harness_paused`); a
        trial's comparison reopens the trial for `reap_trial` to redispatch once every
        contender still has an open PR. A phase-level persona review (a retro, or the phase
        "review this phase" action) has no per-task queue to rejoin, so it is dropped; whoever
        started it re-runs it by hand once the harness is back."""
        kind = entry.get("kind")
        note = f"{kind} review paused ({run.harness or 'the harness'} hit its account limit); will retry once it resumes"
        task = self.store.tasks().get(entry.get("task", ""))
        if kind == "persona" and entry.get("target") == "pr" and task is not None:
            st = self.state.get(task.id)
            required = bool(entry.get("required_evidence"))
            if required:
                st.setdefault("required_evidence", {})[f"persona:{entry.get('persona', '')}"] = "queued"
            self._queue_pending_reviews(st, [{"kind": "persona", "name": entry.get("persona", ""), "required": required}])
            task.log(note)
            self.store.save(task)
            rep.transitions.append(f"{task.id} persona paused (env_error)")
            return
        if kind == "compare" and task is not None:
            st = self.state.get(task.id)
            trial = st.get("trial") or {}
            if trial.get("status") == "comparing":
                trial["status"] = "running"
                trial["compare_paused"] = True
            task.log(note)
            self.store.save(task)
            rep.transitions.append(f"{task.id} compare paused (env_error)")
            return
        if kind == "persona" and entry.get("target") == "phase":
            self._record_retro_persona_failure(run, str(entry.get("persona") or ""), note)
        self.log(f"{entry.get('task')}: {note} (waiting for an explicit phase-review retry)")
        rep.transitions.append(f"{entry.get('task')} {kind} paused (env_error, retry available)")
