#!/usr/bin/env python3
"""Stand-in for the `claude` binary in tests.

Reads the brief from stdin, does something to the cwd (a git worktree) depending on
FAKE_CLAUDE_MODE, and prints a `claude -p --output-format json`-shaped result.

Modes: done (default) | nocommit | blocked | crash | stall (sleeps, no output) | noresult | plan | review-ok | review-bad | review-desc
       | review-rewrite (description-only review that returns description_rewrite)
       | needs_input (asks once; a --resume run finishes) | discover (done + discovered work)
       | discover-kinds (done + a task, a duplicate + cancel decision, and a note)
       | friction (done + a friction list in the result, none in the body)
       | omit-body (a revise round that omits pr_body, leaving the description unchanged)
       | nochange (revise rounds commit nothing) | revise-with-comment (revise with pr_comment) | conflict (edits README.md to collide with main)
       | wont_do (first run reports wont_do; a revise run after a reject finishes normally)
       | no_change (first run finishes normally; a revise round reports no_change)
Records the model it was given in model.txt (cwd) and the brief in FAKE_CLAUDE_BRIEF_COPY.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

mode = os.environ.get("FAKE_CLAUDE_MODE", "done")
brief = sys.stdin.read()
args = sys.argv[1:]
model = args[args.index("--model") + 1] if "--model" in args else ""
stream = "--output-format" in args and args[args.index("--output-format") + 1] == "stream-json"
if "GARDEN_REVIEW:" in brief:
    mode = os.environ.get("FAKE_CLAUDE_REVIEW", "review-ok")
if "GARDEN_COMPARE:" in brief:
    mode = "compare"
if "GARDEN_PERSONA:" in brief:
    mode = "persona"
if "GARDEN_RETRO:" in brief:
    mode = "retro"
try:
    Path("model.txt").write_text(model + "\n")
except OSError:
    pass
Path(os.environ.get("FAKE_CLAUDE_BRIEF_COPY", "/dev/null")).write_text(brief)

if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(1)

if mode == "stall":
    # A worker that goes silent: no commit, no file change, no output. It just sleeps,
    # so the scheduler must notice it is idle and stop it. If it is never killed it wakes
    # and finishes cleanly, so a test that forgets to kill it still terminates.
    import time as _time
    _time.sleep(float(os.environ.get("FAKE_CLAUDE_STALL_SECONDS", "30")))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": 'Awake.\nGARDEN_RESULT: {"status": "done", "summary": "napped", "pr_title": "t", "pr_body": "b"}',
                      "usage": {}, "total_cost_usd": 0.0}))
    sys.exit(0)

if mode == "plan":
    items = [
        {"title": "First planned task", "priority": 1, "estimate": "S", "depends_on": [], "reading": [], "body": "## Goal\n\nA."},
        {"title": "Second planned task", "priority": 2, "estimate": "M", "depends_on": ["First planned task"], "reading": [], "body": "## Goal\n\nB."},
    ]
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(items),
                      "usage": {"input_tokens": 100, "output_tokens": 50}, "total_cost_usd": 0.01}))
    sys.exit(0)

if mode == "compare":
    import re as _re
    labels = _re.findall(r"^- \*\*(.+?)\*\* — branch", brief, flags=_re.M)
    winner = os.environ.get("FAKE_CLAUDE_WINNER") or labels[-1]
    ranking = [{"label": lb, "score": (9 if lb == winner else 6), "summary": f"{lb} did fine"} for lb in labels]
    verdict = {"winner": winner, "rationale": "the winner had tests", "ranking": ranking}
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Compared.\nGARDEN_COMPARE: " + json.dumps(verdict),
                      "usage": {"input_tokens": 3000, "output_tokens": 200}, "total_cost_usd": 0.03}))
    sys.exit(0)

if mode == "persona":
    import re as _re
    m = _re.search(r"^# Persona review: ([a-z0-9-]+)", brief, flags=_re.M)
    name = m.group(1) if m else "persona"
    sev = os.environ.get("FAKE_CLAUDE_PERSONA_SEVERITY", "medium")
    rev = {"persona": name, "score": 7, "overall": f"As the {name}, mostly fine.",
           "findings": [{"severity": sev, "area": "onboarding", "summary": "First run needs a config file the README never mentions", "suggestion": "Add it to Quick start"}]}
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Reviewed as persona.\nGARDEN_PERSONA: " + json.dumps(rev),
                      "usage": {"input_tokens": 1500, "output_tokens": 120}, "total_cost_usd": 0.02}))
    sys.exit(0)

if mode == "retro":
    import re as _re
    fsec = _re.search(r"## Harvested friction.*?(?=\n## |\Z)", brief, flags=_re.S)
    friction_ids = _re.findall(r"^### (\S+):", fsec.group(0), flags=_re.M) if fsec else []
    msec = _re.search(r"## Merged pull requests.*?(?=\n## |\Z)", brief, flags=_re.S)
    merged_ids = _re.findall(r"^- (\S+) —", msec.group(0), flags=_re.M) if msec else []
    cycle = ["fixed", "still_true", "outdated", "disputed"]
    recon = []
    for i, fid in enumerate(friction_ids):
        v = cycle[i % len(cycle)]
        recon.append({"item": f"friction from {fid}", "logged": fid,
                      "pr": (merged_ids[0] if v == "fixed" and merged_ids else ""),
                      "verdict": v, "evidence": f"reconciled {fid} against the merged work"})
    rev = {"reconciliation": recon, "summary": "The phase mostly held together.",
           "personas": "The personas liked the onboarding.", "still_open": ["live worker output"],
           "next_goals": "# Next\n\n- Make waiting visible.\n"}
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "Reconciled.\nGARDEN_RETRO: " + json.dumps(rev),
                      "usage": {"input_tokens": 4000, "output_tokens": 300}, "total_cost_usd": 0.04}))
    sys.exit(0)

if mode.startswith("review"):
    if mode == "review-bad":
        rev = {"verdict": "request_changes", "summary": "criteria not met", "description_ok": False,
               "description_feedback": "explain why, drop 'as requested'",
               "findings": [{"severity": "blocking", "file": "a.py", "line": 3, "summary": "missing test"},
                            {"severity": "nit", "file": "", "line": None, "summary": "naming"}]}
    elif mode == "review-desc":
        # description-only feedback, no blocking findings: round after round can repeat this
        # without tripping the "same finding twice" stall, since that check only looks at
        # blocking findings.
        rev = {"verdict": "request_changes", "summary": "code is correct, description needs work", "description_ok": False,
               "description_feedback": "explain why, drop 'as requested'", "findings": []}
    elif mode == "review-rewrite":
        # description-only feedback with the corrected body supplied: the scheduler applies it
        # directly and starts no revise round.
        rev = {"verdict": "request_changes", "summary": "only the description", "description_ok": False,
               "description_feedback": "give the reader context",
               "description_rewrite": "## What\n\nThe corrected description.", "findings": []}
    else:
        rev = {"verdict": "approve", "summary": "looks good", "description_ok": True, "description_feedback": "", "findings": []}
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Reviewed.\nGARDEN_REVIEW: " + json.dumps(rev),
                      "usage": {"input_tokens": 2000, "output_tokens": 100}, "total_cost_usd": 0.02}))
    sys.exit(0)

resumed = "--resume" in args
if mode == "needs_input" and not resumed:
    # commit partial work, then ask
    Path("partial.txt").write_text("half done\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake", "commit", "-q", "-m", "partial"], check=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "sess-42",
                      "result": 'Stopping.\nGARDEN_RESULT: {"status": "needs_input", "question": "Postgres or SQLite?", "summary": "need a decision"}',
                      "usage": {"input_tokens": 300, "output_tokens": 20}, "total_cost_usd": 0.01}))
    sys.exit(0)
if resumed:
    Path("resumed.txt").write_text(brief)  # the resume prompt (contains the answer)
if mode == "nochange" and "Revision round" in brief:
    final = 'Nothing to change.\nGARDEN_RESULT: {"status": "done", "summary": "no change", "pr_title": "t", "pr_body": "b"}'
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": final, "usage": {}, "total_cost_usd": 0.01}))
    sys.exit(0)
if mode == "wont_do" and "Revision round" not in brief:
    # the worker judges the task should not be done; it commits nothing
    final = 'I do not think this should be done.\nGARDEN_RESULT: {"status": "wont_do", "reason": "This duplicates DM-002; doing it would create a conflicting second path.", "summary": "should not be done"}'
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": final, "usage": {"input_tokens": 200, "output_tokens": 15}, "total_cost_usd": 0.01}))
    sys.exit(0)
if mode == "no_change" and "Revision round" in brief:
    # a revise round with nothing to change: the failing check was the environment, not the diff
    final = 'The code is already correct.\nGARDEN_RESULT: {"status": "no_change", "reason": "The failing check is an environment mismatch, not this diff; the code is right."}'
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": final, "usage": {"input_tokens": 210, "output_tokens": 18}, "total_cost_usd": 0.01}))
    sys.exit(0)
if mode == "conflict":
    Path("README.md").write_text("# demo\n\nchanged by worker\n")

if mode in ("done", "noresult", "needs_input", "discover", "discover-kinds", "nochange", "revise-with-comment", "conflict", "wont_do", "no_change", "friction", "omit-body"):
    p = Path("worker-output.txt")
    n = int(p.read_text().strip() or 0) + 1 if p.exists() else 1
    p.write_text(f"{n}\n")
    subprocess.run(["git", "add", "-A"], check=True)
    # A stacked task's worktree can branch from the exact tree+parent another task's run
    # produces (both start from the same commit and write the same counter value), and
    # commits made in the same wall-clock second are otherwise byte-identical: git then
    # dedupes them to one object, and the stacked task's "own" commit silently vanishes
    # (its branch is already at that commit, so it looks like it made no commits at all).
    # Mixing the run identity into the message keeps every run's commit object distinct.
    tag = f"{os.environ.get('GARDEN_TASK_ID', '')}/{os.environ.get('GARDEN_RUN_ID', '')}"
    subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake", "commit", "-q", "-m", f"fake change {n} ({tag})"], check=True)

if mode == "noresult":
    final = "I did some things but forgot the result line."
elif mode == "blocked":
    final = 'Need a decision.\nGARDEN_RESULT: {"status": "blocked", "summary": "Which database?", "notes": ""}'
else:
    revise = "Revision round" in brief
    result = {
        "status": "done",
        "summary": "revised per feedback" if revise else ("resumed and finished" if resumed else "implemented the thing"),
        "pr_title": "Fake: implemented the thing",
        "pr_body": "## What\n\nA fake change.\n\n## Friction\n\nNone.",
        "notes": "",
    }
    if mode == "discover":
        result["discovered"] = [
            {"title": "Fix the flaky widget test", "body": "## Goal\n\nIt flakes.", "difficulty": "easy", "blocking": False},
            {"title": "Add the missing config schema", "body": "## Goal\n\nNeeded first.", "difficulty": "medium", "blocking": True},
            {"title": "First task", "body": "duplicate title, must be skipped"},
        ]
    if mode == "discover-kinds":
        result["discovered"] = [
            {"kind": "task", "title": "A real follow-up task", "body": "## Goal\n\nDo it.", "difficulty": "easy"},
            {"kind": "duplicate", "of": "DM-001", "duplicates": "DM-002", "reason": "DM-002 restates DM-001"},
            {"kind": "cancel", "task": "DM-003", "reason": "obsolete after the refactor"},
            {"kind": "note", "note": "The brief for DM-001 was missing the spec link."},
        ]
    if mode == "revise-with-comment":
        result["pr_comment"] = "I addressed the feedback by adding the missing test."
    if mode == "friction":
        # friction travels in its own field, not the body
        result["pr_body"] = "## What\n\nA fake change."
        result["friction"] = ["The spec never linked the schema.", "Tests needed PYTHONPATH set."]
    if mode == "omit-body" and revise:
        # a revise round that only reworded things: no description change, so pr_body is omitted
        result.pop("pr_body")
        result["summary"] = "reworded; description unchanged"
    final = "All done.\n" + "GARDEN_RESULT: " + json.dumps(result)
result_obj = {
    "type": "result", "subtype": "success", "is_error": False, "result": final,
    "usage": {"input_tokens": 1234, "output_tokens": 321, "cache_read_input_tokens": 100},
    "total_cost_usd": 0.05, "num_turns": 3, "session_id": "fake",
}
if stream:
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "fake", "tools": []}))
    print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Working on the task..."}]}}))
    print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo working"}}]}}))
    print(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "working"}]}]}}))
    print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "git log --oneline -1"}}]}}))
    print(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "abc1234 fake change\n"}]}}))
    print(json.dumps(result_obj))
else:
    print(json.dumps(result_obj))
