"""What a worker reports besides code: discovered tasks, duplicate/cancel decisions, friction and notes."""

from __future__ import annotations

import re
from typing import Any

from ..brief import brief_gaps
from ..github import GitHubError, mark_garden_comment
from ..harness import DIFFICULTIES
from ..model import Status, Task, now_iso
from ..runs import Run

_WORD_RE = re.compile(r"[a-z0-9]+")

# A file path with an extension (bare filename, or a path with slashes), and something that
# names the symptom: backtick or quoted text, or an `XxxError`/`XxxException` identifier.
_FILE_RE = re.compile(
    r"\b(?:[\w.-]+/)+[\w.-]+\.[a-zA-Z]{1,6}\b"
    r"|\b[\w-]+\.(?:py|ts|tsx|js|jsx|md|yaml|yml|json|toml|sh|rs|go|rb|c|cpp|h|hpp|java|cfg|ini)\b",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"`([^`]+)`|\"([^\"]{4,80})\"|\b([A-Z]\w*(?:Error|Exception)\b(?:[:\s][^`\"\n.]{0,60})?)")


def _normalise_title(text: str) -> str:
    """Case- and punctuation-insensitive title for matching a discovery against an open
    task: lowercase, words only, whitespace collapsed."""
    return " ".join(_WORD_RE.findall(str(text or "").lower()))


def _finding_signature(text: str) -> tuple[set[str], set[str]]:
    """Files and symptoms (error text) named in a discovery's free-text body. A backtick or
    quoted span that turns out to just be one of the file names (agents often wrap a path in
    backticks too) doesn't count as a symptom on its own."""
    files = {m.group(0).lower() for m in _FILE_RE.finditer(text)}
    errors = set()
    for m in _ERROR_RE.finditer(text):
        val = next(g for g in m.groups() if g).strip().lower()
        if val and val not in files:
            errors.add(val)
    return files, errors


def _same_finding(a: str, b: str) -> bool:
    """Two free-text reports name the same finding if they share a file and a symptom;
    either alone (e.g. two reports touching the same file) is too weak to merge on."""
    a_files, a_errors = _finding_signature(a)
    if not a_files or not a_errors:
        return False
    b_files, b_errors = _finding_signature(b)
    return bool(a_files & b_files) and bool(a_errors & b_errors)


