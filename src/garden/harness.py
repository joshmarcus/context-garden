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

# Substrings (checked case-insensitively across stdout, stderr and the parsed final message)
# that mark a run as failing for lack of a login rather than for anything the brief asked:
# claude's own CLI prints "Not logged in · Please run /login" when the scrubbed environment's
# CLAUDE_CONFIG_DIR carries no credentials. `Harness.parse` tags these `env_error: True,
# env_kind: "auth"` (the same environment-stop convention a quota/spend-limit error uses) so
# `garden doctor` (`Harness.check_login`) can report the fix, and so the scheduler can pause
# the harness instead of failing the task.
AUTH_FAILURE_MARKERS = ("not logged in", "not authenticated")

DEFAULT_HARNESSES: dict[str, dict[str, Any]] = {
    "claude": {
        "bin": "claude",
        "output": "claude-json",        # claude -p --output-format json
        "permission_mode": "acceptEdits",  # or "bypass"
        "allowed_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "MultiEdit"],
        "models": {"easy": "haiku", "medium": "sonnet", "hard": "opus"},
        "review_model": "",              # empty = the task's difficulty tier
        "resume": True,                  # `claude -p --resume <session>` after a human answers
    },
    "codex": {
        "bin": "codex",
        "output": "codex-jsonl",         # codex exec --json
        "permission_mode": "workspace-write",  # or "read-only", "bypass"
        "models": {"easy": "gpt-5.6-luna", "medium": "gpt-5.6-terra", "hard": "gpt-5.6-sol"},
        "review_model": "",
        "resume": True,                  # `codex exec resume <id>` after a human answers
    },
}

DIFFICULTIES = ("easy", "medium", "hard")

