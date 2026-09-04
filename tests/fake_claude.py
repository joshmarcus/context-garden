#!/usr/bin/env python3
"""Stand-in for the `claude` binary in tests.

Reads the brief from stdin, does something to the cwd (a git worktree) depending on
FAKE_CLAUDE_MODE, and prints a `claude -p --output-format json`-shaped result.

Modes: done (default) | nocommit | blocked | crash | noresult | plan | review-ok | review-bad
       | needs_input (asks once; a --resume run finishes) | discover (done + discovered work)
       | nochange (revise rounds commit nothing) | revise-with-comment (revise with pr_comment) | conflict (edits README.md to collide with main)
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
try:
    Path("model.txt").write_text(model + "\n")
except OSError:
    pass
Path(os.environ.get("FAKE_CLAUDE_BRIEF_COPY", "/dev/null")).write_text(brief)

if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(1)

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

if mode.startswith("review"):
    if mode == "review-bad":
        rev = {"verdict": "request_changes", "summary": "criteria not met", "description_ok": False,
               "description_feedback": "explain why, drop 'as requested'",
               "findings": [{"severity": "blocking", "file": "a.py", "line": 3, "summary": "missing test"},
                            {"severity": "nit", "file": "", "line": None, "summary": "naming"}]}
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
if mode == "conflict":
    Path("README.md").write_text("# demo\n\nchanged by worker\n")

if mode in ("done", "noresult", "needs_input", "discover", "nochange", "revise-with-comment", "conflict"):
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
    if mode == "revise-with-comment":
        result["pr_comment"] = "I addressed the feedback by adding the missing test."
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
