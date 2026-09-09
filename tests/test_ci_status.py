from __future__ import annotations

import json

from garden.ci_status import (
    CIStatus,
    github_status,
    register_status_provider,
    resolve_status,
    worker_check_status,
)
from garden.github import PRInfo


def test_github_status_fails_closed_for_missing_unknown_and_failure():
    pr = PRInfo(1, "https://example.test/pr/1", "OPEN", head_sha="abc")
    assert github_status(pr, required=True).state == "missing"
    pr.checks = "MYSTERY"
    assert not github_status(pr, required=True).green
    pr.checks, pr.failed_checks = "FAILURE", ["tests"]
    status = github_status(pr, required=True)
    assert status.failures == ["tests"] and not status.green


def test_worker_check_is_exact_head_and_authoritative(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "1" / "result.json"
    result.parent.mkdir(parents=True)
    (result.parent / "execution.json").write_text('{"state":"finished"}')
    result.write_text(json.dumps({"source_sha": "old", "command": "pytest -q",
                                  "selection": ["pytest", "-q"], "exit_code": 0,
                                  "log_location": str(result.parent)}))
    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})
    assert status.state == "mismatched" and status.stale and not status.green

    result.write_text(json.dumps({"source_sha": "new", "command": "pytest -q",
                                  "selection": ["pytest", "-q"], "exit_code": 1,
                                  "log_location": str(result.parent)}))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "failure"
    result.write_text(json.dumps({"source_sha": "new", "command": "pytest -q",
                                  "selection": ["pytest", "-q"], "exit_code": 0,
                                  "log_location": str(result.parent)}))
    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})
    assert status.green and status.exists_for_sha and status.evidence_url == "/runs/CG-1/run"


def test_worker_check_rejects_malformed_or_wrong_command(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "1" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text("not json")
    assert worker_check_status(tmp_path, "CG-1", "new", {}).state == "malformed"
    result.write_text(json.dumps({"source_sha": "new", "command": "focused",
                                  "selection": ["focused"], "exit_code": 0,
                                  "log_location": str(result.parent)}))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "ordinary"}).state == "missing"


def test_worker_check_rejects_missing_selection_and_supervisor_evidence(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "1" / "result.json"
    result.parent.mkdir(parents=True)
    receipt = {"source_sha": "new", "command": "pytest -q", "exit_code": 0,
               "log_location": str(result.parent)}
    result.write_text(json.dumps(receipt))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"

    receipt["selection"] = ["pytest", "-q"]
    result.write_text(json.dumps(receipt))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"


def test_pluggable_provider_timeout_and_mismatched_response_fail_closed(tmp_path):
    pr = PRInfo(1, "", "OPEN", head_sha="head")
    register_status_provider("test-timeout", lambda *_: (_ for _ in ()).throw(TimeoutError()))
    register_status_provider("test-wrong-head", lambda *_: CIStatus("success", "old", exists_for_sha=True))
    assert resolve_status("test-timeout", tmp_path, "CG-1", pr, {}).state == "timeout"
    assert resolve_status("test-wrong-head", tmp_path, "CG-1", pr, {}).state == "malformed"
    assert resolve_status("not-installed", tmp_path, "CG-1", pr, {}).state == "unknown"
