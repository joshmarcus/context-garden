from __future__ import annotations

import contextlib
import io
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path


def run(args: list[str], brief: str, cwd: Path, env: Mapping[str, str]) -> tuple[str, str, int | None]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        model = args[args.index("-m") + 1] if "-m" in args else ""
        if "GARDEN_REVIEW:" in brief:
            final = 'GARDEN_REVIEW: {"verdict":"approve","summary":"checked","description_ok":true,"findings":[]}'
        elif "GARDEN_PERSONA:" in brief:
            final = 'GARDEN_PERSONA: {"persona":"security","score":9,"overall":"complete","findings":[]}'
        else:
            (cwd / "openrouter-output.txt").write_text(f"model={model}\n")
            subprocess.run(["git", "add", "-A"], cwd=cwd, env=dict(env), check=True)
            subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake",
                            "commit", "-q", "-m", "openrouter change"], cwd=cwd, env=dict(env), check=True)
            final = 'GARDEN_RESULT: {"status":"done","summary":"complete","pr_title":"change","pr_body":"body"}'
        print(json.dumps({"type": "thread.started", "thread_id": "openrouter-fake"}))
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": final}}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120,
                         "cached_input_tokens": 20, "output_tokens": 30, "cost": 0.0042}}))
        if "--output-last-message" in args:
            Path(args[args.index("--output-last-message") + 1]).write_text(final)
    return out.getvalue(), err.getvalue(), 0

def main() -> int:
    import os
    import sys
    stdout, stderr, code = run(sys.argv[1:], sys.stdin.read(), Path.cwd(), os.environ)
    print(stdout, end="")
    print(stderr, end="", file=sys.stderr)
    return int(code or 0)
