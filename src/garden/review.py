"""Automated review pass: one headless run per PR round that checks the diff against the
task's acceptance criteria and the PR description against the garden's standards, then
reports JSON. The scheduler posts the result on the PR and, on `request_changes`, routes
the findings into the normal revise loop."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .brief import _parse_marked_json, build_brief
from .criteria import parse_criteria, reconcile
from .model import Task
from .store import Store

REVIEW_MARKER = "GARDEN_REVIEW:"

INTERACTION_PATHS = (
    "src/garden/web/", "src/garden/tui/", "src/garden/cli/", "src/garden/scheduler/", "src/garden/qa/",
    "src/garden/runner/", "src/garden/brief.py", "src/garden/checkrun.py", "src/garden/checks.py",
    "src/garden/config.py", "src/garden/gitops.py", "src/garden/github.py", "src/garden/harness.py",
    "src/garden/inbox.py", "src/garden/kickoff.py", "src/garden/model.py", "src/garden/notify.py",
    "src/garden/outcomes.py", "src/garden/review.py", "src/garden/run_supervisor.py", "src/garden/runs.py",
    "src/garden/stabilization.py", "src/garden/store.py", "src/garden/walkthrough.py",
)

SCALABILITY_LOAD_KINDS = {"controlled", "real_model_harnesses"}


def _numbers(value: Any, *, minimum_items: int) -> list[int | float] | None:
    if not isinstance(value, list) or len(value) < minimum_items:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
           for item in value):
        return None
    return value


def interaction_requirement(changed: list[str], *review_context: str) -> tuple[bool, bool, str]:
    """Classify reviews that need a running-app journey, and performance claims that need load evidence."""
    affected = [path for path in changed if path.startswith(INTERACTION_PATHS)]
    required = bool(affected)
    scalability = bool(re.search(
        r"\b(scalab(?:ility|le)|performance|latency|p95|cache.expir|history (?:size|scan)|read/scan)\b",
        "\n".join(review_context), re.I,
    ))
    reason = "affected UI/lifecycle paths: " + ", ".join(affected[:6]) if affected else "non-UI change"
    return required, scalability, reason


def interaction_evidence_gaps(review: dict[str, Any], *, required: bool, scalability: bool,
                              expected_head: str) -> list[str]:
    """Return mechanical blockers in a reviewer's claimed running-app evidence."""
    if not required and not scalability:
        return []
    row = review.get("interaction")
    if not isinstance(row, dict):
        return ["running-application interaction evidence was not reported"]
    gaps: list[str] = []
    if row.get("head") != expected_head:
        gaps.append("interaction evidence is stale or not tied to the reviewed head")
    if row.get("environment") != "disposable":
        gaps.append("interaction was not performed in a disposable garden")
    command = row.get("command")
    if not isinstance(command, str) or not command.strip() or command.strip() in {"true", ":"}:
        gaps.append("interaction command was not reported")
    states = row.get("states") if isinstance(row.get("states"), dict) else {}
    for state in ("affected", "empty", "failure_recovery"):
        evidence = states.get(state) if isinstance(states.get(state), dict) else {}
        actions = evidence.get("actions")
        observed = evidence.get("observed")
        if (evidence.get("status") != "pass"
                or not isinstance(actions, list) or not actions
                or any(not isinstance(action, str) or not action.strip() for action in actions)
                or not isinstance(observed, str) or not observed.strip()):
            gaps.append(f"{state.replace('_', '/')} interaction is missing or failed")
    artifacts = row.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts or any(not isinstance(path, str) for path in artifacts):
        gaps.append("interaction artifact paths were not reported")
    elif any(not Path(path).exists() for path in artifacts):
        gaps.append("one or more interaction artifacts do not exist")
    else:
        records = []
        for artifact in artifacts:
            path = Path(artifact)
            if path.suffix.lower() != ".json":
                continue
            try:
                record = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("head") == expected_head and record.get("states") == states:
                records.append(record)
        if not records:
            gaps.append("a structured interaction artifact tied to the reviewed head, actions, and observations was not reported")
    if not isinstance(row.get("automated_checks"), list):
        gaps.append("automated checks were not distinguished from real interaction")
    if not isinstance(row.get("unverified"), list):
        gaps.append("unverified requirements were not stated")
    elif row.get("unverified"):
        gaps.append("interaction requirements remain unverified")
    if scalability:
        load = row.get("scalability") if isinstance(row.get("scalability"), dict) else {}
        if not isinstance(load.get("served_app"), str) or not load["served_app"].startswith(("http://", "https://")):
            gaps.append("scalability served_app must be a served HTTP URL")
        sizes = _numbers(load.get("history_sizes"), minimum_items=2)
        if sizes is None or any(size < 0 for size in sizes) or sizes != sorted(set(sizes)):
            gaps.append("scalability history_sizes must contain at least two distinct increasing numeric sizes")
        intervals = load.get("cache_expiry_intervals")
        if isinstance(intervals, bool) or not isinstance(intervals, int) or intervals < 2:
            gaps.append("scalability cache_expiry_intervals must be an integer of at least two")
        processes = load.get("executing_processes")
        if isinstance(processes, bool) or not isinstance(processes, int) or processes < 1:
            gaps.append("scalability executing_processes must be a positive integer")
        latencies = _numbers(load.get("latencies"), minimum_items=2)
        if latencies is None or any(latency < 0 for latency in latencies):
            gaps.append("scalability latencies must contain at least two non-negative numeric samples")
        counts = load.get("read_scan_counts")
        if not isinstance(counts, dict) or any(
            isinstance(counts.get(name), bool) or not isinstance(counts.get(name), (int, float))
            or not math.isfinite(counts[name]) or counts[name] < 0 for name in ("reads", "scans")
        ):
            gaps.append("scalability read_scan_counts must contain non-negative numeric reads and scans")
        if load.get("load_kind") not in SCALABILITY_LOAD_KINDS:
            gaps.append("scalability load_kind must be controlled or real_model_harnesses")
    return gaps

