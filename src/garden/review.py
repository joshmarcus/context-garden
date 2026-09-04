"""Automated review pass: one headless run per PR round that checks the diff against the
task's acceptance criteria and the PR description against the garden's standards, then
reports JSON. The scheduler posts the result on the PR and, on `request_changes`, routes
the findings into the normal revise loop."""

from __future__ import annotations

import json
from typing import Any

from .brief import build_brief
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

1. **Acceptance criteria.** For each criterion in the task, say whether the diff meets it
   and point at the evidence (file, test). Missing or untested criteria are blocking.
2. **Correctness.** Bugs, unhandled cases, broken behaviour, security problems.
3. **Scope.** Changes outside the task, or task work that is missing.
4. **PR description.** It must give a reader without the task file the broader context:
   what is being accomplished and why, how it fits the phase goals, what was verified, and
   any follow-ups. It must have no scar tissue: no references to earlier review rounds or
   abandoned approaches ("as requested", "reverted the previous attempt"), no narration of
   the process, no leftover TODO/debug notes. The diff must be equally clean: no
   commented-out code, no stray debug output, no "fixed review comment" commit messages
   left in the final story of the change. Describe the change as if it were written right
   the first time.
5. **Principles.** Tests skipped or weakened, scope widened, history rewritten, new
   dependencies without justification.

Severity: `blocking` means the PR should not merge as is; `nit` is optional polish. Only
request changes for blocking findings or a description that fails the standard above.

End your final message with exactly one line:

  {marker} {{"verdict": "approve" | "request_changes", "summary": "<1-2 sentences>", "description_ok": true | false, "description_feedback": "<what to change in the PR description, or empty>", "findings": [{{"severity": "blocking" | "nit", "file": "<path or empty>", "line": <number or null>, "summary": "<one sentence>"}}]}}

The JSON must be on one line.
"""


def review_brief(store: Store, task: Task, *, branch: str, base: str, pr_title: str, pr_body: str, diff: str,
                 max_diff_chars: int, pr_comment: str = "") -> str:
    task_brief = build_brief(store, task, include_rules=False)
    parts = [
        f"# Review: PR for task {task.id} ({task.title})\n",
        REVIEW_RULES.format(branch=branch, base=base, marker=REVIEW_MARKER),
        "## Task brief (what the author was given)\n\n" + task_brief.text,
        f"## PR title\n\n{pr_title}\n\n## PR description\n\n{pr_body.strip() or '(empty)'}\n",
    ]
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
