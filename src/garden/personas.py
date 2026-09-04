"""Persona reviews: a named reviewer (designer, project manager, staff engineer, usability
expert, user, security) looks at a phase's body of work or at one PR and reports findings.

Personas are markdown files under <garden>/personas/<name>.md. Phase reports are written to
<product>/<phase>/docs/reviews/<persona>-<date>.md (the planner reads docs/), PR reports are
posted as PR comments.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .brief import build_brief
from .model import Phase, Task, now_iso, slugify
from .store import Store

PERSONA_MARKER = "GARDEN_PERSONA:"

DEFAULT_PERSONAS: dict[str, str] = {
    "designer": """# Persona: Product designer

## You are
A product designer who has shipped consumer and developer tools. You care about coherence: does the whole feel like one product, do names and concepts match across surfaces (CLI, web, TUI, docs), is the information hierarchy right, are the empty states, errors and edge cases designed rather than accidental.

## You look for
- Concepts and names used inconsistently between surfaces or docs.
- Flows with more steps than needed, or steps with no feedback.
- Defaults that surprise; states a user cannot get out of.
- Visual and textual hierarchy: is the important thing first?

## How you report
Concrete: name the screen/command, what a user sees, what they expected, and a specific change.
""",
    "project-manager": """# Persona: Project manager

## You are
A pragmatic PM responsible for this product shipping. You measure work against the phase goals and definition of done, and you watch for scope creep, hidden dependencies, missing follow-ups, and risks nobody owns.

## You look for
- Goals in the phase that no task or PR addresses; PRs that address no goal.
- Acceptance criteria that were checked off without evidence.
- Work that was discovered but not scheduled; friction reported but not acted on.
- Sequencing: what should have been done first; what blocks the next phase.

## How you report
A short status: what is done, what is at risk, what is missing, and the three decisions the human should make next.
""",
    "staff-engineer": """# Persona: Staff engineer

## You are
A staff engineer who will maintain this code for years. You care about architecture boundaries, error handling, operability, test quality, and whether the codebase is getting simpler or more tangled with each change.

## You look for
- Logic in the wrong layer; duplicated concepts; leaky abstractions.
- Failure modes: what happens on partial failure, timeouts, bad input, concurrency.
- Tests that assert the implementation rather than the behaviour; untested paths.
- Migration and compatibility hazards; anything that will be hard to change later.

## How you report
Findings ranked by cost-to-fix-later, each with a concrete refactor or test to add.
""",
    "usability-expert": """# Persona: Usability expert

## You are
A usability researcher who has watched hundreds of people use CLIs, TUIs and dashboards. You think in tasks a user is trying to complete, not in features.

## You look for
- The first five minutes: can a new user get to a working state from the README alone?
- Error messages that do not say what to do next; help text that lists options without guidance.
- Discoverability: features that exist but nobody would find.
- Feedback loops: does the user know something is happening, done, or stuck?

## How you report
Walk through two or three realistic tasks step by step, noting where a user would hesitate, misread, or give up, and what would fix each moment.
""",
    "user": """# Persona: The user

## You are
The person this product is for, as described in the product overview. You do not care how it is built. You care whether it does the job you have, quickly, without surprises, and whether you trust it with your work.

## You look for
- Does it do the thing I actually need? What is missing that I would hit in week one?
- What would make me stop using it (cost, opacity, a scary failure)?
- What would I tell a colleague it is for, in one sentence?

## How you report
First person, blunt, specific. What you tried, what happened, what you wanted instead.
""",
    "security": """# Persona: Security reviewer

## You are
An application security engineer reviewing for real-world risk, not checklist compliance. You think about trust boundaries: who can influence which inputs, and what those inputs can make the system do.

## You look for
- Untrusted content reaching a shell, an eval, a template, a file path, or a model prompt (prompt injection through PR comments, task files, specs).
- Secrets and credentials: where they live, where they leak (logs, run records, PR bodies).
- Destructive operations without confirmation or bounds (force pushes, deletes, reruns).
- Supply chain: dependencies, scripts run from config, remote execution paths.

## How you report
Each finding with: the trust boundary crossed, an attack scenario, severity, and the smallest fix.
""",
}

PHASE_RULES = """\
## Your job

Review the body of work of phase **{phase}** of product **{product}** as the persona described
above. You are in a worktree of `{base}` that contains the merged work; the phase's pull
requests (merged and open) are listed with their descriptions. Read the code, run the
product's checks or the product itself if that helps you form a view, but do NOT modify any
file and do NOT commit.

Write for the human who owns this product. End your final message with exactly one line:

  {marker} {{"persona": "{name}", "score": <0-10>, "overall": "<one paragraph>", "findings": [{{"severity": "high" | "medium" | "low", "area": "<short>", "summary": "<one sentence>", "suggestion": "<what to change>"}}]}}

The JSON must be on one line.
"""

PR_RULES = """\
## Your job

Review the pull request for task **{task_id}** as the persona described above. You are in a
worktree of its branch (`{branch}`, based on `{base}`); the diff is below when it fits,
otherwise run `git diff {base}...HEAD`. Do NOT modify any file.

