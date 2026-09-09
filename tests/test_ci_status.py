from __future__ import annotations

import json
import shlex

from garden.ci_status import (
    CIStatus,
    github_status,
    register_status_provider,
    resolve_status,
    worker_check_status,
)
from garden.github import PRInfo


def write_receipt(path, *, source_sha="new", selection=None, exit_code=0):
    selection = selection or ["pytest", "-q"]
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "execution.json").write_text("{}")
    (path.parent / "exit_code").write_text(str(exit_code))
    (path.parent / "stderr.log").write_text("")
    path.write_text(json.dumps({"source_sha": source_sha, "command": shlex.join(selection),
                                "selection": selection, "exit_code": exit_code,
                                "log_location": str(path.parent)}))


def set_started_at(path, value):
    (path.parent / "execution.json").write_text(json.dumps({"execution_started_at": value}))


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
    write_receipt(result, source_sha="old")
    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})
    assert status.state == "mismatched" and status.stale and not status.green

    write_receipt(result, exit_code=1)
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "failure"
    write_receipt(result)
    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})
    assert status.green and status.exists_for_sha and status.evidence_url == "/runs/CG-1/run"


def test_worker_check_rejects_malformed_or_wrong_command(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "1" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text("not json")
    assert worker_check_status(tmp_path, "CG-1", "new", {}).state == "malformed"
    write_receipt(result, selection=["focused"])
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "ordinary"}).state == "missing"


def test_worker_check_rejects_missing_selection_or_durable_log(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "1" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps({"source_sha": "new", "command": "pytest -q",
                                  "exit_code": 0, "log_location": str(result.parent)}))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"
    write_receipt(result)
    (result.parent / "stderr.log").unlink()
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"


def test_worker_check_uses_execution_time_not_attempt_directory_name(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "999" / "result.json"
    newer = validations / "1000" / "result.json"
    write_receipt(older)
    set_started_at(older, "2026-09-09T10:00:00+00:00")
    write_receipt(newer, exit_code=1)
    set_started_at(newer, "2026-09-09T11:00:00+00:00")

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.state == "failure"
    assert status.evidence_url == "/runs/CG-1/run"


def test_worker_check_does_not_fall_back_after_malformed_latest_attempt(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "1" / "result.json"
    newer = validations / "2" / "result.json"
    write_receipt(older)
    set_started_at(older, "2026-09-09T10:00:00+00:00")
    write_receipt(newer)
    set_started_at(newer, "2026-09-09T11:00:00+00:00")
    (newer.parent / "stderr.log").unlink()

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.state == "malformed"
    assert status.exists_for_sha and not status.green


def test_pluggable_provider_timeout_and_mismatched_response_fail_closed(tmp_path):
    pr = PRInfo(1, "", "OPEN", head_sha="head")
    register_status_provider("test-timeout", lambda *_: (_ for _ in ()).throw(TimeoutError()))
    register_status_provider("test-wrong-head", lambda *_: CIStatus("success", "old", exists_for_sha=True))
    assert resolve_status("test-timeout", tmp_path, "CG-1", pr, {}).state == "timeout"
    assert resolve_status("test-wrong-head", tmp_path, "CG-1", pr, {}).state == "malformed"
    assert resolve_status("not-installed", tmp_path, "CG-1", pr, {}).state == "unknown"
