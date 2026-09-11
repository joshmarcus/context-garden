from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from garden.ci_status import (
    CIStatus,
    command_status,
    github_status,
    register_status_provider,
    resolve_status,
    status_diagnostic,
    status_disposition,
    tick_query_cache,
    worker_check_status,
)
from garden.github import PRInfo
from garden.validation import POLICY_ADDOPTS, POLICY_SOURCE_SHA, STRESS_NODES

FAKE_CI_COMMAND = Path(__file__).parent / "fake_ci_command.py"
HEAD = "b" * 40
OTHER_HEAD = "c" * 40
GREEN = {"answer": {"state": "success", "exists_for_sha": True, "stale": False}}


def fake_ci(tmp_path, plan, **policy):
    """A validation-command policy answered by the token-free fake CI."""
    plan_path = tmp_path / "ci-plan.json"
    plan_path.write_text(json.dumps(plan))
    return {"command": f"{sys.executable} {FAKE_CI_COMMAND} {plan_path}", **policy}


def completed_execution():
    return {
        "state": "finished", "slot": 0, "limit": 1, "requested_limit": 1,
        "pid": 123, "owner_scoped": True, "owner": "run:test",
        "execution_started_at": "2026-09-10T01:00:00+00:00",
        "timeout_seconds": 900,
        "deadline_at": "2026-09-10T01:15:00+00:00",
    }


def receipt(source_sha="new", command="pytest -q", exit_code=0):
    requested = ["pytest", "-q"]
    effective = [*requested, *POLICY_ADDOPTS]
    return {"version": 1, "source_sha": source_sha, "command": command,
            "selection": effective, "exit_code": exit_code,
            "log_location": "/logs/1", "policy": {
                "version": 1, "source_sha": POLICY_SOURCE_SHA, "kind": "pytest",
                "stress_opt_in": False, "excluded_nodes": list(STRESS_NODES),
                "requested_selection": requested, "effective_selection": effective,
            },
            "source_dirty": "", "source_changed": False}


def write_receipt(path, row, execution=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**row, "log_location": str(path.parent)}
    path.write_text(json.dumps(row))
    (path.parent / "execution.json").write_text(json.dumps(
        completed_execution() if execution is None else execution
    ))
    (path.parent / "exit_code").write_text(str(row["exit_code"]))
    (path.parent / "stderr.log").write_text("")


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
    write_receipt(result, receipt(source_sha="old"))
    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})
    assert status.state == "mismatched" and status.stale and not status.green

    write_receipt(result, receipt(exit_code=1))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "failure"
    write_receipt(result, receipt())
    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})
    assert status.green and status.exists_for_sha and status.evidence_url == "/runs/CG-1/run"


