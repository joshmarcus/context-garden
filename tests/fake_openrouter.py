#!/usr/bin/env python3
"""Offline Codex/OpenRouter boundary used by harness and scheduler tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


def _config(args: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, arg in enumerate(args[:-1]):
        if arg == "-c":
            key, separator, value = args[index + 1].partition("=")
            if separator:
                values[key] = value.strip('"')
    return values


def handle(args: list[str], brief: str, cwd: Path, env: Mapping[str, str]) -> int:
    config = _config(args)
    key_name = config.get("model_providers.openrouter.env_key", "")
    required = {"model_provider": "openrouter", "model_providers.openrouter.name": "OpenRouter",
                "model_providers.openrouter.wire_api": "responses"}
    if not args or args[0] != "exec" or any(config.get(k) != v for k, v in required.items()):
        return 2
    if not config.get("model_providers.openrouter.base_url") or not key_name or not env.get(key_name):
        return 2
    model = args[args.index("-m") + 1] if "-m" in args else ""
    if not model or not brief.strip():
        return 2
    if "GARDEN_REVIEW:" in brief:
        final = 'GARDEN_REVIEW: {"verdict":"approve","summary":"OpenRouter checked it","description_ok":true,"findings":[]}'
    elif "GARDEN_PERSONA:" in brief:
        final = 'GARDEN_PERSONA: {"persona":"security","score":9,"overall":"OpenRouter persona complete","findings":[]}'
    elif brief == "Reply with the single word: ready.":
        final = "ready"
    else:
        (cwd / "openrouter-output.txt").write_text(f"model={model}\n")
        if (cwd / ".git").exists():
            subprocess.run(["git", "add", "-A"], cwd=cwd, env=dict(env), check=True)
            subprocess.run(["git", "-c", "user.email=fake@example.com", "-c", "user.name=fake",
                            "commit", "-q", "-m", "openrouter change"], cwd=cwd, env=dict(env), check=True)
        final = 'GARDEN_RESULT: {"status":"done","summary":"OpenRouter completed the run","pr_title":"OpenRouter change","pr_body":"body"}'
    final_path = Path(args[args.index("--output-last-message") + 1]) if "--output-last-message" in args else None
    events = [
        {"type": "thread.started", "thread_id": "openrouter-fake"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
        {"type": "turn.completed", "model": model, "usage": {
            "input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 30, "cost": 0.0042}},
    ]
    for event in events:
        print(json.dumps(event))
    if final_path:
        final_path.write_text(final)
    return 0


def run(args: list[str], brief: str, cwd: Path, env: Mapping[str, str]) -> tuple[str, str, int | None]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = handle(list(args), brief, Path(cwd), env)
    return out.getvalue(), err.getvalue(), code


if __name__ == "__main__":
    raise SystemExit(handle(sys.argv[1:], sys.stdin.read(), Path.cwd(), dict(os.environ)))
