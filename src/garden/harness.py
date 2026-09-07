"""Harness definitions: how to run an agent CLI headlessly and read its output.

A harness is data, not code: a binary, argument template, how the prompt is delivered,
and an output format. `claude` and `codex` are built in; override or add more under
`harnesses:` in garden.yaml. Runners (local, ssh) use these to build the command.
"""

from __future__ import annotations

import json
import re
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

# Per-model prices in USD per million tokens, keyed by field name: `input` (uncached),
# `cached_input` (a cache hit; OpenAI prices this at 10% of the input rate), `cache_write`
# (creating a cache entry) and `output`. `long_context_threshold`/`long_context` give an
# alternate `input`/`output` rate for a single request whose total input tokens (uncached +
# cached + cache write) exceed the threshold — gpt-6-astra's tier above 272K tokens/request.
# List prices at the time this table was written (2026-09-05), from the operator's note in
# context-garden/phase-05/specs/cost-aware-model-routing.md after the CG-225 trials; both
# accounts bill by subscription, so this is a common measure of quota consumed, not a bill.
# Prices change — edit `harnesses.codex.prices` (or the generic `prices:` map) in garden.yaml
# rather than here, so a config change survives a `context-garden` upgrade.
CODEX_PRICES: dict[str, dict[str, Any]] = {
    "gpt-6-astra": {
        "input": 10.0, "cached_input": 1.0, "cache_write": 12.5, "output": 50.0,
        "long_context_threshold": 272_000,
        "long_context": {"input": 20.0, "output": 75.0},
    },
    "gpt-5.6-sol": {"input": 4.0, "cached_input": 0.4, "output": 20.0},    # promotional
    "gpt-5.6-terra": {"input": 2.0, "cached_input": 0.2, "output": 12.0},
    "gpt-5.6-luna": {"input": 0.2, "cached_input": 0.02, "output": 1.2},
}

