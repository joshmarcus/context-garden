"""The phase retro: harvest friction, run the missing personas, reconcile, open the PR."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import gitops
from ..github import GitHubError
from ..model import Phase, Status, Task, estimate_tokens, now_iso
from ..personas import phase_brief, valid_name
from ..retro import (
    next_phase_name,
    parse_retro,
    persona_reports,
    reconcile_brief,
    render_next_goals,
    render_retro_doc,
)
from ..runs import Run
from .report import TickReport


class RetroMixin:
    # ---- retro: harvest, personas, reconcile, PR ---------------------------
    def _self_product(self) -> str | None:
        """The product whose repo is the garden's own repo (`self: true`); the retro opens its
        PR there so the retro document and next-goals draft land as a PR, not a live edit."""
        for name in (self.cfg.data.get("products", {}) or {}):
            if self.cfg.product_self(name):
                return name
        return None

    def retro_default_personas(self) -> list[str]:
        from ..personas import DEFAULT_PERSONAS, list_personas

        return list_personas(self.store) or sorted(DEFAULT_PERSONAS)

    def _retro_list(self) -> list[dict[str, Any]]:
        return self.state.get("_retro").setdefault("runs", [])

    def _retro_remove(self, entry: dict[str, Any]) -> None:
        self.state.get("_retro")["runs"] = [e for e in self._retro_list() if e is not entry]

    def _retro_materials(self, phase: Phase, names: list[str]):
        """Harvest what the reconciliation needs: friction from PR bodies, persona reports on
        disk, the phase's task list with statuses, and the merged PRs."""
        from ..friction import harvest

        prod_probe = Task(path=self.store.root, id=f"_{phase.product}", title="", product=phase.product, phase=phase.name)
        slug = self.slug_for(prod_probe)
        friction = harvest(phase, self.runs, github=self.github if slug else None, slug=slug)
        reports = persona_reports(phase, names)
        task_rows = [{"id": t.id, "title": t.title, "status": t.status.value} for t in phase.tasks]
        merged = [{"id": t.id, "title": t.title, "pr": t.pr} for t in phase.tasks if t.pr and t.status == Status.DONE]
        return friction, reports, task_rows, merged

    def retro_plan(self, phase: Phase, personas: list[str] | None = None, skip_personas: bool = False,
                   next_phase: str = "") -> dict[str, Any]:
        """The plan a `--dry-run` prints: which personas run vs reuse, what will be harvested,
        and a token/cost estimate grounded in this garden's own past runs."""
        names = personas or self.retro_default_personas()
        nxt = next_phase or next_phase_name(phase.name)
        have = persona_reports(phase, names)
        reuse = [n for n in names if n in have]
        to_run = [] if skip_personas else [n for n in names if n not in have]
        friction, reports, task_rows, merged = self._retro_materials(phase, names)
        self_prod = self._self_product()
        base = self.cfg.product_base_branch(self_prod or phase.product)
        recon = reconcile_brief(self.store, phase, base, friction, reports, task_rows, merged, nxt)
        persona_toks = 0
        if to_run:
            persona_toks = estimate_tokens(phase_brief(self.store, phase, to_run[0], base, self.phase_prs(phase))) * len(to_run)
        est_tokens = persona_toks + estimate_tokens(recon)
        tot = self.runs.totals()
        seen = int(tot["input_tokens"]) + int(tot["output_tokens"])
        rate = (float(tot["cost_usd"]) / seen) if seen else 0.0
        return {"phase": phase.key, "next_phase": nxt, "self_product": self_prod,
                "personas_run": to_run, "personas_reuse": reuse, "friction": len(friction),
                "merged": len(merged), "tasks": len(task_rows), "est_tokens": est_tokens,
                "est_cost": round(est_tokens * rate, 2), "have_cost_history": bool(seen)}

    def start_retro(self, phase: Phase, personas: list[str] | None = None, skip_personas: bool = False,
                    next_phase: str = "") -> dict[str, Any]:
        """Start a phase retro. Runs the missing persona reviews (unless `skip_personas`), then
        the reconciliation, then opens a PR to the garden's own repo. Driven across ticks by
        `reap_retro`, like a trial."""
        self_prod = self._self_product()
        if not self_prod:
            raise RuntimeError("garden retro needs a product with `self: true` (the garden's own repo) to "
                               "open the retro PR; see docs/architecture.md")
        names = personas or self.retro_default_personas()
        for n in names:
            valid_name(n)
        nxt = next_phase or next_phase_name(phase.name)
        entry: dict[str, Any] = {"phase": phase.key, "product": phase.product, "phase_name": phase.name,
                                 "personas": names, "skip_personas": bool(skip_personas), "next_phase": nxt,
                                 "self_product": self_prod, "stage": "personas", "persona_runs": {}}
        have = persona_reports(phase, names)
        missing = [] if skip_personas else [n for n in names if n not in have]
        self._retro_list().append(entry)
        if not missing:
            self._dispatch_reconcile(entry)
        else:
            for n in missing:
                run = self.dispatch_persona_phase(phase, n)
                entry["persona_runs"][n] = run.run_id
            self.events.emit("retro_started", "", phase=phase.key, personas=",".join(names),
                             running=",".join(missing), reuse=",".join(n for n in names if n in have))
        self.state.save()
        return entry

    def _dispatch_retro_run(self, probe: Task, brief_text: str, worktree: Path, difficulty: str = "hard") -> Run:
        runner = self.runner_for(probe, "local", str(self.cfg.get("review.harness") or ""))
        run = self.runs.new_run(probe.id, runner.name, mode="retro")
        run.worktree = str(worktree)
        run.model = self.model_for(probe, runner, difficulty)
        run.difficulty = difficulty
        run.brief_tokens = max(1, len(brief_text) // 4)
        run.save()
        runner.start(run, worktree, brief_text)
        self.events.emit("dispatch", run.task_id, run=run.run_id, mode="retro", model=run.model,
                         harness=run.harness, phase=probe.phase)
        return run

    def _dispatch_reconcile(self, entry: dict[str, Any]) -> None:
        phase = self.store.phase(entry["product"], entry["phase_name"])
        probe = Task(path=self.store.root, id=f"_retro-{phase.product}-{phase.name}", title="",
                     product=entry["self_product"], phase="")
        repo = self.repo_for(probe)
        base = self.final_base_for(probe)
        branch = f"garden/retro-{phase.product}-{phase.name}"
        wt = self.cfg.worktree_path(f"_retro-{phase.product}-{phase.name}")
        gitops.fetch(repo)
        gitops.prepare_worktree(repo, wt, branch, base)
        friction, reports, task_rows, merged = self._retro_materials(phase, entry["personas"])
        text = reconcile_brief(self.store, phase, base, friction, reports, task_rows, merged, entry["next_phase"])
        run = self._dispatch_retro_run(probe, text, wt, difficulty=str(self.cfg.get("review.difficulty") or "hard"))
        entry.update({"stage": "reconciling", "recon_run_id": run.run_id, "recon_task": probe.id,
                      "branch": branch, "worktree": str(wt), "base": base, "slug": self.slug_for(probe) or ""})
        self.events.emit("retro_reconcile", "", phase=phase.key, run=run.run_id, branch=branch)

    def retro_pending(self, phase_key: str) -> dict[str, int] | None:
        """The persona-wait state of the phase's active retro, if any is stuck waiting: `{"done":
        n, "total": m}`. For `garden status` and the phase page. None once every requested
        report is in (the reconciliation dispatches) or if no retro is running for the phase."""
        for entry in self._retro_list():
            if entry.get("phase") != phase_key or entry.get("stage") != "personas":
                continue
            phase = self.store.phase(entry["product"], entry["phase_name"])
            have = persona_reports(phase, entry["personas"])
            return {"done": len(have), "total": len(entry["personas"])}
        return None

    def reap_retro(self, rep: TickReport) -> None:
        for entry in list(self._retro_list()):
            try:
                if entry.get("stage") == "personas":
                    # Gated on reports actually on disk, not on whether a run is still active:
                    # a concurrent tick reading state mid-dispatch (start_retro saves after each
                    # persona it kicks off) must not mistake "no run recorded yet" for "done"
                    # and reconcile before every persona has even started.
                    phase = self.store.phase(entry["product"], entry["phase_name"])
                    have = persona_reports(phase, entry["personas"])
                    if len(have) < len(entry["personas"]):
                        continue
                    self._dispatch_reconcile(entry)
                    continue
                if entry.get("stage") != "reconciling":
                    continue
                run = next((r for r in self.runs.runs_for(entry["recon_task"]) if r.run_id == entry.get("recon_run_id")), None)
                if run is None:
                    rep.errors.append(f"retro {entry['phase']}: reconcile run vanished")
                    self._retro_remove(entry)
                    continue
                probe = Task(path=self.store.root, id=entry["recon_task"], title="", product=entry["self_product"], phase="")
                runner = self.runner_for(probe, run.runner, run.harness)
                if not self._finished_or_timed_out(run, runner):
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
                self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode,
                                 cost_usd=run.cost_usd, usage=run.usage, status=run.status)
                self._finish_retro(entry, run, final, rep)
                self._retro_remove(entry)
            except Exception as e:  # noqa: BLE001 - one bad retro must not sink the tick
                rep.errors.append(f"retro {entry.get('phase')}: {e}")
                self._retro_remove(entry)
        self.state.save()

    def _finish_retro(self, entry: dict[str, Any], run: Run, final: str, rep: TickReport) -> None:
        phase = self.store.phase(entry["product"], entry["phase_name"])
        rev = parse_retro(final)
        if not rev:
            rep.errors.append(f"retro {phase.key}: no verdict ({run.error[:100] or 'see final.md'})")
            return
        next_phase = entry["next_phase"]
        reports = persona_reports(phase, entry["personas"])
        wt = Path(entry["worktree"])
        base, branch = entry["base"], entry["branch"]
        rel_phase = phase.path.relative_to(self.store.root)
        rel_product = phase.path.parent.relative_to(self.store.root)
        retro_path = wt / rel_phase / "docs" / "retro.md"
        goals_path = wt / rel_product / next_phase / "goals.md"
        retro_path.parent.mkdir(parents=True, exist_ok=True)
        goals_path.parent.mkdir(parents=True, exist_ok=True)
        retro_path.write_text(render_retro_doc(phase, rev, reports, self.store))
        goals_path.write_text(render_next_goals(phase, next_phase, rev))
        gitops.commit_all(wt, f"garden retro: {phase.key} retrospective and {next_phase} goals draft")
        if gitops.commits_ahead(wt, base) == 0:
            rep.errors.append(f"retro {phase.key}: nothing to commit")
            return
        try:
            gitops.push(wt, branch, base=base)
        except gitops.GitError as e:
            rep.errors.append(f"retro {phase.key}: push failed: {e}")
            return
        n_items = len([f for f in rev.get("reconciliation") or [] if isinstance(f, dict)])
        title = f"Retro: {phase.key} — reconcile friction and draft {next_phase} goals"
        body = (f"Retrospective for **{phase.key}**, produced by `garden retro`.\n\n"
                f"- reconciled {n_items} friction item(s) against what merged\n"
                f"- {len(reports)} persona report(s)\n"
                f"- retro document: `{rel_phase.as_posix()}/docs/retro.md`\n"
                f"- next-phase goals draft: `{rel_product.as_posix()}/{next_phase}/goals.md`\n\n"
                f"{str(rev.get('summary', '')).strip()}\n")
        pr_url = ""
        slug = entry.get("slug") or ""
        if slug and self.github.available:
            try:
                pr = self.github.create_pr(slug, branch, base, title, body,
                                           draft=bool(self.cfg.get("github.draft_pr", False)))
                pr_url = pr.url
            except GitHubError as e:
                rep.errors.append(f"retro {phase.key}: branch pushed but PR failed: {e}")
        self.events.emit("retro_done", "", phase=phase.key, pr=pr_url, branch=branch, items=n_items, cost_usd=run.cost_usd)
        rep.transitions.append(f"retro {phase.key} -> {pr_url or branch}")