REVIEW_RULES = """\
## Your job

You are the automated first reviewer for the pull request described below. The human
reviewer reads your comment before looking at the code, so be precise and terse. You are
in a git worktree of the PR branch (`{branch}`, based on `{base}`); the diff is included
below when it fits, otherwise run `git diff {base}...HEAD`. You may run the project's
checks if they are fast. Do NOT modify tracked worktree files and do NOT commit. Running-app
evidence may write artifacts only into its disposable garden or a temporary directory.

Check, in this order:

1. **Acceptance criteria.** Return one `criteria` entry per criterion in the task, in order:
   quote the `criterion`, set `met` true or false, and give an `evidence` field plus a
   one-line `reason` pointing at it (the diff, a test, a page). The author's own per-criterion evidence is under
   "Author's verification" below; check each claim against the diff rather than taking it on
   trust. A criterion with no evidence, or one the author marked not done without a reason you
   accept, is `met: false` and a blocking finding. If the task has no criteria, judge its Goal
   on the author's evidence and return `criteria: []`.
   Every returned criterion needs its own non-empty `evidence`: the scheduler mechanically
   changes the verdict to `request_changes` for an unmet or evidence-less criterion.
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

When a "Rendered UI captures" section is present, open every listed PNG with the image
reader and inspect layout, overlap, wrapping and empty states. Name every page inspected in
`pages_seen`. Omitting a listed page makes the verdict mechanically `request_changes`.

When "Running-application interaction required" is present, start the proposed head as a
served application against a disposable garden and perform the affected journey through its
HTTP/browser surface. Cover the user objective, an empty state, and a relevant failure followed
by recovery. Record actions and their observed consequences; screenshots and test-client
assertions are supporting artifacts, not performed interaction. Treat no_change reconciliation
and attention prompts as user outcomes when they are affected. Never use the live operator
garden. Report the exact command, artifact paths, separately named automated checks, and every
unverified requirement. Use the reviewed full SHA supplied below as `interaction.head`.

For a scalability claim, additionally use a served disposable app with representative and larger
histories, repeated cache-expiry intervals, actual executing bounded workload processes, empirical
latency samples/distribution, and read/scan counts. State whether load is controlled or uses real
model harnesses; controlled load must not be described as a real harness run.

Severity: `blocking` means the PR should not merge as is; `nit` is optional polish. Only
request changes for blocking findings or a description that fails the standard above.

Every finding carries a concrete `fix`: say what to change and where; include a short code
sketch when it makes the change clearer. `fix` is required for `blocking` and `high` findings
and encouraged for nits. Also return `improvements`: non-blocking suggestions beyond the
acceptance criteria, such as a simpler design, clearer name, missing test, doc line, or cheaper
implementation. They are optional work for the author, not reasons to request changes.

If the *only* problem is the description (`description_ok` is false and there is no blocking
finding), do not send the change back for another round: rewrite the description yourself and
return the full corrected body in `description_rewrite`. The garden applies it directly. Write
it to the same contract the author was given — the permanent description of the change, with
no process narration, no review or rebase references, no scar tissue. Leave `description_rewrite`
empty when a blocking finding means the change is going back anyway.

End your final message with exactly one line:

  {marker} {{"verdict": "approve" | "request_changes", "summary": "<1-2 sentences>", "pages_seen": ["<page slug>"], "interaction": {{"head": "<reviewed full SHA>", "environment": "disposable", "command": "<served-app command>", "states": {{"affected": {{"status": "pass|fail", "actions": ["<action>"], "observed": "<consequence>"}}, "empty": {{"status": "pass|fail", "actions": ["<action>"], "observed": "<consequence>"}}, "failure_recovery": {{"status": "pass|fail", "actions": ["<action>"], "observed": "<failure and recovery consequence>"}}}}, "artifacts": ["<path>"], "automated_checks": ["<separate check>"], "unverified": ["<requirement or empty>"], "scalability": {{"served_app": "<URL>", "history_sizes": [100, 1000], "cache_expiry_intervals": 3, "executing_processes": 2, "latencies": [0.1, 0.2], "read_scan_counts": {{"reads": 3, "scans": 1}}, "load_kind": "controlled|real_model_harnesses"}}}}, "criteria": [{{"criterion": "<acceptance criterion, quoted>", "met": true | false, "reason": "<one line, with the evidence>"}}], "description_ok": true | false, "description_feedback": "<what to change in the PR description, or empty>", "description_rewrite": "<the full corrected PR body, or empty>", "findings": [{{"severity": "blocking" | "high" | "nit", "file": "<path or empty>", "line": <number or null>, "summary": "<one sentence>", "fix": "<concrete change, location, and optional code sketch>"}}], "improvements": [{{"area": "<design, naming, tests, docs, cost>", "suggestion": "<non-blocking improvement>", "why": "<benefit>", "effort": "small" | "medium"}}]}}

The JSON must be on one line.
"""


