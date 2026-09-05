"""`garden retro`: run a phase's retrospective as one process, not a pile of pieces.

The retro harvests the ## Friction sections from the phase's PR bodies, runs (or reuses)
the persona reviews of the phase's body of work, then has one agent reconcile every
friction item against what actually merged — still true, fixed by which task, outdated or
disputed — with the evidence. It writes one retro document plus a draft of the next
phase's goals. The result arrives as a PR to the garden's own repo (a `self: true`
product); nothing edits the live garden directly (see docs/architecture.md).

The reconciliation is the one model call the retro adds on top of `garden friction` and
`garden persona-review`: it carries every friction source (the PR-body friction harvested
per task, the phase's own '## Reported' record, and friction still sitting in marked PR
comments), the persona reports, the phase's task list with statuses and the merged PR
titles, and returns a one-line `GARDEN_RETRO:` verdict list. The document and the
next-goals draft are rendered from that verdict list deterministically here, so the
verdicts are testable without a live model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import Phase, join_frontmatter, now_iso, slugify, split_frontmatter
from .store import Store

RETRO_MARKER = "GARDEN_RETRO:"

# The verdict each friction item is reconciled to, and how it reads in the document.
VERDICTS: dict[str, str] = {
    "still_true": "still true",
    "fixed": "fixed",
    "outdated": "outdated",
    "disputed": "disputed",
}

# The three ways a retro can end for the phase, and how each reads in the document.
PHASE_VERDICTS: dict[str, str] = {
    "close": "Close",
    "close_with_followups": "Close with follow-ups",
    "reopen": "Reopen",
}


def normalize_verdict(v: Any) -> str:
    """The phase verdict, canonicalised; '' when the reconciliation named none (close-phase
    warns in that case). Accepts the shorthand `followups` for `close_with_followups`."""
    s = str(v or "").strip().lower().replace("-", "_")
    if s in PHASE_VERDICTS:
        return s
    if s in ("followups", "close_followups", "close_with_follow_ups", "with_followups"):
        return "close_with_followups"
    return ""


RECONCILE_RULES = """\
## Your job

You are running the retrospective for phase **{phase}** of product **{product}**. Everything
you need is below: the friction workers reported in their PR bodies, friction already
recorded in the phase's '## Reported' log, friction still sitting in marked PR comments,
the persona reviews of the phase's body of work, the phase's task list with its final
statuses, and the titles of the pull requests that merged.

Friction logged by a worker is a snapshot in time: "this worktree has no venv" may have been
true at 21:05 and fixed by 22:06. A retro that quotes those as open findings misleads the
next phase. So reconcile every friction item against what actually merged. For each item,
decide a verdict and give the evidence:

- `still_true` — the friction is still real; nothing merged that resolves it.
- `fixed` — a merged task resolved it; name the task/PR id in `pr`.
- `outdated` — it was a transient snapshot (a mid-run environment state) that no longer holds.
- `disputed` — it was wrong, or the reviewers disagree it is friction.

Then draft the next phase's goals from what is *still true* plus what the personas raised.

Also rank five to eight features for the next phase, drawn from the product-manager persona's
report (if one ran) and from what the other personas raised. Each feature needs a title short
enough to be a task's title, a body giving the user value, why now, the size (easy, medium,
hard) and what it depends on, and a one-sentence rationale. If a feature duplicates a task that
already exists (check the phase task list and the titles of tasks in other phases, given to you
as part of the persona reports and task list above), say so by putting that task's id in
`duplicate_of` instead of proposing it again; do not guess an id that is not shown to you.

Also raise any questions only the owner can answer — a product choice, a tradeoff, a scope call
that the friction or the personas surfaced but that you cannot settle yourself. Give the context
and, if there are natural options, list them. Mark a question `blocking: true` only when the
phase's verdict should not be accepted until the owner has answered it; leave it `false` for
anything that can simply wait for a reply.

Finally, end the retro with a verdict on the phase itself — the one decision the whole
retrospective is for:

