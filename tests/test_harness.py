from pathlib import Path

from garden.harness import Harness
from garden.runs import Run


def test_claude_command_and_models():
    h = Harness("claude", {"bin": "/x/claude"})
    cmd = h.command("opus", Path("/tmp/final.md"))
    assert cmd[:4] == ["/x/claude", "-p", "--output-format", "json"]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "opus"
    assert "--permission-mode" in cmd and "--allowedTools" in cmd
    assert h.model_for("easy") == "haiku" and h.model_for("hard") == "opus" and h.model_for("hard", "custom") == "custom"
    bypass = Harness("claude", {"permission_mode": "bypass"}).command()
    assert "--dangerously-skip-permissions" in bypass and "--allowedTools" not in bypass


def test_codex_command():
    h = Harness("codex", {})
    cmd = h.command("gpt-x", Path("/tmp/f.md"))
    assert cmd[:3] == ["codex", "exec", "--json"] and 'sandbox_mode="workspace-write"' in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-x" and cmd[-1] == "-"
    assert "--output-last-message" in cmd
    assert [h.model_for(t) for t in ("easy", "medium", "hard")] == [
        "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"
    ]
    assert Harness("codex", {"models": {}}).model_for("medium") == ""  # explicit CLI default


def test_custom_harness():
    h = Harness("mine", {"command": ["agent", "--run", "{model}"], "output": "text", "models": {"medium": "m1"}})
    assert h.command("m1") == ["agent", "--run", "m1"]
    out = h.parse('did stuff\nGARDEN_RESULT: {"status": "done", "summary": "s"}\n')
    assert out["result"]["status"] == "done" and out["final_text"].startswith("did stuff")


def test_max_turns_scalar_and_per_tier():
    h_unset = Harness("claude", {})
    assert h_unset.max_turns_for("easy") == 0  # unset → no cap
    assert h_unset.max_turns_for("medium") == 0

    h_scalar = Harness("claude", {"max_turns": 100})
    assert h_scalar.max_turns_for("easy") == 100
    assert h_scalar.max_turns_for("medium") == 100
    assert h_scalar.max_turns_for("hard") == 100

    h_dict = Harness("claude", {"max_turns": {"easy": 40, "medium": 60, "hard": 90}})
    assert h_dict.max_turns_for("easy") == 40
    assert h_dict.max_turns_for("medium") == 60
    assert h_dict.max_turns_for("hard") == 90
    assert h_dict.max_turns_for("unknown") == 60  # falls back to medium when medium is set

    h_partial = Harness("claude", {"max_turns": {"easy": 30}})
    assert h_partial.max_turns_for("easy") == 30
    assert h_partial.max_turns_for("medium") == 0  # not in dict, no fallback
    assert h_partial.max_turns_for("hard") == 0


def test_command_uses_difficulty_max_turns():
    h = Harness("claude", {"max_turns": {"easy": 40, "hard": 80}})
    cmd_easy = h.command("haiku", difficulty="easy")
    cmd_hard = h.command("opus", difficulty="hard")
    assert "--max-turns" in cmd_easy and cmd_easy[cmd_easy.index("--max-turns") + 1] == "40"
    assert "--max-turns" in cmd_hard and cmd_hard[cmd_hard.index("--max-turns") + 1] == "80"


def test_parse_claude_json():
    h = Harness("claude", {})
    out = h.parse('{"type":"result","subtype":"success","result":"hi\\nGARDEN_RESULT: {\\"status\\":\\"done\\"}","usage":{"input_tokens":5},"total_cost_usd":0.5}')
    assert out["usage"]["input_tokens"] == 5 and out["cost_usd"] == 0.5 and out["result"]["status"] == "done"
    err = h.parse('{"type":"result","subtype":"error_max_turns","is_error":true,"result":"","usage":{}}', "stderr text")
    assert err["error"].startswith("worker error")


def test_stream_json_command():
    h = Harness("claude", {"output_format": "stream-json"})
    cmd = h.command("opus")
    assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd


def test_parse_claude_stream_json():
    h = Harness("claude", {"output_format": "stream-json"})
    lines = "\n".join([
        '{"type":"system","subtype":"init","session_id":"s1","tools":[]}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Working..."}]}}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}}]}}',
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":[{"type":"text","text":"file.txt"}]}]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"Done.\\nGARDEN_RESULT: {\\"status\\":\\"done\\"}",'
        '"usage":{"input_tokens":10,"output_tokens":5},"total_cost_usd":0.01,"session_id":"s1"}',
    ])
    out = h.parse(lines)
    assert out["final_text"].startswith("Done.")
    assert out["result"]["status"] == "done"
    assert out["usage"]["input_tokens"] == 10
    assert out["cost_usd"] == 0.01
    assert out["session_id"] == "s1"


def test_parse_claude_stream_json_error():
    h = Harness("claude", {"output_format": "stream-json"})
    lines = '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"","usage":{}}'
    out = h.parse(lines)
    assert out["error"].startswith("worker error")


