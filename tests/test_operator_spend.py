"""The operator's own session spend: parsing a transcript into a heartbeat record, turning
cumulative heartbeats into discrete cost events, and the sessions/totals view (CG-223)."""

from __future__ import annotations

import json
from pathlib import Path

from garden import operator_spend as ops


def _write_transcript(path: Path, turns: list[tuple[str, int, int]]) -> None:
    """turns: (model, input_tokens, output_tokens) per assistant turn."""
    lines = []
    for i, (model, i_tok, o_tok) in enumerate(turns):
        lines.append(json.dumps({
            "timestamp": f"2026-09-05T10:0{i}:00+00:00",
            "message": {"role": "assistant", "model": model,
                       "usage": {"input_tokens": i_tok, "output_tokens": o_tok,
                                "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 10}},
        }))
    lines.append(json.dumps({"message": {"role": "user", "content": "hi"}}))  # ignored: not an assistant turn
    path.write_text("\n".join(lines) + "\n")


def test_record_from_transcript_sums_usage_and_prices_by_model(tmp_path):
    path = tmp_path / "sess-abc123.jsonl"
    _write_transcript(path, [("claude-sonnet-5", 1000, 500), ("claude-sonnet-5", 2000, 1000)])
    rec = ops.record_from_transcript(path)
    assert rec["session"] == "sess-abc123"
    assert rec["turns"] == 2
    assert rec["models"] == {"claude-sonnet-5": 2}
    assert rec["tokens"]["input"] == 3000
    assert rec["tokens"]["output"] == 1500
    assert rec["tokens"]["cache_read"] == 2000
    # sonnet-5: $2/$10/$0.2/$2.5 per MTok -> (3000*2 + 1500*10 + 2000*0.2 + 20*2.5)/1e6
    assert rec["list_price_usd"] == round((3000 * 2.0 + 1500 * 10.0 + 2000 * 0.2 + 20 * 2.5) / 1e6, 2)
    assert rec["avg_context"] == 1000  # 2000 cache_read / 2 turns
    assert rec["first_turn"] == "2026-09-05T10:00:00+00:00"
    assert rec["last_turn"] == "2026-09-05T10:01:00+00:00"


def test_record_from_transcript_unknown_model_marks_pricing_unavailable(tmp_path):
    path = tmp_path / "sess-x.jsonl"
    _write_transcript(path, [("some-future-model", 1000, 1000)])
    rec = ops.record_from_transcript(path)
    assert rec["list_price_usd"] is None
    assert rec["price_status"] == "unavailable"


def test_record_from_codex_transcript_uses_latest_cumulative_usage_without_double_counting(tmp_path):
    path = tmp_path / "rollout-2026-09-06-operator.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in [
        {"timestamp": "2026-09-06T10:00:00Z", "type": "session_meta",
         "payload": {"id": "codex-operator"}},
        {"timestamp": "2026-09-06T10:00:30Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": "2026-09-06T10:01:00Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 1_000, "cached_input_tokens": 400, "output_tokens": 50,
                "reasoning_output_tokens": 10, "total_tokens": 1_050}}}},
        {"timestamp": "2026-09-06T10:02:00Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 2_000, "cached_input_tokens": 900, "output_tokens": 120,
                "reasoning_output_tokens": 30, "cache_write_input_tokens": 20, "total_tokens": 2_120}}}},
        {"timestamp": "2026-09-06T10:03:00Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-terra"}},
        {"timestamp": "2026-09-06T10:04:00Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 3_000, "cached_input_tokens": 1_000, "output_tokens": 180,
                "reasoning_output_tokens": 50, "cache_write_input_tokens": 30, "total_tokens": 3_180}}}},
    ]) + "\n")
    rec = ops.record_from_transcript(path)
    assert rec["harness"] == "codex"
    assert rec["session"] == "codex-operator"
    assert rec["turns"] == 2
    assert rec["models"] == {"gpt-5.6-sol": 1, "gpt-5.6-terra": 1}
    assert rec["tokens"] == {"input": 2_000, "cache_read": 1_000, "cache_write": 30, "output": 180}
    assert rec["tokens"]["input"] + rec["tokens"]["cache_read"] + rec["tokens"]["output"] == 3_180
    assert rec["first_turn"] == "2026-09-06T10:00:30Z"
    assert rec["last_turn"] == "2026-09-06T10:03:00Z"
    assert rec["list_price_usd"] is None
    assert rec["price_status"] == "unavailable"
    assert rec["usage_status"] == "available"
    event = ops.to_cost_events([rec])[0]
    assert event["cost_usd"] is None
    assert event["mode"] == "operator" and event["session"] == "codex-operator"