DEFAULT_HARNESSES: dict[str, dict[str, Any]] = {
    "claude": {
        "bin": "claude",
        "output": "claude-json",        # claude -p --output-format json
        "permission_mode": "acceptEdits",  # or "bypass"
        "allowed_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "MultiEdit"],
        "models": {"easy": "haiku", "medium": "sonnet", "hard": "opus"},
        "review_model": "",              # empty = the task's difficulty tier
        "retro_model": "",                # empty = retro.difficulty's tier; see Scheduler.retro_model_for
        "resume": True,                  # `claude -p --resume <session>` after a human answers
        # A message matching one of these (case-insensitive substring) marks the run an
        # environment stop, not a task failure: see Harness.parse's env_error classification.
        "quota_patterns": ["you've hit your monthly spend limit"],
    },
    "codex": {
        "bin": "codex",
        "output": "codex-jsonl",         # codex exec --json
        "permission_mode": "workspace-write",  # or "read-only", "bypass"
        "models": {"easy": "gpt-5.6-luna", "medium": "gpt-5.6-terra", "hard": "gpt-5.6-sol"},
        "review_model": "",
        "retro_model": "",
        "resume": True,                  # `codex exec resume <id>` after a human answers
        "prices": CODEX_PRICES,
        "quota_patterns": ["you've hit your usage limit"],
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
    def _quota_kind(self, *texts: str) -> str:
        """Classify a worker's error/output against this harness's `quota_patterns` (a
        case-insensitive substring match, configurable per harness): "quota" for a monthly
        spend-limit or usage-limit message, else "". The scheduler treats a quota match as an
        environment stop, not a task failure (see reap.ReapMixin._handle_quota_env_error)."""
        patterns = [str(p).lower() for p in (self.cfg.get("quota_patterns") or []) if str(p)]
        if not patterns:
            return ""
        haystack = " ".join(t for t in texts if t).lower()
        return "quota" if any(p in haystack for p in patterns) else ""

    def _resource_kind(self, *texts: str) -> str:
        """Recognise process/filesystem exhaustion reported by the harness wrapper.

        As with quota matching, callers pass only the CLI's error channel or a short output
        with no result marker, never arbitrary worker prose.
        """
        configured = self.cfg.get("resource_patterns")
        patterns = [str(p).lower() for p in (configured or (
            "no space left on device", "cannot allocate memory", "out of memory", "errno 12",
        )) if str(p)]
        haystack = " ".join(t for t in texts if t).lower()
        return "resource" if any(pattern in haystack for pattern in patterns) else ""

    def parse(self, stdout: str, stderr: str = "", final_path: Path | None = None, model: str = "") -> dict[str, Any]:
        """Normalise output to {final_text, usage, cost_usd, error, session_id, result, model,
        missing_price, env_error, env_kind}. `env_error` is True when the run stopped on
        something outside the worker's control rather than the task (a login failure,
        `env_kind` "auth", or a quota/spend-limit message, `env_kind` "quota"); the scheduler
        pauses the harness instead of failing the task. `model` is the model the run was
        dispatched with (the resolved tier map or a `-m` override); a harness whose own output
        reports the model it actually ran (codex) confirms or overrides it with that.
        `missing_price` names a model that has usage but no entry in this harness's `prices`
        table, so `cost_usd` comes back None instead of silently wrong."""
        out: dict[str, Any] = {"final_text": "", "usage": {}, "cost_usd": None, "error": "", "session_id": "", "result": {},
                               "model": model, "missing_price": "", "env_error": False, "env_kind": ""}
        if final_path is not None and final_path.exists():
            out["final_text"] = final_path.read_text()
        if self.output == "claude-json":
            fmt = str(self.cfg.get("output_format") or "json")
            if fmt == "stream-json":
                _parse_claude_stream(stdout, out)
            else:
                _parse_claude(stdout, out)
        elif self.output == "codex-jsonl":
            _parse_codex(stdout, out, self.cfg.get("prices") or {})
        else:
            out["final_text"] = out["final_text"] or stdout
        if not out["final_text"].strip() and not out["error"]:
            out["error"] = (stderr.strip()[-2000:] or "worker produced no output")
        out["result"] = parse_result(out["final_text"]) or parse_result(stdout)
        # A quota message is the harness's own error: it is in the error text or on stderr,
        # or it is the whole of a short output with no result block. A worker that merely
        # quotes the pattern (one that reads this file, say) has not hit a limit.
        short = len(out["final_text"].strip()) < 2000 and not out["result"]
        kind = (self._quota_kind(out["error"], stderr) or self._resource_kind(out["error"], stderr)
                or ((self._quota_kind(out["final_text"], stdout) or self._resource_kind(out["final_text"], stdout))
                    if short else ""))
        if kind:
            out["env_error"] = True
            out["env_kind"] = kind
        if self._auth_failure(out, stdout, stderr):
            out["env_error"] = True
            out["env_kind"] = "auth"
            if not out["error"]:
                out["error"] = "not logged in"
        out.pop("_parsed_agent_message", None)
        return out

    def progress(self, stdout: str, model: str = "") -> dict[str, Any]:
        """What a run has done so far, read from its partial stream (`parse`'s sibling for a
        run still in flight): `said`, the newest assistant text; `usage`, the tokens read and
        written so far (each claude stream-json message's usage summed, since a long run is
        made of cache reads; codex's `turn.completed` total); `cost_usd`, that usage priced
        with this harness's table, None when the table has no entry for `model` (a page shows
        the tokens then, never an estimate); `tokens`, the usage summed. A finished claude-json
        result line counts too, so the same reader serves a run that has just ended."""
        said, usage = "", {}
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
            kind = ev.get("type")
            if kind == "assistant":  # claude stream-json
                msg = ev.get("message") or {}
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        said = str(block["text"])
                for k, v in (msg.get("usage") or {}).items():
                    if isinstance(v, (int, float)):
                        usage[k] = usage.get(k, 0) + int(v)
            elif kind == "result":  # claude-json: one object at the end
                if isinstance(ev.get("result"), str) and ev["result"]:
                    said = str(ev["result"])
                if ev.get("usage"):
                    usage = {k: int(v) for k, v in ev["usage"].items() if isinstance(v, (int, float))}
            elif kind == "item.completed":  # codex jsonl
                item = ev.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    said = str(item["text"])
            elif kind == "turn.completed":
                u = ev.get("usage") or {}
                usage = {"input_tokens": max(0, int(u.get("input_tokens", 0)) - int(u.get("cached_input_tokens", 0))),
                         "output_tokens": int(u.get("output_tokens", 0)) + int(u.get("reasoning_output_tokens", 0)),
                         "cache_read_input_tokens": int(u.get("cached_input_tokens", 0)),
                         "cache_creation_input_tokens": int(u.get("cache_write_input_tokens", 0))}
        cost = _usage_cost(usage, model, self.cfg.get("prices") or {})[0] if usage else None
        first_line = next((ln.strip() for ln in said.splitlines() if ln.strip()), "")
        return {"said": first_line, "usage": usage, "cost_usd": cost,
                "tokens": sum(int(v) for v in usage.values())}

    @staticmethod
    def _auth_failure(out: dict[str, Any], stdout: str, stderr: str) -> bool:
        """Whether the run stopped because the harness was not logged in. The CLI's own
        message is short and arrives on stderr or as the error text; a worker whose long
        report merely discusses a login outage is not a login failure (four persona reviews
        of phase 04 were discarded that way)."""
        # A parsed result is evidence that the CLI completed a worker turn. Its error
        # text can quote the marker as part of the worker's report, so it must take
        # precedence over the generic error-text check below.
        if out.get("result"):
            return False
        err_text = f"{stderr} {out.get('error') or ''}".lower()
        if any(marker in err_text for marker in AUTH_FAILURE_MARKERS):
            return True
        # An agent message is worker prose, but a separate Codex error event is the
        # CLI's own error and must win even when it follows that prose.
        if out.get("_parsed_agent_message"):
            return False
        if len(stdout.strip()) >= 2000 or out.get("result") or re.search(r"\bGARDEN_[A-Z0-9_]+:", stdout):
            return False
        blob = stdout.lower()
        return any(marker in blob for marker in AUTH_FAILURE_MARKERS)


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


def _price_tier(spec: dict[str, Any], total_input_tokens: int) -> dict[str, Any]:
    """`spec`'s prices, or its `long_context` override where given once `total_input_tokens`
    (uncached + cached + cache write, for one request) passes `long_context_threshold`."""
    threshold = spec.get("long_context_threshold")
    long_context = spec.get("long_context")
    if threshold and isinstance(long_context, dict) and total_input_tokens > threshold:
        return {**spec, **long_context}
    return spec


def _usage_cost(usage: dict[str, Any], model: str, prices: dict[str, Any]) -> tuple[float | None, str]:
    """List-price cost for one run's usage: uncached input, cached input (typically 10% of
    the input price), cache writes and output, each at `model`'s per-million-token rate in
    `prices`. Returns (None, model) when `model` has no entry — usage is still recorded, the
    cost just is not, rather than silently costing it at the wrong rate."""
    spec = prices.get(model)
    if not isinstance(spec, dict):
        return None, model
    input_tok = int(usage.get("input_tokens", 0) or 0)
    cached_tok = int(usage.get("cache_read_input_tokens", 0) or 0)
    write_tok = int(usage.get("cache_creation_input_tokens", 0) or 0)
    output_tok = int(usage.get("output_tokens", 0) or 0)
    tier = _price_tier(spec, input_tok + cached_tok + write_tok)
    cost = (input_tok * tier.get("input", 0) + cached_tok * tier.get("cached_input", 0)
            + write_tok * tier.get("cache_write", 0) + output_tok * tier.get("output", 0)) / 1_000_000
    return cost, ""


def _parse_codex(stdout: str, out: dict[str, Any], prices: dict[str, Any] | None = None) -> None:
    last_msg = ""
    reported_model = ""
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
        if ev.get("model"):
            reported_model = str(ev["model"])
        t = ev.get("type", "")
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                last_msg = str(item["text"])
                out["_parsed_agent_message"] = True
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            out["usage"] = {
                # Codex input includes cached tokens; garden counts them separately.
                "input_tokens": max(0, u.get("input_tokens", 0) - u.get("cached_input_tokens", 0)),
                # Reasoning tokens are billed as output; garden's usage shape (matching
                # claude's) has no separate field for them.
                "output_tokens": u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0),
                "cache_read_input_tokens": u.get("cached_input_tokens", 0),
                "cache_creation_input_tokens": u.get("cache_write_input_tokens", 0),
            }
        elif t == "thread.started":
            out["session_id"] = str(ev.get("thread_id") or "")
        elif t in ("error", "turn.failed"):
            out["error"] = str(ev.get("message") or ev.get("error") or "codex error")
    if not out["final_text"]:
        out["final_text"] = last_msg
    if reported_model:
        out["model"] = reported_model
    if out["usage"]:
        out["cost_usd"], out["missing_price"] = _usage_cost(out["usage"], str(out["model"]), prices or {})
