"""The phase retro: harvest friction, run the missing personas, reconcile, open the PR."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import gitops
from ..events import phase_summary
from ..github import GitHubError
from ..model import Phase, Status, Task, estimate_tokens, now_iso, slugify
from ..operator_spend import default_path as operator_spend_path
from ..operator_spend import read_records as read_operator_records
from ..operator_spend import total_cost as operator_total_cost
from ..personas import (
    SEVERITY_PRIORITY,
    finding_body,
    finding_title,
    parse_persona,
    phase_brief,
    valid_name,
)
from ..retro import (
    flatten_findings,
    group_findings,
    next_phase_name,
    numbers_section,
    parse_retro,
    persona_features,
    persona_reports,
    reconcile_brief,
    render_next_goals,
    render_retro_doc,
    resolve_features,
    resolve_findings,
)
from ..runs import Run
from .report import TickReport

_RUN_FOOTER_RE = re.compile(r"_garden persona run (\S+)_\s*$")


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

    def _persona_revs(self, reports: dict[str, Path]) -> dict[str, dict[str, Any]]:
        """The parsed marker verdict behind each persona's on-disk report: the rendered markdown
        (`reports`, from `persona_reports`) carries only prose, so this recovers the run id from
        its footer line and re-parses that run's `final.md` for the structured findings and the
        persona's own sections (CG-187, CG-188). Every phase persona run is recorded under the
        aux task id `_persona` (see `dispatch_aux`, which falls back to `f"_{kind}"` when
        dispatched with no task), not a per-phase id, so that is where every run's `final.md`
        lives regardless of which phase it reviewed."""
        out: dict[str, dict[str, Any]] = {}
        for name, path in reports.items():
            try:
                text = path.read_text()
            except OSError:
                continue
            m = _RUN_FOOTER_RE.search(text)
            if not m:
                continue
            final_path = self.runs.dir / "_persona" / m.group(1) / "final.md"
            if not final_path.exists():
                continue
            out[name] = parse_persona(final_path.read_text())
        return out

    def _persona_findings(self, phase: Phase, reports: dict[str, Path]) -> dict[str, list[dict[str, Any]]]:
        """One draft task per finding needs severity/area/suggestion (CG-187); pull them from
        the parsed verdicts."""
        return {name: [f for f in rev.get("findings") or [] if isinstance(f, dict)]
                for name, rev in self._persona_revs(reports).items()}

    def _persona_sections(self, reports: dict[str, Path]) -> dict[str, dict[str, Any]]:
        """Each persona's own declared sections (CG-188); only those that reported a `sections`
        object, so `persona_features` can lift structured features into the retro's list."""
        return {name: rev["sections"] for name, rev in self._persona_revs(reports).items()
                if isinstance(rev.get("sections"), dict)}

    def _retro_materials(self, phase: Phase, names: list[str]):
        """Harvest what the reconciliation needs: friction from PR bodies, friction already
        recorded under the phase's '## Reported' log, friction still sitting in marked PR
        comments, persona reports on disk, the phase's task list with statuses, and the
        merged PRs. Read-only: nothing here writes to the live garden (see module docstring
        of `garden.retro`)."""
        from ..friction import collect_comment_friction, extract_section, harvest

        prod_probe = Task(path=self.store.root, id=f"_{phase.product}", title="", product=phase.product, phase=phase.name)
        slug = self.slug_for(prod_probe)
        gh = self.github if slug else None
        friction = harvest(phase, self.runs, github=gh, slug=slug)
        friction_doc = phase.path / "docs" / "friction.md"
        reported = extract_section(friction_doc.read_text(), "Reported") if friction_doc.exists() else ""
        comment_friction = collect_comment_friction(phase, gh, slug)
        reports = persona_reports(phase, names)
        task_rows = [{"id": t.id, "title": t.title, "status": t.status.value} for t in phase.tasks]
        merged = [{"id": t.id, "title": t.title, "pr": t.pr} for t in phase.tasks if t.pr and t.status == Status.DONE]
        return friction, reported, comment_friction, reports, task_rows, merged

    def retro_plan(self, phase: Phase, personas: list[str] | None = None, skip_personas: bool = False,
                   next_phase: str = "") -> dict[str, Any]:
        """The plan a `--dry-run` prints: which personas run vs reuse, what will be harvested,
        and a token/cost estimate grounded in this garden's own past runs."""
        names = personas or self.retro_default_personas()
        nxt = next_phase or next_phase_name(phase.name)
        have = persona_reports(phase, names)
        reuse = [n for n in names if n in have]
        to_run = [] if skip_personas else [n for n in names if n not in have]
        friction, reported, comment_friction, reports, task_rows, merged = self._retro_materials(phase, names)
        self_prod = self._self_product()
        base = self.cfg.product_base_branch(self_prod or phase.product)
        recon = reconcile_brief(self.store, phase, base, friction, reported, comment_friction,
                                reports, task_rows, merged, nxt)
        difficulty = str(self.cfg.get("retro.difficulty") or "hard")
        probe = Task(path=self.store.root, id=f"_retro-{phase.product}-{phase.name}", title="",
                     product=self_prod or phase.product, phase="")
        runner = self.runner_for(probe, "local", str(self.cfg.get("review.harness") or ""))
        model = self.model_for(probe, runner, difficulty)
        persona_toks = 0
        if to_run:
            persona_toks = estimate_tokens(phase_brief(self.store, phase, to_run[0], base, self.phase_prs(phase))) * len(to_run)
        est_tokens = persona_toks + estimate_tokens(recon)
        tot = self.runs.totals()
        seen = int(tot["input_tokens"]) + int(tot["output_tokens"])
        rate = (float(tot["cost_usd"]) / seen) if seen else 0.0
        reported_entries = len(re.findall(r"(?m)^### ", reported))
        comment_items = sum(len(items) for _, items in comment_friction)
        return {"phase": phase.key, "next_phase": nxt, "self_product": self_prod,
                "personas_run": to_run, "personas_reuse": reuse, "friction": len(friction),
                "reported": reported_entries, "comment_friction": comment_items,
                "merged": len(merged), "tasks": len(task_rows), "est_tokens": est_tokens,
                "est_cost": round(est_tokens * rate, 2), "have_cost_history": bool(seen),
                "difficulty": difficulty, "model": model}

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
        have = persona_reports(phase, names)
        if skip_personas and names and not have:
            raise RuntimeError("garden retro --skip-personas found no persona reports on disk for "
                               f"{', '.join(names)}; run without --skip-personas, or reuse only "
                               "personas that already have a report under docs/reviews/")
        entry: dict[str, Any] = {"phase": phase.key, "product": phase.product, "phase_name": phase.name,
                                 "personas": names, "skip_personas": bool(skip_personas), "next_phase": nxt,
                                 "self_product": self_prod, "stage": "personas", "persona_runs": {}}
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
        friction, reported, comment_friction, reports, task_rows, merged = self._retro_materials(phase, entry["personas"])
        text = reconcile_brief(self.store, phase, base, friction, reported, comment_friction,
                               reports, task_rows, merged, entry["next_phase"])
        run = self._dispatch_retro_run(probe, text, wt, difficulty=str(self.cfg.get("retro.difficulty") or "hard"))
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

    def _retro_failed(self, phase: Phase, step: str, error: gitops.GitError, rep: TickReport) -> None:
        """Record a failed commit or push inside a retro reconciliation (CG-147): logged (so a
        human watching `garden watch` or the web dashboard sees it even on an otherwise silent
        tick), added to the tick's own errors (the CLI's `tick`/`watch` output), and emitted as
        a durable event so it outlives the in-memory log. `error`'s own text names the worktree
        (GitError includes the `cwd` it ran in)."""
        msg = f"retro {phase.key}: {step} failed: {error}"
        self.log(msg)
        rep.errors.append(msg)
        self.events.emit("retro_failed", "", phase=phase.key, step=step, error=str(error))

    def _file_retro_features(self, phase: Phase, next_phase: str, rev: dict[str, Any],
                             wt: Path, rel_product: Path, prefix: str, num: int,
                             persona_feats: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], int]:
        """Turn the reconciliation's `features`, plus any structured `features` a persona
        reported (CG-188), into draft task files inside the retro's own worktree, so they land
        with the same PR as the retro document and the next phase's goals draft (the next phase
        may not exist on disk yet, so this cannot go through `store.create_task`, which requires
        an already-discovered phase). Skips whatever `resolve_features` flags as a duplicate;
        ids are reserved locally (continuing from `num`, shared with `_file_retro_findings` so
        the two never collide) since the live store never sees these files until the PR merges."""
        existing_titles = {t.title.strip().lower(): t.id for t in self.store.tasks().values()}
        resolved = resolve_features(rev, existing_titles, persona_feats)
        if not resolved:
            return [], num
        tasks_dir = wt / rel_product / next_phase / "tasks"
        filed: list[dict[str, Any]] = []
        for f in resolved:
            if f["skip"]:
                filed.append({**f, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: feature {f['title']!r} skipped ({f['reason']})")
                continue
            tid = f"{prefix}-{num:03d}"
            num += 1
            try:
                priority = int(f.get("priority"))
            except (TypeError, ValueError):
                priority = 3
            difficulty = f["difficulty"] if f["difficulty"] in ("easy", "medium", "hard") else "medium"
            body = f"## Goal\n\n{f['body'] or f['title']}\n\n## Context\n\nProposed at the {phase.key} retro."
            if f.get("source"):
                body += f" Raised by the {f['source']} persona."
            if f["rationale"]:
                body += f" {f['rationale']}"
            body += "\n"
            t = Task(path=tasks_dir / f"{tid}-{slugify(f['title'])}.md", id=tid, title=f["title"],
                     status=Status.DRAFT, product=phase.product, phase=next_phase, priority=priority,
                     difficulty=difficulty, discovered_from=f"retro:{phase.key}",
                     created=now_iso(), updated=now_iso(), body=body)
            tasks_dir.mkdir(parents=True, exist_ok=True)
            t.path.write_text(t.render())
            filed.append({**f, "task_id": tid, "status": "draft"})
        return filed, num

    def _file_retro_findings(self, phase: Phase, next_phase: str, persona_findings: dict[str, list[dict[str, Any]]],
                             wt: Path, rel_product: Path, prefix: str, num: int) -> tuple[list[dict[str, Any]], int]:
        """Turn every persona finding into a draft task in the next phase (CG-187): every
        severity, not only high, priority from severity, and findings that say the same thing
        across personas collapsed into one task via `group_findings`'s title match. Mirrors
        `_file_retro_features`: writes directly into the retro's own worktree, and shares its
        id counter."""
        flat = flatten_findings(persona_findings)
        if not flat:
            return [], num
        groups = group_findings(flat)
        existing_titles = {t.title.strip().lower(): t.id for t in self.store.tasks().values()}
        resolved = resolve_findings(groups, existing_titles)
        tasks_dir = wt / rel_product / next_phase / "tasks"
        filed: list[dict[str, Any]] = []
        for f in resolved:
            if f["skip"]:
                filed.append({**f, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: finding {f['summary']!r} skipped ({f['reason']})")
                continue
            tid = f"{prefix}-{num:03d}"
            num += 1
            personas = f["personas"]
            provenance = f"persona:{personas[0]}:{phase.key}"
            title = finding_title(f)
            body = finding_body(f, personas, provenance)
            priority = SEVERITY_PRIORITY.get(str(f.get("severity")), 2)
            t = Task(path=tasks_dir / f"{tid}-{slugify(title)}.md", id=tid, title=title,
                     status=Status.DRAFT, product=phase.product, phase=next_phase, priority=priority,
                     difficulty="medium", discovered_from=provenance,
                     created=now_iso(), updated=now_iso(), body=body)
            tasks_dir.mkdir(parents=True, exist_ok=True)
            t.path.write_text(t.render())
            filed.append({**f, "task_id": tid, "status": "draft"})
        return filed, num

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
        prefix, num_s = self.store.next_id(phase.product).rsplit("-", 1)
        num = int(num_s)
        persona_feats = persona_features(self._persona_sections(reports))
        filed, num = self._file_retro_features(phase, next_phase, rev, wt, rel_product, prefix, num,
                                               persona_feats=persona_feats)
        persona_findings = self._persona_findings(phase, reports)
        filed_findings, num = self._file_retro_findings(phase, next_phase, persona_findings, wt, rel_product, prefix, num)
        summary = phase_summary(self.events.read(), {t.id: t for t in phase.tasks})
        operator_records = read_operator_records(operator_spend_path(self.store.root))
        operator_cost = operator_total_cost(operator_records, since=summary["first_dispatch"])
        numbers = numbers_section(summary["cost_usd"], operator_cost)
        retro_path.write_text(render_retro_doc(phase, rev, reports, self.store, filed=filed, filed_findings=filed_findings,
                                               difficulty=run.difficulty, model=run.model, numbers=numbers))
        goals_path.write_text(render_next_goals(phase, next_phase, rev, filed=filed))
        try:
            gitops.commit_all(wt, f"garden retro: {phase.key} retrospective and {next_phase} goals draft")
        except gitops.GitError as e:
            # A failed commit here (e.g. no git identity in the worktree, CG-147) must not just
            # vanish: the rendered retro doc is already on disk, uncommitted, and nothing else
            # will ever surface that. self.log reaches the running log a human can see (the web
            # dashboard, `garden watch`) even when the tick that hit this is otherwise silent;
            # the event makes it durable on the Timeline too, not just in a 200-entry ring buffer.
            self._retro_failed(phase, "commit", e, rep)
            return
        if gitops.commits_ahead(wt, base) == 0:
            rep.errors.append(f"retro {phase.key}: nothing to commit")
            return
        try:
            gitops.push(wt, branch, base=base)
        except gitops.GitError as e:
            self._retro_failed(phase, "push", e, rep)
            return
        n_items = len([f for f in rev.get("reconciliation") or [] if isinstance(f, dict)])
        n_filed = sum(1 for f in filed if f.get("task_id"))
        n_skipped = len(filed) - n_filed
        n_findings_filed = sum(1 for f in filed_findings if f.get("task_id"))
        n_findings_skipped = len(filed_findings) - n_findings_filed
        title = f"Retro: {phase.key} — reconcile friction and draft {next_phase} goals"
        body = (f"Retrospective for **{phase.key}**, produced by `garden retro`.\n\n"
                f"- reconciled {n_items} friction item(s) against what merged\n"
                f"- {len(reports)} persona report(s)\n"
                f"- {n_findings_filed} persona finding(s) filed as draft tasks in {next_phase}"
                + (f" ({n_findings_skipped} duplicate(s) skipped)" if n_findings_skipped else "") + "\n"
                f"- {n_filed} feature(s) filed as draft tasks in {next_phase}"
                + (f" ({n_skipped} duplicate(s) skipped)" if n_skipped else "") + "\n"
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
