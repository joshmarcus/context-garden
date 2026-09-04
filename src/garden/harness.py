"""Harness definitions: how to run an agent CLI headlessly and read its output.

A harness is data, not code: a binary, argument template, how the prompt is delivered,
and an output format. `claude` and `codex` are built in; override or add more under
`harnesses:` in garden.yaml. Runners (local, ssh) use these to build the command.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .brief import parse_result

DEFAULT_HARNESSES: dict[str, dict[str, Any]] = {
    "claude": {
        "bin": "claude",
        "output": "claude-json",        # claude -p --output-format json
        "max_turns": 60,
        "permission_mode": "acceptEdits",  # or "bypass"
        "allowed_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "MultiEdit"],
        "models": {"easy": "haiku", "medium": "sonnet", "hard": "opus"},
        "review_model": "",              # empty = the task's difficulty tier
        "resume": True,                  # `claude -p --resume <session>` after a human answers
    },
    "codex": {
        "bin": "codex",
        "output": "codex-jsonl",         # codex exec --json
        "permission_mode": "full-auto",  # or "bypass"
        "models": {},                    # empty = the CLI default for every tier
        "review_model": "",
        "resume": False,                 # set true if your codex supports `codex exec resume <id>`
    },
}

DIFFICULTIES = ("easy", "medium", "hard")


class Harness:
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        base = dict(DEFAULT_HARNESSES.get(name, {}))
        base.update(cfg or {})
        self.cfg = base

    @property
    def bin(self) -> str:
        return str(self.cfg.get("bin") or self.name)

    @property
    def output(self) -> str:
        return str(self.cfg.get("output") or "text")

    def model_for(self, difficulty: str, override: str = "") -> str:
        if override:
            return override
        models = self.cfg.get("models") or {}
        return str(models.get(difficulty) or models.get("medium") or "")

    # ---- command -----------------------------------------------------------
    def command(self, model: str = "", final_path: Path | None = None) -> list[str]:
        """Argv for one headless run. The brief arrives on stdin; cwd is the worktree."""
        if self.cfg.get("command"):
            # fully custom: a list with {model} / {final} placeholders
            out = []
            for a in self.cfg["command"]:
                a = str(a).replace("{model}", model).replace("{final}", str(final_path or ""))
                if a:
                    out.append(a)
            return out
        mode = str(self.cfg.get("permission_mode") or "")
        if self.output == "claude-json":
            cmd = [self.bin, "-p", "--output-format", "json", "--max-turns", str(self.cfg.get("max_turns", 60))]
            if model:
                cmd += ["--model", model]
            if mode in ("bypass", "bypassPermissions", "dangerously-skip-permissions"):
                cmd.append("--dangerously-skip-permissions")
            else:
                cmd += ["--permission-mode", mode or "acceptEdits"]
                tools = self.cfg.get("allowed_tools") or []
                if tools:
                    cmd += ["--allowedTools", ",".join(tools)]
            cmd += [str(a) for a in (self.cfg.get("extra_args") or [])]
            cmd.append("Carry out the brief that follows. It is the complete specification of your job.")
            return cmd
        if self.output == "codex-jsonl":
            cmd = [self.bin, "exec", "--json", "--skip-git-repo-check"]
            if mode in ("bypass", "dangerously-bypass-approvals-and-sandbox"):
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                cmd.append("--full-auto")
            if model:
                cmd += ["-m", model]
            if final_path is not None:
                cmd += ["--output-last-message", str(final_path)]
            cmd += [str(a) for a in (self.cfg.get("extra_args") or [])]
            cmd.append("-")  # prompt from stdin
            return cmd
        cmd = [self.bin, *[str(a) for a in (self.cfg.get("args") or [])]]
        if model and self.cfg.get("model_flag"):
            cmd += [str(self.cfg["model_flag"]), model]
        return cmd

    def shell_command(self, model: str = "", final_path: Path | None = None) -> str:
        return " ".join(shlex.quote(c) for c in self.command(model, final_path))

    def is_authenticated(self) -> bool:
        if self.name == "claude":
            try:
                subprocess.run([self.bin, "auth", "status", "--json"], capture_output=True, text=True, check=True)
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                return False
        elif self.name == "codex":
            try:
                subprocess.run([self.bin, "exec", "auth", "status"], capture_output=True, text=True, check=True)
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                return False
        return True

    @property
    def can_resume(self) -> bool:
        return bool(self.cfg.get("resume")) and (self.output in ("claude-json", "codex-jsonl") or bool(self.cfg.get("resume_command")))

    def resume_command(self, session_id: str, model: str = "", final_path: Path | None = None) -> list[str]:
        """Argv that continues a previous session; the follow-up prompt arrives on stdin."""
        if self.cfg.get("resume_command"):
            return [str(a).replace("{session}", session_id).replace("{model}", model).replace("{final}", str(final_path or ""))
                    for a in self.cfg["resume_command"] if str(a)]
        if self.output == "claude-json":
            cmd = self.command(model, final_path)
            cmd = cmd[:-1] + ["--resume", session_id, "Continue the task with the answer that follows."]
            return cmd
        if self.output == "codex-jsonl":
            cmd = self.command(model, final_path)
            # codex exec resume <id> [PROMPT]; keep the flags, prompt from stdin
            i = cmd.index("exec") + 1
            cmd = cmd[:i] + ["resume", session_id] + cmd[i:]
            return cmd
        raise ValueError(f"harness {self.name} cannot resume")

    def shell_resume_command(self, session_id: str, model: str = "", final_path: Path | None = None) -> str:
        return " ".join(shlex.quote(c) for c in self.resume_command(session_id, model, final_path))

    # ---- output ------------------------------------------------------------
    def parse(self, stdout: str, stderr: str = "", final_path: Path | None = None) -> dict[str, Any]:
        """Normalise output to {final_text, usage, cost_usd, error, session_id, result}."""
        out: dict[str, Any] = {"final_text": "", "usage": {}, "cost_usd": None, "error": "", "session_id": "", "result": {}}
        if final_path is not None and final_path.exists():
            out["final_text"] = final_path.read_text()
        if self.output == "claude-json":
            _parse_claude(stdout, out)
        elif self.output == "codex-jsonl":
            _parse_codex(stdout, out)
        else:
            out["final_text"] = out["final_text"] or stdout
        if not out["final_text"].strip() and not out["error"]:
            out["error"] = (stderr.strip()[-2000:] or "worker produced no output")
        out["result"] = parse_result(out["final_text"]) or parse_result(stdout)
        return out


def _last_json_object(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _parse_claude(stdout: str, out: dict[str, Any]) -> None:
    data = _last_json_object(stdout)
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
    if not isinstance(data, dict):
        out["final_text"] = out["final_text"] or stdout
        return
    final = data.get("result") if isinstance(data.get("result"), str) else ""
    out["final_text"] = final or out["final_text"]
    out["usage"] = data.get("usage") or {}
    cost = data.get("total_cost_usd", data.get("cost_usd"))
    out["cost_usd"] = float(cost) if isinstance(cost, (int, float)) else None
    out["session_id"] = str(data.get("session_id") or "")
    if data.get("is_error") or str(data.get("subtype", "")).startswith("error"):
        out["error"] = f"worker error: {data.get('subtype') or ''} {final[:500]}".strip()


def _parse_codex(stdout: str, out: dict[str, Any]) -> None:
    last_msg = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type", "")
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                last_msg = str(item["text"])
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            out["usage"] = {
                "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cache_read_input_tokens": u.get("cached_input_tokens", 0),
            }
        elif t == "thread.started":
            out["session_id"] = str(ev.get("thread_id") or "")
        elif t in ("error", "turn.failed"):
            out["error"] = str(ev.get("message") or ev.get("error") or "codex error")
    if not out["final_text"]:
        out["final_text"] = last_msg