def _verification_brief(task: Task, verified: Any) -> str:
    """The author's per-criterion evidence, laid out for the reviewer to check the diff
    against. Empty when the task has no criteria and the author claimed nothing."""
    rows = reconcile(parse_criteria(task.body), verified)
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
                 captures: list[str] | None = None, checks: list[dict[str, Any]] | None = None,
                 reask_missing_fixes: bool = False, interaction_required: bool = False,
                 scalability_required: bool = False, review_head: str = "", interaction_reason: str = "") -> str:
    task_brief = build_brief(store, task, include_rules=False)
    amendments = {int(a["index"]): a for a in task.extra.get("criteria_amended", [])
                  if isinstance(a, dict) and isinstance(a.get("index"), int)}
    criteria_note = ""
    if amendments:
        lines = ["## Amended acceptance criteria\n",
                 "Judge each amended line against its stated outcome; the original wording is superseded.\n"]
        for index, criterion in enumerate(parse_criteria(task.body)):
            if index in amendments:
                lines.append(f"- **{criterion}** *(amended — {amendments[index].get('reason', '')})*")
        criteria_note = "\n".join(lines) + "\n"
    parts = [
        f"# Review: PR for task {task.id} ({task.title})\n",
        REVIEW_RULES.format(branch=branch, base=base, marker=REVIEW_MARKER),
        "## Task brief (what the author was given)\n\n" + task_brief.text,
        f"## PR title\n\n{pr_title}\n\n## PR description\n\n{pr_body.strip() or '(empty)'}\n",
    ]
    if criteria_note:
        parts.append(criteria_note)
    verification = _verification_brief(task, verified)
    if verification:
        parts.append(verification)
    if captures:
        parts.append("## Rendered UI captures\n\nOpen these image paths before judging the UI:\n\n" +
                     "\n".join(f"- `{path}`" for path in captures) + "\n")
    if interaction_required or scalability_required:
        parts.append("## Running-application interaction required\n\n"
                     f"Reviewed head: `{review_head}`\n\nReason: {interaction_reason}.\n\n"
                     + ("This includes the scalability evidence fields described above.\n" if scalability_required else ""))
    if checks:
        parts.append("## Pre-review checks\n\n" + "\n".join(
            f"- **{c.get('name', 'check')}**: {c.get('status', 'unknown')}"
            + (f" — {c.get('summary')}" if c.get("summary") else "") for c in checks) + "\n")
    if pr_comment.strip():
        parts.append(
            "## Author's response to the previous review (posted as a PR comment, not part of the description)\n\n"
            + pr_comment.strip() + "\n"
        )
    if reask_missing_fixes:
        parts.append("## Follow-up required\n\nYour previous review had blocking findings without concrete fixes. "
                     "Return the same review with a `fix` for every blocking finding; do not discover new issues "
                     "unless needed to make those fixes accurate.\n")
    if diff and len(diff) <= max_diff_chars:
        fence = "````" if "```" in diff else "```"
        parts.append(f"## Diff ({base}...HEAD)\n\n{fence}diff\n{diff.rstrip()}\n{fence}\n")
    else:
        parts.append(f"## Diff\n\nThe diff is {len(diff):,} characters; read it with `git diff {base}...HEAD` (and `git log {base}..HEAD`).\n")
    return "\n".join(parts)


