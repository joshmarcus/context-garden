#!/usr/bin/env python3
"""Stand-in for the `claude` binary in tests.

Takes the brief, does something to the cwd (a git worktree) depending on FAKE_CLAUDE_MODE,
and returns a `claude -p --output-format json`-shaped result. `run()` is the entry point
the suite's in-process runner calls (see tests/inprocess.py): no subprocess, no global
cwd or environment, so a scheduler test never waits on a worker. `main()` is the same
thing as a script (stdin, argv, os.environ, the process cwd) for the one path that still
launches a real command: the ssh runner's remote script.

The modes are two tables. `SPECIAL` holds the runs that are not a worker round (crash, stall,
the planner, a comparison, a persona, a retro, a kickoff review, an edit, and every `review-*`
verdict);
`WORKERS` holds one row per worker mode, describing what the round does before it commits,
whether it commits, and how its final message differs from a plain "done". Add a mode by
adding a row, not a branch.

Modes: done (default) | nocommit | blocked | crash | stall (never finishes: no output, no exit)
       | quota (an is_error result carrying the monthly spend-limit message, no commit)
       | noresult | plan | review-ok | review-bad | review-desc
       | review-rewrite (description-only review that returns description_rewrite)
       | review-approve-rewrite (approve verdict, description_ok false, with description_rewrite)
       | review-approve-desc (approve verdict, description_ok false, no rewrite: dispatches a description round)
       | needs_input (asks once; a --resume run finishes) | discover (done + discovered work)
       | discover-kinds (done + a task, a duplicate + cancel decision, and a note)
       | discover-same (done + the identical discovery every run, to test dedup across workers)
       | friction (done + a friction list in the result, none in the body)
       | omit-body (a revise round that omits pr_body, leaving the description unchanged)
       | nochange (revise rounds commit nothing) | revise-with-comment (revise with pr_comment) | conflict (edits README.md to collide with main)
       | rebase-resolve (redoes an aborted rebase and resolves it, favouring this branch)
       | wont_do (first run reports wont_do; a revise run after a reject finishes normally)
       | no_change (first run finishes normally; a revise round reports no_change)
       | escape (leaves the worktree and writes/commits in another repo, whatever the brief said)
       | edit (returns a revised task body folding in the ## Suggestions from the edit brief)
       | skip-criterion (done, but the `verified` list silently omits the first acceptance criterion)
       | qa (the `garden qa` agent: every flow ok, or FAKE_CLAUDE_QA_FAIL's flow failed, plus one finding)
Records the model it was given in model.txt (cwd) and the brief in FAKE_CLAUDE_BRIEF_COPY;
with FAKE_CLAUDE_ENV_DUMP set it also writes its own environment there (used to assert on the
ssh runner's remote scrub).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path


class Stall(Exception):
    """The worker goes silent and never finishes: no commit, no file change, no output."""


@dataclass
class Call:
    """One invocation: the mode, the brief, the worktree it runs in, its environment, and
    what the arguments say about the run."""

    mode: str
    brief: str
    args: list[str]
    model: str
    stream: bool
    resumed: bool
    cwd: Path = field(default_factory=Path.cwd)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    escaped_path: str = ""

    @property
    def revise(self) -> bool:
        return "Revision round" in self.brief


def result_json(final: str, usage: dict, cost: float, **extra) -> str:
    obj = {"type": "result", "subtype": "success", "is_error": False, "result": final,
           "usage": usage, "total_cost_usd": cost, **extra}
    return json.dumps(obj)


def git_commit(message: str, cwd: Path | str, env: Mapping[str, str]) -> None:
    subprocess.run(["git", "add", "-A"], cwd=cwd, env=dict(env), check=True)
    subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake",
                    "commit", "-q", "-m", message], cwd=cwd, env=dict(env), check=True)


# ---- runs that are not a worker round -----------------------------------------------------
# A handler returns the exit code (None for 0) or raises Stall.

def crash(call: Call) -> int:
    print("boom", file=sys.stderr)
    return 1


def stall(call: Call) -> None:
    # A worker that goes silent: the scheduler must notice it is idle and stop it.
    raise Stall


def quota(call: Call) -> int:
    # The account, not the worker: claude -p returns an error result carrying the monthly
    # spend-limit message, no commit, no GARDEN_RESULT.
    print(result_json("You've hit your monthly spend limit. Upgrade your plan or wait for it to reset.",
                      {"input_tokens": 20, "output_tokens": 0}, 0.0, is_error=True, subtype="error_during_execution"))
    return 0


def plan(call: Call) -> None:
    items = [
        {"title": "First planned task", "priority": 1, "estimate": "S", "depends_on": [], "reading": [], "body": "## Goal\n\nA."},
        {"title": "Second planned task", "priority": 2, "estimate": "M", "depends_on": ["First planned task"], "reading": [], "body": "## Goal\n\nB."},
    ]
    print(result_json(json.dumps(items), {"input_tokens": 100, "output_tokens": 50}, 0.01))


def compare(call: Call) -> None:
    labels = re.findall(r"^- \*\*(.+?)\*\* — branch", call.brief, flags=re.M)
    winner = call.env.get("FAKE_CLAUDE_WINNER") or labels[-1]
    ranking = [{"label": lb, "score": (9 if lb == winner else 6), "summary": f"{lb} did fine"} for lb in labels]
    verdict = {"winner": winner, "rationale": "the winner had tests", "ranking": ranking}
    print(result_json("Compared.\nGARDEN_COMPARE: " + json.dumps(verdict), {"input_tokens": 3000, "output_tokens": 200}, 0.03))


def persona(call: Call) -> None:
    m = re.search(r"^# Persona review: ([a-z0-9-]+)", call.brief, flags=re.M)
    name = m.group(1) if m else "persona"
    if call.env.get("FAKE_CLAUDE_PERSONA_FINDINGS") == "all":
        # one finding at each severity, for CG-187: every severity must survive filing
        findings = [
            {"severity": "high", "area": "security", "summary": "Secrets can leak into run logs",
             "suggestion": "Scrub env before logging"},
            {"severity": "medium", "area": "config", "summary": "garden.yaml needs a restart to take effect",
             "suggestion": "Reload config each tick"},
            {"severity": "low", "area": "copy", "summary": "The Inbox button label is inconsistent",
             "suggestion": "Match the wording used on the Board"},
        ]
    else:
        sev = call.env.get("FAKE_CLAUDE_PERSONA_SEVERITY", "medium")
        findings = [{"severity": sev, "area": "onboarding", "summary": "First run needs a config file the README never mentions",
                    "suggestion": "Add it to Quick start"}]
    rev = {"persona": name, "score": 7, "overall": f"As the {name}, mostly fine.", "findings": findings}
    sm = re.search(r"keyed by name \(([^)]+)\)", call.brief)
    if sm:
        # the persona declared its own sections; the brief lists them, so report each one
        section_names = [s.strip() for s in sm.group(1).split(",") if s.strip()]
        sections: dict[str, object] = {}
        for s in section_names:
            if s == "features":
                sections[s] = [
                    {"title": "A form to file a task from the web", "difficulty": "medium", "priority": 2,
                     "body": "Let a user file a task without editing markdown.", "rationale": "week-one need"},
                    {"title": "Show cost per phase on the phase page", "difficulty": "easy", "priority": 3,
                     "body": "A manager sees spend without reading a transcript.", "rationale": "the team next"},
                ]
            else:
                sections[s] = f"The {name}'s {s} section, in its own words."
        rev["sections"] = sections
    print(result_json("Reviewed as persona.\nGARDEN_PERSONA: " + json.dumps(rev), {"input_tokens": 1500, "output_tokens": 120}, 0.02))


def retro(call: Call) -> None:
    fsec = re.search(r"## Harvested friction.*?(?=\n## |\Z)", call.brief, flags=re.S)
    friction_ids = re.findall(r"^### (\S+):", fsec.group(0), flags=re.M) if fsec else []
    msec = re.search(r"## Merged pull requests.*?(?=\n## |\Z)", call.brief, flags=re.S)
    merged_ids = re.findall(r"^- (\S+) —", msec.group(0), flags=re.M) if msec else []
    tsec = re.search(r"## Phase task list with statuses.*?(?=\n## |\Z)", call.brief, flags=re.S)
    task_titles = re.findall(r"^- \S+ \[\S+\] (.+)$", tsec.group(0), flags=re.M) if tsec else []
    cycle = ["fixed", "still_true", "outdated", "disputed"]
    recon = []
    for i, fid in enumerate(friction_ids):
        v = cycle[i % len(cycle)]
        recon.append({"item": f"friction from {fid}", "logged": fid,
                      "pr": (merged_ids[0] if v == "fixed" and merged_ids else ""),
                      "verdict": v, "evidence": f"reconciled {fid} against the merged work"})
    # two new features, plus a proposal that duplicates the phase's first task by title (so
    # the reap can demonstrate the duplicate-skip path with no fixture wiring beyond this)
    features = [
        {"title": "Add a task-creation form to the web UI", "difficulty": "medium", "priority": 2,
         "body": "Let a human file a task from the web without editing markdown.", "rationale": "asked for at this retro"},
        {"title": "One vocabulary across CLI, web and TUI", "difficulty": "medium", "priority": 3,
         "body": "Use the same words for the same concepts on every surface.", "rationale": "personas keep tripping over inconsistent names"},
    ]
    if task_titles:
        features.append({"title": task_titles[0], "difficulty": "easy", "priority": 4,
                         "body": "Already covered.", "rationale": "already tracked"})
    # The phase verdict: `close` by default; a test sets FAKE_RETRO_VERDICT to drive the
    # `close_with_followups` (a follow-up in the next phase) and `reopen` (a blocking task in
    # this phase) paths.
    verdict = os.environ.get("FAKE_CLAUDE_RETRO_VERDICT", "close")
    followups, blocking = [], []
    if verdict == "close_with_followups":
        followups = [{"title": "Document the retro verdict flow", "difficulty": "easy", "priority": 2,
                      "body": "Write up how a retro ends in a verdict."}]
    elif verdict == "reopen":
        blocking = [{"title": "Fix the broken base check", "difficulty": "medium", "priority": 1,
                     "body": "The pre-PR check fails at the phase's base.", "reason": "the base is red"}]
    questions = []
    if os.environ.get("FAKE_CLAUDE_RETRO_QUESTIONS"):
        questions = [
            {"question": "Which rollout should the next phase use?", "context": "It changes the plan.",
             "options": ["gradual", "all at once"]},
            {"question": "Does the broken base require reopening?", "context": "It decides the verdict.",
             "options": ["reopen", "carry forward"], "blocking": True},
        ]
    rev = {"reconciliation": recon, "summary": "The phase mostly held together.",
           "personas": "The personas liked the onboarding.", "still_open": ["live worker output"],
           "questions": questions, "features": features, "verdict": verdict, "followups": followups, "blocking": blocking,
           "next_goals": "# Next\n\n- Make waiting visible.\n"}
    print(result_json("Reconciled.\nGARDEN_RETRO: " + json.dumps(rev), {"input_tokens": 4000, "output_tokens": 300}, 0.04))


def kickoff(call: Call) -> None:
    tm = re.findall(r"^### (\S+) \[", call.brief, flags=re.M)
    task_id = tm[0] if tm else "CG-001"
    data = {
        "design_needed": [{"topic": "Undecided storage format", "why": "Two tasks assume different shapes for the same file.",
                           "tasks": [task_id], "spike": "A short note naming the shape."}],
        "goals_gaps": [{"goal": "The loop runs overnight", "missing": "no task's acceptance criteria measure this"}],
        "questions": [{"question": "Should hard-tier merges wait for two rounds or one?",
                       "context": "affects the default", "options": ["one round", "two rounds"]}],
        "docs": [{"path": "docs/architecture.md", "issue": "still describes the old module layout", "tasks": [task_id]}],
        "ready": False,
        "summary": "Mostly ready; one design question and one doc need attention first.",
    }
    print(result_json("Reviewed the kickoff.\nGARDEN_KICKOFF: " + json.dumps(data), {"input_tokens": 2000, "output_tokens": 150}, 0.02))


def edit(call: Call) -> None:
    cm = re.search(r"## Current task body\n\n(.*?)\n+## Current metadata", call.brief, re.S)
    cur = cm.group(1).strip() if cm else ""
    sm = re.search(r"## Suggestions to fold in\n\n(.*?)\n+Now output", call.brief, re.S)
    sugs = sm.group(1).strip() if sm else ""
    pm = re.search(r"- priority: (\d+)", call.brief)
    dm = re.search(r"- difficulty: (\w+)", call.brief)
    if call.env.get("FAKE_CLAUDE_EDIT") == "noresult":
        print(result_json("I could not produce a revised body.", {"input_tokens": 400, "output_tokens": 10}, 0.01))
        return
    new_body = cur + "\n\n## Integrated suggestions\n\n" + sugs
    obj = {"body": new_body, "priority": int(pm.group(1)) if pm else 3,
           "difficulty": dm.group(1) if dm else "medium", "reading": [], "summary": "folded in the suggestions"}
    print(result_json("Edited.\nGARDEN_EDIT: " + json.dumps(obj), {"input_tokens": 800, "output_tokens": 60}, 0.01))


# The verdict each review mode returns; any other `review-*` mode approves.
REVIEWS: dict[str, dict] = {
    "review-bad": {"verdict": "request_changes", "summary": "criteria not met", "description_ok": False,
                   "description_feedback": "explain why, drop 'as requested'",
                   "findings": [{"severity": "blocking", "file": "a.py", "line": 3, "summary": "missing test"},
                                {"severity": "nit", "file": "", "line": None, "summary": "naming"}]},
    # description-only feedback, no blocking findings: round after round can repeat this
    # without tripping the "same finding twice" stall, since that check only looks at
    # blocking findings.
    "review-desc": {"verdict": "request_changes", "summary": "code is correct, description needs work", "description_ok": False,
                    "description_feedback": "explain why, drop 'as requested'", "findings": []},
    # description-only feedback with the corrected body supplied: the scheduler applies it
    # directly and starts no revise round.
    "review-rewrite": {"verdict": "request_changes", "summary": "only the description", "description_ok": False,
                       "description_feedback": "give the reader context",
                       "description_rewrite": "## What\n\nThe corrected description.", "findings": []},
    # code approved, description flagged, corrected body supplied: applied directly, no
    # pending feedback, task stays in_review.
    "review-approve-rewrite": {"verdict": "approve", "summary": "code is correct, description needs work", "description_ok": False,
                               "description_feedback": "give the reader context",
                               "description_rewrite": "## What\n\nThe corrected description.", "findings": []},
    # code approved, description flagged, no rewrite supplied: a description-only revise
    # round is dispatched instead of leaving feedback parked on an in_review task.
    "review-approve-desc": {"verdict": "approve", "summary": "code is correct, description needs work", "description_ok": False,
                            "description_feedback": "explain why, drop 'as requested'", "findings": []},
}
REVIEW_OK = {"verdict": "approve", "summary": "looks good", "description_ok": True, "description_feedback": "", "findings": []}


def brief_criteria(brief: str) -> list[str]:
    """The acceptance-criteria bullets from the brief's task body (a local copy of
    garden.criteria.parse_criteria, so the script has no garden import on the ssh path)."""
    m = re.search(r"(?im)^#{1,6}\s+Acceptance criteria\s*$", brief)
    if not m:
        return []
    tail = brief[m.end():]
    nxt = re.search(r"(?m)^#{1,6}\s+\S", tail)
    section = tail[: nxt.start()] if nxt else tail
    return [cm.group(1).strip() for cm in re.finditer(r"(?m)^\s*[-*]\s+\[[ xX]\]\s+(.*\S)\s*$", section)]


def verified_for(call: Call, skip: bool = False) -> list[dict]:
    """One `verified` entry per acceptance criterion, each with evidence. With `skip`, the
    first criterion is left out of the list entirely (a silently skipped criterion)."""
    crits = brief_criteria(call.brief)
    out = []
    for i, c in enumerate(crits):
        if skip and i == 0:
            continue
        out.append({"criterion": c, "evidence": f"proved by test_criterion_{i}"})
    return out


def review(call: Call) -> None:
    rev = dict(REVIEWS.get(call.mode, REVIEW_OK))
    # Build one `criteria` entry per criterion from the author's verification section: met
    # unless the author gave no evidence or marked it not done.
    m = re.search(r"(?im)^## Author's verification\s*$", call.brief)
    criteria = []
    if m:
        tail = call.brief[m.end():]
        nxt = re.search(r"(?m)^## \S", tail)
        section = tail[: nxt.start()] if nxt else tail
        for line in re.finditer(r"(?m)^- \*\*(.+?)\*\* — (.*)$", section):
            crit, ev = line.group(1), line.group(2)
            met = "gave no evidence" not in ev and not ev.startswith("author says NOT DONE")
            criteria.append({"criterion": crit, "met": met,
                             "reason": "evidence checks out" if met else "no evidence for this criterion"})
    rev["criteria"] = criteria
    print(result_json("Reviewed.\nGARDEN_REVIEW: " + json.dumps(rev), {"input_tokens": 2000, "output_tokens": 100}, 0.02))


def qa(call: Call) -> None:
    # The QA agent: reports every flow the brief lists as completed (or the one named in
    # FAKE_CLAUDE_QA_FAIL as failed) and one finding on the Inbox, without driving anything.
    names = re.findall(r"^\d+\. \*\*(.+?)\*\*", call.brief, flags=re.M)
    fail = call.env.get("FAKE_CLAUDE_QA_FAIL", "")
    flows = [{"name": n, "ok": n != fail, "page": "/inbox" if n == fail else "/",
              "note": "the button was missing" if n == fail else "fine"} for n in names]
    res = {"flows": flows, "findings": [{"page": "/inbox", "text": "The Inbox has no link back to the phase a card belongs to."}],
           "summary": "drove the flows"}
    print(result_json("Drove the flows.\nGARDEN_QA: " + json.dumps(res), {"input_tokens": 5000, "output_tokens": 400}, 0.10))


SPECIAL: dict[str, Callable[[Call], int | None]] = {
    "crash": crash, "stall": stall, "quota": quota, "plan": plan, "compare": compare, "persona": persona, "retro": retro,
    "kickoff": kickoff, "edit": edit, "qa": qa,
}


# ---- worker rounds ----------------------------------------------------------------------

@dataclass
class Worker:
    """One row of the worker table.

    `early` may finish the run before any commit (return True after printing the result);
    `prepare` touches the worktree or the world before the commit; `commits` says whether
    the round commits the counter file; `final` replaces the "done" result line with another
    final message; `tweak` adjusts the done result before it is printed."""

    early: Callable[[Call], bool] | None = None
    prepare: Callable[[Call], None] | None = None
    commits: bool = True
    final: Callable[[Call], str] | None = None
    tweak: Callable[[Call, dict], None] | None = None


def ask_once(call: Call) -> bool:
    if call.resumed:
        return False
    # commit partial work, then ask
    (call.cwd / "partial.txt").write_text("half done\n")
    git_commit("partial", call.cwd, call.env)
    print(result_json('Stopping.\nGARDEN_RESULT: {"status": "needs_input", "question": "Postgres or SQLite?", "summary": "need a decision"}',
                      {"input_tokens": 300, "output_tokens": 20}, 0.01, session_id="sess-42"))
    return True


def nothing_to_change(call: Call) -> bool:
    if not call.revise:
        return False
    print(result_json('Nothing to change.\nGARDEN_RESULT: {"status": "done", "summary": "no change", "pr_title": "t", "pr_body": "b"}', {}, 0.01))
    return True


def wont_do_first(call: Call) -> bool:
    if call.revise:
        return False
    # the worker judges the task should not be done; it commits nothing
    final = 'I do not think this should be done.\nGARDEN_RESULT: {"status": "wont_do", "reason": "This duplicates DM-002; doing it would create a conflicting second path.", "summary": "should not be done"}'
    print(result_json(final, {"input_tokens": 200, "output_tokens": 15}, 0.01))
    return True


def no_change_on_revise(call: Call) -> bool:
    if not call.revise:
        return False
    # a revise round with nothing to change: the failing check was the environment, not the diff
    final = 'The code is already correct.\nGARDEN_RESULT: {"status": "no_change", "reason": "The failing check is an environment mismatch, not this diff; the code is right."}'
    print(result_json(final, {"input_tokens": 210, "output_tokens": 18}, 0.01))
    return True


def collide_with_main(call: Call) -> None:
    (call.cwd / "README.md").write_text("# demo\n\nchanged by worker\n")


def resolve_rebase(call: Call) -> bool:
    # A rebase-conflict agent: redo the rebase the scheduler aborted and resolve it, favouring
    # this branch's changes (a stand-in for "resolve the conflict, change nothing else"). The
    # runner force-pushes the rebased branch; no extra commit is made.
    m = re.search(r"git rebase origin/(\S+)", call.brief)
    base = m.group(1) if m else "main"
    subprocess.run(["git", "fetch", "origin"], check=False, capture_output=True)
    env = {**os.environ, "GIT_EDITOR": "true"}
    r = subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake",
                        "rebase", "-X", "theirs", f"origin/{base}"], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], check=False, capture_output=True)
        print(result_json('Stuck.\nGARDEN_RESULT: {"status": "needs_input", "question": "How should this conflict be resolved?", "summary": "cannot resolve"}',
                          {"input_tokens": 400, "output_tokens": 20}, 0.01))
        return True
    print(result_json('Resolved the conflict.\nGARDEN_RESULT: {"status": "done", "summary": "resolved the rebase conflict, changed nothing else"}',
                      {"input_tokens": 500, "output_tokens": 40}, 0.02))
    return True


def escape_worktree(call: Call) -> None:
    # Do what CG-092's worker did: leave the worktree and write/commit in another repo
    # (the live garden or the product clone), whatever the brief said.
    escape_dir = call.env["FAKE_CLAUDE_ESCAPE_DIR"]
    escape_file = call.env.get("FAKE_CLAUDE_ESCAPE_FILE", "garden.yaml")
    fp = Path(escape_dir) / escape_file
    call.escaped_path = str(fp)  # a real worker names the path it wrote (Edit file_path / Bash / final msg)
    fp.write_text((fp.read_text() if fp.exists() else "") + "\n# edited by a runaway worker\n")
    if call.env.get("FAKE_CLAUDE_ESCAPE_COMMIT", "1") == "1":
        git_commit("runaway commit", escape_dir, call.env)


def add_discovered(call: Call, result: dict) -> None:
    result["discovered"] = [
        {"title": "Fix the flaky widget test", "body": "## Goal\n\nIt flakes.", "difficulty": "easy", "blocking": False},
        {"title": "Add the missing config schema",
         "body": "## Goal\n\nNeeded first.\n\n## Acceptance criteria\n\n- [ ] The schema validates a config with every known key.\n",
         "difficulty": "medium", "blocking": True},
        {"title": "First task", "body": "duplicate title, must be skipped"},
    ]


def add_discovered_incomplete_brief(call: Call, result: dict) -> None:
    # A blocking discovery whose brief is not ready to dispatch (placeholder criteria):
    # `_file_discovered` must hold it as a draft, not send it straight to ready (CG-209).
    result["discovered"] = [
        {"title": "Add the missing config schema", "body": "## Goal\n\nNeeded first.\n\n## Acceptance criteria\n\n- [ ] ...\n",
         "difficulty": "medium", "blocking": True},
    ]


def add_discovered_kinds(call: Call, result: dict) -> None:
    result["discovered"] = [
        {"kind": "task", "title": "A real follow-up task", "body": "## Goal\n\nDo it.", "difficulty": "easy"},
        {"kind": "duplicate", "of": "DM-001", "duplicates": "DM-002", "reason": "DM-002 restates DM-001"},
        {"kind": "cancel", "task": "DM-003", "reason": "obsolete after the refactor"},
        {"kind": "note", "note": "The brief for DM-001 was missing the spec link."},
    ]


def add_discovered_same(call: Call, result: dict) -> None:
    # Every run in this mode reports the identical finding, whatever task ran it: three
    # workers hitting the same bug should file one draft, not three (CG-199).
    result["discovered"] = [
        {"title": "Retry loop spins forever on a dead runner",
         "body": "`src/garden/scheduler/poll.py` raises `TimeoutError: retry exceeded` under load."},
    ]


def add_pr_comment(call: Call, result: dict) -> None:
    result["pr_comment"] = "I addressed the feedback by adding the missing test."


def add_friction(call: Call, result: dict) -> None:
    # friction travels in its own field, not the body
    result["pr_body"] = "## What\n\nA fake change."
    result["friction"] = ["The spec never linked the schema.", "Tests needed PYTHONPATH set."]


def drop_body_on_revise(call: Call, result: dict) -> None:
    if call.revise:
        # a revise round that only reworded things: no description change, so pr_body is omitted
        result.pop("pr_body")
        result["summary"] = "reworded; description unchanged"


def note_escape(call: Call, result: dict) -> None:
    if call.escaped_path:
        result["notes"] = f"I stepped out of the worktree and edited {call.escaped_path}."


def skip_a_criterion(call: Call, result: dict) -> None:
    result["verified"] = verified_for(call, skip=True)


WORKERS: dict[str, Worker] = {
    "done": Worker(),
    "nocommit": Worker(commits=False),
    "noresult": Worker(final=lambda call: "I did some things but forgot the result line."),
    "authnotloggedin": Worker(commits=False, final=lambda call: "Not logged in · Please run /login"),
    "blocked": Worker(commits=False, final=lambda call: 'Need a decision.\nGARDEN_RESULT: {"status": "blocked", "summary": "Which database?", "notes": ""}'),
    "needs_input": Worker(early=ask_once),
    "discover": Worker(tweak=add_discovered),
    "discover-kinds": Worker(tweak=add_discovered_kinds),
    "discover-same": Worker(tweak=add_discovered_same),
    "discover-incomplete-brief": Worker(tweak=add_discovered_incomplete_brief),
    "nochange": Worker(early=nothing_to_change),
    "revise-with-comment": Worker(tweak=add_pr_comment),
    "conflict": Worker(prepare=collide_with_main),
    "rebase-resolve": Worker(early=resolve_rebase),
    "wont_do": Worker(early=wont_do_first),
    "no_change": Worker(early=no_change_on_revise),
    "friction": Worker(tweak=add_friction),
    "omit-body": Worker(tweak=drop_body_on_revise),
    "escape": Worker(prepare=escape_worktree, tweak=note_escape),
    "skip-criterion": Worker(tweak=skip_a_criterion),
}


def commit_counter(call: Call) -> None:
    p = call.cwd / "worker-output.txt"
    n = int(p.read_text().strip() or 0) + 1 if p.exists() else 1
    p.write_text(f"{n}\n")
    # A stacked task's worktree can branch from the exact tree+parent another task's run
    # produces (both start from the same commit and write the same counter value), and
    # commits made in the same wall-clock second are otherwise byte-identical: git then
    # dedupes them to one object, and the stacked task's "own" commit silently vanishes
    # (its branch is already at that commit, so it looks like it made no commits at all).
    # Mixing the run identity into the message keeps every run's commit object distinct.
    tag = f"{call.env.get('GARDEN_TASK_ID', '')}/{call.env.get('GARDEN_RUN_ID', '')}"
    git_commit(f"fake change {n} ({tag})", call.cwd, call.env)


def done_result(call: Call) -> dict:
    return {
        "status": "done",
        "summary": "revised per feedback" if call.revise else ("resumed and finished" if call.resumed else "implemented the thing"),
        "pr_title": "Fake: implemented the thing",
        "pr_body": "## What\n\nA fake change.\n\n## Friction\n\nNone.",
        "verified": verified_for(call),
        "notes": "",
    }


def run_worker(call: Call, worker: Worker) -> None:
    if call.resumed:
        (call.cwd / "resumed.txt").write_text(call.brief)  # the resume prompt (contains the answer)
    if worker.early and worker.early(call):
        return
    if worker.prepare:
        worker.prepare(call)
    if worker.commits:
        commit_counter(call)
    if worker.final:
        final = worker.final(call)
    else:
        result = done_result(call)
        if worker.tweak:
            worker.tweak(call, result)
        final = "All done.\n" + "GARDEN_RESULT: " + json.dumps(result)
    result_obj = {
        "type": "result", "subtype": "success", "is_error": False, "result": final,
        "usage": {"input_tokens": 1234, "output_tokens": 321, "cache_read_input_tokens": 100},
        "total_cost_usd": 0.05, "num_turns": 3, "session_id": "fake",
    }
    if call.stream:
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "fake", "tools": []}))
        print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Working on the task..."}]}}))
        print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo working"}}]}}))
        print(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "working"}]}]}}))
        print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "git log --oneline -1"}}]}}))
        print(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "abc1234 fake change\n"}]}}))
        print(json.dumps(result_obj))
    else:
        print(json.dumps(result_obj))


# ---- entry points -----------------------------------------------------------------------

def handle(call: Call) -> int:
    """Dispatch one call to its mode; prints the harness output; returns the exit code.
    Raises Stall for a worker that never finishes."""
    mode = call.mode
    # The brief's marker decides the kind of run, whatever FAKE_CLAUDE_MODE says.
    if "GARDEN_REVIEW:" in call.brief:
        mode = call.env.get("FAKE_CLAUDE_REVIEW", "review-ok")
    if "GARDEN_COMPARE:" in call.brief:
        mode = "compare"
    if "GARDEN_PERSONA:" in call.brief:
        mode = "persona"
    if "GARDEN_RETRO:" in call.brief:
        mode = "retro"
    if "GARDEN_KICKOFF:" in call.brief:
        mode = "kickoff"
    if "GARDEN_EDIT:" in call.brief:
        mode = "edit"
    if "GARDEN_QA:" in call.brief:
        mode = "qa"
    call.mode = mode
    try:
        (call.cwd / "model.txt").write_text(call.model + "\n")
    except OSError:
        pass
    Path(call.env.get("FAKE_CLAUDE_BRIEF_COPY", "/dev/null")).write_text(call.brief)
    dump = call.env.get("FAKE_CLAUDE_ENV_DUMP")
    if dump:  # record the environment the harness ran in, so a test can assert on the scrub
        Path(dump).write_text("\n".join(f"{k}={v}" for k, v in sorted(call.env.items())))
    special = SPECIAL.get(mode) or (review if mode.startswith("review") else None)
    if special is not None:
        return int(special(call) or 0)
    # An unknown mode behaves like `done` without a commit, as it always has.
    run_worker(call, WORKERS.get(mode, Worker(commits=False)))
    return 0


def make_call(args: list[str], brief: str, cwd: Path, env: Mapping[str, str]) -> Call:
    model = args[args.index("--model") + 1] if "--model" in args else ""
    stream = "--output-format" in args and args[args.index("--output-format") + 1] == "stream-json"
    return Call(mode=env.get("FAKE_CLAUDE_MODE", "done"), brief=brief, args=list(args), model=model,
                stream=stream, resumed="--resume" in args, cwd=Path(cwd), env=env)


def run(args: list[str], brief: str, cwd: Path, env: Mapping[str, str]) -> tuple[str, str, int | None]:
    """One in-process invocation: (stdout, stderr, exit code). The exit code is None when
    the worker never finishes (the `stall` mode), which is the caller's cue to leave the
    run without an exit_code file."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code: int | None = handle(make_call(args, brief, cwd, env))
        except Stall:
            code = None
    return out.getvalue(), err.getvalue(), code


def main() -> None:
    call = make_call(sys.argv[1:], sys.stdin.read(), Path.cwd(), dict(os.environ))
    try:
        sys.exit(handle(call))
    except Stall:
        # As a process a stalled worker just sleeps, so the scheduler must notice it is idle
        # and stop it. If it is never killed it wakes and finishes cleanly, so a test that
        # forgets to kill it still terminates.
        import time as _time
        _time.sleep(float(call.env.get("FAKE_CLAUDE_STALL_SECONDS", "30")))
        print(result_json('Awake.\nGARDEN_RESULT: {"status": "done", "summary": "napped", "pr_title": "t", "pr_body": "b"}', {}, 0.0))


if __name__ == "__main__":
    main()
