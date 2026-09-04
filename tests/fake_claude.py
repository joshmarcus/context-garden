#!/usr/bin/env python3
"""Stand-in for the `claude` binary in tests.

Reads the brief from stdin, does something to the cwd (a git worktree) depending on
FAKE_CLAUDE_MODE, and prints a `claude -p --output-format json`-shaped result.

Modes: done (default) | nocommit | blocked | crash | noresult | plan | review-ok | review-bad
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
if "GARDEN_REVIEW:" in brief:
    mode = os.environ.get("FAKE_CLAUDE_REVIEW", "review-ok")
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

if mode in ("done", "noresult"):
    p = Path("worker-output.txt")
    n = int(p.read_text().strip() or 0) + 1 if p.exists() else 1
    p.write_text(f"{n}\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake", "commit", "-q", "-m", f"fake change {n}"], check=True)

if mode == "noresult":
    final = "I did some things but forgot the result line."
elif mode == "blocked":
    final = 'Need a decision.\nGARDEN_RESULT: {"status": "blocked", "summary": "Which database?", "notes": ""}'
else:
    revise = "Revision round" in brief
    final = (
        "All done.\n"
        + "GARDEN_RESULT: "
        + json.dumps({
            "status": "done",
            "summary": "revised per feedback" if revise else "implemented the thing",
            "pr_title": "Fake: implemented the thing",
            "pr_body": "## What\n\nA fake change.\n\n## Friction\n\nNone.",
            "notes": "",
        })
    )
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "result": final,
    "usage": {"input_tokens": 1234, "output_tokens": 321, "cache_read_input_tokens": 100},
    "total_cost_usd": 0.05, "num_turns": 3, "session_id": "fake",
}))
