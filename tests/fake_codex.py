#!/usr/bin/env python3
"""Stand-in for `codex exec --json`: takes the prompt, commits a file, emits JSONL events and
writes --output-last-message if given. `run()` is what the suite's in-process runner calls
(see tests/inprocess.py); `main()` is the same thing as a script."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def handle(args: list[str], brief: str, cwd: Path, env: Mapping[str, str]) -> int:
    final_path = None
    if "--output-last-message" in args:
        final_path = Path(args[args.index("--output-last-message") + 1])
    model = args[args.index("-m") + 1] if "-m" in args else ""
    if "--full-auto" in args:
        return 2
    if env.get("FAKE_CODEX_MODE") == "quota":
        # The account, not the worker: codex exec reports the usage-limit message and no commit.
        events = [
            {"type": "thread.started", "thread_id": "th_1"},
            {"type": "turn.failed", "message": "You've hit your usage limit. Upgrade to Pro for more access."},
        ]
        for e in events:
            print(json.dumps(e))
        return 1
    if brief.startswith("# Planning request"):
        final = json.dumps([{"title": "Codex planned task", "difficulty": "medium", "reading": [],
                             "body": "## Goal\nImplement the spec.\n## Acceptance criteria\n- Tests pass."}])
    elif "GARDEN_COMPARE:" in brief:
        labels = re.findall(r"^- \*\*(.+?)\*\* — branch", brief, flags=re.M)
        final = "GARDEN_COMPARE: " + json.dumps({
            "winner": labels[-1], "rationale": "checked both implementations",
            "ranking": [{"label": label, "score": 9 if label == labels[-1] else 6,
                         "summary": "checked"} for label in labels]})
    elif "GARDEN_REVIEW:" in brief:
        final = 'GARDEN_REVIEW: {"verdict":"approve","summary":"checked","description_ok":true,"findings":[]}'
    elif env.get("FAKE_CODEX_MODE") == "sandboxed":
        # A contender whose sandbox denies it the things a work run needs (CG-030's incident):
        # it never commits and never emits a GARDEN_RESULT, just this plain complaint.
        final = "I need resume with writable Git metadata, prepared dependencies, asset network access to continue."
    else:
        waiting = env.get("FAKE_CODEX_MODE") == "needs_input" and "resume" not in args
        filename = "codex-resumed.txt" if "resume" in args else "codex-output.txt"
        (cwd / filename).write_text(f"model={model}\nbrief={brief}\n")
        subprocess.run(["git", "add", "-A"], cwd=cwd, env=dict(env), check=True)
        subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake", "commit", "-q", "-m", "codex change"], cwd=cwd, env=dict(env), check=True)
        result: dict[str, Any] = {
            "status": "needs_input" if waiting else "done", "question": "Which database?" if waiting else "",
            "summary": f"codex did it with {model or 'default'}", "pr_title": "Codex PR", "pr_body": "body",
        }
        if not waiting:
            result["pre_flight"] = [
                {"item": item, "status": "pass", "evidence": "fake check"}
                for item in (
                    "A test or stated reason for every acceptance criterion", "Lint is clean",
                    "No conflict markers remain", "UI changes have 1280px and 390px captures",
                    "The PR description states the goal and outcome without process history",
                    "Every acceptance criterion is addressed by name",
                )
            ]
        final = "Done.\nGARDEN_RESULT: " + json.dumps(result)
    events = [
        {"type": "thread.started", "thread_id": "th_1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
        {"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 50, "output_tokens": 80}},
    ]
    for e in events:
        print(json.dumps(e))
    if final_path:
        final_path.write_text(final)
    return 0


def run(args: list[str], brief: str, cwd: Path, env: Mapping[str, str]) -> tuple[str, str, int | None]:
    """One in-process invocation: (stdout, stderr, exit code); the same shape as fake_claude.run."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = handle(list(args), brief, Path(cwd), env)
    return out.getvalue(), err.getvalue(), code


def main() -> None:
    sys.exit(handle(sys.argv[1:], sys.stdin.read(), Path.cwd(), dict(os.environ)))


if __name__ == "__main__":
    main()
