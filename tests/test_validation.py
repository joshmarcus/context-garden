from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from garden.validation import POLICY_SOURCE_SHA, STRESS_NODES, ValidationPolicyError, resolve_validation


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


def test_old_branch_can_explicitly_opt_in_to_known_stress(tmp_path):
    repo = _checkout(tmp_path, "old")
    requested = [sys.executable, "-m", "pytest", "--run-stress", "-q"]

    effective, policy = resolve_validation(requested, repo)
    result = subprocess.run(effective, cwd=repo, capture_output=True, text=True, timeout=15)

    assert "--run-stress" not in effective  # the old pytest config does not define the option
    assert result.returncode == 1
    assert "3 failed, 1 passed" in result.stdout
    assert policy["stress_opt_in"] is True
    assert policy["excluded_nodes"] == []


def test_shell_hidden_pytest_is_blocked_before_execution(tmp_path):
    repo = _checkout(tmp_path, "old")

    with pytest.raises(ValidationPolicyError, match="invoke pytest directly"):
        resolve_validation(["sh", "-c", "pytest -q"], repo)