- `close` — nothing is left; the phase can join the herbarium as it stands.
- `close_with_followups` — nothing *blocks* closing, but there is work worth carrying into the
  next phase. List it under `followups`; each becomes a draft task in the next phase.
- `reopen` — some work must land in *this* phase before it can close. List it under `blocking`;
  each becomes a task in this phase (with a freeze exception so it still dispatches) and
  `garden close-phase` refuses until each is done or cancelled. Give the reason each item
  blocks. Reserve this for real blockers, not nice-to-haves.

Do NOT edit any file and do NOT commit: the retro document, the next-goals draft, the verdict
and the filed tasks are all written for you from your report. Just report it.

End your final message with exactly one line:

  {marker} {{"reconciliation": [{{"item": "<one friction item, short>", "logged": "<task id that logged it, or empty>", "pr": "<task/PR id that fixed it, or empty>", "verdict": "still_true" | "fixed" | "outdated" | "disputed", "evidence": "<why, one sentence>"}}], "summary": "<what changed this phase, one paragraph>", "personas": "<what the personas said, one paragraph>", "still_open": ["<what is still open, one per item>"], "features": [{{"title": "<short, could be a task title>", "body": "<markdown: user value, why now, size, dependencies>", "difficulty": "easy" | "medium" | "hard", "priority": <1-5, 1 highest>, "rationale": "<why this, why now, one sentence>", "duplicate_of": "<existing task id, or empty>"}}], "questions": [{{"question": "<one sentence>", "context": "<why it matters>", "options": ["<option>"], "blocking": true|false}}], "verdict": "close" | "close_with_followups" | "reopen", "followups": [{{"title": "<short>", "body": "<markdown>", "difficulty": "easy" | "medium" | "hard", "priority": <1-5>}}], "blocking": [{{"title": "<short>", "body": "<markdown>", "difficulty": "easy" | "medium" | "hard", "priority": <1-5>, "reason": "<why this blocks closing>"}}], "next_goals": "<markdown body for the next phase's goals draft>"}}

