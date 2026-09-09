"""The inbox: everything that needs a human, grouped by the kind of decision, with the
concrete action that resolves each item. The web Inbox page, `garden inbox` and the TUI
Inbox tab all render this one list."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .brief import brief_gaps
from .criteria import required_evidence, required_evidence_rows
from .graph import effective_status, ready
from .model import Status, Task, effective_owner, phase_refusal
from .runs import RunStore
from .store import Store

# Each group carries a "kind": "decision" means a person's call is what unblocks the
# item, so it counts toward the badge, the "need you" figure and the digest. "notice" is
# informational — the loop is already handling it — so it renders but never counts.
GROUPS = [
    ("tool", "Garden tool update", "The configured tool base advanced. The controller reports whether the verified build is available, held, installing, waiting for restart, or failed.", "notice"),
    ("question", "Questions to answer", "A worker, kickoff, or retrospective is waiting for your answer.", "decision"),
    ("retro_verdict", "Accept or change a retro's verdict", "A retrospective reopened the phase: the named tasks must land before it can close. Accept to approve them and keep the phase open, or change the verdict to close instead.", "decision"),
    ("decision", "Choose the product outcome", "A worker recommends cancelling or changing the promised outcome. The card explains what each choice does.", "decision"),
    ("triage", "Triage a draft PR", "A worker finished and opened a draft. Your first look decides: ready for review, or send it back.", "decision"),
    ("review", "Review and merge", "Ready for review on GitHub. Comments you leave become a revise run; merging unblocks dependents.", "decision"),
    ("operator", "Operator recovery", "A bounded repair has an explicit owner and preserves the task's current evidence. You do not need to make a product decision.", "notice"),
    ("automated_review", "Automated review", "The scheduler owns this review state. It records the queue, resource wait, and most recent verdict without asking a person to clear it.", "notice"),
    ("deferred", "Deferred work", "This draft is intentionally frozen by phase policy. Move it deliberately when the policy changes; it never needs approval or cancellation merely to clear a badge.", "notice"),
    ("attention", "Needs your decision", "The loop stopped because an unresolved product judgment needs you.", "decision"),
    ("retrying", "Auto-retrying", "A previous attempt failed; a new run is queued or in progress. No action needed unless you want to cancel.", "notice"),
    ("harness", "Harness paused", "A harness hit its account's quota or spend limit. Dispatch for it is paused; a cheap probe resumes it on its own once it responds again.", "notice"),
    ("config_hold", "Confirm a held config change", "garden.yaml changed while a worker run was in flight; the executable parts of the change (notify.command, checks, setup commands, harness bin/command, worker_env.pass) are held until the run is reaped or you confirm it.", "decision"),
    ("manual", "Manual work ready", "These task packets are ready for a person to claim. Taking one records the assignment; finish it from the packet when the work is complete.", "decision"),
    ("manual_waiting", "Manual work waiting", "These manual tasks are deliberately not claimable yet. Their card says whether a dependency, freeze, or existing claim is holding them.", "notice"),
    ("approve", "Approve planned or discovered work", "Draft tasks waiting for a go.", "decision"),
    ("budget", "Budget", "A phase hit its spending cap; raise it or leave it paused.", "decision"),
]

GROUP_KIND = {g[0]: g[3] for g in GROUPS}


def approve_phase_options(store: Store, task: Task) -> list[dict[str, Any]]:
    """Phases a draft could be approved into: the product's open phases, in order, plus the
    task's own phase even if it happens to be closed (so the default is always an option).
    Each entry carries the full "product/phase" value and whether the phase is frozen, for
    the Approve pulldown on the Inbox card and the task page."""
    try:
        prod = store.product(task.product)
    except KeyError:
        return [{"value": task.key, "name": task.phase, "frozen": False}]
    return [{"value": f"{prod.name}/{ph.name}", "name": ph.name, "frozen": bool(ph.frozen)}
            for ph in prod.phases if not ph.closed or ph.name == task.phase]


def split_log(body: str) -> tuple[str, list[str]]:
    """The body before '## Log' and its entries (each line's text, without the leading
    '- '). Everything that reads a task's log — an Inbox card, a board row, the task page —
    goes through this, so a bullet from Acceptance criteria or Out of scope is never mistaken
    for a log entry."""
    if "\n## Log" in body:
        head, _, tail = body.partition("\n## Log")
        lines = [ln[2:] for ln in tail.strip().splitlines() if ln.startswith("- ")]
        return head, lines
    return body, []


def _last_log_line(t: Task) -> str:
    """Return the message portion of the last entry in the task's Log section, or ''."""
    lines = split_log(t.body)[1]
    return lines[-1].split(" ", 1)[-1] if lines else ""


def _age(iso: str) -> str:
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    secs = (dt.datetime.now(dt.UTC) - t).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


# The kinds of needs-human stop the scheduler records (plus the two derived from a failed
# task), each with a name for the decision and one sentence of what happened.
ATTENTION_KINDS = {
    "stall": ("The loop stalled", "A revise round changed nothing (or a review finding came back unchanged), so the garden stopped instead of spending more rounds."),
    "revision_cap": ("Revision cap reached", "The task used all its revision rounds; the garden will not spend more without your go-ahead."),
    "review_cap": ("Automated review rounds used", "The automated reviewer has had its say on this PR; it's yours to review now."),
    "parent_closed": ("Stack parent closed", "The PR this branch is stacked on was closed without merging, so this PR targets a dead branch."),
    "base_broken": ("The base branch is broken", "A pre-PR check fails at the branch's base commit, not because of this branch. The garden re-checks the base every tick and, the moment it goes green, rebases this branch onto it and re-runs the checks by itself — no revise round and nothing for you to do unless you want to step in."),
    "worker_failed": ("A worker run failed", "The last run ended without a usable result and automatic retries are used up."),
    "env_error": ("The garden hit an environment error", "Dispatch, push or git failed on the garden's side; the worker never got a fair run."),
    "check_did_not_run": ("A check could not run", "The check continuation and its PR identity are preserved. A delegated operator may retry it once without changing the task's outcome."),
    "review_clarification": ("Reviewer clarification needs attention", "The reviewer twice returned malformed or out-of-scope requirement targets. The implementation author has not been asked to change code."),
    "deployment": ("Deployment prerequisite", "An operator must complete the named deployment or recovery step before the scheduler can continue. This is operational work, not an unanswered product question."),
    "review_recovery_exhausted": ("Automatic review recovery exhausted", "The scheduler preserved and retried the review request, but its bounded repair budget is spent. Repair review capacity or the reviewer environment, then request one more review."),
    "troubled_task": ("Troubled task", "Substantive revisions are not converging. New implementation dispatch is paused for an explicit bounded decision."),
    "investigation": ("Investigation requested", "Implementation and review mutations are paused while the preserved work reaches a safe boundary for diagnosis."),
    "investigation_report": ("Investigation report ready", "Diagnosis is complete. The report does not restart, cancel, or merge the task; choose the next action explicitly."),
}

