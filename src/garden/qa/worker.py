#!/usr/bin/env python3
"""The QA worker: the stand-in for `claude` inside the throwaway garden `garden qa` serves.

Reads the brief from stdin, makes a commit in the cwd (a git worktree) and prints a
`claude -p --output-format json`-shaped result, like `tests/fake_claude.py`, but the
behaviour is picked from the brief rather than from the environment: a task body that
carries a line `qa-worker: <mode>` reaches the worker inside its brief, so one throwaway
garden can hold a task that asks a question beside one that finds nothing to change.

Modes: done (default) | needs_input (asks once; a --resume run finishes)
       | no_change (the first run finishes; a revise round reports no_change)
A planning prompt returns two tasks; a review brief approves. No network, no tokens.
Standalone on purpose: nothing here imports `garden`, so the harness command is just
`python worker.py`.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

MARKER = re.compile(r"^qa-worker:\s*([a-z_]+)\s*$", re.M)

PLAN = [
    {"title": "Planned: a plain task", "priority": 1, "estimate": "S", "difficulty": "easy", "depends_on": [], "reading": [],
     "body": "## Goal\n\nA task the planner wrote. The worker finishes it in one round.\n\nqa-worker: done\n"},
    {"title": "Planned: a follow-up", "priority": 2, "estimate": "S", "difficulty": "easy", "depends_on": ["Planned: a plain task"], "reading": [],
     "body": "## Goal\n\nA second planned task, after the first.\n\nqa-worker: done\n"},
]


def emit(final: str, cost: float = 0.01, **extra: object) -> None:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": final,
                      "usage": {"input_tokens": 100, "output_tokens": 20}, "total_cost_usd": cost,
                      "session_id": "qa-session", **extra}))


def commit(message: str) -> None:
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.email=qa-worker@example.com", "-c", "user.name=qa-worker",
                    "commit", "-q", "-m", message], check=True)


def main() -> None:
    brief = sys.stdin.read()
    args = sys.argv[1:]
    resumed = "--resume" in args
    if "# Planning request" in brief:
        emit(json.dumps(PLAN))
        return
    if "GARDEN_REVIEW:" in brief:
        verdict = {"verdict": "approve", "summary": "looks good", "description_ok": True, "description_feedback": "", "findings": []}
        emit("Reviewed.\nGARDEN_REVIEW: " + json.dumps(verdict), 0.02)
        return
    m = MARKER.search(brief)
    mode = m.group(1) if m else "done"
    revise = "Revision round" in brief
    if mode == "needs_input" and not resumed:
        Path("partial.txt").write_text("half done\n")
        commit("partial work before asking")
        emit('Stopping.\nGARDEN_RESULT: {"status": "needs_input", "question": "Postgres or SQLite?", "summary": "need a decision"}')
        return
    if mode == "no_change" and revise:
        emit('The code is already correct.\nGARDEN_RESULT: {"status": "no_change", "reason": "The failing check is an environment mismatch, not this diff; the code is right."}')
        return
    p = Path("worker-output.txt")
    n = int(p.read_text().strip() or 0) + 1 if p.exists() else 1
    p.write_text(f"{n}\n")
    commit(f"qa worker change {n}")
    result = {
        "status": "done",
        "summary": "revised per feedback" if revise else ("resumed and finished" if resumed else "implemented the thing"),
        "pr_title": "QA: implemented the thing",
        "pr_body": "## What\n\nA change made by the QA worker.\n",
        "notes": "",
    }
    emit("All done.\nGARDEN_RESULT: " + json.dumps(result), 0.05)


if __name__ == "__main__":
    main()
