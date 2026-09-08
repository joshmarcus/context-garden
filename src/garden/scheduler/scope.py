"""Keep operator-owned changes out of worker checkouts.

Tasks may declare ``deliverables`` in frontmatter.  A deliverable with
``owner: operator`` (or a path outside the checkout) is an operator step, not
part of the worker's assignment.  The scheduler records it as a prerequisite
until an operator supplies concise, reviewable evidence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..model import Task, now_iso


class ScopeMixin:
    def operator_steps(self, task: Task) -> list[dict[str, str]]:
        """Return the non-checkout deliverables declared by a task.

        ``operator_steps`` remains a small compatibility shorthand.  New task
        authors should use ``deliverables`` so checkout ownership is explicit.
        Relative paths belong to the worker checkout; absolute paths and
        explicit operator owners never do.
        """
        raw = list(task.extra.get("operator_steps") or [])
        raw.extend(item for item in (task.extra.get("deliverables") or [])
                   if isinstance(item, dict) and self._operator_owned(item))
        steps: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                steps.append({"action": item.strip(), "path": "live configuration"})
            elif isinstance(item, dict):
                action = str(item.get("action") or item.get("description") or "").strip()
                path = str(item.get("path") or "live configuration").strip()
                if action:
                    steps.append({"action": action, "path": path})
        return steps

    @staticmethod
    def _operator_owned(item: dict[str, Any]) -> bool:
        owner = str(item.get("owner") or "").lower()
        path = str(item.get("path") or "")
        return owner == "operator" or path.startswith("/") or path.startswith("..")

    def operator_scope_ready(self, task: Task) -> bool:
        """Record a pending operator prerequisite and say whether it has proof."""
        steps = self.operator_steps(task)
        if not steps:
            return True
        fingerprint = hashlib.sha256(json.dumps(steps, sort_keys=True).encode()).hexdigest()
        st = self.state.get(task.id)
        evidence = st.get("operator_evidence")
        if isinstance(evidence, dict) and evidence.get("fingerprint") == fingerprint and evidence.get("text"):
            return True
        current = st.get("operator_scope")
        wanted = {"fingerprint": fingerprint, "steps": steps}
        if current != wanted:
            st["operator_scope"] = wanted
            self.events.emit("operator_scope", task.id, steps=steps)
            self.state.save()
        return False

    def submit_operator_evidence(self, task: Task, evidence: str) -> None:
        """Accept evidence for the current operator-owned prerequisite only."""
        text = evidence.strip()
        if not text:
            raise RuntimeError("operator evidence is required")
        if not self.operator_steps(task):
            raise RuntimeError(f"{task.id} has no operator-owned deliverable")
        if self.operator_scope_ready(task):
            return
        st = self.state.get(task.id)
        scope = dict(st.get("operator_scope") or {})
        st["operator_evidence"] = {"fingerprint": str(scope["fingerprint"]), "text": text, "at": now_iso()}
        st.pop("operator_scope", None)
        self.events.emit("operator_evidence", task.id, evidence=text[:200])
        task.log("operator supplied evidence for the live-config prerequisite; checkout work may dispatch")
        self.store.save(task)
        self.state.save()
