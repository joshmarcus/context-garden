import json
from pathlib import Path

import pytest

from garden.harness import Harness
from garden.personas import parse_persona
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


def test_claude_spend_limit_is_a_quota_env_error():
    h = Harness("claude", {})
    stdout = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                         "result": "You've hit your monthly spend limit. Upgrade your plan or wait for it to reset.",
                         "usage": {}, "total_cost_usd": 0.0})
    out = h.parse(stdout)
    assert out["env_error"] is True and out["env_kind"] == "quota"
    assert out["error"]  # still recorded, in case env_error goes unhandled somewhere


def test_codex_usage_limit_is_a_quota_env_error():
    h = Harness("codex", {})
    stdout = "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "th_1"},
        {"type": "turn.failed", "message": "You've hit your usage limit. Upgrade to Pro for more access."},
    ])
    out = h.parse(stdout)
    assert out["env_error"] is True and out["env_kind"] == "quota"


def test_ordinary_error_is_not_a_quota_env_error():
    h = Harness("claude", {})
    stdout = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                         "result": "ran out of turns", "usage": {}, "total_cost_usd": 0.01})
    out = h.parse(stdout)
    assert out["env_error"] is False and out["env_kind"] == ""


def test_quota_patterns_are_configurable_per_harness():
    h = Harness("claude", {"quota_patterns": ["custom limit reached"]})
    stdout = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                         "result": "You've hit your monthly spend limit.", "usage": {}, "total_cost_usd": 0.0})
    assert h.parse(stdout)["env_error"] is False  # the default phrase no longer matches
    stdout2 = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": "Custom limit reached, try later.", "usage": {}, "total_cost_usd": 0.0})
    out2 = h.parse(stdout2)
    assert out2["env_error"] is True and out2["env_kind"] == "quota"


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
    assert parsed["usage"] == {"input_tokens": 8, "cache_read_input_tokens": 2, "output_tokens": 3,
                               "cache_creation_input_tokens": 0}


def _codex_usage_line(**usage: int) -> str:
    import json as _json

    return _json.dumps({"type": "turn.completed", "usage": usage})


@pytest.mark.parametrize("model, expected_cost", [
    ("gpt-5.6-luna", (900 * 0.2 + 100 * 0.02 + 200 * 1.2) / 1_000_000),
    ("gpt-5.6-terra", (900 * 2.0 + 100 * 0.2 + 200 * 12.0) / 1_000_000),
    ("gpt-5.6-sol", (900 * 4.0 + 100 * 0.4 + 200 * 20.0) / 1_000_000),
    ("gpt-6-astra", (900 * 10.0 + 100 * 1.0 + 200 * 50.0) / 1_000_000),
])
def test_codex_cost_for_each_priced_model(model, expected_cost):
    line = _codex_usage_line(input_tokens=1000, cached_input_tokens=100, output_tokens=200)
    out = Harness("codex", {}).parse(line, model=model)
    assert out["cost_usd"] == pytest.approx(expected_cost)
    assert out["missing_price"] == ""
    assert out["model"] == model


def test_codex_cost_unpriced_model_records_usage_with_no_cost():
    line = _codex_usage_line(input_tokens=1000, cached_input_tokens=100, output_tokens=200)
    out = Harness("codex", {}).parse(line, model="some-other-model")
    assert out["usage"] == {"input_tokens": 900, "cache_read_input_tokens": 100, "output_tokens": 200,
                            "cache_creation_input_tokens": 0}
    assert out["cost_usd"] is None
    assert out["missing_price"] == "some-other-model"


def test_codex_cost_counts_cache_write_and_reasoning_tokens():
    line = _codex_usage_line(input_tokens=1000, cached_input_tokens=100, cache_write_input_tokens=50,
                             output_tokens=200, reasoning_output_tokens=30)
    out = Harness("codex", {}).parse(line, model="gpt-6-astra")
    assert out["usage"] == {"input_tokens": 900, "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": 50, "output_tokens": 230}
    expected = (900 * 10.0 + 100 * 1.0 + 50 * 12.5 + 230 * 50.0) / 1_000_000
    assert out["cost_usd"] == pytest.approx(expected)


def test_codex_cost_uses_long_context_tier_above_threshold():
    line = _codex_usage_line(input_tokens=300_000, cached_input_tokens=0, output_tokens=1000)
    out = Harness("codex", {}).parse(line, model="gpt-6-astra")
    expected = (300_000 * 20.0 + 1000 * 75.0) / 1_000_000
    assert out["cost_usd"] == pytest.approx(expected)


def test_codex_model_confirmed_from_cli_output_overrides_dispatch_model():
    lines = "\n".join([
        '{"type":"thread.started","thread_id":"t1","model":"gpt-5.6-terra"}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
    ])
    out = Harness("codex", {}).parse(lines, model="gpt-5.6-luna")
    assert out["model"] == "gpt-5.6-terra"


def test_codex_prices_configurable_and_generic_prices_merge():
    from garden.config import Config

    cfg = Config(root=Path("/tmp"), data={"prices": {"my-model": {"input": 1.0, "output": 2.0}},
                                          "harnesses": {"codex": {"prices": {"gpt-5.6-luna": {"input": 99.0, "output": 99.0}}}}})
    h = cfg.harness("codex")
    assert h.cfg["prices"]["my-model"] == {"input": 1.0, "output": 2.0}
    assert h.cfg["prices"]["gpt-5.6-luna"] == {"input": 99.0, "output": 99.0}
    assert "gpt-6-astra" in h.cfg["prices"]  # codex's own defaults still present


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


