#!/usr/bin/env python3
"""Stand-in for `codex exec --json`: reads the prompt from stdin, commits a file, emits JSONL
events and writes --output-last-message if given."""

import json
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
final_path = None
if "--output-last-message" in args:
    final_path = Path(args[args.index("--output-last-message") + 1])
model = args[args.index("-m") + 1] if "-m" in args else ""
brief = sys.stdin.read()
Path("codex-output.txt").write_text(f"model={model}\n")
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake", "commit", "-q", "-m", "codex change"], check=True)
final = "Done.\nGARDEN_RESULT: " + json.dumps({"status": "done", "summary": f"codex did it with {model or 'default'}", "pr_title": "Codex PR", "pr_body": "body"})
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
