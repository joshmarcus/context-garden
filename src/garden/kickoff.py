"""`garden kickoff`: before a phase starts, one planner-tier run flags what needs design,
which goals lack a definition of done, which decisions only the owner can make, and which
docs are thin or stale for the work ahead.

The run gets the phase goals, the task drafts, the previous phase's retro (if any), the
product overview, the docs the phase's tasks cite, and the walkthrough index — all inlined
as text, the same way the planner works. It returns a `GARDEN_KICKOFF:` verdict; the
scheduler (`scheduler/kickoff.py`) writes it to `<phase>/docs/kickoff.md`, files each design
topic and doc gap as a draft task, raises each question as a decision card, and appends the
goal gaps to goals.md's own '## Open' section. Nothing here opens a PR: like `garden plan`,
it writes straight to the live garden.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import Phase, join_frontmatter, now_iso, split_frontmatter
from .store import Store

KICKOFF_MARKER = "GARDEN_KICKOFF:"

KICKOFF_RULES = """\
## Your job

Before **{phase}** of product **{product}** starts taking runs, look at it the way a careful
tech lead would the morning before kickoff. You have the phase's goals, its draft tasks, the
product overview, the docs its tasks point at, the previous phase's retrospective (if any) and
a capture of the live app (if any). Do NOT modify any file and do NOT commit.

Flag four kinds of gap, and only real ones — an empty list is a fine answer for any of them:

- **Design needed**: a topic a task's acceptance criteria assume is settled but isn't (an
  interface, a data shape, a UX flow, a naming choice shared by more than one task). Name which
  tasks it blocks and, if a short spike would settle it, what that spike should produce.
- **Goals without a measurable outcome**: a goal in the phase's goals document that no task's
  acceptance criteria would prove, or that has no task at all.
- **Questions for the owner**: a decision only a human can make (a product choice, a tradeoff,
  a scope call) that a task's body currently begs rather than answers. Give the context and, if
  there are natural options, list them.
- **Docs that are thin, stale or missing**: a doc a task's reading list cites, or the product
  overview, that no longer matches the tree, or that a task will need and does not have.

End your final message with exactly one line:

  {marker} {{"design_needed": [{{"topic": "<short>", "why": "<one or two sentences>", "tasks": ["<task id>"], "spike": "<what a short spike should produce, or empty>"}}], "goals_gaps": [{{"goal": "<short>", "missing": "<what would prove it done>"}}], "questions": [{{"question": "<one sentence>", "context": "<why it matters>", "options": ["<option>"]}}], "docs": [{{"path": "<repo-relative path>", "issue": "<what is thin or stale>", "tasks": ["<task id>"]}}], "ready": true|false, "summary": "<one paragraph: is this phase ready to start, and why>"}}