ATTENTION_OWNERS = {
    "check_did_not_run": ("Interrupted check", "operator", "Retry the interrupted check"),
    "env_error": ("Interrupted infrastructure", "operator", "Repair the environment, then retry"),
    "base_broken": ("Normal pending work", "scheduler", "Wait for the base branch check"),
    "deployment": ("Explicit hold", "operator", "Complete the named deployment step"),
    "worker_failed": ("Source or worker failure", "implementation worker", "Send the failure to the worker"),
    "revision_cap": ("Source or CI failure", "implementation worker", "Send the preserved failures to the worker"),
    "parent_closed": ("Source conflict", "implementation worker", "Send the conflict to the worker"),
    "rebase_failed": ("Source conflict", "implementation worker", "Send the conflict to the worker"),
    "review_clarification": ("Stale review bookkeeping", "review operator", "Request a current-head review"),
    "review_recovery_exhausted": ("Interrupted review infrastructure", "review operator", "Restore review capacity, then retry"),
    "review_cap": ("Unresolved review decision", "you", "Review the current PR"),
    "stall": ("Unresolved implementation decision", "you", "Decide whether the unchanged result is acceptable"),
}


def needs_human_info(raw: Any) -> dict[str, str] | None:
    """Normalize the needs_human flag to {kind, reason, prior_status, at}. The scheduler
    writes a dict since CG-045; older state files hold a bare reason string."""
    if not raw:
        return None
    if isinstance(raw, dict):
        reason = str(raw.get("reason", ""))
        return {"kind": str(raw.get("kind") or _guess_kind(reason)), "reason": reason,
                "prior_status": str(raw.get("prior_status", "")), "at": str(raw.get("at", "")),
                "delegated_recovery": bool(raw.get("delegated_recovery"))}
    reason = str(raw)
    return {"kind": _guess_kind(reason), "reason": reason, "prior_status": "", "at": ""}


def _guess_kind(reason: str) -> str:
    low = reason.lower()
    if "revision rounds" in low:
        return "revision_cap"
    if "stack parent" in low:
        return "parent_closed"
    return "stall"


def _failed_info(t: Task) -> dict[str, str]:
    """Classify a failed task from its last log line: the garden's own errors (dispatch,
    push, git) are env errors; everything else is the worker's failure."""
    reason = _last_log_line(t) or "the task failed"
    low = reason.lower()
    kind = "env_error" if any(s in low for s in ("dispatch failed", "push failed", "git error")) else "worker_failed"
    return {"kind": kind, "reason": reason, "prior_status": "", "at": ""}


def _resume_target(t: Task, st: Any, info: dict[str, str]) -> str:
    """Where 'nothing to fix, resume' would put the task (mirrors Scheduler.resume_task)."""
    prior = info.get("prior_status", "")
    if prior in (Status.AWAITING_TRIAGE.value, Status.IN_REVIEW.value):
        return prior
    if t.pr and t.status == Status.CHANGES_REQUESTED:
        return Status.AWAITING_TRIAGE.value if st.get("pr_draft") else Status.IN_REVIEW.value
    return t.status.value


