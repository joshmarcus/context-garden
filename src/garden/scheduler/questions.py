"""Shared kickoff and retrospective question decision cards."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import gitops
from ..kickoff import append_question_resolution
from ..model import Phase, now_iso


def append_resolution(path: Path, heading: str, decision: dict[str, Any]) -> None:
    """Append once under a level-two heading, preserving the rest of the document."""
    text = path.read_text()
    marker = f"<!-- decision:{decision['id']} -->"
    if marker in text:
        return
    line = (f"- **{decision['question']}** — {decision['status']}"
            + (f": {decision['answer']}" if decision.get("answer") else "")
            + f" ({decision['resolved_by']}, {decision['resolved_at']}) {marker}\n")
    match = re.search(rf"(?m)^{re.escape(heading)}\s*$", text)
    if match:
        following = re.search(r"(?m)^## ", text[match.end():])
        end = match.end() + following.start() if following else len(text)
        text = text[:end].rstrip() + "\n\n" + line + "\n" + text[end:]
    else:
        text = text.rstrip() + "\n\n" + heading + "\n\n" + line
    path.write_text(text)


class QuestionsMixin:
    def _file_question(self, phase: Phase, item: dict[str, Any], idx: int, run_id: str,
                       source: str = "kickoff", **extra: Any) -> dict[str, Any]:
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
            "discovered_from": f"{source}:{phase.key}", "source": source, **extra,
        }
        self.events.emit("decision", "", decision=did, decision_kind="question", phase=phase.key, run=run_id)
        return {"question": question, "decision_id": did}

    def answer_question(self, decision_id: str, answer: str, by: str = "cli") -> dict[str, Any]:
        if not answer.strip():
            raise RuntimeError("an answer must not be empty")
        return self._resolve_question(decision_id, answer.strip(), by)

    def dismiss_question(self, decision_id: str, by: str = "cli") -> dict[str, Any]:
        return self._resolve_question(decision_id, "", by)

    # Compatibility for integrations using the original kickoff API.
    answer_kickoff_question = answer_question
    dismiss_kickoff_question = dismiss_question

    def _resolve_question(self, decision_id: str, answer: str, by: str) -> dict[str, Any]:
        decisions = self.state.get("_decisions")
        d = decisions.get(decision_id)
        if not isinstance(d, dict) or d.get("kind") != "question" or d.get("status", "pending") != "pending":
            raise KeyError(decision_id)
        if not answer and d.get("blocking"):
            raise RuntimeError("this blocking retro question must be answered before deciding the verdict")
        resolved = d.get("resolution_attempt") or {**d, "status": "answered" if answer else "dismissed", "answer": answer,
                    "resolved_by": by, "resolved_at": now_iso()}
        product, _, name = str(d.get("phase") or "").partition("/")
        phase = self.store.phase(product, name)
        if d.get("source") == "retro":
            if resolved["answer"] != answer:
                raise RuntimeError("retry the original answer to finish publishing it")
            d["resolution_attempt"] = resolved
            self.state.save()
            self._write_retro_answer(resolved)
            decisions[decision_id] = resolved
        else:
            append_question_resolution(phase, str(d["question"]), resolved["status"], answer)
            decisions.pop(decision_id)
        self.events.emit("decision_resolved", "", decision=decision_id, decision_kind="question",
                         accepted=bool(answer), by=by, answer=answer, phase=phase.key)
        self.store.invalidate()
        self.state.save()
        return resolved

    def _write_retro_answer(self, d: dict[str, Any]) -> None:
        """Before the retro lands, amend its branch; afterwards edit the live documents."""
        rels = [Path(d["retro_path"]), Path(d["goals_path"])]
        live = (all((self.store.root / rel).exists() for rel in rels)
                and f"<!-- retro-run:{d['run']} -->" in (self.store.root / rels[0]).read_text())
        root = self.store.root if live else Path(d["worktree"])
        paths = [root / rel for rel in rels]
        if not all(p.is_file() for p in paths):
            raise RuntimeError("retro documents are unavailable; restore the retro worktree before answering")
        append_resolution(paths[0], "## Answers", d)
        append_resolution(paths[1], "## Decisions", d)
        if not live:
            try:
                gitops.commit_all(root, f"garden decide: resolve {d['id']}")
                gitops.push(root, d["branch"], base=d["base"])
            except gitops.GitError as e:
                raise RuntimeError(f"could not publish the retro answer: {e}") from e

    def retro_questions(self, phase_key: str) -> list[dict[str, Any]]:
        return [dict(d) for d in self.state.get("_decisions").values()
                if isinstance(d, dict) and d.get("source") == "retro" and d.get("phase") == phase_key]