def test_auth_marker_inside_a_long_report_is_not_an_env_error():
    """A persona or work report that talks about a 'not logged in' outage is a finished run,
    not a login failure: the marker only counts on stderr, in the error text, or in a short
    output with no result block (the shape of the CLI's own message)."""
    h = Harness("claude", {"output_format": "stream-json"})
    prose = ("Phase review. Workers were briefly not logged in after CG-194's private HOME; "
             "the fix landed the same day. " * 40)
    payload = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": prose + '\nGARDEN_PERSONA: {"persona": "user", "score": 7, "findings": []}',
                          "usage": {"input_tokens": 1}, "total_cost_usd": 0.5})
    out = h.parse(payload)
    assert out["env_error"] is False and out["env_kind"] == ""
    assert out["cost_usd"] == 0.5

    # a short output carrying the CLI's message is still an auth failure
    short = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                        "result": "Not logged in · Please run /login", "usage": {}})
    assert h.parse(short)["env_kind"] == "auth"

    # a GARDEN_RESULT block in a short text also means the worker finished
    done = h.parse('{"type":"result","subtype":"success","result":"the login step said not logged in once; '
                   'fixed.\\nGARDEN_RESULT: {\\"status\\":\\"done\\"}","usage":{},"total_cost_usd":0.1}')
    assert done["env_error"] is False

    # Even an error-shaped result is a completed worker result once it carries a
    # parsed garden block; the marker in the report cannot turn it into auth.
    error_with_result = h.parse('{"type":"result","subtype":"error_during_execution","is_error":true,'
                                '"result":"not logged in was discussed; fixed.\\n'
                                'GARDEN_RESULT: {\\"status\\":\\"done\\"}","usage":{}}')
    assert error_with_result["env_error"] is False and error_with_result["result"]["status"] == "done"

    # Codex's agent message is worker prose, even when it is short and contains the marker.
    codex = Harness("codex", {})
    agent_message = json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "The outage said not logged in, but the task is done."
    }})
    parsed = codex.parse(agent_message, model="gpt-5.6-luna")
    assert parsed["env_error"] is False and parsed["env_kind"] == ""

    # A CLI error must win over worker prose regardless of event order.
    agent_then_error = "\n".join([
        agent_message,
        json.dumps({"type": "error", "message": "not authenticated: run codex login"}),
    ])
    parsed = codex.parse(agent_then_error, model="gpt-5.6-luna")
    assert parsed["env_error"] is True and parsed["env_kind"] == "auth"
    error_then_agent = "\n".join([
        json.dumps({"type": "error", "message": "not authenticated: run codex login"}),
        agent_message,
    ])
    parsed = codex.parse(error_then_agent, model="gpt-5.6-luna")
    assert parsed["env_error"] is True and parsed["env_kind"] == "auth"

    # A short plain output with another garden result block is worker output too.
    garden_block = "not logged in was discussed\nGARDEN_PERSONA: {}"
    parsed = h.parse(garden_block)
    assert parsed["env_error"] is False and parsed["env_kind"] == ""


def test_recovered_persona_result_texts_replay_done_cost_and_persona():
    """Replay the four recovered final persona messages inside result events.

    The raw stdout.json files were truncated and are unavailable. The adjacent fixtures
    preserve the complete marker-bearing result text, while the manifest preserves the
    recorded result metadata and cost; this is the closest possible replay of the incident.
    """
    h = Harness("claude", {"output_format": "stream-json"})
    replay_dir = Path(__file__).parent / "fixtures" / "persona-replay"
    manifest = json.loads((replay_dir / "manifest.json").read_text())
    assert len(manifest) == 4
    for filename, expected in manifest.items():
        report = (replay_dir / filename).read_text()
        assert "GARDEN_PERSONA:" in report
        stdout = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": report + '\nGARDEN_RESULT: {"status":"done"}',
            "usage": expected["usage"], "total_cost_usd": expected["cost_usd"],
        })

        out = h.parse(stdout)

        assert out["env_error"] is False and out["env_kind"] == ""
        assert out["result"]["status"] == "done" and out["cost_usd"] == expected["cost_usd"]
        assert parse_persona(out["final_text"])["persona"] == expected["persona"]


def test_quota_pattern_quoted_in_a_long_transcript_is_not_a_quota_error():
    """A worker that reads harness.py prints the quota pattern into its transcript; that is
    not a limit. The pattern counts in the error text, on stderr, or as a short output with
    no result block (the CLI's own message)."""
    h = Harness("claude", {"output_format": "stream-json"})
    quoted = '"quota_patterns": ["you\'ve hit your monthly spend limit"],'
    lines = "\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": [{"type": "text", "text": quoted}]}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "result": ("Read the harness module and its patterns. " * 60) + '\nGARDEN_RESULT: {"status": "done", "summary": "s"}',
                    "usage": {"input_tokens": 1}, "total_cost_usd": 0.3}),
    ])
    out = h.parse(lines)
    assert out["env_error"] is False and out["env_kind"] == "" and out["result"]["status"] == "done"

    real = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                       "result": "You've hit your monthly spend limit.", "usage": {}, "total_cost_usd": 0.0})
    assert h.parse(real)["env_kind"] == "quota"
    assert h.parse("", "You've hit your monthly spend limit\n")["env_kind"] == "quota"


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