The JSON must be on one line.
"""


def next_phase_name(name: str) -> str:
    """`phase-02-friction` -> `phase-03`. If the name has no leading number, append `-next`."""
    m = re.match(r"^(.*?)(\d+)(.*)$", name)
    if not m:
        return f"{name}-next"
    prefix, num, _rest = m.groups()
    width = len(num)
    return f"{prefix}{int(num) + 1:0{width}d}"


def _read(p: Path | None) -> str:
    try:
        return p.read_text().strip() if p and p.exists() else ""
    except OSError:
        return ""


def persona_reports(phase: Phase, names: list[str]) -> dict[str, Path]:
    """The latest report file on disk for each named persona, from <phase>/docs/reviews/.
    Reports are named `<persona-slug>-<date>[-n].md` (see personas.report_path)."""
    d = phase.path / "docs" / "reviews"
    out: dict[str, Path] = {}
    if not d.exists():
        return out
    for name in names:
        slug = slugify(name)
        matches = sorted(p for p in d.glob(f"{slug}-*.md") if re.match(rf"^{re.escape(slug)}-\d", p.name))
        if matches:
            out[name] = matches[-1]
    return out


def reconcile_brief(store: Store, phase: Phase, base: str, friction: list[tuple[Any, str, str]],
                    reported: str, comment_friction: list[tuple[Any, list[str]]],
                    reports: dict[str, Path], task_rows: list[dict[str, Any]], merged_prs: list[dict[str, Any]],
                    next_phase: str) -> str:
    cfg = store.config
    parts = [f"# Retrospective: {phase.key}\n",
             RECONCILE_RULES.format(phase=phase.name, product=phase.product, marker=RETRO_MARKER)]
    digest = store.root / str(cfg.get("principles_digest"))
    if digest.exists():
        parts.append("## Principles (digest)\n\n" + _read(digest))
    if phase.goals_path:
        parts.append("## Phase goals\n\n" + _read(phase.goals_path))

    if friction:
        lines = []
        for task, pr_url, text in friction:
            head = f"### {task.id}: {task.title}"
            if pr_url:
                head += f"  (PR {pr_url})"
            lines.append(head + "\n\n" + text.strip())
        parts.append("## Harvested friction (from PR bodies)\n\n" + "\n\n".join(lines))
    else:
        parts.append("## Harvested friction (from PR bodies)\n\n(none)")

    parts.append("## Reported friction (friction.md '## Reported' log)\n\n" + (reported.strip() or "(none)"))

    if comment_friction:
        lines = []
        for task, items in comment_friction:
            head = f"### {task.id}"
            if getattr(task, "pr", ""):
                head += f"  (PR {task.pr})"
            lines.append(head + "\n\n" + "\n".join(f"- {i}" for i in items))
        parts.append("## Friction reported in PR comments\n\n" + "\n\n".join(lines))
    else:
        parts.append("## Friction reported in PR comments\n\n(none)")

    if reports:
        lines = []
        for name, path in reports.items():
            lines.append(f"### Persona: {name} ({store.rel(path)})\n\n" + _read(path))
        parts.append("## Persona reviews\n\n" + "\n\n".join(lines))
    else:
        parts.append("## Persona reviews\n\n(none)")

    rows = "\n".join(f"- {r['id']} [{r['status']}] {r['title']}" for r in task_rows) or "(none)"
    parts.append("## Phase task list with statuses\n\n" + rows)

    prs = "\n".join(f"- {p['id']} — {p['title']} ({p['pr']})" for p in merged_prs) or "(none)"
    parts.append("## Merged pull requests\n\n" + prs)

    parts.append(f"## Next phase\n\nDraft the goals for **{next_phase}** of **{phase.product}**.")
    return "\n\n".join(parts) + "\n"


def parse_retro(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(RETRO_MARKER):
            payload = line[len(RETRO_MARKER):].strip()
            s, e = payload.find("{"), payload.rfind("}")
            if s != -1 and e > s:
                try:
                    data = json.loads(payload[s : e + 1])
                    if isinstance(data, dict) and "reconciliation" in data:
                        return data
                except json.JSONDecodeError:
                    continue
    return {}


def numbers_section(worker_cost_usd: float, operator_cost_usd: float) -> str:
    """The phase's spend, workers against the operator watching them (CG-223): what the
    "operator seat is a goal" decision (docs/design.md) asks every retro to report, so the
    loop's most expensive seat is compared against the workers', not guessed at."""
    total = worker_cost_usd + operator_cost_usd
    share = operator_cost_usd / total if total else None
    lines = [f"- workers: ${worker_cost_usd:.2f}",
             f"- operator: ${operator_cost_usd:.2f}" + (f" — {share:.0%} of total" if share is not None else ""),
             f"- total: ${total:.2f}"]
    return "\n".join(lines)


def _verdict_word(v: str) -> str:
    return VERDICTS.get(str(v), str(v) or "?")


def reconciliation_table(rev: dict[str, Any]) -> str:
    """A markdown table: friction item, when logged, by which PR, verdict, evidence."""
    items = [f for f in rev.get("reconciliation") or [] if isinstance(f, dict)]
    if not items:
        return "_No friction to reconcile._"
    out = ["| Friction item | Logged | Fixed by | Verdict | Evidence |",
           "|---|---|---|---|---|"]
    for f in items:
        item = str(f.get("item", "")).replace("|", "\\|").replace("\n", " ").strip()
        logged = str(f.get("logged", "") or "").strip()
        pr = str(f.get("pr", "") or "").strip()
        verdict = _verdict_word(f.get("verdict", ""))
        evidence = str(f.get("evidence", "")).replace("|", "\\|").replace("\n", " ").strip()
        out.append(f"| {item} | {logged or '–'} | {pr or '–'} | {verdict} | {evidence} |")
    return "\n".join(out)


def persona_features(sections_by_persona: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The structured `features` sections of the persona reports, as retro feature candidates
    tagged with the persona that raised them (CG-188). A persona whose `features` section is
    plain markdown (not a list of structured items) carries nothing filable and is skipped.
    Pure: no id assignment, no file I/O."""
    out: list[dict[str, Any]] = []
    for persona, sections in sections_by_persona.items():
        feats = (sections or {}).get("features")
        if not isinstance(feats, list):
            continue
        for f in feats:
            if not isinstance(f, dict) or not str(f.get("title") or "").strip():
                continue
            out.append({"title": str(f["title"]).strip(), "body": str(f.get("body") or "").strip(),
                        "difficulty": str(f.get("difficulty") or "medium").strip() or "medium",
                        "priority": f.get("priority"), "rationale": str(f.get("rationale") or "").strip(),
                        "duplicate_of": str(f.get("duplicate_of") or "").strip(), "source": persona})
    return out


