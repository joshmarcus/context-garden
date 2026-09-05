"""Persona reviews of a PR or a whole phase."""

from __future__ import annotations

from typing import Any

from .. import gitops
from ..github import GitHubError, mark_garden_comment
from ..model import Phase, Status, Task, ensure_open
from ..personas import (
    parse_persona,
    phase_brief,
    pr_brief,
    report_markdown,
    report_path,
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
            st.pop("pending_feedback_easy", None)
            st.pop("pending_feedback_rebase", None)
            self._transition(task, Status.CHANGES_REQUESTED, f"{name} persona review raised {len(highs)} high finding(s)")
            rep.transitions.append(f"{task.id} -> changes_requested (persona {name})")