The JSON must be on one line. `tasks` and `options` may be empty lists; `spike` may be `""`.
"""


def _read(p: Path | None) -> str:
    try:
        return p.read_text().strip() if p and p.exists() else ""
    except OSError:
        return ""


def kickoff_doc_path(phase: Phase) -> Path:
    return phase.path / "docs" / "kickoff.md"


def previous_phase(store: Store, phase: Phase) -> Phase | None:
    """The phase immediately before this one in the product, or None for the first phase."""
    prod = store.product(phase.product)
    names = [p.name for p in prod.phases]
    if phase.name not in names:
        return None
    idx = names.index(phase.name)
    return prod.phases[idx - 1] if idx > 0 else None


def cited_doc_paths(store: Store, phase: Phase) -> list[Path]:
    """Every existing file named in a phase task's reading list, deduplicated, in task order.
    These are "the docs the tasks cite" the design asks the kickoff to judge for staleness."""
    seen: set[Path] = set()
    out: list[Path] = []
    for t in phase.tasks:
        for r in t.reading:
            p = (store.root / r).resolve()
            if p in seen or not p.exists() or not p.is_file():
                continue
            seen.add(p)
            out.append(p)
    return out


def _task_drafts_section(phase: Phase) -> str:
    if not phase.tasks:
        return "(no draft tasks yet)"
    parts = []
    for t in sorted(phase.tasks, key=lambda t: (t.priority, t.id)):
        parts.append(f"### {t.id} [{t.status.value}] {t.title}\n\n{t.body.strip()}\n")
    return "\n".join(parts)


def kickoff_brief(store: Store, phase: Phase) -> str:
    cfg = store.config
    parts = [f"# Kickoff review: {phase.key}\n",
             KICKOFF_RULES.format(phase=phase.name, product=phase.product, marker=KICKOFF_MARKER)]
    digest = store.root / str(cfg.get("principles_digest"))
    if digest.exists():
        parts.append("## Principles (digest)\n\n" + _read(digest))
    prod = store.product(phase.product)
    if prod.overview_path:
        parts.append(f"## Product: {phase.product}\n\n" + _read(prod.overview_path))
    from .model import goals_text

    if phase.goals_path:
        parts.append(f"## Phase goals: {phase.name}\n\n" + goals_text(phase.goals_path))
    parts.append("## Draft tasks in this phase\n\n" + _task_drafts_section(phase))
    prev = previous_phase(store, phase)
    if prev is not None:
        retro_doc = prev.path / "docs" / "retro.md"
        if retro_doc.exists():
            parts.append(f"## Previous phase's retrospective ({prev.key})\n\n" + _read(retro_doc))
    docs = cited_doc_paths(store, phase)
    if docs:
        lines = [f"### {store.rel(p)}\n\n{_read(p)}\n" for p in docs]
        parts.append("## Docs the tasks cite\n\n" + "\n".join(lines))
    from .walkthrough import walkthrough_section

    section = walkthrough_section(phase)
    if section:
        parts.append(section)
    parts.append("Now output the JSON line.")
    return "\n\n".join(parts) + "\n"


def parse_kickoff(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(KICKOFF_MARKER):
            payload = line[len(KICKOFF_MARKER):].strip()
            s, e = payload.find("{"), payload.rfind("}")
            if s != -1 and e > s:
                try:
                    data = json.loads(payload[s : e + 1])
                    if isinstance(data, dict) and any(
                        k in data for k in ("design_needed", "goals_gaps", "questions", "docs", "ready", "summary")
                    ):
                        return data
                except json.JSONDecodeError:
                    continue
    return {}


def _design_section(items: list[dict[str, Any]], filed: list[dict[str, Any]]) -> str:
    if not items:
        return "_No design gaps flagged._"
    by_topic = {f["topic"]: f for f in filed if f.get("topic")}
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        topic = str(it.get("topic") or "").strip()
        if not topic:
            continue
        f = by_topic.get(topic)
        head = f"- **{topic}**" + (f" — {f['task_id']} [draft, spike]" if f and f.get("task_id") else "")
        out.append(head)
        why = str(it.get("why") or "").strip()
        if why:
            out.append(f"  - {why}")
        tasks_ref = [str(t) for t in (it.get("tasks") or [])]
        if tasks_ref:
            out.append(f"  - blocks: {', '.join(tasks_ref)}")
        spike = str(it.get("spike") or "").strip()
        if spike:
            out.append(f"  - spike: {spike}")
    return "\n".join(out)


def _docs_section(items: list[dict[str, Any]], filed: list[dict[str, Any]]) -> str:
    if not items:
        return "_No doc gaps flagged._"
    by_path = {f["path"]: f for f in filed if f.get("path")}
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        path = str(it.get("path") or "").strip()
        if not path:
            continue
        f = by_path.get(path)
        if f and f.get("task_id"):
            head = f"- **`{path}`** — {f['task_id']} [draft]"
        else:
            head = f"- **`{path}`** — noted only (no task cites it yet)"
        out.append(head)
        issue = str(it.get("issue") or "").strip()
        if issue:
            out.append(f"  - {issue}")
        tasks_ref = [str(t) for t in (it.get("tasks") or [])]
        if tasks_ref:
            out.append(f"  - needed by: {', '.join(tasks_ref)}")
    return "\n".join(out)


def _questions_section(items: list[dict[str, Any]], filed: list[dict[str, Any]]) -> str:
    if not items:
        return "_No open questions._"
    by_q = {f["question"]: f for f in filed if f.get("question")}
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        question = str(it.get("question") or "").strip()
        if not question:
            continue
        f = by_q.get(question)
        head = f"- **{question}**" + (f" — decision card `{f['decision_id']}`" if f and f.get("decision_id") else "")
        out.append(head)
        context = str(it.get("context") or "").strip()
        if context:
            out.append(f"  - {context}")
        options = [str(o) for o in (it.get("options") or [])]
        if options:
            out.append(f"  - options: {', '.join(options)}")
    return "\n".join(out)


def _goals_gaps_section(gaps: list[dict[str, Any]]) -> str:
    if not gaps:
        return "_No goal gaps flagged._"
    out = []
    for g in gaps:
        goal = str(g.get("goal") or "").strip()
        if not goal:
            continue
        missing = str(g.get("missing") or "").strip()
        out.append(f"- **{goal}**" + (f" — {missing}" if missing else "") + " (added to goals.md under '## Open')")
    return "\n".join(out) if out else "_No goal gaps flagged._"


def render_kickoff_doc(phase: Phase, data: dict[str, Any], filed_design: list[dict[str, Any]],
                       filed_docs: list[dict[str, Any]], filed_questions: list[dict[str, Any]],
                       goals_gaps: list[dict[str, Any]], difficulty: str = "", model: str = "") -> str:
    tier = f" · {difficulty} tier ({model})" if difficulty else ""
    ready = data.get("ready")
    verdict = "ready to start" if ready else "not ready yet" if ready is False else "no verdict given"
    out = [f"# Kickoff: {phase.key}", "", f"_{now_iso()}{tier}_", "", f"**Verdict:** {verdict}", ""]
    summary = str(data.get("summary", "")).strip()
    if summary:
        out += [summary, ""]
    out += ["## Design needed", "", _design_section(data.get("design_needed") or [], filed_design), ""]
    out += ["## Goals without a measurable outcome", "", _goals_gaps_section(goals_gaps), ""]
    out += ["## Questions for the owner", "", _questions_section(data.get("questions") or [], filed_questions), ""]
    out += ["## Docs that need attention", "", _docs_section(data.get("docs") or [], filed_docs), ""]
    return "\n".join(out).rstrip() + "\n"


def append_goal_gaps(phase: Phase, gaps: list[dict[str, Any]]) -> None:
    """Append goal gaps under goals.md's own '## Open' heading (created if missing), skipping
    any gap already recorded there verbatim so a re-run of the kickoff does not pile up
    duplicates."""
    lines = []
    for g in gaps:
        goal = str(g.get("goal") or "").strip()
        if not goal:
            continue
        missing = str(g.get("missing") or "").strip()
        lines.append(f"- **{goal}**: {missing}" if missing else f"- **{goal}**")
    if not lines:
        return
    goals_path = phase.goals_path or (phase.path / "goals.md")
    meta: dict[str, Any] = {}
    body = ""
    if goals_path.exists():
        try:
            meta, body = split_frontmatter(goals_path.read_text())
        except (OSError, ValueError):
            body = _read(goals_path)
    body = body.rstrip("\n")
    existing = set()
    m = re.search(r"^## Open\s*$(.*)", body, re.MULTILINE | re.DOTALL)
    if m:
        existing = {ln.strip() for ln in m.group(1).splitlines() if ln.strip().startswith("-")}
    new_lines = [ln for ln in lines if ln not in existing]
    if not new_lines:
        return
    if m:
        body = body + "\n" + "\n".join(new_lines) + "\n"
    else:
        body = body + "\n\n## Open\n\n" + "\n".join(new_lines) + "\n"
    goals_path.parent.mkdir(parents=True, exist_ok=True)
    goals_path.write_text(join_frontmatter(meta, body) if meta else body.lstrip("\n"))


def append_question_resolution(phase: Phase, question: str, status: str, answer: str) -> None:
    """Record a question's resolution (answered or dismissed) onto the kickoff document, so
    the report a person reads reflects what happened to every item it raised, not just what
    the run originally proposed."""
    path = kickoff_doc_path(phase)
    if not path.exists():
        return
    text = path.read_text()
    line = f"- **{question}** — {status}" + (f": {answer}" if answer else "")
    heading = "## Questions for the owner"
    idx = text.find(heading)
    if idx == -1:
        return
    end = text.find("\n## ", idx + len(heading))
    section_end = end if end != -1 else len(text)
    section = text[idx:section_end].rstrip("\n") + "\n" + line + "\n\n"
    path.write_text(text[:idx] + section + text[section_end:].lstrip("\n"))
