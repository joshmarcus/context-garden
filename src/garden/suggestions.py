"""Suggestions on a task's own spec: capture, integrate, and the planner-style edit brief.

A person (on the task page, `garden suggest`, or a chat session) writes a suggestion about
the task itself — its goal, context, acceptance criteria, reading list, priority or
difficulty. It lands in a `## Suggestions` section of the task file as an unchecked item and
emits a `suggestion` event. Later an `edit` run folds the pending suggestions into the task
body and marks them integrated (`- [x]`). Nothing here touches scheduler-owned fields.

The functions that manipulate a task body are pure text; no network, no store writes, so they
stay testable offline like the rest of the model layer.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any

from .model import Task, estimate_tokens
from .store import Store

SUGGESTIONS_HEADING = "## Suggestions"
LOG_HEADING = "## Log"
EDIT_MARKER = "GARDEN_EDIT:"

APPLIES_TO = ("goal", "context", "acceptance", "reading", "priority", "difficulty", "anything")


@dataclass
class Suggestion:
    text: str
    author: str = ""
    date: str = ""
    applies_to: str = ""
    integrated: bool = False
    raw: str = ""


# ---- body sections ---------------------------------------------------------
def _section_span(body: str, heading: str) -> tuple[int, int, int] | None:
    """(heading_start, content_start, end) char offsets for a level-2 section, or None.
    `end` is the start of the next `## ` heading or the end of the body."""
    m = re.search(rf"(?m)^{re.escape(heading)}[ \t]*$", body)
    if not m:
        return None
    content_start = m.end()
    nxt = re.search(r"(?m)^## ", body[content_start:])
    end = content_start + nxt.start() if nxt else len(body)
    return (m.start(), content_start, end)


def decompose(body: str) -> tuple[str, list[str], str]:
    """Split a task body into (spec, suggestion_lines, log_content).

    The spec is everything before the garden-managed `## Suggestions` / `## Log` sections."""
    sug = _section_span(body, SUGGESTIONS_HEADING)
    log = _section_span(body, LOG_HEADING)
    starts = [s[0] for s in (sug, log) if s]
    spec_end = min(starts) if starts else len(body)
    spec = body[:spec_end].rstrip()
    sug_lines: list[str] = []
    if sug:
        sug_lines = [ln.rstrip() for ln in body[sug[1]:sug[2]].splitlines() if ln.strip().startswith("- ")]
    log_content = body[log[1]:log[2]].strip("\n") if log else ""
    return spec, sug_lines, log_content


def compose(spec: str, sug_lines: list[str], log_content: str) -> str:
    """Rebuild a body with the managed sections after the spec, `## Log` always last."""
    parts = [spec.rstrip()]
    if sug_lines:
        parts.append(SUGGESTIONS_HEADING + "\n\n" + "\n".join(sug_lines))
    if log_content:
        parts.append(LOG_HEADING + "\n\n" + log_content.strip("\n"))
    return "\n\n".join(p for p in parts if p) + "\n"


def spec_body(body: str) -> str:
    """The task's spec: goal/context/acceptance/etc, without the managed sections."""
    return decompose(body)[0]


def set_spec_body(body: str, new_spec: str) -> str:
    """Replace the spec, keeping the existing `## Suggestions` and `## Log` sections."""
    _, sug_lines, log_content = decompose(body)
    return compose(new_spec, sug_lines, log_content)


# ---- suggestions -----------------------------------------------------------
def add_suggestion(body: str, text: str, author: str, date: str, applies_to: str = "") -> str:
    """Append an unchecked suggestion line to the `## Suggestions` section."""
    text = " ".join(text.strip().split())
    applies = f" (applies to {applies_to.strip()})" if applies_to.strip() and applies_to.strip() != "anything" else ""
    line = f"- [ ] {date} {author or 'someone'}{applies}: {text}"
    spec, sug_lines, log_content = decompose(body)
    sug_lines.append(line)
    return compose(spec, sug_lines, log_content)


def parse_suggestions(body: str) -> list[Suggestion]:
    _, sug_lines, _ = decompose(body)
    out: list[Suggestion] = []
    for ln in sug_lines:
        m = re.match(r"^- \[([ xX])\]\s*(.*)$", ln)
        if m:
            integrated = m.group(1).lower() == "x"
            rest = m.group(2)
        else:
            integrated = False
            rest = ln[2:].strip()
        date = author = applies = ""
        text = rest
        mm = re.match(r"^(\S+)\s+(.*?):\s*(.*)$", rest)
        if mm:
            date, who, text = mm.group(1), mm.group(2), mm.group(3)
            am = re.match(r"^(.*?)\s*\(applies to ([^)]*)\)\s*$", who)
            if am:
                author, applies = am.group(1).strip(), am.group(2).strip()
            else:
                author = who.strip()
        out.append(Suggestion(text=text, author=author, date=date, applies_to=applies, integrated=integrated, raw=ln))
    return out


