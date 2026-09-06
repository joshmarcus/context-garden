"""Automated review pass: one headless run per PR round that checks the diff against the
task's acceptance criteria and the PR description against the garden's standards, then
reports JSON. The scheduler posts the result on the PR and, on `request_changes`, routes
the findings into the normal revise loop."""

from __future__ import annotations

import json
from typing import Any

from .brief import build_brief
from .criteria import parse_criteria, reconcile
from .model import Task
from .store import Store

REVIEW_MARKER = "GARDEN_REVIEW:"

REVIEW_RULES = """\
## Your job

You are the automated first reviewer for the pull request described below. The human
reviewer reads your comment before looking at the code, so be precise and terse. You are
in a git worktree of the PR branch (`{branch}`, based on `{base}`); the diff is included
below when it fits, otherwise run `git diff {base}...HEAD`. You may run the project's
checks if they are fast. Do NOT modify any file and do NOT commit.

Check, in this order:

1. **Worker pre-flight.** The author must report every item in the pre-flight checklist below.
   A missing item is a blocking finding; check the evidence rather than trusting it.
2. **Acceptance criteria.** Return one `criteria` entry per criterion in the task, in order:
   quote the `criterion`, set `met` true or false, and give a one-line `reason` pointing at
   the evidence (the diff, a test, a page). The author's own per-criterion evidence is under
   "Author's verification" below; check each claim against the diff rather than taking it on
   trust. A criterion with no evidence, or one the author marked not done without a reason you
   accept, is `met: false` and a blocking finding.
3. **Correctness.** Bugs, unhandled cases, broken behaviour, security problems.
4. **Scope.** Changes outside the task, or task work that is missing.
5. **PR description.** It must give a reader without the task file the broader context:
   what is being accomplished and why, how it fits the phase goals, what was verified, and
   any follow-ups. It must have no scar tissue: no references to earlier review rounds or
   abandoned approaches ("as requested", "reverted the previous attempt"), no narration of
   the process, no leftover TODO/debug notes. The diff must be equally clean: no
   commented-out code, no stray debug output, no "fixed review comment" commit messages
   left in the final story of the change. Describe the change as if it were written right
   the first time.
6. **Principles.** Tests skipped or weakened, scope widened, history rewritten, new
   dependencies without justification.

When a "Rendered UI captures" section is present, open every listed PNG with the image
reader and inspect layout, overlap, wrapping and empty states. Name every page inspected in
`pages_seen`. Omitting a listed page makes the verdict mechanically `request_changes`.

Severity: `blocking` means the PR should not merge as is; `nit` is optional polish. Only
request changes for blocking findings or a description that fails the standard above.

If the *only* problem is the description (`description_ok` is false and there is no blocking
finding), do not send the change back for another round: rewrite the description yourself and
return the full corrected body in `description_rewrite`. The garden applies it directly. Write
it to the same contract the author was given — the permanent description of the change, with
no process narration, no review or rebase references, no scar tissue. Leave `description_rewrite`
empty when a blocking finding means the change is going back anyway.

End your final message with exactly one line:

  {marker} {{"verdict": "approve" | "request_changes", "summary": "<1-2 sentences>", "pages_seen": ["<page slug>"], "criteria": [{{"criterion": "<acceptance criterion, quoted>", "met": true | false, "reason": "<one line, with the evidence>"}}], "description_ok": true | false, "description_feedback": "<what to change in the PR description, or empty>", "description_rewrite": "<the full corrected PR body, or empty>", "findings": [{{"severity": "blocking" | "nit", "file": "<path or empty>", "line": <number or null>, "summary": "<one sentence>"}}]}}

The JSON must be on one line.
"""


def _verification_brief(task: Task, verified: Any, criteria: list[str] | None = None) -> str:
    """The author's per-criterion evidence, laid out for the reviewer to check the diff
    against. Empty when the task has no criteria and the author claimed nothing."""
    rows = reconcile(criteria if criteria is not None else parse_criteria(task.body), verified)
    if not rows:
        return ""
    lines = ["## Author's verification\n", "One row per acceptance criterion; check each against the diff.\n"]
    for row in rows:
        if row["not_done"]:
            lines.append(f"- **{row['criterion']}** — author says NOT DONE: {row['worker_reason'] or 'no reason given'}")
        elif row["evidence"]:
            lines.append(f"- **{row['criterion']}** — {row['evidence']}")
        else:
            lines.append(f"- **{row['criterion']}** — author gave no evidence")
    return "\n".join(lines) + "\n"


