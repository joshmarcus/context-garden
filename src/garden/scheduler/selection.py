"""Pure worker ordering, shared by dispatch and read-only queue previews."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..graph import ready
from ..model import Status, Task


def worker_candidates(tasks: dict[str, Task], state: Any, max_revisions: int,
                      stack: bool, edit_pending: Callable[[Task], bool]) -> list[tuple[Task, str]]:
    queue = [(t, "rebase") for t in tasks.values()
             if t.status == Status.CHANGES_REQUESTED and state.get(t.id).get("rebase_pending")
             and not state.get(t.id).get("needs_human")]
    queue += [(t, "revise") for t in tasks.values()
              if t.status == Status.CHANGES_REQUESTED and state.get(t.id).get("pending_feedback")
              and not state.get(t.id).get("rebase_pending") and not state.get(t.id).get("needs_human")
              and (state.get(t.id).get("pending_feedback_rebase")
                   or int(state.get(t.id).get("revisions", 0)) < max_revisions)]
    return queue + [(t, "work") for t in ready(tasks, stack=stack) if not edit_pending(t)]
