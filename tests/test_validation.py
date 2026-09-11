from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from garden import validation
from garden.validation import (
    POLICY_ADDOPTS,
    POLICY_SOURCE_SHA,
    STRESS_NODES,
    ValidationPolicyError,
    enforce_validation_policy_env,
    resolve_validation,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _checkout(tmp_path: Path, shape: str) -> Path:
    repo = tmp_path / shape
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_web.py").write_text(
        "def test_functional(tmp_path):\n"
        "    (tmp_path / 'functional').write_text('ran')\n\n"
        "def test_initial_pages_stay_bounded_with_large_run_history():\n"
        "    raise AssertionError('stress workload ran')\n\n"
        "def test_retained_history_journey_stays_responsive_with_running_and_waiting_pytest():\n"
        "    raise AssertionError('stress workload ran')\n\n"
        "def test_served_incident_controls_retry_and_restart_during_overload():\n"
        "    raise AssertionError('stress workload ran')\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "old source")
    if shape == "current":
        (tests / "conftest.py").write_text(
            "def pytest_addoption(parser):\n"
            "    parser.addoption('--run-stress', action='store_true')\n"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "current policy hook")
    elif shape == "stacked":
        _git(repo, "checkout", "-q", "-b", "parent")
        (repo / "parent.txt").write_text("parent implementation\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "parent implementation")
        _git(repo, "checkout", "-q", "-b", "child")
        (repo / "child.txt").write_text("child implementation\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "child implementation")
    elif shape == "dirty":
        (repo / "implementation.py").write_text("valuable_uncommitted_edit = True\n")
    return repo


@pytest.mark.parametrize("shape", ["old", "current", "stacked", "dirty"])
def test_current_policy_excludes_old_stress_without_rewriting_checkout(tmp_path, shape):
    repo = _checkout(tmp_path, shape)
    head_before = _git(repo, "rev-parse", "HEAD")
    diff_before = _git(repo, "diff", "--", ".")
    requested = [sys.executable, "-m", "pytest", "-q"]

    effective, policy = resolve_validation(requested, repo)
    result = subprocess.run(effective, cwd=repo, capture_output=True, text=True, timeout=15)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "3 deselected" in result.stdout
    assert policy["source_sha"] == POLICY_SOURCE_SHA
    assert policy["excluded_nodes"] == list(STRESS_NODES)
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "diff", "--", ".") == diff_before
    if shape == "dirty":
        assert (repo / "implementation.py").read_text() == "valuable_uncommitted_edit = True\n"


@pytest.mark.parametrize("inherited_policy", [False, True], ids=["clean", "worker-policy"])
def test_old_branch_can_explicitly_opt_in_to_known_stress(tmp_path, inherited_policy):
    repo = _checkout(tmp_path, "old")
    requested = [sys.executable, "-m", "pytest", "--run-stress", "-q"]

    effective, policy = resolve_validation(requested, repo)
    env = os.environ.copy()
    pytest_addopts = shlex.split(env.get("PYTEST_ADDOPTS", ""))
    if inherited_policy:
        pytest_addopts.extend(option for option in POLICY_ADDOPTS if option not in pytest_addopts)
    inherited_options = tuple(pytest_addopts)
    pytest_addopts = [option for option in pytest_addopts if option not in POLICY_ADDOPTS]
    env["PYTEST_ADDOPTS"] = shlex.join(pytest_addopts)
    if inherited_policy:
        assert all(option in inherited_options for option in POLICY_ADDOPTS)
    result = subprocess.run(
        effective, cwd=repo, env=env, capture_output=True, text=True, timeout=15,
    )

    assert "--run-stress" not in effective  # the old pytest config does not define the option
    assert result.returncode == 1
    assert "3 failed, 1 passed" in result.stdout
    assert policy["stress_opt_in"] is True
    assert policy["excluded_nodes"] == []


@pytest.mark.parametrize("command", [
    ["sh", "-c", "pytest -q"],
    ["env", "-u", "PYTEST_ADDOPTS", "pytest", "-q"],
    ["uv", "run", "pytest"],
    ["tox"],
    ["make", "test"],
    ["./scripts/test"],
    [sys.executable, "project_test.py"],
])
def test_unproven_validation_launcher_is_blocked_before_execution(tmp_path, command):
    repo = _checkout(tmp_path, "old")

    with pytest.raises(ValidationPolicyError, match="cannot be proven non-pytest"):
        resolve_validation(command, repo)


def test_policy_block_writes_auditable_receipt_without_execution(tmp_path, monkeypatch):
    repo = _checkout(tmp_path, "old")
    run_dir = tmp_path / "run"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GARDEN_EXECUTION_RUN_DIR", str(run_dir))
    monkeypatch.setenv("GARDEN_EXECUTION_OWNER", "test-owner")
    monkeypatch.setattr(sys, "argv", ["garden.validation", "--", "make", "test"])

    assert validation.main() == 2

    receipts = list((run_dir / "validations").glob("*/result.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["source_sha"] == _git(repo, "rev-parse", "HEAD")
    assert receipt["selection"] == []
    assert receipt["exit_code"] == 2
    assert receipt["policy"]["source_sha"] == POLICY_SOURCE_SHA
    assert receipt["policy"]["kind"] == "blocked"
    assert receipt["policy"]["requested_selection"] == ["make", "test"]


def test_worker_environment_enforces_policy_for_plain_pytest(tmp_path):
    repo = _checkout(tmp_path, "old")
    env = {"PYTEST_ADDOPTS": "-ra"}
    enforce_validation_policy_env(env)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=repo, env=env,
        capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout and "3 deselected" in result.stdout
    assert env["PYTEST_ADDOPTS"].startswith("-ra ")
