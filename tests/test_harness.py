from pathlib import Path

from garden.harness import Harness


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
    assert cmd[:3] == ["codex", "exec", "--json"] and "--full-auto" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-x" and cmd[-1] == "-"
    assert "--output-last-message" in cmd
    assert h.model_for("medium") == ""  # cli default


def test_custom_harness():
    h = Harness("mine", {"command": ["agent", "--run", "{model}"], "output": "text", "models": {"medium": "m1"}})
    assert h.command("m1") == ["agent", "--run", "m1"]
    out = h.parse('did stuff\nGARDEN_RESULT: {"status": "done", "summary": "s"}\n')
    assert out["result"]["status"] == "done" and out["final_text"].startswith("did stuff")


def test_parse_claude_json():
    h = Harness("claude", {})
    out = h.parse('{"type":"result","subtype":"success","result":"hi\\nGARDEN_RESULT: {\\"status\\":\\"done\\"}","usage":{"input_tokens":5},"total_cost_usd":0.5}')
    assert out["usage"]["input_tokens"] == 5 and out["cost_usd"] == 0.5 and out["result"]["status"] == "done"
    err = h.parse('{"type":"result","subtype":"error_max_turns","is_error":true,"result":"","usage":{}}', "stderr text")
    assert err["error"].startswith("worker error")


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
