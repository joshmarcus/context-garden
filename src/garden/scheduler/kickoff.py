"""`garden kickoff`: dispatch the one-run planning review, then on reap file its findings
straight into the live garden (draft tasks, decision cards, goals.md) — the same way `garden
plan` writes, unlike the retro, which opens a PR. See `garden.kickoff` for the brief and the
document rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import gitops
from ..kickoff import (
    KICKOFF_MARKER,
    append_goal_gaps,
    append_question_resolution,
    kickoff_brief,
    kickoff_doc_path,
    parse_kickoff,
    render_kickoff_doc,
)
from ..model import Phase, Task, now_iso
from .report import TickReport


class KickoffMixin:
    # ---- kickoff: dispatch, reap, file --------------------------------------
    def has_kickoff(self, phase: Phase) -> bool:
        return kickoff_doc_path(phase).exists()

    def kickoff_pending(self, phase_key: str) -> bool:
        return any(e.get("kind") == "kickoff" and f"{e.get('product')}/{e.get('phase')}" == phase_key
                   for e in self._aux_list())

    def start_kickoff(self, phase: Phase) -> Any:
        if self.kickoff_pending(phase.key):
            raise RuntimeError(f"{phase.key} already has a kickoff run in flight")
        product = phase.product
        probe = Task(path=self.store.root, id=f"_kickoff-{product}-{phase.name}", title="",
                     product=product, phase=phase.name)
        repo = self.repo_for(probe)
        base = self.final_base_for(probe)
        wt = self.cfg.worktree_path(f"_kickoff-{product}-{phase.name}")
        gitops.fetch(repo)
        if wt.exists():
            gitops.git("checkout", "-q", "--detach", gitops.base_ref(wt, base), cwd=wt)
        else:
            wt.parent.mkdir(parents=True, exist_ok=True)
            gitops.git("worktree", "add", "--detach", str(wt), gitops.base_ref(repo, base), cwd=repo)
        text = kickoff_brief(self.store, phase)
        difficulty = str(self.cfg.get("retro.difficulty") or "hard")
        return self.dispatch_aux("kickoff", None, text, wt, {"id": probe.id, "product": product, "phase": phase.name},
                                 harness_name=str(self.cfg.get("review.harness") or ""), difficulty=difficulty)

    # ---- filing what the review raised ---------------------------------------
    def _file_kickoff_design(self, phase: Phase, item: dict[str, Any]) -> dict[str, Any]:
        topic = str(item.get("topic") or "").strip()
        if not topic:
            return {}
        why = str(item.get("why") or "").strip()
        body = f"## Goal\n\n{why or topic}\n"
        spike = str(item.get("spike") or "").strip()
        if spike:
            body += f"\n## Suggested spike\n\n{spike}\n"
        tasks_ref = [str(t) for t in (item.get("tasks") or [])]
        body += f"\n## Context\n\nRaised at the {phase.key} kickoff."
        if tasks_ref:
            body += " Relevant to " + ", ".join(tasks_ref) + "."
        body += "\n"
        t = self.store.create_task(phase.product, phase.name, f"Spike: {topic}", body,
                                   priority=2, difficulty="hard", status="draft",
                                   discovered_from=f"kickoff:{phase.key}")
        t.extra["spike"] = True
        self.store.save(t)
        return {"topic": topic, "task_id": t.id}

    def _file_kickoff_doc(self, phase: Phase, item: dict[str, Any]) -> dict[str, Any]:
        path = str(item.get("path") or "").strip()
        issue = str(item.get("issue") or "").strip()
        if not path or not issue:
            return {}
        tasks_ref = [str(t) for t in (item.get("tasks") or [])]
        if not tasks_ref:
            # trivial: no task needs it yet, so the report itself is the record; no draft.
            return {"path": path, "issue": issue, "task_id": ""}
        body = (f"## Goal\n\nUpdate `{path}`: {issue}\n\n"
                f"## Context\n\nRaised at the {phase.key} kickoff; needed by " + ", ".join(tasks_ref) + ".\n")
        t = self.store.create_task(phase.product, phase.name, f"Update docs: {path}", body,
                                   priority=3, difficulty="easy", status="draft",
                                   discovered_from=f"kickoff:{phase.key}")
        return {"path": path, "issue": issue, "task_id": t.id}

    def _file_question_decision(self, phase: Phase, item: dict[str, Any], idx: int, run_id: str,
                                source: str, next_phase: str = "") -> dict[str, Any]:
        """File one question as a pending decision card. Shared by the kickoff (CG-224) and the
        retro (CG-225): `source` ("kickoff" or "retro") says which review raised it and which
        document its eventual answer/dismissal is recorded onto; `next_phase` (retro only) is
        where a resolution's '## Decisions' line lands, once that phase exists."""
        question = str(item.get("question") or "").strip()
        if not question:
            return {}
        decisions = self.state.get("_decisions")
        did = f"{run_id}-q{idx}"
        decisions[did] = {
            "id": did, "kind": "question", "target": "", "target_title": question[:80],
            "phase": phase.key, "question": question, "context": str(item.get("context") or "").strip(),
            "options": [str(o) for o in (item.get("options") or [])], "proposed_by": f"{source}:{phase.key}",
            "reason": "", "run": run_id, "at": now_iso(), "status": "pending",
            "discovered_from": f"{source}:{phase.key}", "source": source,
            "blocking": bool(item.get("blocking")), "next_phase": next_phase,
        }
        self.events.emit("decision", "", decision=did, decision_kind="question", phase=phase.key, run=run_id, source=source)
        return {"question": question, "decision_id": did}

    def _file_kickoff_question(self, phase: Phase, item: dict[str, Any], idx: int, run_id: str) -> dict[str, Any]:
        return self._file_question_decision(phase, item, idx, run_id, "kickoff")

    def answer_kickoff_question(self, decision_id: str, answer: str, by: str = "cli") -> dict[str, Any]:
        d = self._pop_kickoff_question(decision_id)
        self._resolve_question_doc(d, "answered", answer.strip(), by)
        self.events.emit("decision_resolved", "", decision=decision_id, decision_kind="question", accepted=True)
        self.state.save()
        return d

    def dismiss_kickoff_question(self, decision_id: str, by: str = "cli") -> dict[str, Any]:
        d = self._pop_kickoff_question(decision_id)
        self._resolve_question_doc(d, "dismissed", "", by)
        self.events.emit("decision_resolved", "", decision=decision_id, decision_kind="question", accepted=False)
        self.state.save()
        return d

    def _resolve_question_doc(self, d: dict[str, Any], status: str, answer: str, by: str) -> None:
        """Record a question's resolution onto the document its source writes to: the kickoff's
        own doc (next to the question, as before), or the retro's docs/retro.md '## Answers'
        section plus the next phase's goals.md '## Decisions' section (CG-225)."""
        phase = self._phase_of_decision(d)
        if phase is None:
            return
        if str(d.get("source") or "kickoff") == "retro":
            from ..retro import append_goals_decision, append_retro_answer

            at = now_iso()
            append_retro_answer(phase, str(d["question"]), status, answer, by, at)
            next_phase = self._next_phase_of_decision(d)
            if next_phase is not None:
                append_goals_decision(next_phase, str(d["question"]), status, answer, by, at)
        else:
            append_question_resolution(phase, str(d["question"]), status, answer)

    def _pop_kickoff_question(self, decision_id: str) -> dict[str, Any]:
        decisions = self.state.get("_decisions")
        d = decisions.get(decision_id)
        if not isinstance(d, dict) or d.get("kind") != "question" or d.get("status", "pending") != "pending":
            raise KeyError(decision_id)
        decisions.pop(decision_id, None)
        return d

    def _phase_of_decision(self, d: dict[str, Any]) -> Phase | None:
        product, _, name = str(d.get("phase") or "").partition("/")
        if not product or not name:
            return None
        try:
            return self.store.phase(product, name)
        except KeyError:
            return None

    def _next_phase_of_decision(self, d: dict[str, Any]) -> Phase | None:
        """The retro's next phase named on a question decision, if it exists on disk yet (it
        may not: the retro's own PR, which drafts that phase's goals.md, may not have merged)."""
        product, _, _ = str(d.get("phase") or "").partition("/")
        next_name = str(d.get("next_phase") or "")
        if not product or not next_name:
            return None
        try:
            return self.store.phase(product, next_name)
        except KeyError:
            return None

    def file_kickoff(self, phase: Phase, data: dict[str, Any], run_id: str, difficulty: str = "", model: str = "") -> Path:
        """File everything a kickoff verdict raised — draft tasks for design gaps and doc
        gaps, decision cards for questions, goal gaps appended to goals.md — and write the
        report. Shared by the async dispatch (`_finish_kickoff`) and `run_kickoff_now`'s
        synchronous call, so `garden kickoff`/the phase-page button and `garden plan`'s
        run-it-first default land the same result."""
        filed_design = [f for f in (self._file_kickoff_design(phase, it)
                                    for it in data.get("design_needed") or [] if isinstance(it, dict)) if f]
        filed_docs = [f for f in (self._file_kickoff_doc(phase, it)
                                  for it in data.get("docs") or [] if isinstance(it, dict)) if f]
        filed_questions = [f for f in (self._file_kickoff_question(phase, it, i, run_id)
                                       for i, it in enumerate(data.get("questions") or []) if isinstance(it, dict)) if f]
        goals_gaps = [g for g in data.get("goals_gaps") or [] if isinstance(g, dict) and str(g.get("goal") or "").strip()]
        if goals_gaps:
            append_goal_gaps(phase, goals_gaps)
        path = kickoff_doc_path(phase)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_kickoff_doc(phase, data, filed_design, filed_docs, filed_questions, goals_gaps,
                                           difficulty=difficulty, model=model))
        self.store.invalidate()
        self.events.emit("kickoff_done", "", phase=phase.key, run=run_id, ready=bool(data.get("ready")))
        return path

    def run_kickoff_now(self, phase: Phase) -> Path:
        """The synchronous path: one blocking harness call, like the planner's own
        `run_planner`. Used by `garden plan`'s "run the kickoff first" default, where the CLI
        is already waiting on one model call and a second tick-bound dispatch would just make
        the person run `garden tick` twice for no reason."""
        from ..planner import run_planner

        text = kickoff_brief(self.store, phase)
        harness_name = str(self.cfg.get("review.harness") or "")
        difficulty = str(self.cfg.get("retro.difficulty") or "hard")
        raw = run_planner(self.store, text, harness_name=harness_name, difficulty=difficulty)
        data = parse_kickoff(raw)
        if not data:
            raise RuntimeError(f"kickoff for {phase.key} produced no {KICKOFF_MARKER} verdict")
        model = self.cfg.harness(harness_name or str(self.cfg.get("harness") or "claude")).model_for(difficulty)
        run_id = f"plan-{now_iso()}"
        return self.file_kickoff(phase, data, run_id, difficulty, model)

    def _finish_kickoff(self, entry: dict[str, Any], run: Any, final: str, rep: TickReport) -> None:
        phase = self.store.phase(str(entry["product"]), str(entry["phase"]))
        data = parse_kickoff(final)
        if not data:
            rep.errors.append(f"kickoff {phase.key}: no verdict ({run.error[:100] or 'see final.md'})")
            return
        path = self.file_kickoff(phase, data, run.run_id, run.difficulty, run.model)
        rep.transitions.append(f"kickoff {phase.key} -> {self.store.rel(path)}")
