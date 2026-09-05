"""The inbox: everything that needs a human, grouped by the kind of decision, with the
concrete action that resolves each item. The web Inbox page, `garden inbox` and the TUI
Inbox tab all render this one list."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .graph import effective_status
from .model import Status, Task
from .runs import RunStore
from .store import Store

# Each group carries a "kind": "decision" means a person's call is what unblocks the
# item, so it counts toward the badge, the "need you" figure and the digest. "notice" is
# informational — the loop is already handling it — so it renders but never counts.
GROUPS = [
    ("tool", "Upgrade the garden's tool", "A PR merged into the tool's own product; the pinned install can move forward onto the merged code.", "notice"),
    ("question", "Answer a worker's question", "A worker paused and is waiting for you. Its session resumes with your answer.", "decision"),
    ("decision", "Accept or reject a worker's call", "A worker says the task should not be done, or a revise round had nothing to change. Read its reasoning, then accept or send it back with a note.", "decision"),
    ("triage", "Triage a draft PR", "A worker finished and opened a draft. Your first look decides: ready for review, or send it back.", "decision"),
    ("review", "Review and merge", "Ready for review on GitHub. Comments you leave become a revise run; merging unblocks dependents.", "decision"),
    ("attention", "Needs a decision", "The loop stopped on purpose: a stall, a cap, a closed PR, a failed worker.", "decision"),
    ("retrying", "Auto-retrying", "A previous attempt failed; a new run is queued or in progress. No action needed unless you want to cancel.", "notice"),
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


def _last_log_line(t: Task) -> str:
    """Return the message portion of the last log entry in the task body, or ''."""
    for ln in reversed(t.body.splitlines()):
        if ln.startswith("- "):
            return ln[2:].split(" ", 1)[-1]
    return ""


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
}


def needs_human_info(raw: Any) -> dict[str, str] | None:
    """Normalize the needs_human flag to {kind, reason, prior_status, at}. The scheduler
    writes a dict since CG-045; older state files hold a bare reason string."""
    if not raw:
        return None
    if isinstance(raw, dict):
        reason = str(raw.get("reason", ""))
        return {"kind": str(raw.get("kind") or _guess_kind(reason)), "reason": reason,
                "prior_status": str(raw.get("prior_status", "")), "at": str(raw.get("at", ""))}
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
    return out


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
    can_resume = info is not None
    if info is None:
        if t.status != Status.FAILED:
            return None
        info = _failed_info(t)
    kind_title, kind_blurb = ATTENTION_KINDS.get(info["kind"], ("Needs a decision", ""))
    evidence = _evidence_lines(t, st, runs)
    resume_to = _resume_target(t, st, info)
    retry_detail = ("keeps the PR and queues a revise run on this branch to address what is outstanding; it does not start the work over"
                    if t.pr and t.status in (Status.CHANGES_REQUESTED, Status.IN_REVIEW, Status.AWAITING_TRIAGE, Status.FAILED)
                    else "resets attempts and starts a fresh work run from the task brief")
    actions: list[dict[str, str]] = []
    if can_resume:
        actions.append({"label": "Nothing to fix, resume", "kind": "resume", "command": f"garden resume {t.id}",
                        "detail": f"clears the stop and returns the task to {resume_to.replace('_', ' ')}; no run starts"})
    if info["kind"] == "review_cap" and t.pr:
        actions.append({"label": "One more automated review", "kind": "review-again", "command": f"garden review {t.id}",
                        "detail": "raises this task's review cap by one round and dispatches an automated review now"})
        actions.append({"label": "Send back with a note", "kind": "triage-changes", "command": f'garden triage {t.id} --changes "..."',
                        "detail": "queues a revise run against your note instead of an automated review"})
    actions.append({"label": "Continue the loop" if can_resume else "Retry", "kind": "retry", "command": f"garden retry {t.id}",
                    "detail": retry_detail})
    actions.append({"label": "Discuss", "kind": "discuss", "command": f"garden discuss {t.id}",
                    "detail": "a ready-made prompt with the task, the reason and the evidence, for a chat session or `garden take`"})
    actions.append({"label": "Cancel", "kind": "cancel", "command": f"garden cancel {t.id}",
                    "detail": "kills any running worker and closes the task as cancelled" + ("; the PR stays open on GitHub" if t.pr else "")})
    if t.pr:
        actions.append({"label": "Open PR", "kind": "link", "href": t.pr, "detail": "the pull request on GitHub"})
    return {"kind": info["kind"], "kind_title": kind_title, "kind_blurb": kind_blurb, "reason": info["reason"],
            "resume_to": resume_to if can_resume else "", "evidence": evidence, "actions": actions,
            "discuss": discuss_prompt(t, info, evidence, actions)}


def build_inbox(store: Store, sched: Any) -> list[dict[str, Any]]:
    tasks = store.tasks()
    state = sched.state
    runs = getattr(sched, "runs", None) or RunStore(store.config.garden_dir)
    stack = bool(store.config.get("stack", True))
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
        items.append({"group": group, "group_title": titles[group], "task": t.id, "title": t.title, "phase": t.key,
                      "status": t.status.value, "pr": t.pr, "why": why, "actions": actions, "age": _age(t.updated),
                      "difficulty": t.difficulty, **extra})

    for t in sorted(tasks.values(), key=lambda t: (t.priority, t.id)):
        st = state.get(t.id)
        if t.status == Status.WAITING_HUMAN and st.get("decision"):
            dec = st.get("decision") or {}
            kind = str(dec.get("kind") or "")
            reason = str(dec.get("reason") or "(no reason given)")
            verb = "will not do this task" if kind == "wont_do" else "found nothing to change this round"
            add("decision", t, f"the worker {verb}: {reason}", [
                {"label": "Accept", "kind": "accept", "command": f"garden accept {t.id}"},
                {"label": "Reject", "kind": "reject", "command": f'garden reject {t.id} "..."'},
            ], decision_kind=kind, reason=reason, final=str(dec.get("final") or ""))
        elif t.status == Status.WAITING_HUMAN:
            add("question", t, str(st.get("question") or "(no question recorded)"),
                [{"label": "Answer", "kind": "answer", "command": f'garden answer {t.id} "..."'}], question=st.get("question", ""))
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
            why = (st.get("review_decision") or "no review yet").lower().replace("_", " ")
            if st.get("checks"):
                why += f" · CI {st['checks'].lower()}"
            if st.get("automerge_blocked"):
                why += f" · automerge held: {st['automerge_blocked']}"
            add("review", t, why, [{"label": "Open PR", "kind": "link", "href": t.pr},
                                   {"label": "Mark done", "kind": "done", "command": f"garden set-status {t.id} done"}],
                automerge_blocked=str(st.get("automerge_blocked") or ""))
        if (st.get("needs_human") and not t.status.terminal) or t.status == Status.FAILED:
            att = attention_view(t, st, runs)
            if att:
                add("attention", t, f"{att['kind_title']} — {att['reason'][:140]}", att["actions"],
                    **{k: att[k] for k in ("kind", "kind_title", "kind_blurb", "reason", "resume_to", "evidence", "discuss")})
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
            actions = [{"label": "Approve", "kind": "approve", "command": f"garden approve {t.id}"}]
            if move_to:
                actions.append({"label": f"Move to {move_to.split('/', 1)[1]}", "kind": "move",
                                "command": f"garden move {t.id} {move_to}"})
            actions.append({"label": "Drop", "kind": "cancel", "command": f"garden cancel {t.id}"})
            add("approve", t, why, actions, attempts=t.attempts, last_log=last, move_to=move_to,
                move_label=move_to.split("/", 1)[1] if move_to else "",
                phase_name=t.phase, approve_phases=approve_phase_options(store, t))
        if t.attempts > 0 and not st.get("needs_human") and not t.status.terminal and t.status in (Status.READY, Status.RUNNING) and not (t.status == Status.RUNNING and t.attempts <= 1):
            last = _last_log_line(t)
            why = last or f"{t.attempts} attempt{'s' if t.attempts != 1 else ''} failed"
            add("retrying", t, why, [{"label": "Cancel", "kind": "cancel", "command": f"garden cancel {t.id}"}],
                attempts=t.attempts, last_log=last)

    up = getattr(sched, "upgrade_available", lambda: None)()
    if up:
        sha = str(up.get("sha") or "")[:12]
        count = up.get("count")
        why = f"tool update available: {sha}"
        if count is not None:
            why += f", {count} merged PR{'s' if count != 1 else ''} since {str(up.get('from') or '')[:12] or 'the current install'}"
        items.append({"group": "tool", "group_title": titles["tool"], "task": "", "title": f"{up.get('product', 'tool')} → {sha}",
                      "phase": "", "status": "", "pr": "", "why": why,
                      "actions": [{"label": "Upgrade", "kind": "upgrade", "command": "garden upgrade"}],
                      "age": _age(str(up.get("at") or "")), "difficulty": ""})

    for d in getattr(sched, "pending_decisions", list)():
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
            "actions": [
                {"label": "Accept", "kind": "decision-accept", "command": f"garden decide {d['id']} --accept"},
                {"label": "Reject", "kind": "decision-reject", "command": f"garden decide {d['id']} --reject"},
            ],
            "age": _age(tgt.updated if tgt else str(d.get("at") or "")),
            "difficulty": tgt.difficulty if tgt else "",
            "decision": str(d.get("id") or ""), "decision_kind": str(d.get("kind") or ""),
        })

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
    return [i for i in items if GROUP_KIND.get(i["group"]) == "decision"]


def notices(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items whose group is informational only — rendered, never counted."""
    return [i for i in items if GROUP_KIND.get(i["group"]) == "notice"]


def running_now(store: Store) -> list[dict[str, Any]]:
    rs = RunStore(store.config.garden_dir)
    tasks = store.tasks()
    warn = float(store.config.get("idle_minutes", 0) or 0)
    out = []
    for r in rs.active():
        t = tasks.get(r.task_id)
        idle = round(r.idle_minutes())
        out.append({"task": r.task_id, "title": t.title if t else "", "mode": r.mode, "model": r.model,
                    "minutes": round(r.elapsed_minutes()), "host": r.host,
                    "idle": idle if warn and idle >= warn else None})
    return out
