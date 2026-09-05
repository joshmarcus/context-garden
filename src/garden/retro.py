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

from .model import Phase, now_iso, slugify
from .store import Store

RETRO_MARKER = "GARDEN_RETRO:"

# The verdict each friction item is reconciled to, and how it reads in the document.
VERDICTS: dict[str, str] = {
    "still_true": "still true",
    "fixed": "fixed",
    "outdated": "outdated",
    "disputed": "disputed",
}


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

Do NOT edit any file and do NOT commit: the retro document, the next-goals draft, and the
filed feature tasks are all written for you from your verdict list. Just report it.

End your final message with exactly one line:

  {marker} {{"reconciliation": [{{"item": "<one friction item, short>", "logged": "<task id that logged it, or empty>", "pr": "<task/PR id that fixed it, or empty>", "verdict": "still_true" | "fixed" | "outdated" | "disputed", "evidence": "<why, one sentence>"}}], "summary": "<what changed this phase, one paragraph>", "personas": "<what the personas said, one paragraph>", "still_open": ["<what is still open, one per item>"], "features": [{{"title": "<short, could be a task title>", "body": "<markdown: user value, why now, size, dependencies>", "difficulty": "easy" | "medium" | "hard", "priority": <1-5, 1 highest>, "rationale": "<why this, why now, one sentence>", "duplicate_of": "<existing task id, or empty>"}}], "next_goals": "<markdown body for the next phase's goals draft>"}}

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


def resolve_features(rev: dict[str, Any], existing_titles: dict[str, str]) -> list[dict[str, Any]]:
    """Match each proposed feature against existing task titles (case-insensitive) or the
    retro's own `duplicate_of`, deciding which get filed as draft tasks. Pure: assigns no
    ids and touches no files, so it is testable without a live model or a worktree.

    `existing_titles` maps a lowercased task title to the id of the task that has it.
    """
    out: list[dict[str, Any]] = []
    for f in rev.get("features") or []:
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
            "skip": bool(reason),
            "reason": reason,
        })
    return out


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
                     filed: list[dict[str, Any]] | None = None) -> str:
    out = [f"# Retrospective: {phase.key}", "", f"_{now_iso()}_", ""]
    summary = str(rev.get("summary", "")).strip()
    if summary:
        out += ["## What changed", "", summary, ""]
    out += ["## Friction reconciled", "", reconciliation_table(rev), ""]
    personas = str(rev.get("personas", "")).strip()
    if personas:
        out += ["## What the personas said", "", personas, ""]
    still_open = [str(s).strip() for s in rev.get("still_open") or [] if str(s).strip()]
    if still_open:
        out += ["## Still open", ""] + [f"- {s}" for s in still_open] + [""]
    out += ["## Features for the next phase", "", features_section(filed or []), ""]
    if reports:
        out += ["## Persona reports", ""] + [f"- [{name}]({store.rel(path)})" for name, path in reports.items()] + [""]
    return "\n".join(out).rstrip() + "\n"


def render_next_goals(phase: Phase, next_phase: str, rev: dict[str, Any],
                      filed: list[dict[str, Any]] | None = None) -> str:
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
    return "\n".join(out).rstrip() + "\n"