def test_parse_claude_stream_json_no_result():
    h = Harness("claude", {"output_format": "stream-json"})
    out = h.parse('{"type":"system","subtype":"init"}')
    assert out["final_text"] != ""


def test_stdout_events(tmp_path):
    r = Run(task_id="T", run_id="r1", dir=str(tmp_path), runner="local")
    assert r.stdout_events() == []
    (tmp_path / "stdout.json").write_text(
        '{"type":"system","session_id":"s1"}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}}]}}\n'
        '{"type":"result","subtype":"success","result":"done"}\n'
    )
    evs = r.stdout_events()
    assert len(evs) == 3 and evs[1]["message"]["content"][0]["name"] == "Bash"
    assert len(r.stdout_events(n=2)) == 2 and r.stdout_events(n=2)[0]["type"] == "assistant"


def test_parse_codex_jsonl(tmp_path):
    h = Harness("codex", {})
    lines = "\n".join([
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"GARDEN_RESULT: {\\"status\\":\\"done\\"}"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":3}}',
    ])
    out = h.parse(lines)
    assert out["session_id"] == "t1" and out["usage"]["cache_read_input_tokens"] == 2 and out["result"]["status"] == "done"
    final = tmp_path / "final.md"
    final.write_text("from file\nGARDEN_RESULT: {\"status\": \"blocked\"}")
    out = h.parse(lines, final_path=final)
    assert out["final_text"].startswith("from file") and out["result"]["status"] == "blocked"


def test_turn_cap_is_optional_and_off_by_default():
    assert "--max-turns" not in Harness("claude", {"bin": "/x/claude"}).command()
    assert "--max-turns" not in Harness("claude", {"bin": "/x/claude", "max_turns": 0}).command()
    capped = Harness("claude", {"bin": "/x/claude", "max_turns": 80}).command()
    assert capped[capped.index("--max-turns") + 1] == "80"


def test_codex_resume_and_permissions():
    h = Harness("codex", {})
    assert h.can_resume
    cmd = h.resume_command("thread-123", "gpt-test", Path("/tmp/final.md"))
    assert cmd[:4] == ["codex", "exec", "resume", "thread-123"]
    assert cmd[-1] == "-" and 'approval_policy="never"' in cmd
    assert "--full-auto" not in cmd
    assert Harness("codex", {"permission_mode": "full-auto"}).command() == h.command()
    assert 'sandbox_mode="read-only"' in Harness("codex", {"permission_mode": "read-only"}).command()
    assert "--dangerously-bypass-approvals-and-sandbox" in Harness("codex", {"permission_mode": "bypass"}).command()


def test_codex_usage_does_not_double_count_cache():
    parsed = Harness("codex", {}).parse('{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":3}}')
    assert parsed["usage"] == {"input_tokens": 8, "cache_read_input_tokens": 2, "output_tokens": 3}


def test_parse_classifies_not_logged_in_as_an_auth_env_error():
    """CG-217: a worker's isolated HOME can leave a harness unable to find its own saved
    login; that must read as an environment problem (env_error/auth), not a task failure."""
    h = Harness("claude", {})
    out = h.parse("", "Not logged in · Please run /login\n")
    assert out["env_error"] is True and out["env_kind"] == "auth"
    assert "not logged in" in out["error"].lower()

    # a run that used its own explicit error message keeps it, only tagged
    tagged = Harness("codex", {}).parse('{"type":"error","message":"not authenticated: run codex login"}')
    assert tagged["env_error"] is True and tagged["env_kind"] == "auth"
    assert tagged["error"] == "not authenticated: run codex login"

    # an ordinary failure carries no env_error at all
    ordinary = h.parse("", "some other crash\n")
    assert ordinary["env_error"] is False and ordinary["env_kind"] == ""


def test_login_probe_and_check_login():
    h = Harness("claude", {"bin": "/x/claude"})
    cmd, stdin_text = h.login_probe()
    assert cmd[0] == "/x/claude" and "-p" in cmd and "--output-format" in cmd
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "haiku"
    assert "ready" in cmd[-1].lower() and stdin_text == ""

    codex = Harness("codex", {"bin": "/x/codex"})
    cmd2, stdin2 = codex.login_probe()
    assert cmd2[:2] == ["/x/codex", "exec"] and cmd2[-1] == "-"
    assert "ready" in stdin2.lower()  # the trivial prompt goes on stdin for codex


def test_check_login_reports_auth_failure(monkeypatch):
    import subprocess

    h = Harness("claude", {"bin": "/x/claude"})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Not logged in · Please run /login\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = h.check_login({})
    assert ok is False and "not logged in" in detail.lower()


def test_check_login_ok(monkeypatch):
    import json as _json
    import subprocess

    h = Harness("claude", {"bin": "/x/claude"})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "ready"}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = h.check_login({})
    assert ok is True and detail == ""