def _diff_summary(diff_stat: str) -> str:
    """The 'N files changed, +X/-Y' summary line from a `git diff --stat` block, or ''."""
    lines = [ln for ln in diff_stat.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def _latest_diff_summary(t: Task, runs: RunStore) -> str:
    """The diff summary from the most recent run that has one. A review run has no diff
    of its own, so this looks back to the work/revise run that actually pushed the PR."""
    for r in reversed(runs.runs_for(t.id)):
        if r.diff_stat:
            return _diff_summary(r.diff_stat)
    return ""


def _automated_review_is_current(st: Any) -> bool:
    """Whether the recorded automated approval examined the PR head now in state.

    A verdict without its reviewed head is deliberately treated as stale.  Older state files
    therefore wait for one fresh review rather than inviting a person to act on an unknown
    revision.
    """
    review = st.get("last_review") or {}
    return (str(review.get("verdict") or "") == "approve"
            and bool(st.get("head_sha"))
            and str(st.get("last_review_head") or "") == str(st.get("head_sha") or ""))


def automated_review_is_queued(t: Task, st: Any) -> bool:
    """Whether the scheduler owns the next review step for an in-review task."""
    return t.status == Status.IN_REVIEW and bool(st.get("review_run") or st.get("pending_reviews"))


def _automated_review_wait(t: Task, st: Any, sched: Any) -> str:
    """A concise operational explanation for a review the scheduler still owns."""
    pending = list(st.get("pending_reviews") or [])
    review = st.get("last_review") or {}
    verdict = str(review.get("verdict") or "")
    prior = ""
    if verdict:
        detail = str(review.get("summary") or "").strip()
        prior = f"; prior automated verdict: {verdict.replace('_', ' ')}" + (f" — {detail}" if detail else "")
    if st.get("review_run"):
        return "automated review running" + prior
    if pending:
        _, reason = sched.review_wait_reason(t)
        return f"automated review queued: {reason}" + prior
    if verdict:
        detail = str(review.get("summary") or "").strip()
        return f"last automated verdict: {verdict.replace('_', ' ')}" + (f" — {detail}" if detail else "") + "; a fresh review is required for this head"
    return "automated review not recorded yet; the scheduler will queue one"


def _evidence_lines(t: Task, st: Any, runs: RunStore | None) -> list[str]:
    """The evidence behind an attention card, as plain lines: recent runs, the last
    automated review, the PR state and the revision count."""
    out: list[str] = []
    for r in (runs.runs_for(t.id) if runs else [])[-2:]:
        line = f"run {r.run_id} ({r.mode}): {r.status}"
        detail = (r.error or "").strip() or str((r.result or {}).get("summary") or "").strip()
        if detail:
            line += f" — {detail[:140]}"
        diff_summary = _diff_summary(r.diff_stat)
        if diff_summary:
            line += f" · {diff_summary}"
        out.append(line)
        # Only a terminal interrupted check needs its full diagnostic on the card. Other
        # attention cards already have a concise run summary, and their check output can be
        # large or unrelated to the decision.
        stop = st.get("needs_human") or {}
        interrupted = isinstance(stop, dict) and stop.get("kind") == "check_did_not_run"
        if interrupted:
            for check in (r.result or {}).get("checks") or []:
                trace = str(check.get("details") or "").strip()
                if trace:
                    out.append(f"{check.get('name', 'check')} diagnostic:\n{trace}")
    rev = st.get("last_review") or {}
    if rev:
        out.append(f"last automated review: {str(rev.get('verdict', '')).replace('_', ' ')} — {str(rev.get('summary', ''))[:160]}")
    if t.pr:
        bits = ["draft" if st.get("pr_draft") else str(st.get("pr_state") or "open").lower()]
        if st.get("review_decision"):
            bits.append(f"review {str(st['review_decision']).lower().replace('_', ' ')}")
        if st.get("checks"):
            bits.append(f"CI {str(st['checks']).lower()}")
        out.append("PR: " + " · ".join(bits))
    if st.get("revisions"):
        out.append(f"{st['revisions']} revision round(s) used")
    if st.get("review_rounds"):
        out.append(f"{st['review_rounds']} automated review round(s) used")
    history = list(st.get("difficulty_escalations") or [])
    for row in history[-3:]:
        out.append(f"escalated {row.get('from')} → {row.get('to')} at revision {row.get('counter')} · model {row.get('model') or 'runner default'}")
    all_runs = runs.runs_for(t.id) if runs else []
    known_cost = sum(float(r.cost_usd or 0) for r in all_runs)
    if known_cost:
        out.append(f"cost so far: ${known_cost:.2f}")
    if st.get("troubled") or st.get("investigation") or (
        isinstance(st.get("needs_human"), dict)
        and st["needs_human"].get("kind") in ("troubled_task", "investigation", "investigation_report")
    ):
        out.append(f"current owner: {str((st.get('investigation') or {}).get('owner') or 'product owner')}")
    return out


def _evidence_links(t: Task, st: Any, runs: RunStore | None) -> list[dict[str, str]]:
    """Short links to the evidence named by the card, with currentness made explicit."""
    links = [
        {"label": f"{r.mode} run · {r.status}", "href": f"/runs/{t.id}/{r.run_id}"}
        for r in (runs.runs_for(t.id) if runs else [])[-2:]
    ]
    if t.pr:
        head = str(st.get("head_sha") or "")
        links.append({"label": "Current PR" + (f" at {head[:8]}" if head else " (head unavailable)"),
                      "href": t.pr})
    return links


def _attention_blockers(info: dict[str, str], st: Any) -> list[dict[str, str]]:
    """Keep independent operational and source blockers separate on a combined card."""
    label, _, _ = ATTENTION_OWNERS.get(info["kind"], ("Unclassified stop", "you", "Inspect the task"))
    blockers = [{"category": label, "summary": info["reason"]}]
    feedback = str(st.get("pending_feedback") or "").strip()
    checks = str(st.get("checks") or "").upper()
    if info["kind"] == "check_did_not_run" and (feedback or checks in {"FAILURE", "FAILED", "ERROR"}):
        summary = "The branch also has source or CI failures; retrying the interrupted check does not resolve them."
        blockers.append({"category": "Source or CI failure", "summary": summary})
    review = st.get("last_review") or {}
    verdict = str(review.get("verdict") or "")
    if info["kind"] == "check_did_not_run" and verdict in {"changes_requested", "request_changes"}:
        blockers.append({"category": "Review findings", "summary":
                         "The preserved review still asks for implementation changes on its recorded head."})
    return blockers


def discuss_prompt(t: Task, info: dict[str, str], evidence: list[str], actions: list[dict[str, str]]) -> str:
    """A ready-made prompt about a stopped task, for pasting into a chat session or
    `garden take`: the task, the reason, the PR, the run ids and the options."""
    title, blurb = ATTENTION_KINDS.get(info["kind"], ("Needs a decision", ""))
    lines = [
        f"I need to decide what to do with context-garden task {t.id} ({t.title}).",
        "",
        f"The loop stopped — {title.lower()}: {info['reason']}",
    ]
    if blurb:
        lines.append(blurb)
    if t.pr:
        lines.append(f"PR: {t.pr}")
    lines += evidence
    lines += ["", "My options:"]
    for a in actions:
        if a.get("command"):
            lines.append(f"- `{a['command']}` — {a.get('detail', '')}")
    lines += ["", "Tell me which option fits and why, or what to fix first. Ask me to paste the task file, the PR diff or a run log if you need more context."]
    return "\n".join(lines)


def attention_view(t: Task, st: Any, runs: RunStore | None = None) -> dict[str, Any] | None:
    """Everything an attention card needs: which decision is being asked, the evidence for
    it, and what each button will do. Shared by the Inbox, the task page and the CLI."""
    if t.status.terminal:
        return None
    info = needs_human_info(st.get("needs_human"))
    if info and info["kind"] == "review_cap" and any(row["state"] != "posted" for row in required_evidence_rows(required_evidence(t.body, t.extra.get("requires")), st)):
        return None
    can_resume = info is not None
    if info is None:
        if t.status != Status.FAILED:
            return None
        info = _failed_info(t)
    kind_title, kind_blurb = ATTENTION_KINDS.get(info["kind"], ("Needs a decision", ""))
    category, owner, recommendation = ATTENTION_OWNERS.get(
        info["kind"], ("Unclassified stop", "you", "Inspect the task before continuing"))
    evidence = _evidence_lines(t, st, runs)
    evidence_links = _evidence_links(t, st, runs)
    resume_to = _resume_target(t, st, info)
    retry_detail = ("keeps the PR and queues a revise run on this branch to address what is outstanding; it does not start the work over"
                    if t.pr and t.status in (Status.CHANGES_REQUESTED, Status.IN_REVIEW, Status.AWAITING_TRIAGE, Status.FAILED)
                    else "resets attempts and starts a fresh work run from the task brief")
    actions: list[dict[str, str]] = []
    stale_check_stop = (info["kind"] == "check_did_not_run"
                        and str(st.get("checks") or "").upper() in {"SUCCESS", "PASSED"}
                        and not st.get("check_run"))
    delegated = bool(info.get("delegated_recovery")) or info["kind"] == "check_did_not_run"
    reviewer_owned = info["kind"] == "review_clarification"
    check_recovery = info["kind"] == "check_did_not_run"
    if check_recovery:
        actionable = bool(str(st.get("pending_feedback") or "").strip()) or (
            str(st.get("checks") or "").upper() == "FAILURE"
        ) or bool(st.get("failed_checks"))
        actions.append({
            "label": "Recover check and continue revision" if actionable else "Recover check and resume pipeline",
            "kind": "recover-check", "command": f"garden recover-check {t.id}",
            "detail": ("clears this terminal check pointer and retains the current feedback/check failure for "
                       "an existing-branch revision" if actionable else
                       "clears this terminal check pointer and resumes pipeline progression without an implementation run"),
        })
    troubled = info["kind"] in ("troubled_task", "investigation", "investigation_report")
    if info["kind"] == "revision_cap" and not delegated:
        owner = "you"
        recommendation = "Authorize one more bounded revision"
    if stale_check_stop:
        category, owner, recommendation = "Stale bookkeeping", "operator", "Clear the resolved stop"
        actions.append({"label": "Clear resolved stop", "kind": "recover-check",
                        "command": f"garden recover-check {t.id}",
                        "detail": "keeps the successful current check and clears only the obsolete stop"})
    elif delegated:
        label = "Retry the interrupted check" if info["kind"] == "check_did_not_run" else "Send failures to the worker"
        actions.append({"label": label, "kind": "recover", "command": f"garden recover {t.id}",
                        "detail": "runs one guarded continuation with the current PR, head, feedback, and counters preserved; repeat clicks cannot duplicate it"})
    if can_resume and info["kind"] == "deployment":
        label = "Deployment completed — continue"
    elif can_resume and not reviewer_owned and not check_recovery and not troubled and not stale_check_stop:
        label = "Continue the loop"
        actions.append({"label": label, "kind": "resume", "command": f"garden resume {t.id}",
                        "detail": f"clears the stop and returns the task to {resume_to.replace('_', ' ')}; no run starts"})
    if info["kind"] == "review_cap" and t.pr:
        actions.append({"label": "One more automated review", "kind": "review-again", "command": f"garden review {t.id}",
                        "detail": "raises this task's review cap by one round and dispatches an automated review now"})
        actions.append({"label": "Send back with a note", "kind": "triage-changes", "command": f'garden triage {t.id} --changes "..."',
                        "detail": "queues a revise run against your note instead of an automated review"})
    if reviewer_owned and t.pr:
        actions.append({"label": "One more automated review", "kind": "review-again",
                        "command": f"garden review {t.id}",
                        "detail": "clears this reviewer-owned stop and requests another review; no author revision is queued"})
    if troubled:
        actions.append({"label": "Continue one revision", "kind": "troubled-continue", "command": f"garden troubled-continue {t.id}",
                        "detail": "grants one bounded revision under normal capacity; lifetime counts, feedback, branch and PR remain"})
        actions.append({"label": "Pause for investigation", "kind": "investigate", "command": f'garden investigate {t.id} "..."',
                        "detail": "records a bounded read-only diagnosis request; active work drains safely and no implementation restarts"})
        actions.append({"label": "Change approach", "kind": "change-approach", "command": f'garden troubled-change-approach {t.id} "..."',
                        "detail": "adds the owner's new approach to preserved feedback and queues one bounded revision"})
        actions.append({"label": "Defer", "kind": "defer", "command": f'garden troubled-defer {t.id} "..."',
                        "detail": "keeps all work and leaves implementation paused until an explicit later decision"})
        if info["kind"] == "investigation":
            if investigation := st.get("investigation"):
                if investigation.get("status") == "requested" and investigation.get("owner") == "operator":
                    actions.append({"label": "Take investigation", "kind": "investigation-take",
                                    "command": f"garden investigation-take {t.id}",
                                    "detail": "claims the bounded diagnosis for the operator; implementation stays paused"})
                if investigation.get("status") == "failed":
                    actions.append({"label": "Retry investigation agent", "kind": "investigation-retry",
                                    "command": f"garden investigation-retry {t.id}",
                                    "detail": "queues a new bounded diagnosis and retains the failed transcript and cost"})
            actions.append({"label": "Publish investigation report", "kind": "investigation-report",
                            "command": f'garden investigation-report {t.id} "..."',
                            "detail": "returns a durable diagnosis to the Inbox without restarting or cancelling the task"})
    elif not reviewer_owned and not delegated and not stale_check_stop and info["kind"] not in {"base_broken", "deployment"}:
        retry_label = "Send failure to the worker" if info["kind"] == "worker_failed" else "Send outstanding work to a worker"
        if info["kind"] == "revision_cap":
            retry_label = "Authorize one more revision"
        actions.append({"label": retry_label, "kind": "retry", "command": f"garden retry {t.id}",
                        "detail": retry_detail})
    actions.append({"label": "Discuss", "kind": "discuss", "command": f"garden discuss {t.id}",
                    "detail": "a ready-made prompt with the task, the reason and the evidence, for a chat session or `garden take`"})
    cancel_command = f'garden troubled-cancel {t.id} "..."' if troubled else f"garden cancel {t.id}"
    actions.append({"label": "Cancel", "kind": "troubled-cancel" if troubled else "cancel", "command": cancel_command,
                    "detail": ("requires a reason and closes only after the writer drains; branch, PR, runs and artifacts stay preserved"
                               if troubled else "kills any running worker and closes the task as cancelled" + ("; the PR stays open on GitHub" if t.pr else ""))})
    if t.pr:
        actions.append({"label": "Open PR", "kind": "link", "href": t.pr, "detail": "the pull request on GitHub"})
    investigation = st.get("investigation") if isinstance(st.get("investigation"), dict) else {}
    if investigation.get("report"):
        report = investigation["report"]
        if isinstance(report, dict):
            evidence.insert(0, f"investigation recommendation: {report.get('recommendation', 'not stated')}")
            evidence.insert(0, f"likely cause ({report.get('confidence', 'unknown')} confidence): {report.get('likely_cause', 'not stated')}")
        else:
            evidence.insert(0, "investigation report: " + str(report))
    effect = ("The task remains paused; its branch, PR, findings, and counters are preserved."
              if actions else "The task remains parked while the scheduler watches its prerequisite.")
    happened = {
        "check_did_not_run": "A required check was interrupted twice before it produced a result.",
        "env_error": "The worker could not complete because its execution environment failed repeatedly.",
        "worker_failed": "The implementation run ended without a usable result after its automatic retries.",
        "revision_cap": "Implementation or CI failures remain after the bounded revision rounds.",
        "parent_closed": "This branch can no longer merge because its parent PR closed without merging.",
        "rebase_failed": "The automated conflict repair did not complete after its bounded retry.",
        "review_cap": "The automated review allowance is spent and the current PR needs judgment.",
        "stall": "The same implementation outcome returned unchanged, so automatic work stopped.",
    }.get(info["kind"], kind_blurb or info["reason"])
    return {"kind": info["kind"], "kind_title": kind_title, "kind_blurb": kind_blurb, "reason": info["reason"],
            "category": category, "owner": owner, "recommendation": recommendation,
            "happened": happened, "effect": effect, "user_decision": owner == "you",
            "blockers": _attention_blockers(info, st),
            "resume_to": resume_to if can_resume else "", "evidence": evidence,
            "evidence_links": evidence_links, "actions": actions,
            "delegated": delegated or owner != "you",
            "discuss": discuss_prompt(t, info, evidence, actions)}


def decision_card_view(t: Task, st: Any, runs: RunStore | None = None) -> dict[str, Any] | None:
    """The pending human decision shown on both the Inbox and a task page.

    A worker report takes precedence over its status so an interrupted transition cannot send
    a person to a task page that has the report in state but no decision to make.
    """
    if t.status.terminal:
        return None
    dec = st.get("decision")
    if isinstance(dec, dict) and dec.get("kind"):
        kind = str(dec["kind"])
        cancelling = kind == "wont_do"
        return {
            "type": "worker_decision", "kind": kind,
            "title": "Decide whether to cancel this work" if cancelling else "Decide whether to change the promised outcome",
            "reason": str(dec.get("reason") or "(no reason given)"),
            "blurb": ("Recommendation: cancel the task. Cancelling ends it without merging; keeping it sends the reason back for another bounded revision."
                      if cancelling else
                      "Recommendation: accept the stated limitation. Accepting continues verification with that limitation; keeping the original outcome sends it back for another bounded revision."),
            "final": str(dec.get("final") or ""),
            "evidence": _evidence_lines(t, st, runs),
        }
    if t.status == Status.WAITING_HUMAN:
        question = str(st.get("question") or "").strip()
        if not question:
            evidence = _evidence_lines(t, st, runs)
            return {
                "type": "attention", "title": "Recovery needed: waiting state is incomplete",
                "reason": "No question was recorded, so there is nothing for you to answer.",
                "blurb": "This is an operational state mismatch. Continue the loop to reconcile the live run and task state.",
                "final": "", "evidence": evidence,
                "attention": {"category": "Stale bookkeeping",
                    "effect": "The task stays paused while its live check and continuation are reconciled.",
                    "owner": "operator", "recommendation": "Reconcile the recorded check state",
                    "user_decision": False, "blockers": [], "evidence_links": _evidence_links(t, st, runs),
                    "actions": [
                    {"label": "Reconcile state", "kind": "recover-check", "command": f"garden recover-check {t.id}",
                     "detail": "checks live run state and returns the task to the automated loop"}], "discuss": ""},
            }
        return {
            "type": "question", "title": "The worker is waiting for you",
            "reason": question,
            "blurb": "Its session resumes with your answer.", "final": "",
            "evidence": _evidence_lines(t, st, runs),
        }
    attention = attention_view(t, st, runs)
    if attention is not None:
        title = (f"Needs your decision: {attention['kind_title']}" if attention["user_decision"]
                 else f"Operator recovery: {attention['kind_title']}")
        return {"type": "attention", "title": title,
                "reason": attention["happened"], "blurb": attention["kind_blurb"], "final": "",
                "evidence": attention["evidence"], "evidence_links": attention["evidence_links"],
                "attention": attention}
    return None


def merge_queue_view(store: Store, state: Any, drop_events: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """What the merge queue is doing, for the Board/Inbox: the in-flight head (rebased, waiting
    for its rollup) and whether it is still waiting on CI, the candidates queued behind it with
    why each is held, and the last head that left the queue with its reason. Reads only queue
    state (written by `scheduler/queue.py`) and the `merge_head` events. Returns None when the
    queue is empty and nothing has ever dropped, so the panel stays hidden until it has news."""
    tasks = store.tasks()
    head: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []
    for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
        if t.status != Status.IN_REVIEW:
            continue
        st = state.get(t.id)
        if st.get("merge_head"):
            checks = str(st.get("checks") or "").upper()
            head = {"task": t.id, "title": t.title, "pr": t.pr,
                    "checks": checks.lower(), "waits_on_ci": checks in ("", "PENDING"),
                    "ready_at": str(st.get("automerge_ready_at") or "")}
        elif st.get("automerge_candidate"):
            candidates.append({"task": t.id, "title": t.title, "pr": t.pr,
                               "ready_at": str(st.get("automerge_ready_at") or ""),
                               "blocked": str(st.get("automerge_blocked") or "")})
    candidates.sort(key=lambda c: (c["ready_at"], c["task"]))
    last_drop: dict[str, str] | None = None
    for ev in reversed(drop_events or []):
        if ev.get("kind") == "merge_head" and ev.get("left"):
            last_drop = {"task": str(ev.get("task") or ""), "reason": str(ev.get("reason") or ""),
                         "at": str(ev.get("at") or "")}
            break
    if head is None and not candidates and last_drop is None:
        return None
    return {"head": head, "candidates": candidates, "last_drop": last_drop}


def build_inbox(store: Store, sched: Any) -> list[dict[str, Any]]:
    tasks = store.tasks()
    state = sched.state
    runs = getattr(sched, "runs", None) or RunStore(store.config.garden_dir)
    stack = bool(store.config.get("stack", True))
    ready_ids = {task.id for task in ready(tasks, stack=stack)}
    phases = {phase.key: phase for product in store.products() for phase in product.phases}
    items: list[dict[str, Any]] = []
    order = {g[0]: i for i, g in enumerate(GROUPS)}
    titles = {g[0]: g[1] for g in GROUPS}

    # A draft in a frozen phase (a feature freeze) usually belongs in the next one; offer to
    # move it there. `next_open_phase` maps a phase key to the next open, unfrozen phase of its
    # product, as "product/phase".
    frozen_phases: set[str] = set()
    next_open_phase: dict[str, str] = {}
    for prod in store.products():
        for i, ph in enumerate(prod.phases):
            if ph.frozen:
                frozen_phases.add(ph.key)
            nxt = next((p2 for p2 in prod.phases[i + 1:] if not p2.closed and not p2.frozen), None)
            if nxt:
                next_open_phase[ph.key] = f"{prod.name}/{nxt.name}"

    def add(group: str, t: Task, why: str, actions: list[dict[str, str]], **extra: Any) -> None:
        owner, owner_source = effective_owner(t, store.phase(t.product, t.phase))
        items.append({"group": group, "group_title": titles[group], "task": t.id, "title": t.title, "phase": t.key,
                      "status": t.status.value, "pr": t.pr, "why": why, "actions": actions, "age": _age(t.updated),
                      "difficulty": t.difficulty, "owner": owner, "owner_source": owner_source, **extra})

    for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
        st = state.get(t.id)
        recovery = st.get("review_recovery") or {}
        if recovery and st.get("pending_reviews") and not st.get("needs_human"):
            add("operator", t,
                f"automatic review recovery {recovery.get('attempts', 0)}/{recovery.get('limit', 0)} queued · scheduler owns the retry",
                [{"label": "Cancel", "kind": "cancel", "command": f"garden cancel {t.id}"}],
                kind="review_recovery", kind_title="Automatic review recovery",
                kind_blurb="The scheduler retained the current-head review request and will retry after admission and backoff permit it.",
                reason=str(recovery.get("reason") or "review did not produce a verdict"), evidence=[])

        # `build_inbox` also feeds lightweight reader facades in CLI/tests. Resolve the
        # configured runner from the task/store rather than requiring a live Scheduler.
        is_manual = (t.runner or store.config.product_runner(t.product)) == "manual"
        phase_hold = phase_refusal(phases[t.key], t) if t.key in phases else ""
        if is_manual and not t.status.terminal and t.status == Status.READY and not st.get("needs_human") and not st.get("decision"):
            if phase_hold:
                add("manual_waiting", t, f"waiting: {phase_hold}", [], kind="frozen")
            elif t.id not in ready_ids:
                add("manual_waiting", t, "waiting: dependencies must finish before this packet can be claimed", [], kind="blocked")
            elif any(run.task_id == t.id for run in runs.active()):
                add("manual_waiting", t, "claimed already; waiting for the existing manual session to finish", [], kind="claimed")
            else:
                add("manual", t, "ready for a person · assignment: unclaimed manual session", [
                    {"label": "Take task", "kind": "take", "command": f"garden take {t.id}"},
                    {"label": "Open task packet", "kind": "packet", "href": f"/tasks/{t.id}"},
                ])
            continue
        if is_manual and t.status == Status.RUNNING:
            add("manual_waiting", t, "claimed already; a manual session owns this task packet", [
                {"label": "Open assigned packet", "kind": "packet", "href": f"/tasks/{t.id}/packet"},
            ], kind="claimed")
            continue
        if is_manual and not t.status.terminal and t.status == Status.CHANGES_REQUESTED and not st.get("needs_human") and not st.get("decision"):
            if phase_hold:
                add("manual_waiting", t, f"waiting: {phase_hold}", [], kind="frozen")
            elif any(run.task_id == t.id for run in runs.active()):
                add("manual_waiting", t, "claimed already; waiting for the existing manual session to finish", [], kind="claimed")
            elif int(st.get("revisions", 0)) >= int(store.config.get("max_revisions", 3)):
                add("manual_waiting", t, "paused: revision limit reached; an Inbox decision is required before this task can resume", [], kind="paused")
            elif str(st.get("pending_feedback") or "").strip():
                add("manual", t, "paused for a person · revision feedback is ready to resume manually", [
                    {"label": "Resume task", "kind": "take", "command": f"garden take {t.id}"},
                    {"label": "Open task packet", "kind": "packet", "href": f"/tasks/{t.id}"},
                ])
            else:
                add("manual_waiting", t, "paused: revision feedback is required before this task can resume", [], kind="paused")
            continue
        if st.get("decision") and not t.status.terminal:
            dec = st.get("decision") or {}
            kind = str(dec.get("kind") or "")
            reason = str(dec.get("reason") or "(no reason given)")
            cancelling = kind == "wont_do"
            why = (("recommendation: cancel this task" if cancelling else "recommendation: accept a changed outcome")
                   + f" · why: {reason}")
            add("decision", t, why, [
                {"label": "Cancel this task" if cancelling else "Accept the changed outcome", "kind": "accept", "command": f"garden accept {t.id}"},
                {"label": "Keep this task" if cancelling else "Keep the original outcome", "kind": "reject", "command": f'garden reject {t.id} "..."'},
            ], decision_kind=kind, reason=reason, final=str(dec.get("final") or ""),
                decision_card=decision_card_view(t, st, runs), card_task=t)
        elif t.status == Status.WAITING_HUMAN:
            question = str(st.get("question") or "").strip()
            if question:
                add("question", t, question,
                    [{"label": "Answer", "kind": "answer", "command": f'garden answer {t.id} "..."'}], question=question,
                    decision_card=decision_card_view(t, st, runs), card_task=t)
            else:
                card = decision_card_view(t, st, runs)
                add("attention", t, "waiting state is incomplete; no question exists to answer",
                    card["attention"]["actions"], kind="state_mismatch", kind_title="Waiting state is incomplete",
                    kind_blurb=card["blurb"], reason=card["reason"], resume_to="", evidence=card["evidence"],
                    discuss="", decision_card=card, card_task=t)
        elif automated_review_is_queued(t, st):
            # A queued review owns the next step even when an earlier stop remains in state.
            # The scheduler's fresh review is the authority for this head; showing the old
            # stop as a human action would invite a person to bypass that workflow.
            add("automated_review", t, _automated_review_wait(t, st, sched), [],
                prior_verdict=str((st.get("last_review") or {}).get("verdict") or ""),
                review_head=str(st.get("last_review_head") or ""),
                current_head=str(st.get("head_sha") or ""))
            continue
        elif t.status == Status.AWAITING_TRIAGE:
            rev = st.get("last_review") or {}
            why = "draft PR open"
            if rev:
                why += f" · automated review: {str(rev.get('verdict', '')).replace('_', ' ')}"
            if st.get("review_run"):
                why += " · review running"
            diff_summary = _latest_diff_summary(t, runs)
            if diff_summary:
                why += f" · {diff_summary}"
            add("triage", t, why, [
                {"label": "Ready for review", "kind": "triage-ready", "command": f"garden triage {t.id} --ready"},
                {"label": "Send back", "kind": "triage-changes", "command": f'garden triage {t.id} --changes "..."'},
                {"label": "Open PR", "kind": "link", "href": t.pr},
            ], review=rev, diff_stat=diff_summary)
        elif t.status == Status.IN_REVIEW and not st.get("needs_human"):
            if st.get("ci_missing"):
                diagnostic = str(st.get("ci_diagnostic") or "CI has not reported a status for this PR head")
                add("operator", t, diagnostic, [
                    {"label": "Open PR", "kind": "link", "href": t.pr,
                     "detail": "inspect or re-run the configured CI provider; the PR and its feedback remain unchanged"},
                ], kind="ci_missing", kind_title="CI status missing",
                    kind_blurb="This is an operational prerequisite, not approval of the product outcome.",
                    reason=diagnostic, evidence=_evidence_lines(t, st, runs))
                continue
            # A current approval is actionable only after every automated review of this
            # head has finished.  A queued or running follow-up remains scheduler-owned.
            if st.get("review_run") or st.get("pending_reviews"):
                add("automated_review", t, _automated_review_wait(t, st, sched), [],
                    prior_verdict=str((st.get("last_review") or {}).get("verdict") or ""),
                    review_head=str(st.get("last_review_head") or ""),
                    current_head=str(st.get("head_sha") or ""))
            elif _automated_review_is_current(st):
                why = "automated review approved this PR head"
                if st.get("checks"):
                    why += f" · CI {st['checks'].lower()}"
                if st.get("automerge_blocked"):
                    why += f" · automerge held: {st['automerge_blocked']}"
                add("review", t, why, [{"label": "Open PR", "kind": "link", "href": t.pr}],
                    automerge_blocked=str(st.get("automerge_blocked") or ""))
            else:
                add("automated_review", t, _automated_review_wait(t, st, sched), [],
                    prior_verdict=str((st.get("last_review") or {}).get("verdict") or ""),
                    review_head=str(st.get("last_review_head") or ""),
                    current_head=str(st.get("head_sha") or ""))
        if (st.get("needs_human") and not t.status.terminal) or t.status == Status.FAILED:
            att = attention_view(t, st, runs)
            if att:
                add("operator" if att.get("delegated") or att["kind"] == "deployment" else "attention", t,
                    f"{att['kind_title']} — {att['reason'][:140]}", att["actions"],
                    **{k: att[k] for k in ("kind", "kind_title", "kind_blurb", "reason", "category",
                                           "owner", "recommendation", "happened", "effect", "user_decision",
                                           "blockers", "resume_to", "evidence", "evidence_links", "discuss")},
                    decision_card=decision_card_view(t, st, runs), card_task=t)
        elif t.status == Status.DRAFT:
            eff = effective_status(t, tasks, stack)
            why = "discovered by " + t.discovered_from if t.discovered_from else "planned, not yet approved"
            if eff == "blocked":
                why += " · blocked until deps merge"
            last = _last_log_line(t)
            if t.attempts:
                why += f" · {t.attempts} attempt{'s' if t.attempts != 1 else ''}"
            if last:
                why += f" · {last}"
            move_to = next_open_phase.get(t.key, "") if t.key in frozen_phases else ""
            gaps = brief_gaps(store, t)
            if gaps:
                why += " · brief incomplete, fix before approving"
            actions = [{"label": "Approve", "kind": "approve", "command": f"garden approve {t.id}"}]
            if move_to:
                actions.append({"label": f"Move to {move_to.split('/', 1)[1]}", "kind": "move",
                                "command": f"garden move {t.id} {move_to}"})
            actions.append({"label": "Drop", "kind": "cancel", "command": f"garden cancel {t.id}"})
            group = "deferred" if t.key in frozen_phases else "approve"
            if group == "deferred":
                why = "deferred by the phase freeze"
                if last:
                    why += f" · policy: {last}"
                elif move_to:
                    why += f" · move deliberately to {move_to.split('/', 1)[1]} when ready"
                actions = ([{"label": f"Move to {move_to.split('/', 1)[1]}", "kind": "move",
                             "command": f"garden move {t.id} {move_to}"}] if move_to else [])
            add(group, t, why, actions, attempts=t.attempts, last_log=last, move_to=move_to,
                move_label=move_to.split("/", 1)[1] if move_to else "",
                phase_name=t.phase, approve_phases=approve_phase_options(store, t), gaps=gaps)
        if t.attempts > 0 and not st.get("needs_human") and not t.status.terminal and t.status in (Status.READY, Status.RUNNING) and not (t.status == Status.RUNNING and t.attempts <= 1):
            last = _last_log_line(t)
            why = last or f"{t.attempts} attempt{'s' if t.attempts != 1 else ''} failed"
            add("retrying", t, why, [{"label": "Cancel", "kind": "cancel", "command": f"garden cancel {t.id}"}],
                attempts=t.attempts, last_log=last)
        hold = st.get("infrastructure_hold")
        if isinstance(hold, dict) and hold.get("diagnostic"):
            add("operator", t, f"capture prerequisite: {hold['diagnostic']}", [
                {"label": "Check environment", "kind": "doctor", "command": "garden doctor",
                 "detail": "verify the prepared worker environment, then the scheduler will retry admission"},
            ], kind="infrastructure_hold", kind_title="Capture runtime unavailable",
                kind_blurb="No worker attempt was consumed; this is an operator environment repair, not a product decision.",
                reason=str(hold["diagnostic"]), evidence=[])
        scope = st.get("operator_scope")
        if isinstance(scope, dict) and scope.get("steps"):
            steps = list(scope["steps"])
            summary = "; ".join(f"{step.get('path')}: {step.get('action')}" for step in steps)
            add("operator", t, f"live-config prerequisite: {summary}", [
                {"label": "Record operator evidence", "kind": "operator-evidence", "command": f'garden evidence {t.id} "..."',
                 "detail": "record what was verified; the worker remains restricted to its checkout"},
            ], kind="operator_scope", kind_title="Operator-owned configuration",
                kind_blurb="This configuration is outside the worker checkout. Verify it as an operator, then dispatch resumes without granting production-write access.",
                reason=summary, evidence=[])

    up = getattr(sched, "upgrade_available", lambda: None)()
    if up:
        sha = str(up.get("sha") or "")[:12]
        count = up.get("count")
        status = str(up.get("status") or "available")
        why = f"tool update {status}: {sha}"
        if count is not None:
            why += f", {count} commit{'s' if count != 1 else ''} on configured base since {str(up.get('from') or '')[:12] or 'the current install'}"
        if up.get("reason"):
            why += f" · {up['reason']}"
        if up.get("diagnosis") and status == "failed":
            why += f" · {up['diagnosis']}"
        items.append({"group": "tool", "group_title": titles["tool"], "task": "", "title": f"{up.get('product', 'tool')} → {sha}",
                      "phase": "", "status": "", "pr": "", "why": why,
                      "actions": ([{"label": "Upgrade", "kind": "upgrade", "command": "garden upgrade"}]
                                  if status in {"available", "held"} else []),
                      "age": _age(str(up.get("at") or "")), "difficulty": ""})

    for d in getattr(sched, "pending_decisions", list)():
        if d.get("kind") == "question":
            source = str(d.get("source") or d.get("discovered_from") or "")
            origin = "retro" if source.startswith("retro:") else "kickoff"
            items.append({
            "group": "question", "group_title": titles["question"], "task": "",
                "title": str(d.get("question") or ""),
                "phase": str(d.get("phase") or ""), "status": "", "pr": "",
                "why": f"the {d.get('phase') or 'phase'} {origin} is asking",
                "question_context": str(d.get("context") or ""),
                "question_options": list(d.get("options") or []),
                "actions": [
                    {"label": "Answer", "kind": "decision-answer", "command": f"garden decide {d['id']} --answer '...'"},
                    {"label": "Dismiss", "kind": "decision-dismiss", "command": f"garden decide {d['id']} --dismiss"},
                ],
                "age": _age(str(d.get("at") or "")), "difficulty": "",
                "decision": str(d.get("id") or ""), "decision_kind": "question",
            })
            continue
        target = str(d.get("target", ""))
        tgt = tasks.get(target)
        reason = str(d.get("reason") or "").strip()
        proposer = str(d.get("proposed_by") or "a worker")
        if d.get("kind") == "duplicate":
            why = f"{proposer} says this duplicates {d.get('of') or 'another task'}"
        else:
            why = f"{proposer} says this task is now obsolete"
        if reason:
            why += f': "{reason}"'
        items.append({
            "group": "attention", "group_title": titles["attention"], "task": target,
            "title": (tgt.title if tgt else str(d.get("target_title") or "")) or target,
            "phase": str(d.get("phase") or ""), "status": tgt.status.value if tgt else "",
            "pr": tgt.pr if tgt else "", "why": why,
            "actions": ([
                {"label": "Cancel this task", "kind": "decision-accept", "command": f"garden decide {d['id']} --accept"},
                {"label": "Keep this task", "kind": "decision-reject", "command": f"garden decide {d['id']} --reject"},
            ] if d.get("kind") == "cancel" else [
                {"label": f"Use {d.get('of') or 'the existing task'}", "kind": "decision-accept", "command": f"garden decide {d['id']} --accept"},
                {"label": "Keep both tasks", "kind": "decision-reject", "command": f"garden decide {d['id']} --reject"},
            ]),
            "age": _age(tgt.updated if tgt else str(d.get("at") or "")),
            "difficulty": tgt.difficulty if tgt else "",
            "decision": str(d.get("id") or ""), "decision_kind": str(d.get("kind") or ""),
        })

    for v in getattr(sched, "pending_retro_verdicts", list)():
        phase_key = str(v.get("phase_key") or "")
        product, _, phase_name = phase_key.partition("/")
        blocking_ids = [str(b) for b in (v.get("blocking_ids") or [])]
        names = [f"{bid} ({tasks[bid].title})" if bid in tasks else bid for bid in blocking_ids]
        why = ("reopen: " + ", ".join(names) + " must land before the phase can close") if names \
            else "reopen: the retro named no blocking tasks"
        gaps = v.get("brief_gaps") or {}
        if isinstance(gaps, dict) and gaps:
            why += "; brief needed: " + ", ".join(
                f"{tid} ({gap})" for tid, gap in sorted(gaps.items())
            )
        items.append({
            "group": "retro_verdict", "group_title": titles["retro_verdict"], "task": "",
            "title": phase_key, "phase": phase_key, "status": "", "pr": "",
            "why": why, "age": _age(str(v.get("at") or "")), "difficulty": "",
            "product": product, "phase_name": phase_name, "blocking_ids": blocking_ids,
            "actions": [
                {"label": "Accept reopen", "kind": "retro-decide-reopen", "command": f"garden retro-decide {phase_key} reopen"},
                {"label": "Change to close", "kind": "retro-decide-close", "command": f"garden retro-decide {phase_key} close"},
            ],
        })

    hold = getattr(sched, "config_hold", dict)()
    if hold:
        keys = ", ".join(hold.get("keys") or [])
        runs = ", ".join(hold.get("runs") or [])
        items.append({"group": "config_hold", "group_title": titles["config_hold"], "task": "", "title": keys,
                      "phase": "", "status": "", "pr": "", "why": f"held since {hold.get('since', '')}; runs: {runs}",
                      "actions": [{"label": "Confirm now", "kind": "config-accept", "command": "garden config accept"}],
                      "age": _age(str(hold.get("since") or "")), "difficulty": ""})

    for name, entry in sorted(getattr(sched, "paused_harnesses", dict)().items()):
        items.append({"group": "harness", "group_title": titles["harness"], "task": "", "title": name, "phase": "",
                      "status": "", "pr": "", "why": str(entry.get("reason") or "paused"),
                      "actions": [], "age": _age(str(entry.get("at") or "")), "difficulty": ""})

    for key in sorted({t.key for t in tasks.values()}):
        budget = sched.budget_for(key)
        if budget and sched.spent_for(key) >= budget:
            probe = next(t for t in tasks.values() if t.key == key)
            items.append({"group": "budget", "group_title": titles["budget"], "task": "", "title": key, "phase": key,
                          "status": "", "pr": "", "why": f"spent ${sched.spent_for(key):.2f} of ${budget:.2f}; dispatch paused",
                          "actions": [{"label": "Raise in garden.yaml", "kind": "config", "command": f"# budgets: {{{key}: <usd>}}"}],
                          "age": "", "difficulty": probe.difficulty})
    items.sort(key=lambda i: (order[i["group"]], i["task"]))
    return items


def counts(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it["group"]] = out.get(it["group"], 0) + 1
    return out


def decisions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items whose group needs a person's call — what the badge and digest count."""
    return [i for i in items if needs_you(i)]


def needs_you(item: dict[str, Any]) -> bool:
    """Whether an Inbox item needs a person, shared by every surface's badge or count."""
    return GROUP_KIND.get(str(item.get("group") or "")) == "decision"


def notices(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items whose group is informational only — rendered, never counted."""
    return [i for i in items if GROUP_KIND.get(i["group"]) == "notice"]


def running_now(store: Store) -> list[dict[str, Any]]:
    rs = RunStore(store.config.garden_dir)
    tasks = store.tasks()
    warn = float(store.config.get("idle_minutes", 0) or 0)
    out = []
    for r in rs.active():
        has_process = r.pid is not None and not r.process_finished()
        if not has_process:
            continue
        t = tasks.get(r.task_id)
        idle = round(r.idle_minutes())
        out.append({"task": r.task_id, "title": t.title if t else "", "mode": r.mode, "model": r.model,
                    "minutes": round(r.elapsed_minutes()), "host": r.host,
                    "idle": idle if warn and idle >= warn else None})
    return out
