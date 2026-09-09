"""Compose current-head feedback without losing another completed producer's findings."""
from __future__ import annotations

from typing import Any

from ..review import feedback_from_review


def remember_pending_feedback(st: dict[str, Any], head: str) -> dict[str, str]:
    """Capture the current rendering before a producer changes its source record."""
    pending = str(st.get("pending_feedback") or "").strip()
    saved = st.get("pending_feedback_sources")
    saved = saved if isinstance(saved, dict) else {}
    if head and saved.get("head") == head and saved.get("rendered") == pending:
        parts = dict(saved.get("parts") or {})
    elif pending and head and str(st.get("head_sha") or "") == head and (
        not saved or saved.get("head") == head
    ):
        # Recognize only an exact known review rendering. An operator's edited/resolved
        # handoff is authoritative opaque text, never reconstructed from last_review.
        previous = st.get("last_review")
        review_run = str(st.get("last_review_run") or "")
        known_review = (not saved and isinstance(previous, dict) and review_run
                        and st.get("last_review_head") == head
                        and feedback_from_review(previous, run_id=review_run, source_head=head) == pending)
        parts = {"review" if known_review else "prior": pending}
    else:
        parts = {}
    return parts


def merge_pending_feedback(st: dict[str, Any], head: str, source: str, text: str) -> str:
    """Replace one producer's contribution, preserving other current contributions.

    A new review replaces only its review part. CI and individual GitHub comments keep
    separate identities across reap/restart. No historical verdict is rehydrated, so a
    supported operator action's resolved/superseded feedback remains exactly as written.
    """
    parts = remember_pending_feedback(st, head)
    text = text.strip()
    if text:
        parts[source] = text
    else:
        parts.pop(source, None)
    order = sorted(parts, key=lambda key: (key != "prior", key != "review", key == "ci", key))
    rendered = "\n\n".join(dict.fromkeys(parts[key] for key in order if parts[key]))
    st["pending_feedback_sources"] = {"head": head, "parts": parts, "rendered": rendered}
    st["pending_feedback"] = rendered
    return rendered