# Claude model aliases and the full model names in use today, offered alongside a claude
# harness's own tier map when picking a trial contender (see Harness.known_models).
CLAUDE_MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable")
CLAUDE_MODEL_NAMES = ("claude-sonnet-5", "claude-opus-4-8", "fable")


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

    def known_models(self) -> list[str]:
        """Model choices to offer when picking this harness for a trial contender: its tier
        map and review model from config, plus (for claude) the aliases and full model names
        in use today. Order is stable and deduplicated."""
        out: list[str] = []
        for tier in DIFFICULTIES:
            m = str((self.cfg.get("models") or {}).get(tier) or "")
            if m and m not in out:
                out.append(m)
        review = str(self.cfg.get("review_model") or "")
        if review and review not in out:
            out.append(review)
        if self.name == "claude":
            for m in (*CLAUDE_MODEL_ALIASES, *CLAUDE_MODEL_NAMES):
                if m not in out:
                    out.append(m)
        return out

    def max_turns_for(self, difficulty: str) -> int:
        """Get max_turns for a difficulty tier. Returns 0 if unset (no cap). Supports scalar or per-tier dict."""
        max_turns = self.cfg.get("max_turns")
        if not max_turns:
            return 0
        if isinstance(max_turns, dict):
            return int(max_turns.get(difficulty) or max_turns.get("medium") or 0)
        return int(max_turns)

    # ---- command -----------------------------------------------------------
    def fence_settings(self, deny_paths: list[str] | None, worktree: Path | str | None) -> str:
        """A `--settings` JSON payload that keeps a worker's writes inside its worktree.

        `deny_paths` are directories a worker must never touch (the live garden, the product
        clone): each becomes a `permissions.deny` rule for the file-editing tools and for the
        obvious `Bash` escapes. Deny rules are evaluated before the `acceptEdits` mode, so an
        edit inside the worktree still needs no prompt while an edit outside it is refused
        (and in `-p` mode a refused edit simply fails). When the harness config sets
        `sandbox: true` and a worktree is known, an OS-level sandbox confines every process's
        writes to the worktree — belt to the deny rules' braces."""
        deny: list[str] = []
        for p in deny_paths or []:
            ap = str(p).rstrip("/")
            if not ap:
                continue
            rule_path = "//" + ap.lstrip("/")
            for tool in ("Edit", "Write"):
                deny.append(f"{tool}({rule_path}/**)")
            deny.append(f"Bash(cd {ap}*)")
            deny.append(f"Bash(git -C {ap}*)")
        settings: dict[str, Any] = {}
        if deny:
            settings["permissions"] = {"deny": deny}
        if worktree and self.cfg.get("sandbox"):
            wt = str(worktree).rstrip("/")
            settings["sandbox"] = {"enabled": True,
                                   "filesystem": {"allowWrite": [wt, "$TMPDIR"], "denyWrite": ["//"]}}
        return json.dumps(settings, separators=(",", ":")) if settings else ""

    def command(self, model: str = "", final_path: Path | None = None, difficulty: str = "",
                deny_paths: list[str] | None = None, worktree: Path | str | None = None) -> list[str]:
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
            fmt = str(self.cfg.get("output_format") or "json")
            cmd = [self.bin, "-p", "--output-format", fmt]
            if fmt == "stream-json":
                cmd.append("--verbose")
            # A hard turn cap is optional and off by default: a run that hits it ends with no
            # final message and no result line, so the wall-clock timeout and the phase budget
            # are the guards that count. Set `max_turns` to a number (or a per-tier dict) to add the cap.
            max_turns = self.max_turns_for(difficulty or "medium")
            if max_turns > 0:
                cmd += ["--max-turns", str(max_turns)]
            if model:
                cmd += ["--model", model]
            if mode in ("bypass", "bypassPermissions", "dangerously-skip-permissions"):
                cmd.append("--dangerously-skip-permissions")
            else:
                cmd += ["--permission-mode", mode or "acceptEdits"]
                tools = self.cfg.get("allowed_tools") or []
                if tools:
                    cmd += ["--allowedTools", ",".join(tools)]
                fence = self.fence_settings(deny_paths, worktree)
                if fence:
                    cmd += ["--settings", fence]
            cmd += [str(a) for a in (self.cfg.get("extra_args") or [])]
            cmd.append("Carry out the brief that follows. It is the complete specification of your job.")
            return cmd
        if self.output == "codex-jsonl":
            cmd = [self.bin, "exec", "--json", "--skip-git-repo-check"]
            if mode in ("bypass", "dangerously-bypass-approvals-and-sandbox"):
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                # Config overrides work for both exec and exec resume. full-auto is
                # retained as a garden config alias for older configurations.
                sandbox = "workspace-write" if mode in ("", "full-auto") else mode
                if sandbox not in ("workspace-write", "read-only"):
                    raise ValueError(f"unsupported Codex permission_mode: {mode}")
                cmd += ["-c", f'sandbox_mode="{sandbox}"', "-c", 'approval_policy="never"']
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

    def shell_command(self, model: str = "", final_path: Path | None = None, difficulty: str = "") -> str:
        return " ".join(shlex.quote(c) for c in self.command(model, final_path, difficulty))

    def login_probe(self) -> tuple[list[str], str]:
        """argv and stdin text for a trivial one-line prompt used only to check login —
        `garden doctor`'s use, never a real work run. No permission flags, no fence: the
        probe writes nothing and needs none."""
        prompt = "Reply with the single word: ready."
        model = self.model_for("easy")
        if self.output == "claude-json":
            cmd = [self.bin, "-p", "--output-format", "json"]
            if model:
                cmd += ["--model", model]
            cmd.append(prompt)
            return cmd, ""
        if self.output == "codex-jsonl":
            cmd = [self.bin, "exec", "--json", "--skip-git-repo-check", "-c", 'approval_policy="never"']
            if model:
                cmd += ["-m", model]
            cmd.append("-")
            return cmd, prompt
        return [self.bin], prompt  # a fully custom harness: best effort with its default shape

    def check_login(self, env: dict[str, str], timeout: int = 30) -> tuple[bool, str]:
        """Run the one-line prompt from `login_probe` through `env` (see
        `runner.base.scrubbed_env`) — the environment a worker actually gets, not the
        operator's shell — and report (logged in, detail). False when `parse` classifies the
        output as an auth failure, or the binary could not be run at all."""
        cmd, stdin_text = self.login_probe()
        try:
            proc = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True,
                                  timeout=timeout, env=env, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        parsed = self.parse(proc.stdout, proc.stderr)
        if parsed.get("env_kind") == "auth":
            return False, parsed.get("error") or "not logged in"
        if proc.returncode != 0:
            return False, parsed.get("error") or f"exited {proc.returncode}"
        return True, ""

    @property
    def can_resume(self) -> bool:
        return bool(self.cfg.get("resume")) and (self.output in ("claude-json", "codex-jsonl") or bool(self.cfg.get("resume_command")))

    def resume_command(self, session_id: str, model: str = "", final_path: Path | None = None, difficulty: str = "",
                       deny_paths: list[str] | None = None, worktree: Path | str | None = None) -> list[str]:
        """Argv that continues a previous session; the follow-up prompt arrives on stdin."""
        if self.cfg.get("resume_command"):
            return [str(a).replace("{session}", session_id).replace("{model}", model).replace("{final}", str(final_path or ""))
                    for a in self.cfg["resume_command"] if str(a)]
        if self.output == "claude-json":
            cmd = self.command(model, final_path, difficulty, deny_paths=deny_paths, worktree=worktree)
            cmd = cmd[:-1] + ["--resume", session_id, "Continue the task with the answer that follows."]
            return cmd
        if self.output == "codex-jsonl":
            cmd = self.command(model, final_path, difficulty, deny_paths=deny_paths, worktree=worktree)
            # codex exec resume <id> [PROMPT]; keep the flags, prompt from stdin
            i = cmd.index("exec") + 1
            cmd = cmd[:i] + ["resume", session_id] + cmd[i:]
            return cmd
        raise ValueError(f"harness {self.name} cannot resume")

    def shell_resume_command(self, session_id: str, model: str = "", final_path: Path | None = None, difficulty: str = "") -> str:
        return " ".join(shlex.quote(c) for c in self.resume_command(session_id, model, final_path, difficulty))

    # ---- output ------------------------------------------------------------
    def parse(self, stdout: str, stderr: str = "", final_path: Path | None = None) -> dict[str, Any]:
        """Normalise output to {final_text, usage, cost_usd, error, session_id, result,
        env_error, env_kind}. `env_error` is True when the run stopped on something outside
        the worker's control rather than the task (currently just a login failure, `env_kind`
        "auth"); the scheduler pauses the harness instead of failing the task."""
        out: dict[str, Any] = {"final_text": "", "usage": {}, "cost_usd": None, "error": "", "session_id": "", "result": {},
                               "env_error": False, "env_kind": ""}
        if final_path is not None and final_path.exists():
            out["final_text"] = final_path.read_text()
        if self.output == "claude-json":
            fmt = str(self.cfg.get("output_format") or "json")
            if fmt == "stream-json":
                _parse_claude_stream(stdout, out)
            else:
                _parse_claude(stdout, out)
        elif self.output == "codex-jsonl":
            _parse_codex(stdout, out)
        else:
            out["final_text"] = out["final_text"] or stdout
        if not out["final_text"].strip() and not out["error"]:
            out["error"] = (stderr.strip()[-2000:] or "worker produced no output")
        out["result"] = parse_result(out["final_text"]) or parse_result(stdout)
        blob = f"{out['final_text']} {stdout} {stderr}".lower()
        if any(marker in blob for marker in AUTH_FAILURE_MARKERS):
            out["env_error"] = True
            out["env_kind"] = "auth"
            if not out["error"]:
                out["error"] = "not logged in"
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


