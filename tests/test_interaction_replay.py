from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from garden import interaction_replay
from garden import runner as runner_registry
from garden.runner.local import LocalRunner
from garden.scheduler import Scheduler
from garden.store import Store


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


def test_replay_records_performed_requests_and_existing_artifacts(monkeypatch, tmp_path):
    """The served replay may report only the requests it made and files it wrote."""
    # The regular suite substitutes deterministic in-process workers. The replay's served
    # app intentionally exercises the actual disposable worker command instead.
    monkeypatch.setitem(runner_registry.REGISTRY, "local", LocalRunner)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setattr("sys.argv", ["replay", "--out", str(tmp_path), "--head", head, "--nonce", "nonce"])

    assert interaction_replay.main() == 0

    manifest = tmp_path / "interaction-manifest.json"
    record = json.loads(manifest.read_text())
    assert record["artifacts"] == [str(manifest)]
    assert all(Path(path).exists() for path in record["artifacts"])
    requests = [request for flow in record["flows"] for request in flow["requests"]]
    assert all(any(
        event["kind"] == request["kind"]
        and event["method"] == request["method"]
        and event["url"] == request["url"]
        and event["status_code"] == request["status_code"]
        and event["at"] == request["at"]
        for request in requests
    ) for event in record["events"])


def test_task_specific_harness_pause_is_observed_through_the_served_app(tmp_path):
    """Reproduce the CG-332 distinction: this journey observes harness-pause behavior,
    while the generic fence replay above does not and must not stand in for it."""
    box = interaction_replay.start(tmp_path / "sandbox")
    try:
        scheduler = Scheduler(Store(box.garden))
        scheduler.pause_harness("claude", "bounded quota fixture")
        paused = httpx.get(box.base_url + "/", timeout=20)
        assert paused.status_code == 200
        assert "Harness paused" in paused.text
        assert "bounded quota fixture" in paused.text

        scheduler.resume_harness("claude")
        recovered = httpx.get(box.base_url + "/", timeout=20)
        assert recovered.status_code == 200
        assert "bounded quota fixture" not in recovered.text
    finally:
        box.stop()
