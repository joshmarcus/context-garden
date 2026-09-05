"""The phase retro: harvest friction, run the missing personas, reconcile, open the PR."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import gitops
from ..events import phase_summary
from ..github import GitHubError
from ..model import Phase, Status, Task, estimate_tokens, now_iso, phase_refusal, slugify
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
    PHASE_VERDICTS,
    flatten_findings,
    group_findings,
    next_phase_name,
    normalize_verdict,
    numbers_section,
    parse_retro,
    persona_features,
    persona_reports,
    reconcile_brief,
    render_next_goals,
    render_retro_doc,
    resolve_features,
    resolve_findings,
    resolve_retro_tasks,
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
        difficulty = str(self.effective("retro.difficulty") or "hard")
        probe = Task(path=self.store.root, id=f"_retro-{phase.product}-{phase.name}", title="",
                     product=self_prod or phase.product, phase="")
        runner = self.runner_for(probe, "local", str(self.cfg.get("review.harness") or ""))
        model = self.retro_model_for(runner) or self.model_for(probe, runner, difficulty)
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
        run.model = self.retro_model_for(runner) or self.model_for(probe, runner, difficulty)
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
        run = self._dispatch_retro_run(probe, text, wt, difficulty=str(self.effective("retro.difficulty") or "hard"))
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
                    run.model = str(collected.get("model") or run.model)
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

    @staticmethod
    def _retro_priority(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 3

    def _write_worktree_draft(self, tasks_dir: Path, tid: str, phase: Phase, target_phase: str,
                              title: str, body: str, priority: int, difficulty: str) -> None:
        """Write one draft task file into the retro worktree (the target phase may not exist on
        disk yet, so this cannot go through `store.create_task`); it lands with the retro PR."""
        t = Task(path=tasks_dir / f"{tid}-{slugify(title)}.md", id=tid, title=title,
                 status=Status.DRAFT, product=phase.product, phase=target_phase, priority=priority,
                 difficulty=difficulty if difficulty in ("easy", "medium", "hard") else "medium",
                 discovered_from=f"retro:{phase.key}", created=now_iso(), updated=now_iso(), body=body)
        tasks_dir.mkdir(parents=True, exist_ok=True)
        t.path.write_text(t.render())

    def _file_retro_features(self, phase: Phase, next_phase: str, rev: dict[str, Any], wt: Path,
                             rel_product: Path, existing_titles: dict[str, str],
                             prefix: str, num: int,
                             persona_feats: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], int]:
        """Turn the reconciliation's `features`, plus any structured `features` a persona
        reported (CG-188), into draft task files inside the retro's own worktree, so they land
        with the same PR as the retro document and the next phase's goals draft (the next phase
        may not exist on disk yet, so this cannot go through `store.create_task`, which requires
        an already-discovered phase). Skips whatever `resolve_features` flags as a duplicate;
        returns the filed list and the next free id number."""
        resolved = resolve_features(rev, existing_titles, persona_feats)
        tasks_dir = wt / rel_product / next_phase / "tasks"
        filed: list[dict[str, Any]] = []
        for f in resolved:
            if f["skip"]:
                filed.append({**f, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: feature {f['title']!r} skipped ({f['reason']})")
                continue
            tid = f"{prefix}-{num:03d}"
            num += 1
            body = f"## Goal\n\n{f['body'] or f['title']}\n\n## Context\n\nProposed at the {phase.key} retro."
            if f.get("source"):
                body += f" Raised by the {f['source']} persona."
            if f["rationale"]:
                body += f" {f['rationale']}"
            body += "\n"
            self._write_worktree_draft(tasks_dir, tid, phase, next_phase, f["title"], body,
                                       self._retro_priority(f.get("priority")), f["difficulty"])
            existing_titles[f["title"].strip().lower()] = tid
            filed.append({**f, "task_id": tid, "status": "draft"})
        return filed, num

    def _file_retro_findings(self, phase: Phase, next_phase: str, persona_findings: dict[str, list[dict[str, Any]]],
                             wt: Path, rel_product: Path, existing_titles: dict[str, str],
                             prefix: str, num: int) -> tuple[list[dict[str, Any]], int]:
        """Turn every persona finding into a draft task in the next phase (CG-187): every
        severity, not only high, priority from severity, and findings that say the same thing
        across personas collapsed into one task via `group_findings`'s title match. Mirrors
        `_file_retro_features`: writes directly into the retro's own worktree, and shares its
        id counter."""
        flat = flatten_findings(persona_findings)
        if not flat:
            return [], num
        groups = group_findings(flat)
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
            existing_titles[title.strip().lower()] = tid
            filed.append({**f, "task_id": tid, "status": "draft"})
        return filed, num

    def _file_retro_followups(self, phase: Phase, next_phase: str, rev: dict[str, Any], wt: Path,
                              rel_product: Path, existing_titles: dict[str, str],
                              prefix: str, num: int) -> tuple[list[dict[str, Any]], int]:
        """File the verdict's `followups` as draft tasks in the next phase (in the worktree, like
        features): a `close_with_followups` verdict carries work worth doing next but not blocking
        the close. Returns the filed list and the next free id number."""
        resolved = resolve_retro_tasks(rev.get("followups"), existing_titles)
        tasks_dir = wt / rel_product / next_phase / "tasks"
        filed: list[dict[str, Any]] = []
        for f in resolved:
            if f["skip"]:
                filed.append({**f, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: follow-up {f['title']!r} skipped ({f['dup_reason']})")
                continue
            tid = f"{prefix}-{num:03d}"
            num += 1
            body = (f"## Goal\n\n{f['body'] or f['title']}\n\n## Context\n\n"
                    f"A follow-up carried into {next_phase} by the {phase.key} retro verdict.\n")
            self._write_worktree_draft(tasks_dir, tid, phase, next_phase, f["title"], body,
                                       self._retro_priority(f.get("priority")), f["difficulty"])
            existing_titles[f["title"].strip().lower()] = tid
            filed.append({**f, "task_id": tid, "status": "draft"})
        return filed, num

    def _file_retro_blocking(self, phase: Phase, rev: dict[str, Any],
                             existing_titles: dict[str, str]) -> list[dict[str, Any]]:
        """File the verdict's `blocking` items as draft tasks in the *current* phase, live (the
        phase exists), with `retro_blocking` and a freeze exception so a frozen phase still
        dispatches them and `close-phase` refuses until they are done. Skips a duplicate title."""
        resolved = resolve_retro_tasks(rev.get("blocking"), existing_titles)
        filed: list[dict[str, Any]] = []
        for b in resolved:
            if b["skip"]:
                filed.append({**b, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: blocking task {b['title']!r} skipped ({b['dup_reason']})")
                continue
            reason = b["reason"] or "retro reopen: must land before the phase can close"
            body = (f"## Goal\n\n{b['body'] or b['title']}\n\n## Context\n\n"
                    f"Filed by the {phase.key} retro `reopen` verdict: it must land before the phase "
                    f"can close. Reason: {reason}\n")
            t = self.store.create_task(phase.product, phase.name, b["title"], body,
                                       priority=self._retro_priority(b.get("priority")), status="draft",
                                       difficulty=b["difficulty"] if b["difficulty"] in ("easy", "medium", "hard") else "medium")
            t.discovered_from = f"retro:{phase.key}"
            t.retro_blocking = True
            t.freeze_exception = True
            t.freeze_exception_reason = reason
            t.log(f"filed by the {phase.key} retro reopen verdict (blocking)")
            self.store.save(t)
            self.store.invalidate()
            existing_titles[b["title"].strip().lower()] = t.id
            filed.append({**b, "task_id": t.id, "status": "draft"})
            self.events.emit("retro_blocking_filed", t.id, phase=phase.key, title=b["title"])
        return filed

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
        questions = [f for f in (self._file_question(
            phase, item, i, run.run_id, source=f"retro:{phase.key}", document_paths=[
                retro_path, goals_path, phase.path / "docs" / "retro.md", phase.path.parent / next_phase / "goals.md",
            ]
        ) for i, item in enumerate(rev.get("questions") or []) if isinstance(item, dict)) if f]
        for question in questions:
            question.update(retro_worktree=str(wt), retro_branch=branch, retro_base=base,
                            live_retro_path=str(phase.path / "docs" / "retro.md"))
            self.state.get("_decisions")[question["decision_id"]].update(question)
        existing_titles = {t.title.strip().lower(): t.id for t in self.store.tasks().values()}
        # Blocking tasks go live into the current phase (it exists, they must dispatch and block
        # the close); features, followups and findings go into the worktree next phase (which may
        # not exist yet) to land with the retro PR. Blocking is filed first so the live id counter
        # is past those ids before the worktree drafts reserve theirs.
        blocking = self._file_retro_blocking(phase, rev, existing_titles)
        prefix, num_s = self.store.next_id(phase.product).rsplit("-", 1)
        num = int(num_s)
        persona_feats = persona_features(self._persona_sections(reports))
        filed, num = self._file_retro_features(phase, next_phase, rev, wt, rel_product, existing_titles, prefix, num,
                                               persona_feats=persona_feats)
        persona_findings = self._persona_findings(phase, reports)
        filed_findings, num = self._file_retro_findings(phase, next_phase, persona_findings, wt, rel_product,
                                                         existing_titles, prefix, num)
        followups, num = self._file_retro_followups(phase, next_phase, rev, wt, rel_product, existing_titles, prefix, num)
        summary = phase_summary(self.events.read(), {t.id: t for t in phase.tasks})
        operator_records = read_operator_records(operator_spend_path(self.store.root))
        operator_cost = operator_total_cost(operator_records, since=summary["first_dispatch"])
        numbers = numbers_section(summary["cost_usd"], operator_cost)
        retro_path.write_text(render_retro_doc(phase, rev, reports, self.store, filed=filed,
                                               filed_findings=filed_findings, filed_questions=questions, followups=followups,
                                               blocking=blocking, next_phase=next_phase,
                                               difficulty=run.difficulty, model=run.model, numbers=numbers))
        goals_path.write_text(render_next_goals(phase, next_phase, rev, filed=filed, followups=followups))
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
        n_followups = sum(1 for f in followups if f.get("task_id"))
        n_blocking = sum(1 for b in blocking if b.get("task_id"))
        n_questions = len(questions)
        verdict = normalize_verdict(rev.get("verdict"))
        title = f"Retro: {phase.key} — reconcile friction and draft {next_phase} goals"
        body = (f"Retrospective for **{phase.key}**, produced by `garden retro`.\n\n"
                f"- verdict: **{PHASE_VERDICTS.get(verdict, 'none')}**\n"
                f"- reconciled {n_items} friction item(s) against what merged\n"
                f"- {len(reports)} persona report(s)\n"
                f"- {n_findings_filed} persona finding(s) filed as draft tasks in {next_phase}"
                + (f" ({n_findings_skipped} duplicate(s) skipped)" if n_findings_skipped else "") + "\n"
                f"- {n_filed} feature(s) filed as draft tasks in {next_phase}"
                + (f" ({n_skipped} duplicate(s) skipped)" if n_skipped else "") + "\n"
                + (f"- {n_followups} follow-up(s) filed in {next_phase}\n" if n_followups else "")
                + (f"- {n_blocking} blocking task(s) filed in {phase.key}\n" if n_blocking else "")
                + (f"- {n_questions} owner question(s) filed as decision cards\n" if n_questions else "")
                + f"- retro document: `{rel_phase.as_posix()}/docs/retro.md`\n"
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
        self._apply_retro_verdict(phase, rev, followups, blocking, next_phase, pr_url)

    # ---- the verdict: close, close with follow-ups, or reopen --------------
    def _apply_retro_verdict(self, phase: Phase, rev: dict[str, Any], followups: list[dict[str, Any]],
                             blocking: list[dict[str, Any]], next_phase: str, pr_url: str) -> None:
        """Record the retro's phase verdict and act on it: `close`/`close_with_followups` close
        the phase at once (the owner decided closing does not wait for approval); `reopen` leaves
        the phase open and records a pending decision that approves the blocking tasks when
        accepted (see `retro_decide`). The record is what the phase page, the retro page and
        `close-phase` read."""
        verdict = normalize_verdict(rev.get("verdict"))
        at = now_iso()
        rec: dict[str, Any] = {
            "phase": phase.key, "verdict": verdict, "at": at, "next_phase": next_phase,
            "followup_ids": [f["task_id"] for f in followups if f.get("task_id")],
            "blocking_ids": [b["task_id"] for b in blocking if b.get("task_id")],
            "pr": pr_url, "note": "", "accepted_by": "", "accepted_at": "", "status": "recorded",
        }
        if verdict in ("close", "close_with_followups"):
            # Close at once (the owner decided closing does not wait for approval), but do not
            # force past genuinely open work: if a task is still in flight, leave the phase open
            # with the verdict recorded so `close-phase` can follow it once the work lands.
            try:
                self.close_phase(phase, force=False)
                rec.update(status="accepted", accepted_by="retro", accepted_at=at)
            except RuntimeError as e:
                self.log(f"retro {phase.key}: verdict {verdict} recorded but the phase is not "
                         f"closeable yet: {e}")
        elif verdict == "reopen":
            rec["status"] = "pending"  # a decision: accept to approve the blocking tasks
        self.state.get("_retro_verdicts")[phase.key] = rec
        self.events.emit("retro_verdict", "", phase=phase.key, verdict=verdict or "none",
                         status=rec["status"], blocking=",".join(rec["blocking_ids"]),
                         followups=",".join(rec["followup_ids"]))
        self.state.save()

    def retro_verdict(self, phase_key: str) -> dict[str, Any] | None:
        """The recorded verdict for a phase (verdict, status, who accepted it and when, and the
        ids of the tasks it filed), or None if no retro has run. A copy, so callers can't mutate
        the stored record."""
        rec = self.state.get("_retro_verdicts").get(phase_key)
        return dict(rec) if isinstance(rec, dict) else None

    def pending_retro_verdicts(self) -> list[dict[str, Any]]:
        """Retro verdicts still waiting for a person's call: a `reopen` verdict not yet accepted
        or changed. Stamped with `phase_key`. What the Inbox shows and the badge counts."""
        out: list[dict[str, Any]] = []
        for phase_key, rec in self.state.get("_retro_verdicts").items():
            if isinstance(rec, dict) and rec.get("status") == "pending":
                out.append({**rec, "phase_key": phase_key})
        return sorted(out, key=lambda r: str(r["phase_key"]))

    def retro_blocking_open(self, phase: Phase) -> list[Task]:
        """The phase's `retro_blocking` tasks that are not yet done or cancelled -- what
        `close-phase` refuses on."""
        return [t for t in phase.tasks if t.retro_blocking and not t.status.terminal]

    def _approve_retro_blocking(self, phase: Phase, blocking_ids: list[str]) -> list[str]:
        """Approve (draft -> ready) the phase's still-draft blocking tasks named by the verdict."""
        approved: list[str] = []
        tasks = self.store.tasks()
        for tid in blocking_ids:
            t = tasks.get(tid)
            if t is None or t.status != Status.DRAFT:
                continue
            refusal = phase_refusal(phase, t)
            if refusal:
                self.log(f"retro {phase.key}: cannot approve blocking {tid}: {refusal}")
                continue
            t.status = Status.READY
            t.log("approved by the retro reopen verdict")
            self.store.save(t)
            approved.append(tid)
        if approved:
            self.store.invalidate()
        return approved

    def retro_decide(self, phase: Phase, choice: str, note: str = "", by: str = "cli") -> dict[str, Any]:
        """Accept or change a phase's retro verdict. `reopen` (re)opens the phase and approves
        its blocking tasks; `close`/`close_with_followups` close the phase (refusing on open
        tasks the way `close-phase` does). Records who decided and when."""
        choice = normalize_verdict(choice)
        if not choice:
            raise RuntimeError("choose one of: close, followups, reopen")
        vs = self.state.get("_retro_verdicts")
        if phase.key not in vs:
            raise RuntimeError(f"{phase.key} has no retro verdict to decide; run `garden retro {phase.key}` first")
        pending_blocking = [d for d in self.pending_decisions()
                            if d.get("kind") == "question" and d.get("source") == f"retro:{phase.key}"
                            and d.get("blocking")]
        if pending_blocking:
            raise RuntimeError("answer the retro's blocking question before accepting its verdict: "
                               + str(pending_blocking[0].get("question") or "(unnamed question)"))
        rec = vs[phase.key]
        if choice == "reopen":
            if phase.closed:
                self.reopen_phase(phase)
                phase = self.store.phase(phase.product, phase.name)
            self._approve_retro_blocking(phase, list(rec.get("blocking_ids") or []))
        else:
            self.close_phase(phase)  # raises on open tasks, like close-phase
        rec.update(verdict=choice, status="accepted", note=note, accepted_by=by, accepted_at=now_iso())
        self.events.emit("retro_verdict", "", phase=phase.key, verdict=choice, status="accepted", by=by)
        self.state.save()
        return dict(rec)

    def _publish_retro_question_answer(self, decision: dict[str, Any]) -> None:
        """An answer made before the retro PR merges belongs on that PR branch; after merge
        the live document was updated directly and needs no branch mutation."""
        if Path(str(decision.get("live_retro_path") or "")).exists():
            return
        worktree = Path(str(decision.get("retro_worktree") or ""))
        branch, base = str(decision.get("retro_branch") or ""), str(decision.get("retro_base") or "")
        if not worktree.exists() or not branch or not base:
            return
        try:
            gitops.commit_all(worktree, "garden retro: record owner question answer")
            if gitops.commits_ahead(worktree, base):
                gitops.push(worktree, branch, base=base)
        except gitops.GitError as e:
            self.log(f"retro question {decision.get('id', '')}: could not update its PR branch: {e}")