def _parse_claude_dict(data: dict[str, Any], out: dict[str, Any]) -> None:
    """Extract fields from a claude result dict into out."""
    final = data.get("result") if isinstance(data.get("result"), str) else ""
    out["final_text"] = final or out["final_text"]
    out["usage"] = data.get("usage") or {}
    cost = data.get("total_cost_usd", data.get("cost_usd"))
    out["cost_usd"] = float(cost) if isinstance(cost, (int, float)) else None
    out["session_id"] = str(data.get("session_id") or "")
    if data.get("is_error") or str(data.get("subtype", "")).startswith("error"):
        out["error"] = f"worker error: {data.get('subtype') or ''} {final[:500]}".strip()


def _parse_claude(stdout: str, out: dict[str, Any]) -> None:
    data = _last_json_object(stdout)
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
    if not isinstance(data, dict):
        out["final_text"] = out["final_text"] or stdout
        return
    _parse_claude_dict(data, out)


def _parse_claude_stream(stdout: str, out: dict[str, Any]) -> None:
    """Parse claude --output-format stream-json JSONL: locate the final result event."""
    result_data: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            result_data = ev
    if not isinstance(result_data, dict):
        out["final_text"] = out["final_text"] or stdout
        return
    _parse_claude_dict(result_data, out)


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
        if not isinstance(ev, dict):
            continue
        t = ev.get("type", "")
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                last_msg = str(item["text"])
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            out["usage"] = {
                # Codex input includes cached tokens; garden counts them separately.
                "input_tokens": max(0, u.get("input_tokens", 0) - u.get("cached_input_tokens", 0)),
                "output_tokens": u.get("output_tokens", 0),
                "cache_read_input_tokens": u.get("cached_input_tokens", 0),
            }
        elif t == "thread.started":
            out["session_id"] = str(ev.get("thread_id") or "")
        elif t in ("error", "turn.failed"):
            out["error"] = str(ev.get("message") or ev.get("error") or "codex error")
    if not out["final_text"]:
        out["final_text"] = last_msg
