from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from garden import interaction_replay
from garden.review import _replay_manifest_gaps


@pytest.mark.parametrize(("head", "dirty"), [("different-head", ""), ("expected-head", " M src/garden/review.py")])
def test_replay_rejects_wrong_or_uncommitted_source_before_serving(monkeypatch, tmp_path, head, dirty):
    monkeypatch.setattr("sys.argv", ["replay", "--out", str(tmp_path), "--head", "expected-head", "--nonce", "nonce"])
    monkeypatch.setattr(interaction_replay.subprocess, "check_output",
                        lambda command, **kwargs: head if command[1] == "rev-parse" else dirty)
    launched = []
    monkeypatch.setattr(interaction_replay, "start", lambda *args, **kwargs: launched.append(True))
    with pytest.raises(SystemExit) as exc:
        interaction_replay.main()
    assert exc.value.code == 2
    assert launched == []


def test_replay_serves_the_fence_incident_and_records_all_outcomes(tmp_path, monkeypatch):
    """The review artifact is a real disposable HTTP replay, not a TestClient assertion."""
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setattr("sys.argv", ["replay", "--out", str(tmp_path), "--head", head, "--nonce", "nonce"])

    assert interaction_replay.main() == 0

    manifest = tmp_path / "interaction-manifest.json"
    record = json.loads(manifest.read_text())
    assert record["serve_command"].startswith("uvicorn.Server(")
    assert [event["state"] for event in record["events"]] == ["affected", "failure", "recovery", "empty"]
    assert _replay_manifest_gaps(manifest, head, "nonce", hashlib.sha256(manifest.read_bytes()).hexdigest()) == []