def merge_features(rev: dict[str, Any], persona_feats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reconciliation's own features followed by the persona features whose title is not
    already among them (case-insensitive): the retro deduplicates across the two sources so a
    feature a persona named and the reconciliation also proposed is filed once."""
    feats = [f for f in rev.get("features") or [] if isinstance(f, dict)]
    seen = {str(f.get("title") or "").strip().lower() for f in feats}
    for pf in persona_feats:
        key = pf["title"].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        feats.append(pf)
    return feats


def resolve_features(rev: dict[str, Any], existing_titles: dict[str, str],
                     persona_feats: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Match each proposed feature against existing task titles (case-insensitive) or the
    retro's own `duplicate_of`, deciding which get filed as draft tasks. Pure: assigns no
    ids and touches no files, so it is testable without a live model or a worktree.

    `existing_titles` maps a lowercased task title to the id of the task that has it.
    `persona_feats` (from `persona_features`) are merged in and deduplicated by title.
    """
    out: list[dict[str, Any]] = []
    for f in merge_features(rev, persona_feats or []):
        if not isinstance(f, dict):
            continue
        title = str(f.get("title") or "").strip()
        if not title:
            continue
        dup_of = str(f.get("duplicate_of") or "").strip()
        title_dup = existing_titles.get(title.lower())
        if dup_of:
            reason = f"flagged by the retro as a duplicate of {dup_of}"
        elif title_dup:
            reason = f"same title as {title_dup}"
        else:
            reason = ""
        out.append({
            "title": title,
            "body": str(f.get("body") or "").strip(),
            "difficulty": str(f.get("difficulty") or "medium").strip() or "medium",
            "priority": f.get("priority"),
            "rationale": str(f.get("rationale") or "").strip(),
            "source": str(f.get("source") or "").strip(),
            "skip": bool(reason),
            "reason": reason,
        })
    return out


_SEVERITY_WEIGHT: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


def _normalize_finding_title(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def flatten_findings(persona_findings: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Every persona's findings, one entry each, stamped with who raised it. Pure: no id
    assignment, no file I/O, so a fixed dict of parsed persona reviews is enough to test it."""
    out: list[dict[str, Any]] = []
    for persona, findings in persona_findings.items():
        for f in findings or []:
            if not isinstance(f, dict) or not str(f.get("summary") or "").strip():
                continue
            out.append({"personas": [persona], "severity": str(f.get("severity") or "low"),
                        "area": str(f.get("area") or "").strip(), "summary": str(f["summary"]).strip(),
                        "suggestion": str(f.get("suggestion") or "").strip()})
    return out


def group_findings(flat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings that say the same thing across personas collapse into one group (CG-187): a
    normalised-title match ("the reconciliation's duplicate_of, or a title match"). The worst
    severity in a group wins, and every persona that raised it is kept for the task body."""
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for f in flat:
        title = _normalize_finding_title(f.get("summary"))
        if not title:
            continue
        if title not in groups:
            groups[title] = {"personas": [], "severity": f["severity"], "area": f["area"],
                             "summary": f["summary"], "suggestion": f["suggestion"]}
            order.append(title)
        g = groups[title]
        for p in f["personas"]:
            if p not in g["personas"]:
                g["personas"].append(p)
        if _SEVERITY_WEIGHT.get(f["severity"], 1) > _SEVERITY_WEIGHT.get(g["severity"], 1):
            g["severity"] = f["severity"]
        if not g["suggestion"] and f["suggestion"]:
            g["suggestion"] = f["suggestion"]
    return [groups[t] for t in order]


def resolve_findings(groups: list[dict[str, Any]], existing_titles: dict[str, str]) -> list[dict[str, Any]]:
    """Which grouped findings get filed as new draft tasks, and which are skipped because a
    task with the same title already exists. Mirrors `resolve_features`: pure, so it is
    testable without a live model or a worktree."""
    out: list[dict[str, Any]] = []
    for g in groups:
        title = str(g.get("summary") or "").strip()
        if not title:
            continue
        dup = existing_titles.get(title.lower())
        reason = f"same title as {dup}" if dup else ""
        out.append({**g, "skip": bool(reason), "reason": reason})
    return out


def findings_section(filed: list[dict[str, Any]]) -> str:
    """Render '## Findings from persona reviews': every finding, grouped by severity, with the
    task id it became (or why it was skipped) — CG-187's "the retro document lists every
    finding with the task id it became, grouped by severity"."""
    if not filed:
        return "_No findings from this phase's persona reviews._"
    out: list[str] = []
    for sev in ("high", "medium", "low"):
        items = [f for f in filed if str(f.get("severity")) == sev]
        if not items:
            continue
        out.append(f"### {sev.capitalize()}")
        out.append("")
        for f in items:
            personas = ", ".join(f.get("personas") or [])
            summary = str(f.get("summary", "")).strip()
            if f.get("task_id"):
                out.append(f"- **{personas}** — {summary} → {f['task_id']} [{f.get('status', 'draft')}]")
            else:
                out.append(f"- **{personas}** — {summary} — _skipped: {f.get('reason') or 'duplicate'}_")
        out.append("")
    return "\n".join(out).rstrip()


_NO_ANSWERS_YET = "_No answers recorded yet._"


def _questions_section(items: list[dict[str, Any]], filed: list[dict[str, Any]]) -> str:
    """Render '## Questions for the owner': one bullet per question the reconciliation raised,
    cross-referenced to the decision card it became (CG-225, mirroring the kickoff's own
    `garden.kickoff._questions_section`)."""
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
        if it.get("blocking"):
            head += " (blocks the verdict until answered)"
        out.append(head)
        context = str(it.get("context") or "").strip()
        if context:
            out.append(f"  - {context}")
        options = [str(o) for o in (it.get("options") or [])]
        if options:
            out.append(f"  - options: {', '.join(options)}")
    return "\n".join(out) if out else "_No open questions._"


def append_retro_answer(phase: Phase, question: str, status: str, answer: str, by: str, at: str) -> None:
    """Record a retro question's resolution under docs/retro.md's own '## Answers' heading, with
    who and when (CG-225). Unlike the kickoff, whose resolution note lands next to the question
    itself, the retro keeps a separate audit trail so the document reads as a record of what was
    decided after the retro, not just what was asked. A no-op until the retro's own PR has
    merged and the document exists on disk (see `garden.retro`'s module docstring: nothing here
    edits the live garden directly ahead of that)."""
    path = phase.path / "docs" / "retro.md"
    if not path.exists():
        return
    text = path.read_text()
    heading = "## Answers"
    idx = text.find(heading)
    if idx == -1:
        return
    when = f" (by {by}, {at[:10]})" if by or at else ""
    line = f"- **{question}** — {status}" + (f": {answer}" if answer else "") + when
    end = text.find("\n## ", idx + len(heading))
    section_end = end if end != -1 else len(text)
    body = text[idx + len(heading) : section_end].strip("\n")
    if body == _NO_ANSWERS_YET:
        body = ""
    new_body = (body + "\n" + line).strip("\n") if body else line
    section = heading + "\n\n" + new_body + "\n\n"
    path.write_text(text[:idx] + section + text[section_end:].lstrip("\n"))


def append_goals_decision(phase: Phase, question: str, status: str, answer: str, by: str, at: str) -> None:
    """Record a retro question's resolution under the next phase's goals.md '## Decisions'
    heading, with who and when, so `garden plan` for that phase sees it (goals_text inlines the
    whole body). A no-op if the next phase's goals.md does not exist yet on disk -- it may not
    have merged from the retro's own PR yet."""
    goals_path = phase.goals_path or (phase.path / "goals.md")
    if not goals_path.exists():
        return
    try:
        meta, body = split_frontmatter(goals_path.read_text())
    except (OSError, ValueError):
        meta, body = {}, goals_path.read_text()
    body = body.rstrip("\n")
    when = f" (by {by}, {at[:10]})" if by or at else ""
    line = f"- **{question}** — {status}" + (f": {answer}" if answer else "") + when
    heading = "## Decisions"
    m = re.search(rf"^{re.escape(heading)}\s*$(.*)", body, re.MULTILINE | re.DOTALL)
    if m and line.strip() in {ln.strip() for ln in m.group(1).splitlines()}:
        return
    if m:
        body = body + "\n" + line + "\n"
    else:
        body = body + "\n\n" + heading + "\n\n" + line + "\n"
    goals_path.write_text(join_frontmatter(meta, body) if meta else body.lstrip("\n"))


def resolve_retro_tasks(items: Any, existing_titles: dict[str, str]) -> list[dict[str, Any]]:
    """Normalise a `followups`/`blocking` list from the reconciliation into task drafts,
    flagging any whose title already belongs to a task so the scheduler can skip it. Pure:
    assigns no ids and touches no files, so it is testable without a live model or worktree.

    `existing_titles` maps a lowercased task title to the id of the task that has it. A
    `blocking` item may carry a `reason`; a `followup` will not."""
    out: list[dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        dup = existing_titles.get(title.lower())
        out.append({
            "title": title,
            "body": str(it.get("body") or "").strip(),
            "difficulty": str(it.get("difficulty") or "medium").strip() or "medium",
            "priority": it.get("priority"),
            "reason": str(it.get("reason") or "").strip(),
            "skip": bool(dup),
            "dup_reason": f"same title as {dup}" if dup else "",
        })
    return out


def verdict_section(phase: Phase, verdict: str, followups: list[dict[str, Any]] | None,
                    blocking: list[dict[str, Any]] | None, next_phase: str) -> str:
    """The retro document's `## Verdict` body: the choice, and the tasks it named by id."""
    v = normalize_verdict(verdict)
    followups = [f for f in followups or [] if f.get("task_id")]
    blocking = [b for b in blocking or [] if b.get("task_id")]
    if not v:
        return "_The reconciliation named no verdict; decide with `garden retro-decide`._"
    out = [f"**{PHASE_VERDICTS[v]}.**"]
    if v == "close":
        out.append(f"Nothing blocks closing {phase.key}; it can join the herbarium as it stands.")
    elif v == "close_with_followups":
        out.append(f"Nothing blocks closing {phase.key}. The follow-ups below become draft "
                   f"tasks in {next_phase}.")
    else:
        out.append(f"These must land before {phase.key} can close; each carries a freeze exception "
                   "so it still dispatches.")
    if followups:
        out += ["", f"Follow-ups filed in {next_phase}:"]
        out += [f"- {f['task_id']}: {f['title']}" for f in followups]
    if blocking:
        out += ["", "Blocking tasks filed in this phase:"]
        out += [f"- {b['task_id']}: {b['title']}" + (f" — {b['reason']}" if b.get('reason') else "")
                for b in blocking]
    return "\n".join(out)


def features_section(filed: list[dict[str, Any]]) -> str:
    """Render the ranked '## Features for the next phase' body: rank order is the order the
    reconciliation returned them in. Each filed feature shows its new task id; each skipped
    one shows why."""
    if not filed:
        return "_No features proposed for the next phase._"
    out = []
    for i, f in enumerate(filed, 1):
        title = str(f.get("title", "")).strip()
        if f.get("task_id"):
            head = f"{i}. **{title}** — {f['task_id']} [{f.get('status', 'draft')}]"
        else:
            head = f"{i}. **{title}** — _skipped: {f.get('reason') or 'duplicate'}_"
        out.append(head)
        difficulty = str(f.get("difficulty") or "").strip()
        if difficulty:
            out.append(f"   - size: {difficulty}")
        rationale = str(f.get("rationale") or "").strip()
        if rationale:
            out.append(f"   - why now: {rationale}")
        body = str(f.get("body") or "").strip()
        if body:
            out.append(f"   - {body}")
    return "\n".join(out)


def render_retro_doc(phase: Phase, rev: dict[str, Any], reports: dict[str, Path], store: Store,
                     filed: list[dict[str, Any]] | None = None,
                     filed_findings: list[dict[str, Any]] | None = None,
                     followups: list[dict[str, Any]] | None = None,
                     blocking: list[dict[str, Any]] | None = None,
                     filed_questions: list[dict[str, Any]] | None = None,
                     next_phase: str = "",
                     difficulty: str = "", model: str = "", numbers: str = "") -> str:
    tier = f" · {difficulty} tier ({model})" if difficulty else ""
    out = [f"# Retrospective: {phase.key}", "", f"_{now_iso()}{tier}_", ""]
    summary = str(rev.get("summary", "")).strip()
    if summary:
        out += ["## What changed", "", summary, ""]
    if numbers:
        out += ["## Numbers", "", numbers, ""]
    out += ["## Verdict", "",
            verdict_section(phase, rev.get("verdict", ""), followups, blocking,
                            next_phase or next_phase_name(phase.name)), ""]
    out += ["## Friction reconciled", "", reconciliation_table(rev), ""]
    personas = str(rev.get("personas", "")).strip()
    if personas:
        out += ["## What the personas said", "", personas, ""]
    still_open = [str(s).strip() for s in rev.get("still_open") or [] if str(s).strip()]
    if still_open:
        out += ["## Still open", ""] + [f"- {s}" for s in still_open] + [""]
    out += ["## Questions for the owner", "", _questions_section(rev.get("questions") or [], filed_questions or []), ""]
    out += ["## Answers", "", _NO_ANSWERS_YET, ""]
    out += ["## Findings from persona reviews", "", findings_section(filed_findings or []), ""]
    out += ["## Features for the next phase", "", features_section(filed or []), ""]
    if reports:
        out += ["## Persona reports", ""] + [f"- [{name}]({store.rel(path)})" for name, path in reports.items()] + [""]
    return "\n".join(out).rstrip() + "\n"


def render_next_goals(phase: Phase, next_phase: str, rev: dict[str, Any],
                      filed: list[dict[str, Any]] | None = None,
                      followups: list[dict[str, Any]] | None = None) -> str:
    body = str(rev.get("next_goals", "")).strip()
    out = [f"# {next_phase} goals (draft)", "",
           f"_Drafted by `garden retro` from {phase.key}; edit before planning._", ""]
    if body:
        out.append(body)
    else:
        out.append("_(the reconciliation produced no draft; write the goals here)_")
    filed_ok = [f for f in filed or [] if f.get("task_id")]
    if filed_ok:
        out += ["", "## Features for the next phase", ""]
        out += [f"- {f['task_id']}: {f['title']}" for f in filed_ok]
    followups_ok = [f for f in followups or [] if f.get("task_id")]
    if followups_ok:
        out += ["", "## Follow-ups carried from the retro verdict", ""]
        out += [f"- {f['task_id']}: {f['title']}" for f in followups_ok]
    return "\n".join(out).rstrip() + "\n"