def review_brief(store: Store, task: Task, *, branch: str, base: str, pr_title: str, pr_body: str, diff: str,
                 max_diff_chars: int, pr_comment: str = "", verified: Any = None,
                 captures: list[str] | None = None, criteria_snapshot: list[str] | None = None,
                 pre_flight: Any = None) -> str:
    frozen = criteria_snapshot if criteria_snapshot is not None else parse_criteria(task.body)
    task_brief = build_brief(store, task, include_rules=False, criteria_snapshot=frozen)
    parts = [
        f"# Review: PR for task {task.id} ({task.title})\n",
        REVIEW_RULES.format(branch=branch, base=base, marker=REVIEW_MARKER),
        "## Task brief (what the author was given)\n\n" + task_brief.text,
        f"## PR title\n\n{pr_title}\n\n## PR description\n\n{pr_body.strip() or '(empty)'}\n",
    ]
    verification = _verification_brief(task, verified, frozen)
    if verification:
        parts.append(verification)
    if isinstance(pre_flight, list):
        parts.append("## Author's pre-flight\n\n" + "\n".join(
            f"- **{row.get('item', '')}** — {row.get('status', '')}: {row.get('evidence', '')}"
            for row in pre_flight if isinstance(row, dict)
        ) + "\n")
    current = parse_criteria(task.body)
    if current != frozen:
        parts.append("## Criteria changed after dispatch\n\nThe worker was judged against the frozen criteria above. "
                     "The task now has:\n\n" + "\n".join(f"- {item}" for item in current) + "\n")
    if captures:
        parts.append("## Rendered UI captures\n\nOpen these image paths before judging the UI:\n\n" +
                     "\n".join(f"- `{path}`" for path in captures) + "\n")
    if pr_comment.strip():
        parts.append(
            "## Author's response to the previous review (posted as a PR comment, not part of the description)\n\n"
            + pr_comment.strip() + "\n"
        )
    if diff and len(diff) <= max_diff_chars:
        fence = "````" if "```" in diff else "```"
        parts.append(f"## Diff ({base}...HEAD)\n\n{fence}diff\n{diff.rstrip()}\n{fence}\n")
    else:
        parts.append(f"## Diff\n\nThe diff is {len(diff):,} characters; read it with `git diff {base}...HEAD` (and `git log {base}..HEAD`).\n")
    return "\n".join(parts)


def parse_review(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(REVIEW_MARKER):
            payload = line[len(REVIEW_MARKER):].strip()
            s, e = payload.find("{"), payload.rfind("}")
            if s != -1 and e > s:
                try:
                    data = json.loads(payload[s : e + 1])
                    if isinstance(data, dict) and "verdict" in data:
                        return data
                except json.JSONDecodeError:
                    continue
    return {}


def review_to_markdown(rev: dict[str, Any], run_id: str = "") -> str:
    verdict = str(rev.get("verdict", "?"))
    icon = "✅" if verdict == "approve" else "🔁"
    out = [f"{icon} **Automated review: {verdict.replace('_', ' ')}** — {rev.get('summary', '')}".rstrip(" —")]
    criteria = [c for c in (rev.get("criteria") or []) if isinstance(c, dict)]
    if criteria:
        out.append("\n**Acceptance criteria**")
        for c in criteria:
            mark = "✅" if c.get("met") is True else "❌"
            out.append(f"- {mark} {c.get('criterion', '')}" + (f" — {c['reason']}" if c.get("reason") else ""))
    findings = [f for f in (rev.get("findings") or []) if isinstance(f, dict)]
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    nits = [f for f in findings if f.get("severity") != "blocking"]
    if blocking:
        out.append("\n**Blocking**")
        out += [_finding_line(f) for f in blocking]
    if nits:
        out.append("\n**Nits**")
        out += [_finding_line(f) for f in nits]
    if not rev.get("description_ok", True):
        out.append("\n**PR description**\n\n" + str(rev.get("description_feedback") or "needs work"))
    if run_id:
        out.append(f"\n_garden review run {run_id}_")
    return "\n".join(out)


def review_is_description_only(rev: dict[str, Any]) -> bool:
    """True when a request_changes verdict has no blocking code findings, only a PR
    description fix. CG-109: that revise round is a paragraph rewrite, not a code review,
    and should not cost a code-review-tier model."""
    findings = [f for f in (rev.get("findings") or []) if isinstance(f, dict)]
    return not any(f.get("severity") == "blocking" for f in findings) and not rev.get("description_ok", True)


def feedback_from_review(rev: dict[str, Any]) -> str:
    """The revise-brief text for a request_changes verdict."""
    items = []
    for f in rev.get("findings") or []:
        if isinstance(f, dict) and f.get("severity") == "blocking":
            items.append("- **automated review** blocking" + _where(f) + ": " + str(f.get("summary", "")))
    if not rev.get("description_ok", True):
        items.append("- **automated review** PR description: " + str(rev.get("description_feedback") or "rewrite it to give broader context and remove scar tissue") +
                     " (put the new description in `pr_body`; it replaces the current one)")
    return "\n".join(items)


def _where(f: dict[str, Any]) -> str:
    if f.get("file"):
        return f" (`{f['file']}`" + (f":{f['line']}" if f.get("line") else "") + ")"
    return ""


def _finding_line(f: dict[str, Any]) -> str:
    return f"- {f.get('summary', '')}{_where(f)}"
