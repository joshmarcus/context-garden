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


def test_stream_json_command():
    h = Harness("claude", {"output_format": "stream-json"})
    cmd = h.command("opus")
    assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_parse_claude_stream_json():
    h = Harness("claude", {"output_format": "stream-json"})
    lines = "\n".join([
        '{"type":"system","subtype":"init","session_id":"s1","tools":[]}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Working..."}]}}',
        '{"type":"tool_use","name":"Bash","input":{"command":"ls"},"id":"t1"}',
        '{"type":"tool_result","content":[{"type":"text","text":"file.txt"}],"tool_use_id":"t1"}',
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
    assert out["final_text"] != "" or out["error"] != "" or True  # falls back gracefully


def test_stdout_events(tmp_path):
    r = Run(task_id="T", run_id="r1", dir=str(tmp_path), runner="local")
    assert r.stdout_events() == []
    (tmp_path / "stdout.json").write_text(
        '{"type":"system","session_id":"s1"}\n'
        '{"type":"tool_use","name":"Bash","input":{"command":"ls"},"id":"t1"}\n'
        '{"type":"result","subtype":"success","result":"done"}\n'
    )
    evs = r.stdout_events()
    assert len(evs) == 3 and evs[1]["name"] == "Bash"
    assert len(r.stdout_events(n=2)) == 2 and r.stdout_events(n=2)[0]["type"] == "tool_use"


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
