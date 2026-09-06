"""Persona reviews of a PR or a whole phase."""

from __future__ import annotations

from typing import Any

from .. import gitops
from ..github import GitHubError, mark_garden_comment
from ..model import Phase, Status, Task, ensure_open
from ..personas import (
    SEVERITY_PRIORITY,
    finding_body,
    finding_title,
    parse_persona,
    phase_brief,
    pr_brief,
    report_markdown,
    report_path,
    severity_ok,
    valid_name,
)
from ..runs import Run
from .report import TickReport


class PersonaMixin:
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

    def dispatch_persona_phase(self, phase: Phase, name: str, file_tasks: bool = False, min_severity: str = "low") -> Run:
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
                                                             "persona": name, "target": "phase", "file_tasks": file_tasks,
                                                             "min_severity": min_severity},
                                 harness_name=str(self.cfg.get("review.harness") or ""), difficulty=str(self.effective("retro.difficulty") or "hard"))

    def dispatch_persona_pr(self, task: Task, name: str, request_changes: bool = False,
                            member: dict[str, Any] | None = None) -> Run:
        ensure_open(task)
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
        captures: list[str] = []
        for check_run in reversed(self.runs.runs_for(task.id)):
            captures = [str(path) for result in (check_run.result or {}).get("checks", [])
                        if result.get("name") == "ui" for path in result.get("captures", [])
                        if str(path).endswith(".png")]
            if captures:
                break
        text = pr_brief(self.store, task, name, branch, base, pr_title, pr_body, diff,
                        int(self.cfg.get("review.max_diff_chars", 60000)), captures=captures)
        review_tier = str(self.effective("review.difficulty") or task.difficulty or "medium")
        member = member if member is not None else self.select_pool_member(task, review_tier, review=True)
        if self.pool_members(review_tier, review=True) and member is None:
            raise RuntimeError("every review pool member is paused")
        harness_name = str((member or {}).get("harness") or self.cfg.get("review.harness") or "")
        return self.dispatch_aux("persona", task, text, wt, {"persona": name, "target": "pr", "request_changes": request_changes},
                                 harness_name=harness_name, difficulty=str(self.effective("retro.difficulty") or "hard"),
                                 model_override=(member or {}).get("model") or None,
                                 pool_member=str((member or {}).get("label") or ""))

    def _finding_target_phase(self, phase: Phase) -> Phase:
        """Where a persona finding is filed: the reviewed phase, unless it is frozen or closed
        (its own tasks would just sit deferred or refused), in which case the next phase in the
        product, if it already exists on disk. Falls back to the reviewed phase itself when
        there is no next phase yet, so a finding is never lost to a missing directory."""
        if not phase.frozen and not phase.closed:
            return phase
        product = self.store.product(phase.product)
        names = [p.name for p in product.phases]
        if phase.name in names:
            idx = names.index(phase.name)
            if idx + 1 < len(product.phases):
                return product.phases[idx + 1]
        return phase

    def _file_finding_task(self, phase: Phase, f: dict[str, Any], name: str, run_id: str, report_rel: str) -> Task | None:
        """File one persona finding as a draft task, or fold it into an existing finding-task
        with a matching title (a mechanical stand-in for cross-persona dedup on a single, live
        dispatch, where no reconciliation step ever sees every persona's findings at once)."""
        title = finding_title(f)
        if not title:
            return None
        for t in self.store.tasks().values():
            if t.title.strip().lower() == title.lower() and t.discovered_from.startswith("persona:"):
                if name not in t.body:
                    t.body = t.body.rstrip() + f"\n\nAlso raised by the {name} persona review ({report_rel}).\n"
                    self.store.save(t)
                return None
        target = self._finding_target_phase(phase)
        provenance = f"persona:{name}:{run_id}"
        priority = SEVERITY_PRIORITY.get(str(f.get("severity")), 2)
        t = self.store.create_task(target.product, target.name, title, finding_body(f, [name], provenance),
                                   priority=priority, status="draft")
        t.discovered_from = provenance
        self.store.save(t)
        self.store.invalidate_tasks()
        return t

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
                min_severity = str(entry.get("min_severity") or "low")
                report_rel = self.store.rel(path)
                for f in rev.get("findings") or []:
                    if not isinstance(f, dict) or not f.get("summary"):
                        continue
                    if not severity_ok(str(f.get("severity") or ""), min_severity):
                        continue
                    t = self._file_finding_task(phase, f, name, run.run_id, report_rel)
                    if t is not None:
                        self.events.emit("discovered", entry["task"], new_task=t.id, title=t.title, blocking=False,
                                         status="draft", persona=name, severity=f.get("severity"))
                self.store.invalidate_tasks()
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
            st.pop("pending_feedback_easy", None)
            st.pop("pending_feedback_rebase", None)
            self._transition(task, Status.CHANGES_REQUESTED, f"{name} persona review raised {len(highs)} high finding(s)")
            rep.transitions.append(f"{task.id} -> changes_requested (persona {name})")
