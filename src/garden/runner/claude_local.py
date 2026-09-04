"""Run `claude -p` headless in the task worktree, detached from the scheduler process.

The scheduler is a plain Python process, so waiting costs nothing. Only the worker
spends tokens, and it only sees the brief.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..brief import parse_result
from ..runs import Run
from .base import Runner


class ClaudeLocalRunner(Runner):
    name = "claude-local"

    def command(self, brief_path: Path) -> list[str]:
        c = self.config
        cmd = [c.get("bin", "claude"), "-p", "--output-format", "json", "--max-turns", str(c.get("max_turns", 60))]
        model = c.get("model")
        if model:
            cmd += ["--model", str(model)]
        mode = str(c.get("permission_mode", "acceptEdits"))
        if mode in ("bypass", "bypassPermissions", "dangerously-skip-permissions"):
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd += ["--permission-mode", mode]
            tools = c.get("allowed_tools") or []
            if tools:
                cmd += ["--allowedTools", ",".join(tools)]
        for extra in c.get("extra_args") or []:
            cmd.append(str(extra))
        cmd.append("Carry out the task brief that follows. It is the complete specification of your job.")
        return cmd

    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        d = run.path
        brief_path = d / "brief.md"
        brief_path.write_text(brief_text)
        cmd = self.command(brief_path)
        timeout_min = int(self.config.get("timeout_minutes", 90) or 0)
        inner = " ".join(shlex.quote(c) for c in cmd)
        if timeout_min and shutil.which("timeout"):
            inner = f"timeout {timeout_min * 60} {inner}"
        script = (
            f"cd {shlex.quote(str(worktree))} && {inner} "
            f"< {shlex.quote(str(brief_path))} > {shlex.quote(str(d / 'stdout.json'))} "
            f"2> {shlex.quote(str(d / 'stderr.log'))}; echo $? > {shlex.quote(str(d / 'exit_code'))}"
        )
        env = dict(os.environ)
        # allow launching from inside another Claude Code session
        env.pop("CLAUDECODE", None)
        env.setdefault("GARDEN_TASK_ID", run.task_id)
        proc = subprocess.Popen(
            ["sh", "-c", script],
            cwd=str(worktree),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        run.pid = proc.pid
        run.save()
        (d / "command.txt").write_text(script + "\n")

    def collect(self, run: Run) -> dict[str, Any]:
        raw = run.stdout_text()
        out: dict[str, Any] = {"result": {}, "usage": {}, "cost_usd": None, "final_text": "", "error": ""}
        if not raw.strip():
            out["error"] = (run.stderr_text().strip()[-2000:] or "worker produced no output")
            return out
        data: Any = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # stream-json or trailing noise: take the last JSON object line
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
        if isinstance(data, list):
            data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), data[-1] if data else None)
        if not isinstance(data, dict):
            out["final_text"] = raw
            out["result"] = parse_result(raw)
            if not out["result"]:
                out["error"] = "could not parse worker output"
            return out
        final = data.get("result") if isinstance(data.get("result"), str) else ""
        out["final_text"] = final or ""
        out["usage"] = data.get("usage") or {}
        cost = data.get("total_cost_usd", data.get("cost_usd"))
        out["cost_usd"] = float(cost) if isinstance(cost, (int, float)) else None
        out["num_turns"] = data.get("num_turns")
        out["session_id"] = data.get("session_id")
        if data.get("is_error") or data.get("subtype", "").startswith("error"):
            out["error"] = f"worker error: {data.get('subtype') or ''} {final[:500]}".strip()
        out["result"] = parse_result(final or raw)
        return out

    def doctor(self) -> list[str]:
        problems = []
        if not shutil.which(self.config.get("bin", "claude")):
            problems.append(f"claude binary {self.config.get('bin', 'claude')!r} not found on PATH")
        return problems
