"""The edit run: fold a task's pending suggestions into its body before a worker sees the spec."""

from __future__ import annotations

from typing import Any

from ..harness import DIFFICULTIES
from ..model import Status, Task, ensure_open, now_iso
from ..runs import Run
from .report import TickReport


class EditsMixin:
    # ---- suggestions: the edit run -----------------------------------------
    EDIT_MAX_ATTEMPTS = 2

    def _edit_pending(self, task: Task) -> bool:
        """True while a task's pending suggestions still warrant an edit run (one is in
        flight, or none has been tried past the cap). Used to hold a work run until the
        spec has been folded in."""
        from ..suggestions import has_pending

        st = self.state.get(task.id)
        if st.get("edit_run"):
            return True
        return has_pending(task.body) and int(st.get("edit_attempts", 0)) < self.EDIT_MAX_ATTEMPTS

    def dispatch_edits(self, rep: TickReport) -> None:
        """Fold pending suggestions into draft/ready tasks before a worker sees the spec.
        Running tasks wait (their suggestions ride the next revise brief); tasks mid-cycle
        (an open PR, a human decision) are left to `garden integrate` / the page button."""
        from ..suggestions import has_pending

        tasks = self.store.tasks()
        active = {r.task_id for r in self.active_runs()}
        for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
            if self.slots_free() <= 0:
                break
            st = self.state.get(t.id)
            if st.get("edit_run") or t.id in active:
                continue
            if t.status not in (Status.DRAFT, Status.READY):
                continue
            if int(st.get("edit_attempts", 0)) >= self.EDIT_MAX_ATTEMPTS:
                continue
            if not has_pending(t.body) or self.budget_exceeded(t):
                continue
            try:
                self.dispatch_edit(t)
                rep.dispatched.append(f"{t.id}(edit)")
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{t.id}: edit dispatch failed: {e}")

    def integrate_now(self, task: Task) -> Run:
        """Force an edit run for a task with pending suggestions (page button / `garden integrate`)."""
        from ..suggestions import has_pending

        ensure_open(task)
        if task.status == Status.RUNNING:
            raise RuntimeError(f"{task.id} is running; its suggestions will ride the next revise brief")
        if self.state.get(task.id).get("edit_run"):
            raise RuntimeError(f"{task.id} already has an edit run in flight")
        if not has_pending(task.body):
            raise RuntimeError(f"{task.id} has no pending suggestions to integrate")
        return self.dispatch_edit(task)

    def dispatch_edit(self, task: Task) -> Run:
        """One cheap, text-only run that rewrites the task body to fold in its suggestions.
        The old body is kept in the run directory so the page can show the diff."""
        from ..suggestions import edit_brief, pending_suggestions

        harness_name = str(self.cfg.get("review.harness") or "")
        runner = self.runner_for(task, "local", harness_name)
        runner.config = {**runner.config, "setup": {}}  # a text edit needs no product env
        text = edit_brief(self.store, task, pending_suggestions(task.body))
        run = self.runs.new_run(task.id, runner.name, mode="edit")
        difficulty = str(self.effective("review.difficulty") or task.difficulty or "medium")
        if difficulty not in DIFFICULTIES:
            difficulty = "medium"
        run.difficulty = difficulty
        run.model = self.model_for(task, runner, difficulty)
        if runner.harness and runner.harness.cfg.get("review_model"):
            run.model = str(runner.harness.cfg["review_model"])
        run.brief_tokens = max(1, len(text) // 4)
        run.save()
        (run.path / "old_body.md").write_text(task.body)
        runner.start(run, run.path, text)
        st = self.state.get(task.id)
        st["edit_run"] = run.run_id
        self.events.emit("dispatch", task.id, run=run.run_id, mode="edit", model=run.model, harness=run.harness)
        self.state.save()
        return run

    def reap_edit(self, task: Task, rep: TickReport) -> bool:
        from ..suggestions import parse_edit, pending_suggestions

        st = self.state.get(task.id)
        run_id = st.get("edit_run")
        if not run_id:
            return False
        run = next((r for r in self.runs.runs_for(task.id) if r.run_id == run_id), None)
        if run is None or run.status != "running":
            st["edit_run"] = ""
            return False
        runner = self.runner_for(task, run.runner, run.harness)
        if not self._finished_or_timed_out(run, runner):
            return False
        st["edit_run"] = ""
        revised: dict[str, Any] = {}
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
            revised = parse_edit(final)
            run.result = revised
            run.status = "done" if revised else "failed"
            run.save()
        cost = f" cost=${run.cost_usd:.2f}" if run.cost_usd is not None else ""
        self.events.emit("run_finished", task.id, run=run.run_id, mode="edit", cost_usd=run.cost_usd,
                         usage=run.usage, status=run.status)
        if not revised:
            st["edit_attempts"] = int(st.get("edit_attempts", 0)) + 1
            task.log(f"suggestion integration produced no revised body ({run.error[:120] or run.status}){cost}")
            self.store.save(task)
            rep.transitions.append(f"{task.id} edit failed")
            return True
        st["edit_attempts"] = 0
        self._apply_edit(task, run, revised, cost, rep, len(pending_suggestions(task.body)))
        return True

    def _apply_edit(self, task: Task, run: Run, revised: dict[str, Any], cost: str, rep: TickReport, n: int) -> None:
        """Write the revised body, apply any proposed priority/difficulty/reading, mark the
        suggestions integrated, and record the new body for the diff. Scheduler-owned fields
        (status, branch, pr, attempts, depends_on) are never touched."""
        from ..suggestions import mark_all_integrated, set_spec_body

        new_spec = str(revised.get("body") or "").strip()
        if new_spec:
            task.body = set_spec_body(task.body, new_spec)
        pr = revised.get("priority")
        if isinstance(pr, int) and 1 <= pr <= 5:
            task.priority = pr
        diff = str(revised.get("difficulty") or "")
        if diff in DIFFICULTIES:
            task.difficulty = diff
        reading = revised.get("reading")
        if isinstance(reading, list) and reading:
            task.reading = [str(r) for r in reading]
        task.body, marked = mark_all_integrated(task.body)
        count = marked or n
        task.log(f"integrated {count} suggestion(s) (run {run.run_id}){cost}")
        self.store.save(task)
        (run.path / "new_body.md").write_text(task.body)
        self.events.emit("integrated", task.id, run=run.run_id, count=count)
        self.log(f"{task.id}: integrated {count} suggestion(s) (run {run.run_id})")
        rep.transitions.append(f"{task.id} integrated {count} suggestion(s)")
