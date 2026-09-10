from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from garden.store import Store
from garden.web.app import create_app
from scripts.benchmark_web_pages import _fixture, _routes

SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_web_pages.py"


def test_fixture_routes_follow_one_task_and_one_run(tmp_path):
    _fixture(tmp_path, tasks=1, runs=1, events=0)
    client = TestClient(create_app(Store(tmp_path), watch=False, host="testserver"))

    routes = _routes(1, 1)
    assert routes == ("/now", "/inbox", "/board", "/tasks/BM-0000", "/runs/BM-0000/seed-0000", "/config")
    assert all(client.get(route).status_code == 200 for route in routes)


def test_routes_keep_the_default_representative_identities_and_handle_mixed_sizes():
    assert _routes(600, 1200)[3:] == ("/tasks/BM-0001", "/runs/BM-0001/seed-0001", "/config")
    assert _routes(2, 3)[3:] == ("/tasks/BM-0001", "/runs/BM-0001/seed-0001", "/config")
    assert _routes(1, 3)[3:] == ("/tasks/BM-0000", "/runs/BM-0000/seed-0001", "/config")


def test_zero_task_or_run_counts_are_rejected_before_serving(tmp_path):
    env = {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    for option, message in (("--tasks", "--tasks must be at least 1"),
                            ("--runs", "--runs must be at least 1")):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), option, "0", "--repeats", "1"],
            cwd=tmp_path, env=env, capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert message in result.stderr


def test_small_benchmark_preserves_json_contract(tmp_path):
    env = {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tasks", "1", "--runs", "1", "--events", "0", "--repeats", "1"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["fixture"]["tasks"] == 1
    assert payload["before"]["server"]["/tasks/BM-0000"].keys() >= {"cold_ms", "median_ms", "max_ms", "bytes"}
    assert payload["before"]["server"]["/runs/BM-0000/seed-0000"]["bytes"] > 0