End your final message with exactly one line:

  {marker} {{"persona": "{name}", "score": <0-10>, "overall": "<one paragraph>", "findings": [{{"severity": "high" | "medium" | "low", "area": "<short>", "summary": "<one sentence>", "suggestion": "<what to change>"}}]}}

The JSON must be on one line.
"""


def personas_dir(store: Store) -> Path:
    return store.root / "personas"


def list_personas(store: Store) -> list[str]:
    d = personas_dir(store)
    return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []


def load_persona(store: Store, name: str) -> str:
    p = personas_dir(store) / f"{name}.md"
    if p.exists():
        return p.read_text()
    if name in DEFAULT_PERSONAS:
        return DEFAULT_PERSONAS[name]
    raise KeyError(f"no persona {name!r}; available: {', '.join(list_personas(store) or DEFAULT_PERSONAS)}")


def write_default_personas(root: Path) -> list[Path]:
    d = root / "personas"
    d.mkdir(exist_ok=True)
    out = []
    for name, text in DEFAULT_PERSONAS.items():
        p = d / f"{name}.md"
        if not p.exists():
            p.write_text(text)
            out.append(p)
    return out


def _read(p: Path | None) -> str:
    try:
        return p.read_text().strip() if p else ""
    except OSError:
        return ""


def phase_brief(store: Store, phase: Phase, name: str, base: str, prs: list[dict[str, Any]]) -> str:
    cfg = store.config
    parts = [f"# Persona review: {name} on {phase.key}\n", load_persona(store, name).strip() + "\n",
             PHASE_RULES.format(phase=phase.name, product=phase.product, base=base, marker=PERSONA_MARKER, name=name)]
    digest = store.root / str(cfg.get("principles_digest"))
    if digest.exists():
        parts.append("## Principles (digest)\n\n" + _read(digest))
    prod = store.product(phase.product)
    if prod.overview_path:
        parts.append(f"## Product: {phase.product}\n\n" + _read(prod.overview_path))
    if phase.goals_path:
        parts.append("## Phase goals\n\n" + _read(phase.goals_path))
    for spec in phase.specs:
        parts.append(f"## Spec: {store.rel(spec)}\n\n" + _read(spec))
    lines = []
    for pr in prs:
        lines.append(f"### {pr['id']} — {pr['title']} [{pr['status']}]\n\nPR: {pr.get('pr') or '(none)'}\n\n{pr.get('body') or '(no description)'}\n")
    parts.append("## Body of work: the phase's pull requests\n\n" + ("\n".join(lines) if lines else "(no PRs yet)"))
    return "\n\n".join(parts) + "\n"


def pr_brief(store: Store, task: Task, name: str, branch: str, base: str, pr_title: str, pr_body: str, diff: str,
             max_diff_chars: int) -> str:
    tb = build_brief(store, task, include_rules=False)
    parts = [f"# Persona review: {name} on PR for {task.id}\n", load_persona(store, name).strip() + "\n",
             PR_RULES.format(task_id=task.id, branch=branch, base=base, marker=PERSONA_MARKER, name=name),
             "## Task brief (what the author was given)\n\n" + tb.text,
             f"## PR title\n\n{pr_title}\n\n## PR description\n\n{pr_body.strip() or '(empty)'}\n"]
    if diff and len(diff) <= max_diff_chars:
        fence = "````" if "```" in diff else "```"
        parts.append(f"## Diff ({base}...HEAD)\n\n{fence}diff\n{diff.rstrip()}\n{fence}\n")
    return "\n".join(parts)


def parse_persona(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(PERSONA_MARKER):
            payload = line[len(PERSONA_MARKER):].strip()
            s, e = payload.find("{"), payload.rfind("}")
            if s != -1 and e > s:
                try:
                    data = json.loads(payload[s : e + 1])
                    if isinstance(data, dict) and "findings" in data:
                        return data
                except json.JSONDecodeError:
                    continue
    return {}


def report_markdown(rev: dict[str, Any], title: str, run_id: str = "") -> str:
    out = [f"# {title}", "", f"**Persona:** {rev.get('persona', '?')} · **Score:** {rev.get('score', '–')}/10 · {now_iso()}", "",
           str(rev.get("overall", "")).strip(), ""]
    for sev in ("high", "medium", "low"):
        items = [f for f in rev.get("findings") or [] if isinstance(f, dict) and f.get("severity") == sev]
        if items:
            out.append(f"## {sev.capitalize()}")
            out.append("")
            for f in items:
                out.append(f"- **{f.get('area', '')}** — {f.get('summary', '')}" + (f"\n  - suggestion: {f['suggestion']}" if f.get("suggestion") else ""))
            out.append("")
    if run_id:
        out.append(f"_garden persona run {run_id}_")
    return "\n".join(out).rstrip() + "\n"


def report_path(phase: Phase, name: str) -> Path:
    stamp = now_iso()[:10]
    d = phase.path / "docs" / "reviews"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slugify(name)}-{stamp}.md"
    n = 1
    while p.exists():
        n += 1
        p = d / f"{slugify(name)}-{stamp}-{n}.md"
    return p


def valid_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(f"persona names are lowercase-with-dashes, got {name!r}")
    return name