def test_record_from_codex_transcript_without_usage_marks_usage_unavailable(tmp_path):
    path = tmp_path / "rollout-empty.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "codex-empty"}}) + "\n")
    rec = ops.record_from_transcript(path)
    assert rec["harness"] == "codex"
    assert rec["tokens"] is None
    assert rec["usage_status"] == "unavailable"


def test_find_transcript_picks_newest_and_can_match_a_session(tmp_path):
    older = tmp_path / "sess-aaa.jsonl"
    newer = tmp_path / "sess-bbb.jsonl"
    older.write_text("")
    newer.write_text("")
    import os
    import time

    os.utime(older, (time.time() - 100, time.time() - 100))
    assert ops.find_transcript(tmp_path).name == "sess-bbb.jsonl"
    assert ops.find_transcript(tmp_path, session="aaa").name == "sess-aaa.jsonl"


def test_find_transcript_raises_when_nothing_matches(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        ops.find_transcript(tmp_path)
    (tmp_path / "sess-aaa.jsonl").write_text("")
    with pytest.raises(FileNotFoundError):
        ops.find_transcript(tmp_path, session="zzz")


def test_find_codex_transcript_searches_dated_sessions_and_matches_thread_id(tmp_path):
    path = tmp_path / "2026" / "09" / "06" / "rollout-operator.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-operator"}}) + "\n")
    assert ops.find_codex_transcript(tmp_path).name == path.name
    assert ops.find_codex_transcript(tmp_path, "thread-operator") == path


def test_project_dir_for_encodes_the_path_like_claude_code():
    d = ops.project_dir_for(Path("/home/joshua/context-garden"))
    assert d.name == "-home-joshua-context-garden"


def test_append_and_read_records_roundtrip(tmp_path):
    path = tmp_path / "docs" / "operator-spend.jsonl"
    ops.append(path, {"at": "t1", "session": "a", "list_price_usd": 1.0})
    ops.append(path, ops.compacted_record("a"))
    records = ops.read_records(path)
    assert len(records) == 2
    assert records[1]["kind"] == "compacted"
    assert ops.read_records(tmp_path / "missing.jsonl") == []


def test_to_cost_events_turns_cumulative_heartbeats_into_deltas():
    records = [
        {"at": "2026-09-05T10:00:00+00:00", "session": "a", "list_price_usd": 1.0},
        {"at": "2026-09-05T11:00:00+00:00", "session": "a", "list_price_usd": 2.5},
        {"at": "2026-09-05T10:30:00+00:00", "session": "b", "list_price_usd": 0.5},
        ops.compacted_record("a"),  # carries no cost
    ]
    events = ops.to_cost_events(records)
    assert len(events) == 3  # the compacted marker never becomes an event
    by_session_a = sorted((e for e in events if e["session"] == "a"), key=lambda e: e["at"])
    assert [e["cost_usd"] for e in by_session_a] == [1.0, 1.5]  # 1.0, then 2.5-1.0
    assert all(e["kind"] == "run_finished" and e["mode"] == "operator" for e in events)
    b = next(e for e in events if e["session"] == "b")
    assert b["cost_usd"] == 0.5


def test_attributed_totals_preserve_baseline_across_phase_and_window_changes():
    records = [
        {"at": "2026-09-04T10:00:00+00:00", "session": "a", "turns": 5,
         "list_price_usd": 10.0, "product": "other", "phase": "phase-01"},
        {"at": "2026-09-05T10:00:00+00:00", "session": "a", "turns": 7,
         "list_price_usd": 12.0, "product": "context-garden", "phase": "phase-05"},
        {"at": "2026-09-05T11:00:00+00:00", "session": "a", "turns": 10,
         "list_price_usd": 15.0},
        {"at": "2026-09-05T12:00:00+00:00", "session": "b", "turns": 4,
         "list_price_usd": 8.0, "product": "other", "phase": "phase-02"},
    ]
    since = "2026-09-05T00:00:00+00:00"
    def selected(row):
        return (row.get("product"), row.get("phase")) == ("context-garden", "phase-05")

    def unattributed(row):
        return not row.get("product") and not row.get("phase")

    assert ops.attributed_totals(records, since=since, include=selected) == (2.0, 2)
    assert ops.attributed_totals(records, since=since, include=unattributed) == (3.0, 3)

    # Independent later spend cannot change either selected total.
    records.append({"at": "2026-09-05T13:00:00+00:00", "session": "b", "turns": 6,
                    "list_price_usd": 11.0, "product": "other", "phase": "phase-02"})
    assert ops.attributed_totals(records, since=since, include=selected) == (2.0, 2)
    assert ops.attributed_totals(records, since=since, include=unattributed) == (3.0, 3)


