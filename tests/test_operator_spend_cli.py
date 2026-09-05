"""`garden operator-spend record` / `garden operator-spend`: the CLI moved from
tools/operator_spend.py (CG-223)."""

from __future__ import annotations

import json

from tests.test_cli import run


def _write_transcript(path, cost_calls: int = 2) -> None:
    lines = []
    for i in range(cost_calls):
        lines.append(json.dumps({
            "timestamp": f"2026-09-05T10:0{i}:00+00:00",
            "message": {"role": "assistant", "model": "claude-sonnet-5",
                       "usage": {"input_tokens": 1000, "output_tokens": 500}},
        }))
    path.write_text("\n".join(lines) + "\n")


def test_operator_spend_bare_reports_no_records_yet(garden):
    r = run(garden, "operator-spend")
    assert r.exit_code == 0, r.output
    assert "no operator spend recorded yet" in r.output
    assert "docs/operator-spend.jsonl" in r.output


def test_operator_spend_record_from_transcript_appends_and_prints(garden, tmp_path):
    transcript = tmp_path / "sess-deadbeef.jsonl"
    _write_transcript(transcript)
    r = run(garden, "operator-spend", "record", "--transcript", str(transcript))
    assert r.exit_code == 0, r.output
    assert "sess-deadb" in r.output or "session" in r.output

    out_path = garden / "docs" / "operator-spend.jsonl"
    records = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["session"] == "sess-deadbeef"
    assert records[0]["turns"] == 2
    assert records[0]["list_price_usd"] > 0


def test_operator_spend_prints_sessions_and_totals_after_recording(garden, tmp_path):
    transcript = tmp_path / "sess-one.jsonl"
    _write_transcript(transcript)
    run(garden, "operator-spend", "record", "--transcript", str(transcript))

    r = run(garden, "operator-spend")
    assert r.exit_code == 0, r.output
    assert "sess-one" in r.output.replace("\n", "")
    assert "total" in r.output

    r_json = run(garden, "operator-spend", "--json")
    rows = json.loads(r_json.output)
    assert rows[0]["session"] == "sess-one"


def test_operator_spend_record_compacted_needs_a_session(garden):
    r = run(garden, "operator-spend", "record", "--compacted")
    assert r.exit_code == 2
    assert "--session" in r.output


def test_operator_spend_record_compacted_appends_a_marker(garden):
    r = run(garden, "operator-spend", "record", "--compacted", "--session", "sess-one")
    assert r.exit_code == 0, r.output
    out_path = garden / "docs" / "operator-spend.jsonl"
    records = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert records[0] == {"at": records[0]["at"], "session": "sess-one", "kind": "compacted"}


def test_operator_spend_record_out_overrides_the_default_path(garden, tmp_path):
    transcript = tmp_path / "sess-two.jsonl"
    _write_transcript(transcript, cost_calls=1)
    out = tmp_path / "custom-spend.jsonl"
    r = run(garden, "operator-spend", "record", "--transcript", str(transcript), "--out", str(out))
    assert r.exit_code == 0, r.output
    assert out.exists()
    assert not (garden / "docs" / "operator-spend.jsonl").exists()


def test_operator_spend_record_transcript_not_found(garden, tmp_path):
    r = run(garden, "operator-spend", "record", "--project", str(tmp_path / "nope"))
    assert r.exit_code == 1
    assert "no transcript found" in r.output
