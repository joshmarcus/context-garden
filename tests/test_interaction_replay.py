from __future__ import annotations

import pytest

from garden import interaction_replay


@pytest.mark.parametrize(("head", "dirty"), [("different-head", ""), ("expected-head", " M src/garden/review.py")])
def test_replay_rejects_wrong_or_uncommitted_source_before_serving(monkeypatch, tmp_path, head, dirty):
    monkeypatch.setattr("sys.argv", ["replay", "--out", str(tmp_path), "--head", "expected-head", "--nonce", "nonce"])
    monkeypatch.setattr(interaction_replay.subprocess, "check_output",
                        lambda command, **kwargs: head if command[1] == "rev-parse" else dirty)
    launched = []
    monkeypatch.setattr(interaction_replay, "run_qa", lambda *args, **kwargs: launched.append(True))
    with pytest.raises(SystemExit) as exc:
        interaction_replay.main()
    assert exc.value.code == 2
    assert launched == []
