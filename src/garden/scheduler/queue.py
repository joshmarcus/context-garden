"""The merge queue's state, written in one place.

Four `state.json` facts describe where a PR sits in the automerge queue: `automerge_candidate`
(it is eligible and waiting), `automerge_ready_at` (when it first became eligible, the oldest-
first order key), `merge_head` (it is the in-flight head, rebased and awaiting its rollup) and
`automerge_blocked` (why it is not merging, read by the Inbox). Every write of those four goes
through the helpers here so there is one writer per fact (CG-202); `tests/test_queue_state.py`
asserts no other module mutates them. Reads stay wherever they are needed.
"""

from __future__ import annotations

from ..model import Task, now_iso

_QUEUE_KEYS = ("automerge_candidate", "automerge_ready_at", "merge_head", "automerge_blocked")


class QueueMixin:
    def _queue_join(self, task: Task) -> None:
        """This PR became an automerge candidate: it enters the queue, ordered by when it first
        became ready, and any stale block is cleared."""
        st = self.state.get(task.id)
        st.pop("automerge_blocked", None)
        st["automerge_candidate"] = True
        st.setdefault("automerge_ready_at", now_iso())

    def _queue_head(self, task: Task, *, announce: bool = False) -> None:
        """Promote the candidate to the in-flight head (rebased, awaiting its rollup). `announce`
        emits the `merge_head` event and logs it — set it when the promotion followed a force-push
        that restarted the rollup, not when the head was already on the base's tip."""
        self.state.get(task.id)["merge_head"] = True
        if announce:
            self.events.emit("merge_head", task.id, waiting=True, reason="rebased; awaiting rollup")
            self.log(f"{task.id}: rebased before merge; in flight until its rollup is green")

    def _queue_drop_head(self, task: Task) -> bool:
        """Drop only the in-flight head marker, leaving the candidate and its order key intact
        (used when a task leaves `in_review` for a round it may return from). Returns True if the
        marker was set."""
        return self.state.get(task.id).pop("merge_head", None) is not None

    def _queue_leave(self, task: Task) -> bool:
        """Take the task off the queue cleanly (merged, no longer a candidate, PR replaced): drop
        the candidate, its order key, the head marker and any block. Returns True if anything was
        cleared."""
        st = self.state.get(task.id)
        changed = False
        for k in _QUEUE_KEYS:
            changed = st.pop(k, None) is not None or changed
        return changed

    def _queue_hold(self, task: Task, reason: str, *, keep: bool = False) -> None:
        """Record why the PR is not merging (the Inbox reads `automerge_blocked`). By default the
        task also leaves the queue; `keep=True` holds the reason without dropping the candidate or
        the head, for a transient failure the next tick should retry. Logs only when the reason
        changes, so a held task does not repeat the line every tick."""
        st = self.state.get(task.id)
        if not keep:
            st.pop("automerge_candidate", None)
            st.pop("automerge_ready_at", None)
            st.pop("merge_head", None)
        if st.get("automerge_blocked") != reason:
            st["automerge_blocked"] = reason
            self.log(f"{task.id}: automerge held: {reason}")
