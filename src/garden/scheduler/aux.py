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
                run.model = str(collected.get("model") or run.model)
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
                elif entry["kind"] == "kickoff":
                    self._finish_kickoff(entry, run, final, rep)
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{entry['task']}: {entry['kind']} failed: {e}")
        self.state.get("_aux")["runs"] = remaining