def test_worker_check_uses_newest_completion_not_receipt_name(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "remote-9" / "result.json"
    newer = validations / "remote-10" / "result.json"
    write_receipt(older, receipt())
    write_receipt(newer, receipt(exit_code=1))
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.state == "failure"
    assert status.failures == ["validation exited 1"]


def test_worker_check_newest_matching_malformed_receipt_supersedes_older_success(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "remote-20" / "result.json"
    newer = validations / "remote-3" / "result.json"
    write_receipt(older, receipt())
    write_receipt(newer, receipt())
    newer.unlink()
    newer.write_text(json.dumps({**receipt(), "selection": []}))
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.state == "malformed" and status.exists_for_sha and not status.green


def test_worker_check_newest_matching_receipt_missing_command_supersedes_older_success(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "remote-20" / "result.json"
    newer = validations / "remote-3" / "result.json"
    write_receipt(older, receipt())
    malformed = receipt()
    malformed.pop("command")
    write_receipt(newer, malformed)
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.state == "malformed" and status.exists_for_sha and not status.green


def test_worker_check_newest_non_object_receipt_supersedes_older_success(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "remote-20" / "result.json"
    newer = validations / "remote-3" / "result.json"
    write_receipt(older, receipt())
    newer.parent.mkdir(parents=True)
    newer.write_text("[]")
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.state == "malformed" and not status.exists_for_sha and not status.green


def test_worker_check_newest_matching_truncated_receipt_supersedes_older_success(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "remote-20" / "result.json"
    newer = validations / "remote-3" / "result.json"
    write_receipt(older, receipt())
    newer.parent.mkdir(parents=True)
    newer.write_text(
        '{"version": 1, "source_sha": "new", "command": "pytest -q", '
        '"policy": {"source_sha": "policy-source"',
    )
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.state == "malformed" and status.exists_for_sha and not status.green


def test_worker_check_newest_pre_identity_truncation_supersedes_older_success(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "remote-20" / "result.json"
    newer = validations / "remote-3" / "result.json"
    write_receipt(older, receipt())
    newer.parent.mkdir(parents=True)
    newer.write_text('{"version": 1, "source_')
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.state == "malformed" and not status.exists_for_sha and not status.green


def test_worker_check_newer_truncated_other_source_does_not_block_valid_receipt(tmp_path):
    validations = tmp_path / "runs" / "CG-1" / "run" / "validations"
    older = validations / "remote-20" / "result.json"
    newer = validations / "remote-3" / "result.json"
    write_receipt(older, receipt())
    newer.parent.mkdir(parents=True)
    newer.write_text('{"version": 1, "source_sha": "other", "command": "pytest -q"')
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    assert worker_check_status(
        tmp_path, "CG-1", "new", {"command": "pytest -q"},
    ).green


def test_worker_check_rejects_malformed_or_wrong_command(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "1" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text("not json")
    assert worker_check_status(tmp_path, "CG-1", "new", {}).state == "malformed"
    result.write_text(json.dumps(receipt(command="focused")))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "ordinary"}).state == "missing"


def test_worker_check_rejects_incomplete_or_inconsistent_remote_receipt(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "remote-0" / "result.json"
    result.parent.mkdir(parents=True)
    incomplete = {"source_sha": "new", "command": "pytest -q", "exit_code": 0,
                  "log_location": "/remote/path", "source_dirty": "", "source_changed": False,
                  "policy": {"source_sha": POLICY_SOURCE_SHA}}
    result.write_text(json.dumps(incomplete))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"

    inconsistent = receipt()
    inconsistent["selection"] = ["pytest", "-q"]
    write_receipt(result, inconsistent)
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"

    missing_clean_source_proof = receipt()
    missing_clean_source_proof.pop("source_changed")
    write_receipt(result, missing_clean_source_proof)
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"

    write_receipt(result, receipt())
    (result.parent / "exit_code").write_text("1")
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"

    write_receipt(result, receipt())
    stored = json.loads(result.read_text())
    stored["log_location"] = str(result.parent.parent)
    result.write_text(json.dumps(stored))
    assert worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"}).state == "malformed"


def test_worker_check_requires_consistent_finished_supervisor_execution(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "remote-0" / "result.json"
    invalid_executions = [
        {},
        {"state": "running"},
        {"state": "timeout", "exit_code": 124},
        {"state": "done"},
        {"state": "finished"},
        {"state": "finished", "exit_code": 1},
        {**completed_execution(), "owner": ""},
        {**completed_execution(), "deadline_at": "2026-09-10T01:14:59+00:00"},
        {**completed_execution(), "owner_scoped": False},
    ]

    for execution in invalid_executions:
        write_receipt(result, receipt(), execution)

        status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

        assert status.state == "malformed" and status.exists_for_sha and not status.green


def test_worker_check_accepts_complete_inherited_supervisor_execution(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "remote-0" / "result.json"
    execution = completed_execution()
    for key in ("slot", "limit", "requested_limit", "owner_scoped"):
        execution.pop(key)
    execution["inherited_lease"] = True
    write_receipt(result, receipt(), execution)

    status = worker_check_status(tmp_path, "CG-1", "new", {"command": "pytest -q"})

    assert status.green


def test_worker_check_accepts_old_branch_authorized_stress_opt_in_receipt(tmp_path):
    result = tmp_path / "runs" / "CG-1" / "run" / "validations" / "remote-0" / "result.json"
    requested = ["pytest", "--run-stress", "-q"]
    opted_in = receipt(command="pytest --run-stress -q", exit_code=1)
    opted_in["selection"] = ["pytest", "-q"]
    opted_in["policy"].update({
        "stress_opt_in": True,
        "excluded_nodes": [],
        "requested_selection": requested,
        "effective_selection": ["pytest", "-q"],
    })
    write_receipt(result, opted_in)

    status = worker_check_status(
        tmp_path, "CG-1", "new", {"command": "pytest --run-stress -q"},
    )

    assert status.state == "failure" and status.exists_for_sha


def test_command_passes_only_a_current_result_for_the_exact_commit(tmp_path):
    policy = fake_ci(tmp_path, {"by_sha": {HEAD: GREEN}, "default": {"answer": {"state": "missing"}}})

    status = command_status(HEAD, policy)

    assert status.green and status.provider == "command"
    assert status_disposition(status) == "green"
    # The same command asked about any other commit answers about that commit only.
    assert command_status(OTHER_HEAD, policy).state == "missing"
    assert (tmp_path / "queries.log").read_text().split() == [HEAD, OTHER_HEAD]


@pytest.mark.parametrize(("plan_row", "state", "disposition"), [
    ({"answer": {"state": "failure", "exists_for_sha": True}}, "failure", "failure"),
    ({"answer": {"state": "pending", "exists_for_sha": True}}, "pending", "waiting"),
    ({"answer": {"state": "missing"}}, "missing", "absent"),
    # A pass that is superseded, or that belongs to no build for this commit, is not a pass.
    ({"answer": {"state": "success", "exists_for_sha": True, "stale": True}}, "success", "absent"),
    ({"answer": {"state": "success", "exists_for_sha": False, "stale": False}}, "success", "absent"),
    # A failure nobody can attribute to this commit holds the gate instead of blaming it.
    ({"answer": {"state": "failure", "exists_for_sha": True, "stale": True}}, "failure", "absent"),
    ({"answer": {"state": "unavailable"}}, "unavailable", "unavailable"),
    # Contract violations: an unclaimed pass, an unknown state, another commit's answer,
    # output that is not one JSON object, no output at all, and unbounded output.
    ({"answer": {"state": "success", "exists_for_sha": True}}, "malformed", "unavailable"),
    ({"answer": {"state": "green", "exists_for_sha": True, "stale": False}}, "malformed", "unavailable"),
    ({"answer": {**GREEN["answer"], "sha": OTHER_HEAD}}, "malformed", "unavailable"),
    ({"answer": {"state": "success", "exists_for_sha": True, "stale": "no"}}, "malformed", "unavailable"),
    ({"raw_stdout": "build 12 is fine"}, "malformed", "unavailable"),
    ({}, "malformed", "unavailable"),
    ({"raw_stdout": "x" * 70_000}, "malformed", "unavailable"),
    ({"exit_code": 3}, "unavailable", "unavailable"),
])
def test_command_fails_closed_for_every_outcome_that_is_not_a_current_pass(
    tmp_path, plan_row, state, disposition,
):
    status = command_status(HEAD, fake_ci(tmp_path, {"default": plan_row}))

    assert (status.state, status_disposition(status)) == (state, disposition)
    assert not status.green


def test_command_timeout_is_not_a_pass(tmp_path):
    policy = fake_ci(tmp_path, {"default": {"sleep_seconds": 30, **GREEN}}, timeout_seconds=0.5)

    status = command_status(HEAD, policy)

    assert status.state == "timeout" and not status.green
    assert status_disposition(status) == "unavailable"


def test_command_reports_its_own_shape_not_the_command_output(tmp_path):
    policy = fake_ci(tmp_path, {"default": {
        "exit_code": 3, "stderr": "queried-system-name: access denied for build-service",
    }})

    status = command_status(HEAD, policy)

    # An operator reads the local diagnostic; the command's own text may name internal
    # systems, so only the shape of the failure is carried into garden state.
    assert status.failures == ["validation command exited 3"]
    assert "build-service" not in json.dumps(status.to_dict())


def test_command_detail_is_bounded_and_redacted(tmp_path):
    policy = fake_ci(tmp_path, {"default": {"answer": {
        "state": "failure", "exists_for_sha": True,
        "evidence_url": "https://ci.example.test/b/1?access_token=SECRET",
        "failures": ["unit suite failed", "y" * 400, *["also"] * 8],
    }}})

    status = command_status(HEAD, policy)

    assert status.state == "failure"
    assert len(status.failures) == 5 and max(len(item) for item in status.failures) == 200
    assert "SECRET" not in status.evidence_url and "[REDACTED]" in status.evidence_url


def test_command_refuses_unusable_policies_and_heads_that_are_not_commits(tmp_path):
    policy = fake_ci(tmp_path, {"default": GREEN})

    assert command_status(HEAD, {"command": ""}).state == "malformed"
    assert command_status(HEAD, {**policy, "timeout_seconds": 0}).state == "malformed"
    assert command_status(HEAD, {**policy, "timeout_seconds": 4000}).state == "malformed"
    assert command_status(HEAD, {**policy, "timeout_seconds": "soon"}).state == "malformed"
    # A branch name moves; only a commit id may be handed to the command.
    assert command_status("garden/dm-001-first-task", policy).state == "malformed"
    assert command_status("", policy).state == "malformed"
    assert not (tmp_path / "queries.log").exists()
    assert command_status(HEAD, {"command": str(tmp_path / "absent-adapter")}).state == "unavailable"


def test_one_tick_asks_each_command_once_per_commit(tmp_path):
    policy = fake_ci(tmp_path, {"default": GREEN})
    log = tmp_path / "queries.log"

    with tick_query_cache():
        assert command_status(HEAD, policy).green
        assert command_status(HEAD, policy).green
        command_status(OTHER_HEAD, policy)
    assert log.read_text().split() == [HEAD, OTHER_HEAD]

    # The next tick observes again, so a build that finished meanwhile is seen.
    with tick_query_cache():
        command_status(HEAD, policy)
    assert log.read_text().split() == [HEAD, OTHER_HEAD, HEAD]


def test_command_diagnostics_separate_an_absent_result_from_an_unanswerable_one():
    absent = CIStatus("missing", HEAD, provider="command")
    unanswerable = CIStatus("timeout", HEAD, provider="command")

    assert "no current result" in status_diagnostic(absent)
    assert "exact commit" in status_diagnostic(absent)
    assert "could not report a status" in status_diagnostic(unanswerable)
    assert HEAD[:12] in status_diagnostic(absent)


def test_command_provider_is_reachable_through_the_configured_policy(tmp_path):
    pr = PRInfo(1, "", "OPEN", head_sha=HEAD)
    policy = {"status_provider": "command", "required": True,
              "command": fake_ci(tmp_path, {"default": GREEN})}

    assert resolve_status("command", tmp_path, "CG-1", pr, policy).green
    assert resolve_status("command", tmp_path, "CG-1", PRInfo(1, "", "OPEN"), policy).state == "malformed"


def test_pluggable_provider_timeout_and_mismatched_response_fail_closed(tmp_path):
    pr = PRInfo(1, "", "OPEN", head_sha="head")
    register_status_provider("test-timeout", lambda *_: (_ for _ in ()).throw(TimeoutError()))
    register_status_provider("test-wrong-head", lambda *_: CIStatus("success", "old", exists_for_sha=True))
    assert resolve_status("test-timeout", tmp_path, "CG-1", pr, {}).state == "timeout"
    assert resolve_status("test-wrong-head", tmp_path, "CG-1", pr, {}).state == "malformed"
    assert resolve_status("not-installed", tmp_path, "CG-1", pr, {}).state == "unknown"