def pending_suggestions(body: str) -> list[Suggestion]:
    return [s for s in parse_suggestions(body) if not s.integrated]


def has_pending(body: str) -> bool:
    return bool(pending_suggestions(body))


def mark_all_integrated(body: str) -> tuple[str, int]:
    """Mark every pending suggestion `- [x]`. Returns the new body and how many were marked."""
    spec, sug_lines, log_content = decompose(body)
    count = 0
    new_lines: list[str] = []
    for ln in sug_lines:
        m = re.match(r"^- \[( )\]\s*(.*)$", ln)
        if m:
            new_lines.append("- [x] " + m.group(2))
            count += 1
        elif re.match(r"^- (?!\[)", ln):  # a bare "- ..." line: treat as pending
            new_lines.append("- [x] " + ln[2:].lstrip())
            count += 1
        else:
            new_lines.append(ln)
    return compose(spec, new_lines, log_content), count


# ---- capture ---------------------------------------------------------------
def record_suggestion(store: Store, task: Task, text: str, *, author: str, applies_to: str = "", date: str | None = None) -> Suggestion:
    """Append a suggestion to the task file, emit a `suggestion` event, and return it.

    Shared by `garden suggest` and the web form so both land the same line and event."""
    from .events import EventLog

    date = date or dt.date.today().isoformat()
    task.body = add_suggestion(task.body, text, author, date, applies_to)
    store.save(task)
    EventLog(store.config.garden_dir / "events.jsonl").emit(
        "suggestion", task.id, author=author or "someone", applies_to=applies_to, text=" ".join(text.strip().split())[:200])
    return Suggestion(text=text.strip(), author=author, date=date, applies_to=applies_to, integrated=False)


# ---- the edit brief --------------------------------------------------------
EDIT_INSTRUCTIONS = """\
You are editing a single task in a context garden. Fold the human suggestions below into the
task body, keeping everything else intact. This is a text-only edit: do not use tools, do not
touch any files, and do not widen the task or invent new work — only reflect what the
suggestions ask for.

Rules:
- Rewrite the body so each suggestion is reflected in the right place (## Goal, ## Context,
  ## Acceptance criteria, ## Out of scope). Preserve the existing structure and any content the
  suggestions do not touch. Keep the wording tight and plain.
- Do NOT include a ## Suggestions or ## Log section in the body you return; the garden manages those.
- Propose a new `priority` (1-5, 1 highest) or `difficulty` (easy|medium|hard) only if a
  suggestion asks for it; otherwise return the current values.
- Add to `reading` only if a suggestion names files to read; otherwise return the current list.
- Output ONLY one line, no prose and no code fences:

  {marker} {{"body": "<full revised markdown body>", "priority": <int>, "difficulty": "easy|medium|hard", "reading": ["<path>", ...], "summary": "<one sentence on what changed>"}}

The JSON must be on one line.
"""


def edit_brief(store: Store, task: Task, suggestions: list[Suggestion]) -> str:
    spec = spec_body(task.body)
    sug_text = "\n".join(
        f"- {s.text}" + (f" (applies to {s.applies_to})" if s.applies_to else "") for s in suggestions
    ) or "- (none)"
    reading = ", ".join(task.reading) or "(none)"
    parts = [
        f"# Integrate suggestions into task {task.id} ({task.title})\n",
        EDIT_INSTRUCTIONS.format(marker=EDIT_MARKER),
        "## Current task body\n\n" + (spec.strip() or "(empty)") + "\n",
        f"## Current metadata\n\n- priority: {task.priority}\n- difficulty: {task.difficulty}\n- reading: {reading}\n",
        "## Suggestions to fold in\n\n" + sug_text + "\n",
        "Now output the JSON object.",
    ]
    return "\n\n".join(parts) + "\n"


def parse_edit(text: str) -> dict[str, Any]:
    """Find the trailing GARDEN_EDIT line and return the revised-body object, or {}."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(EDIT_MARKER):
            payload = line[len(EDIT_MARKER):].strip()
            s, e = payload.find("{"), payload.rfind("}")
            if s != -1 and e > s:
                try:
                    data = json.loads(payload[s : e + 1])
                    if isinstance(data, dict) and str(data.get("body") or "").strip():
                        return data
                except json.JSONDecodeError:
                    continue
    return {}


def edit_prompt_tokens(text: str) -> int:
    return estimate_tokens(text)