def test_attributed_summary_distinguishes_zero_priced_and_unpriced_deltas():
    records = [
        {"at": "2026-09-05T10:00:00+00:00", "session": "priced", "turns": 1,
         "list_price_usd": 0.0, "product": "demo", "phase": "p1"},
        {"at": "2026-09-05T10:01:00+00:00", "session": "unknown", "turns": 2,
         "list_price_usd": None, "product": "demo", "phase": "p1"},
    ]
    summary = ops.attributed_summary(records, include=lambda row: row.get("product") == "demo")
    assert summary == {"known_cost_usd": 0.0, "turns": 3, "priced_records": 1,
                       "unpriced_records": 1, "cost_complete": False}

def test_to_cost_events_never_produces_a_negative_delta_if_a_heartbeat_regresses():
    records = [
        {"at": "2026-09-05T10:00:00+00:00", "session": "a", "list_price_usd": 5.0},
        {"at": "2026-09-05T11:00:00+00:00", "session": "a", "list_price_usd": 4.0},  # should not happen, but must not go negative
    ]
    events = ops.to_cost_events(records)
    assert all(e["cost_usd"] >= 0 for e in events)


def test_compaction_marks_pulls_out_only_compacted_records():
    records = [{"at": "t1", "session": "a", "list_price_usd": 1.0}, ops.compacted_record("a")]
    marks = ops.compaction_marks(records)
    assert marks == [{"at": marks[0]["at"], "session": "a"}]


def test_total_cost_windows_by_since():
    records = [
        {"at": "2026-09-04T10:00:00+00:00", "session": "a", "list_price_usd": 1.0},
        {"at": "2026-09-05T10:00:00+00:00", "session": "a", "list_price_usd": 3.0},
    ]
    assert ops.total_cost(records) == 3.0
    assert ops.total_cost(records, since="2026-09-05T00:00:00+00:00") == 2.0  # only the second heartbeat's delta


def test_session_rows_uses_latest_heartbeat_and_counts_compactions():
    records = [
        {"at": "2026-09-05T10:00:00+00:00", "session": "a", "turns": 5, "avg_context": 1000, "list_price_usd": 1.0},
        {"at": "2026-09-05T11:00:00+00:00", "session": "a", "turns": 9, "avg_context": 2000, "list_price_usd": 2.0},
        ops.compacted_record("a"),
        {"at": "2026-09-05T09:00:00+00:00", "session": "b", "turns": 1, "avg_context": 100, "list_price_usd": 0.1},
    ]
    rows = {r["session"]: r for r in ops.session_rows(records)}
    assert rows["a"]["turns"] == 9
    assert rows["a"]["cost_usd"] == 2.0
    assert rows["a"]["compactions"] == 1
    assert rows["b"]["compactions"] == 0
    # newest first
    assert ops.session_rows(records)[0]["session"] == "a"


def test_default_path_is_under_product_docs():
    from types import SimpleNamespace

    root = Path("/x")
    config = SimpleNamespace(data={"products": {"context-garden": {"self": True}}},
                             get=lambda key: None)
    assert ops.default_path(root, config) == root / "context-garden/docs/operator-spend.jsonl"


def test_default_path_stays_under_garden_docs_without_tool_product():
    assert ops.default_path(Path("/x")) == Path("/x/docs/operator-spend.jsonl")


def test_total_turns_is_windowed_from_cumulative_heartbeats():
    records = [
        {"at": "2026-01-01T00:00:00+00:00", "session": "a", "turns": 5},
        {"at": "2026-01-02T00:00:00+00:00", "session": "a", "turns": 9},
        {"at": "2026-01-03T00:00:00+00:00", "session": "a", "turns": 12},
    ]
    assert ops.total_turns(records, since="2026-01-02T00:00:00+00:00") == 7