class DiscoveredMixin:
    # ---- discovered work ---------------------------------------------------
    def _open_discovery_candidates(self, task: Task) -> list[Task]:
        """Open (non-terminal) tasks in `task`'s own phase and the phase right after it: the
        scope a discovered item is checked against before it is filed as a new draft, so a
        finding from a later phase's work still catches a duplicate filed against the phase
        ahead of it."""
        try:
            names = [p.name for p in self.store.product(task.product).phases]
            i = names.index(task.phase)
            scope = {task.phase} | ({names[i + 1]} if i + 1 < len(names) else set())
        except (KeyError, ValueError):
            scope = {task.phase}
        return [t for t in self.store.tasks().values()
                if t.product == task.product and t.phase in scope and not t.status.terminal]

    def _match_existing_discovery(self, title: str, body: str, candidates: list[Task]) -> Task | None:
        norm = _normalise_title(title)
        for cand in candidates:
            if norm and norm == _normalise_title(cand.title):
                return cand
        if body.strip():
            for cand in candidates:
                if _same_finding(body, cand.body):
                    return cand
        return None

    def _attach_discovery(self, existing: Task, task: Task, run: Run, title: str) -> None:
        """A discovered item that matches a task already open: note who else found it on that
        task instead of filing a second draft (CG-199)."""
        existing.log(f"also found by {task.id} ({task.title}) during run `{run.run_id}`")
        self.store.save(existing)
        self.events.emit("discovered_duplicate", existing.id, found_by=task.id, run=run.run_id, title=title)
        self.log(f"{task.id}: discovery {title!r} matches open {existing.id}; noted, not filed")

    def _file_discovered(self, task: Task, run: Run, result: dict[str, Any]) -> list[Task]:
        """File a worker's discoveries. Each item carries a `kind` (default `task`):
        `task` becomes a draft task file (as before, unless it matches an already-open task
        in this phase or the next by title or by file+symptom, in which case it is noted on
        that task instead - see `_attach_discovery`); `duplicate` and `cancel` become
        decision cards for a human (they never file work); `note` goes to the phase's
        friction record and makes no card."""
        items = result.get("discovered") or []
        if not isinstance(items, list) or not items:
            return []
        auto_blocking = bool(self.cfg.get("discovered.auto_approve_blocking", True))
        candidates = self._open_discovery_candidates(task)
        created: list[Task] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "task").strip().lower()
            if kind in ("duplicate", "cancel"):
                self._record_decision(task, run, kind, item)
                continue
            if kind == "note":
                self._record_note(task, run, item)
                continue
            if not str(item.get("title") or "").strip():
                continue
            title = str(item["title"]).strip()
            body_in = str(item.get("body") or "")
            match = self._match_existing_discovery(title, body_in, candidates)
            if match is not None:
                self._attach_discovery(match, task, run, title)
                continue
            blocking = bool(item.get("blocking"))
            body = body_in.strip() or f"## Goal\n\n{title}\n"
            body += f"\n\n## Provenance\n\nDiscovered by {task.id} ({task.title}) during run `{run.run_id}`."
            diff = str(item.get("difficulty") or "medium")
            try:
                deferred = bool(self.store.phase(task.product, task.phase).frozen)
            except KeyError:
                deferred = False
            want_ready = blocking and auto_blocking and not deferred
            t = self.store.create_task(
                task.product, task.phase, title, body,
                priority=int(item.get("priority") or task.priority), reading=[str(r) for r in (item.get("reading") or [])] or list(task.reading),
                status="draft" if deferred else ("ready" if want_ready else "draft"),
                difficulty=diff if diff in DIFFICULTIES else "medium",
            )
            t.discovered_from = task.id
            # A blocking discovery normally skips the `approve` gate straight to ready; but
            # its brief must still be complete (CG-193's brief_gaps), or it dispatches a run
            # against placeholder criteria or a dead reading path. Hold it as a draft instead
            # so it shows up in the approve queue with the gap flagged, same as any other draft.
            gaps = brief_gaps(self.store, t) if want_ready else []
            if gaps:
                self._transition(t, Status.DRAFT, "held as draft: incomplete brief")
            note = f"discovered by {task.id}"
            if deferred:
                note += "; deferred by the freeze"
            elif gaps:
                note += "; blocking, but held as draft: incomplete brief (" + "; ".join(gaps) + ")"
            elif blocking:
                note += " (blocking)"
            t.log(note)
            self.store.save(t)
            candidates.append(t)
            created.append(t)
            self.events.emit("discovered", task.id, new_task=t.id, title=title, blocking=blocking, status=t.status.value)
            tag = " [blocking, ready]" if t.status == Status.READY else (" [blocking, held: incomplete brief]" if gaps else "")
            self.log(f"{task.id}: discovered {t.id} {title!r}" + tag)
        if created:
            st = self.state.get(task.id)
            st["discovered_ids"] = sorted(set(st.get("discovered_ids", []) + [t.id for t in created]))
            task.log("discovered work filed: " + ", ".join(t.id for t in created))
            self.store.invalidate_tasks()
        return created

    # ---- needs-human stops -------------------------------------------------
    def _set_needs_human(self, task: Task, kind: str, reason: str, **extra: Any) -> None:
        """Record a structured stop: which decision is being asked (kind), why, and where
        the task stood when the loop stopped — so resume_task can put it back. `extra` carries
        stop-specific fields (e.g. the base branch and its probed tip for a `base_broken` stop,
        so a later tick can tell when the base has moved and continue on its own)."""
        self.state.get(task.id)["needs_human"] = {
            "kind": kind, "reason": reason, "prior_status": task.status.value, "at": now_iso(), **extra}

    # ---- decisions (discovered work that is a choice, not a task) -----------
    def _record_decision(self, task: Task, run: Run, kind: str, item: dict[str, Any]) -> None:
        """A `duplicate`/`cancel` discovery: propose cancelling a named task. It becomes a
        pending decision card (Accept/Reject) rather than filing new work."""
        reason = str(item.get("reason") or item.get("body") or item.get("title") or "").strip()
        if kind == "duplicate":
            target = str(item.get("duplicates") or "").strip()
            of = str(item.get("of") or "").strip()
        else:
            target = str(item.get("task") or item.get("target") or "").strip()
            of = ""
        if not target:
            self.log(f"{task.id}: ignored a {kind} discovery with no target task")
            return
        tgt = self.store.tasks().get(target)
        if tgt is None:
            self.log(f"{task.id}: {kind} discovery names unknown task {target!r}; ignored")
            return
        if tgt.status.terminal:
            self.log(f"{task.id}: {kind} discovery on {target}, already {tgt.status.value}; ignored")
            return
        decisions = self.state.get("_decisions")
        for d in decisions.values():
            if isinstance(d, dict) and d.get("target") == target and d.get("status", "pending") == "pending":
                return  # a pending decision already proposes cancelling this task
        did = f"{run.run_id}-{target}"
        decisions[did] = {
            "id": did, "kind": kind, "target": target, "target_title": tgt.title, "of": of,
            "reason": reason, "proposed_by": task.id, "proposed_by_title": task.title,
            "run": run.run_id, "phase": tgt.key, "at": now_iso(), "status": "pending",
        }
        self.events.emit("decision", target, decision=did, decision_kind=kind, proposed_by=task.id,
                         of=of, reason=reason, run=run.run_id)
        self.log(f"{task.id}: {kind} decision on {target}: {reason[:80]}")

    def _record_friction(self, task: Task, run: Run, result: dict[str, Any]) -> None:
        """A worker's reported friction (the result's `friction` list): post it as one marked
        PR comment and append it to the phase's friction record. It never goes in the PR body;
        the next planning round harvests it with `garden friction`."""
        from ..friction import (
            declined_improvement_items,
            friction_comment,
            friction_items,
            record_friction,
        )

        items = friction_items(result) + declined_improvement_items(result)
        if not items:
            return
        slug = self.slug_for(task)
        number = self._pr_number(task)
        if slug and number and self.github.available:
            try:
                self.github.comment(slug, number, mark_garden_comment(friction_comment(items), run.run_id))
            except GitHubError as e:
                self.log(f"{task.id}: could not post friction comment: {e}")
        try:
            ph = self.store.phase(task.product, task.phase)
            doc = ph.path / "docs" / "friction.md"
            record_friction(doc, items, f"reported by {task.id} ({task.title}) in run {run.run_id}", now_iso()[:10])
        except KeyError:
            self.log(f"{task.id}: cannot file friction; phase {task.key} not found")
        self.events.emit("friction", task.id, run=run.run_id, phase=task.key, items=len(items))

    def _record_note(self, task: Task, run: Run, item: dict[str, Any]) -> None:
        """A `note` discovery: information for the phase's friction record, no card, no task."""
        text = str(item.get("note") or item.get("body") or item.get("title") or "").strip()
        if not text:
            return
        try:
            ph = self.store.phase(task.product, task.phase)
        except KeyError:
            self.log(f"{task.id}: cannot file a note; phase {task.key} not found")
            return
        from ..friction import append_friction_report

        doc = ph.path / "docs" / "friction.md"
        if doc.exists() and text in doc.read_text():
            return  # already recorded
        provenance = f"discovered by {task.id} ({task.title}) in run {run.run_id}"
        append_friction_report(doc, text, provenance, now_iso()[:10])
        self.events.emit("note", task.id, run=run.run_id, phase=task.key)
        self.log(f"{task.id}: filed a friction note to {self.store.rel(doc)}")

    def pending_decisions(self) -> list[dict[str, Any]]:
        decisions = self.state.get("_decisions")
        out = [d for d in decisions.values() if isinstance(d, dict) and d.get("status", "pending") == "pending"]
        return sorted(out, key=lambda d: (str(d.get("phase", "")), str(d.get("target", ""))))

    def resolve_decision(self, decision_id: str, accept: bool) -> dict[str, Any]:
        """Accept cancels the named task (recording who proposed it and from which run);
        Reject dismisses the card and logs the disagreement on the task. Either way the
        card is removed."""
        decisions = self.state.get("_decisions")
        d = decisions.get(decision_id)
        if not isinstance(d, dict) or d.get("status", "pending") != "pending":
            raise KeyError(decision_id)
        if d.get("kind") == "question":
            raise RuntimeError(f"{decision_id} is a kickoff question; use --answer/--dismiss (garden decide), not accept/reject")
        target, kind = str(d.get("target", "")), str(d.get("kind", ""))
        proposer, run_id, reason = str(d.get("proposed_by", "")), str(d.get("run", "")), str(d.get("reason", ""))
        of = str(d.get("of") or "")
        tgt = self.store.tasks().get(target)
        if accept:
            if tgt is not None and not tgt.status.terminal:
                prov = f"proposed by {proposer}"
                if kind == "duplicate" and of:
                    prov = f"duplicate of {of}, {prov}"
                note = f"cancelled by decision: {prov} (run {run_id})" + (f"; reason: {reason}" if reason else "")
                # A duplicate names the task to keep (`of`); move any dependents onto it before
                # cancelling, so they are not left permanently blocked (blockers() treats a
                # cancelled dep as unsatisfied, only DONE as satisfied).
                if kind == "duplicate" and of:
                    self._repoint_dependents(target, of)
                self.cancel(tgt, note)
        else:
            if tgt is not None:
                tgt.log(f"decision rejected: {proposer} proposed cancelling this ({kind}); kept"
                        + (f". reason given: {reason}" if reason else ""))
                self.store.save(tgt)
        self.events.emit("decision_resolved", target, decision=decision_id, decision_kind=kind,
                         proposed_by=proposer, accepted=accept)
        decisions.pop(decision_id, None)
        self.state.save()
        return d

    def _repoint_dependents(self, old: str, new: str) -> None:
        """Move every task that depends on `old` onto `new` (the retained duplicate), dropping
        the swap if it would self-reference or duplicate an existing dep. Called before a
        duplicate is cancelled so its dependents keep an unmet-until-merged edge to the task
        that is actually being done, instead of a cancelled one that never clears."""
        if not new or old == new:
            return
        tasks = self.store.tasks()
        if new not in tasks:
            return
        for t in tasks.values():
            if old not in t.depends_on or t.id == new:
                continue
            deps: list[str] = []
            for dep in t.depends_on:
                dep = new if dep == old else dep
                if dep != t.id and dep not in deps:
                    deps.append(dep)
            t.depends_on = deps
            t.log(f"dependency {old} repointed to {new} ({old} cancelled as a duplicate of {new})")
            self.store.save(t)