def parse_review(text: str) -> dict[str, Any]:
    data = _parse_marked_json(text, REVIEW_MARKER)
    if "verdict" in data:
        return data
    return {}


def enforce_criteria_verdict(review: dict[str, Any]) -> dict[str, Any]:
    """Make unsupported or unmet criteria mechanically request changes.

    A reviewer is advisory about its conclusion, but not about this gate: an approving
    top-level verdict cannot override a criterion marked unmet or without evidence.
    """
    unsupported = [
        criterion for criterion in review.get("criteria") or []
        if isinstance(criterion, dict)
        and (criterion.get("met") is not True or not str(criterion.get("evidence") or "").strip())
    ]
    if not unsupported:
        return review

    review["verdict"] = "request_changes"
    findings = review.setdefault("findings", [])
    if not isinstance(findings, list):
        findings = review["findings"] = []
    existing = {str(finding.get("summary") or "") for finding in findings if isinstance(finding, dict)}
    for criterion in unsupported:
        text = str(criterion.get("criterion") or "unnamed criterion")
        summary = f"Acceptance criterion lacks a passing, evidenced assessment: {text}"
        if summary not in existing:
            findings.append({"severity": "blocking", "file": "", "line": None, "summary": summary,
                             "fix": "Make the criterion pass and cite concrete review evidence."})
    return review


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
    high = [f for f in findings if f.get("severity") == "high"]
    nits = [f for f in findings if f.get("severity") not in ("blocking", "high")]
    if blocking:
        out.append("\n**Blocking**")
        out += [_finding_line(f) for f in blocking]
    if high:
        out.append("\n**High priority**")
        out += [_finding_line(f) for f in high]
    if nits:
        out.append("\n**Nits**")
        out += [_finding_line(f) for f in nits]
    improvements = [i for i in (rev.get("improvements") or []) if isinstance(i, dict)]
    if improvements:
        out.append("\n**Improvements**")
        for item in improvements:
            area = str(item.get("area") or "general")
            effort = str(item.get("effort") or "")
            detail = str(item.get("suggestion") or "")
            why = str(item.get("why") or "")
            suffix = f" — {why}" if why else ""
            effort_text = f" · {effort}" if effort else ""
            out.append(f"- **{area}{effort_text}**: {detail}{suffix}")
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
        if not isinstance(f, dict):
            continue
        severity = str(f.get("severity") or "nit")
        fix = str(f.get("fix") or "").strip()
        line = "- **automated review** " + severity + _where(f) + ": " + str(f.get("summary", ""))
        items.append(line + (f"\n  - **Fix:** {fix}" if fix else "\n  - **Fix:** reviewer did not provide one; determine the smallest correct change."))
    if not rev.get("description_ok", True):
        items.append("- **automated review** PR description: " + str(rev.get("description_feedback") or "rewrite it to give broader context and remove scar tissue") +
                     " (put the new description in `pr_body`; it replaces the current one)")
    improvements = [i for i in (rev.get("improvements") or []) if isinstance(i, dict)]
    if improvements:
        items.append("\n### Optional improvements\n\nTake or decline each item. In your result, set `improvements_taken` "
                     "to the suggestions you took and `improvements_declined` to objects with `suggestion` and `reason`; "
                     "declined items are kept for the retro.")
        for item in improvements:
            area = str(item.get("area") or "general")
            effort = str(item.get("effort") or "")
            suggestion = str(item.get("suggestion") or "")
            why = str(item.get("why") or "")
            items.append(f"- **{area}{f' · {effort}' if effort else ''}**: {suggestion}" + (f" — {why}" if why else ""))
    return "\n".join(items)


def _where(f: dict[str, Any]) -> str:
    if f.get("file"):
        return f" (`{f['file']}`" + (f":{f['line']}" if f.get("line") else "") + ")"
    return ""


def _finding_line(f: dict[str, Any]) -> str:
    fix = str(f.get("fix") or "").strip()
    return f"- {f.get('summary', '')}{_where(f)}" + (f"\n  - **Fix:** {fix}" if fix else "")
