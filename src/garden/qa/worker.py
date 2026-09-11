#!/usr/bin/env python3
"""The QA worker: the stand-in for `claude` inside the throwaway garden `garden qa` serves.

Reads the brief from stdin, makes a commit in the cwd (a git worktree) and prints a
`claude -p --output-format json`-shaped result, like `tests/fake_claude.py`, but the
behaviour is picked from the brief rather than from the environment: a task body that
carries a line `qa-worker: <mode>` reaches the worker inside its brief, so one throwaway
garden can hold a task that asks a question beside one that finds nothing to change.

Modes: done (default) | needs_input (asks once; a --resume run finishes)
       | no_change_decision (the first run finishes; a revise round asks a person to accept no_change)
A planning prompt returns two tasks; a review brief approves. No network, no tokens.
Standalone on purpose: nothing here imports `garden`, so the harness command is just
`python worker.py`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

MARKER = re.compile(r"^qa-worker:\s*([a-z_]+)\s*$", re.M)
ESCAPE = re.compile(r"^qa-escape:\s*(.+)$", re.M)
FROZEN_CRITERIA = re.compile(
    r"^#{1,2} Criteria frozen for this dispatch\s*$\n(?P<body>.*?)(?=^# |\Z)",
    re.M | re.S,
)

PREFLIGHT_ITEMS = (
    "A test or stated reason for every acceptance criterion",
    "Lint is clean",
    "No conflict markers remain",
    "UI changes have 1280px and 390px captures",
    "The PR description states the goal and outcome without process history",
    "Every acceptance criterion is addressed by name",
)

PLAN = [
    {"title": "Planned: a plain task", "priority": 1, "estimate": "S", "difficulty": "easy", "depends_on": [], "reading": ["demo/p1/specs/spec.md"],
     "body": "## Goal\n\nA task the planner wrote. The worker finishes it in one round.\n\n## Acceptance criteria\n\n- [ ] The plain task lands with a passing test.\n\nqa-worker: done\n"},
    {"title": "Planned: a follow-up", "priority": 2, "estimate": "S", "difficulty": "easy", "depends_on": ["Planned: a plain task"], "reading": ["demo/p1/specs/spec.md"],
     "body": "## Goal\n\nA second planned task, after the first.\n\n## Acceptance criteria\n\n- [ ] The follow-up builds on the first task and is tested.\n\nqa-worker: done\n"},
]


def emit(final: str, cost: float = 0.01, **extra: object) -> None:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": final,
                      "usage": {"input_tokens": 100, "output_tokens": 20}, "total_cost_usd": cost,
                      "session_id": "qa-session", **extra}))


def commit(message: str) -> None:
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.email=qa-worker@example.com", "-c", "user.name=qa-worker",
                    "commit", "-q", "-m", message], check=True)


def verified_for(brief: str) -> list[dict[str, str]]:
    """Echo the dispatch's frozen criteria with evidence, like a real worker must."""
    section = FROZEN_CRITERIA.search(brief)
    if not section:
        return []
    return [
        {"criterion": line[2:].strip(), "evidence": "QA worker exercised the scripted flow"}
        for line in section.group("body").splitlines()
        if line.startswith("- ")
    ]


def main() -> None:
    brief = sys.stdin.read()
    context_root = Path(os.environ.get("GARDEN_CONTEXT_DIR", ""))
    if context_root.is_dir():
        brief += "\n" + "\n".join(
            path.read_text() for path in sorted(context_root.rglob("*")) if path.is_file()
        )
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
    escape = ESCAPE.search(brief)
    revise = "Revision round" in brief
    if mode == "needs_input" and not resumed:
        Path("partial.txt").write_text("half done\n")
        commit("partial work before asking")
        emit('Stopping.\nGARDEN_RESULT: {"status": "needs_input", "question": "Postgres or SQLite?", "summary": "need a decision"}')
        return
    if mode == "no_change_decision" and revise:
        emit('The code is already correct.\nGARDEN_RESULT: {"status": "no_change", "reason": "The requested outcome should remain unchanged.", "verified": [{"criterion": "Change the promised outcome", "not_done": true, "reason": "The existing promise is correct."}]}')
        return
    if mode == "escape" and escape is not None:
        target = Path(escape.group(1).strip())
        target.write_text(target.read_text() + "\n# qa worker fence escape\n")
        print(json.dumps({"type": "assistant", "message": {"content": [{
            "type": "tool_use", "name": "Bash",
            "input": {"command": f"printf '%s\\n' worker > {target}"},
        }]}}))
    p = Path("worker-output.txt")
    n = int(p.read_text().strip() or 0) + 1 if p.exists() else 1
    p.write_text(f"{n}\n")
    commit(f"qa worker change {n}")
    result = {
        "status": "done",
        "summary": "revised per feedback" if revise else ("resumed and finished" if resumed else "implemented the thing"),
        "pr_title": "QA: implemented the thing",
        "pr_body": "## What\n\nA change made by the QA worker.\n",
        "pre_flight": [
            {"item": item, "status": "not_applicable" if item.startswith("UI changes") else "pass",
             "evidence": "QA worker clean check"}
            for item in PREFLIGHT_ITEMS
        ],
        "verified": verified_for(brief),
        "notes": "",
    }
    emit("All done.\nGARDEN_RESULT: " + json.dumps(result), 0.05)


if __name__ == "__main__":
    main()
