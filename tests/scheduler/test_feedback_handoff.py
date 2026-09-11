"""Review and asynchronous CI feedback reach one complete, head-bound revise brief."""

import hashlib
import json

import pytest

from garden import gitops
from garden.github import Feedback
from garden.model import Status
from garden.scheduler import Scheduler
from garden.scheduler.report import TickReport
from garden.store import Store
from tests.reference_context import agent_context


def _open_task(sched, fake_github):
    # These handoff tests use synthetic immutable SHA values to exercise ordering and
    # replay.  Keep FakeGitHub from replacing them with the fixture remote's branch tip.
    fake_github.remote = None
    sched.cfg.data["review"] = {**sched.cfg.data["review"], "enabled": False}
    sched.tick()
    sched.tick()
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    pr = fake_github.prs[task.branch]
    pr.head_sha = "a" * 40
    pr.updated_at = "review-head"
    st = sched.state.get(task.id)
    st["head_sha"] = pr.head_sha
    st["pr_state"] = "OPEN"
    sched.state.save()
    return task, pr


def _review(summary="Bounded environment recovery is incomplete", finding="Environment errors retry forever"):
    return {
        "verdict": "request_changes",
        "summary": summary,
        "description_ok": True,
        "criteria": [{
            "criterion": "Bound every started-review recovery",
            "met": False,
            "failure_category": "implementation",
            "reason": "Started environment errors bypass the recovery limit.",
            "evidence": "six focused recovery tests fail",
        }],
        "findings": [{
            "severity": "blocking",
            "failure_category": "implementation",
            "file": "src/garden/scheduler/review.py",
            "line": 950,
            "summary": finding,
            "fix": "Route started environment errors through the bounded recovery counter.",
        }],
    }


def _approval():
    return {
        "verdict": "approve",
        "summary": "The current head addresses every review finding.",
        "description_ok": True,
        "criteria": [{
            "criterion": "Bound every started-review recovery",
            "met": True,
            "reason": "The bounded path is covered.",
            "evidence": "focused recovery tests pass",
        }],
        "findings": [],
    }


def _review_run(sched, task, head, review):
    run = sched.runs.new_run(task.id, "local", mode="review")
    run.status = "done"
    run.result = review
    run.env_snapshot = {"review_head": head, "criteria": ["Bound every started-review recovery"]}
    run.save()
    return run


def _ci_run(sched, task, head):
    run = sched.runs.new_run(task.id, "local", mode="check")
    run.status = "done"
    run.env_snapshot = {"ci_head": head}
    run.save()
    return run


def _failed_ci():
    return [{"name": "actions", "status": "fail", "failure_category": "implementation",
             "summary": "six focused tests failed", "details": "test recovery[0-5]"}]


def _finish_ci(sched, task, run, head, rep=None, failure_identity=""):
    rep = rep or TickReport()
    if not failure_identity:
        failure_identity = sched._ci_failure_identity(sched.github.prs[task.branch])
    sched._after_ci_check(
        task,
        run,
        _failed_ci(),
        {"head": head, "ci_note": "- **CI** is failing on this branch (failed checks: test).",
         "ci_failure_identity": failure_identity},
        rep,
    )
    return rep


def test_review_then_ci_delivers_the_complete_feedback_in_the_dispatched_brief(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    review = _review()
    reviewed = _review_run(sched, task, pr.head_sha, review)

    sched._apply_review(task, reviewed, review, TickReport(), emitted=False)
    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)
    sched.dispatch(task, mode="revise", runner=sched.runner_for(task))

    brief = agent_context(sched.runs.latest(task.id))
    assert "Environment errors retry forever" in brief
    assert "Route started environment errors through the bounded recovery counter" in brief
    assert "Bound every started-review recovery" in brief
    assert "six focused recovery tests fail" in brief
    assert "CI" in brief and "six focused tests failed" in brief


def test_ci_then_review_waits_for_the_review_run_and_dispatches_one_combined_revision(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    review = _review()
    reviewed = _review_run(sched, task, pr.head_sha, review)
    st = sched.state.get(task.id)
    st["review_run"] = reviewed.run_id

    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)

    assert task.status == Status.CHANGES_REQUESTED
    assert not any(candidate.id == task.id and mode == "revise"
                   for candidate, mode, _ in sched.dispatch_queue())

    sched._apply_review(task, reviewed, review, TickReport(), emitted=False)
    queued = [(candidate.id, mode) for candidate, mode, _ in sched.dispatch_queue()]
    assert (task.id, "revise") in queued
    sched.dispatch(task, mode="revise", runner=sched.runner_for(task))
    brief = agent_context(sched.runs.latest(task.id))
    assert "Environment errors retry forever" in brief
    assert "six focused tests failed" in brief
    assert sched.state.get(task.id)["revisions"] == 1


def test_stale_ci_completion_after_a_head_move_cannot_change_or_revive_feedback(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    old_head = pr.head_sha
    st = sched.state.get(task.id)
    st["pending_feedback"] = "Operator resolved the old review; keep this current note."
    st["last_review"] = _review(finding="obsolete finding")
    pr.head_sha = "b" * 40
    st["head_sha"] = pr.head_sha
    sched.state.save()
    stale = _ci_run(sched, task, old_head)

    rep = _finish_ci(sched, task, stale, old_head)

    assert st["pending_feedback"] == "Operator resolved the old review; keep this current note."
    assert not st.get("pending_feedback_sources")
    assert not st.get("applied_ci_feedback")
    assert rep.transitions == []


def test_ci_reap_is_restart_safe_deduplicated_and_charges_one_revision(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    check = _ci_run(sched, task, pr.head_sha)
    first = _finish_ci(sched, task, check, pr.head_sha)
    rendered = sched.state.get(task.id)["pending_feedback"]
    assert first.transitions == ["DM-001 -> changes_requested"]

    restarted = Scheduler(Store(sched.store.root), github=fake_github)
    restarted_task = restarted.store.task(task.id)
    second = _finish_ci(restarted, restarted_task, check, pr.head_sha)

    assert restarted.state.get(task.id)["pending_feedback"] == rendered
    assert rendered.count("six focused tests failed") == 1
    routes = restarted.state.get(task.id)["implementation_failure_escalations"]
    assert len(routes) == 1
    assert routes[0]["signal"] == "failed_final_verification"
    assert json.loads(routes[0]["identity"])["head"] == pr.head_sha
    assert second.transitions == []
    restarted.dispatch(restarted_task, mode="revise", runner=restarted.runner_for(restarted_task))
    assert restarted.state.get(task.id)["revisions"] == 1


def test_ci_infrastructure_failure_does_not_escalate_implementation(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    check = _ci_run(sched, task, pr.head_sha)
    results = [{
        "name": "actions", "status": "fail", "summary": "exit 127", "details": "",
        "exit_code": 127, "unavailable": True,
    }]

    sched._after_ci_check(
        task, check, results,
        {"head": pr.head_sha, "ci_note": "- **CI** failed, but its analyser is unavailable."},
        TickReport(),
    )

    assert not sched.state.get(task.id).get("implementation_failure_escalations")


def test_ci_authentication_failure_does_not_escalate_without_implementation_verdict(
    sched, fake_github,
):
    task, pr = _open_task(sched, fake_github)
    check = _ci_run(sched, task, pr.head_sha)
    results = [{
        "name": "actions", "status": "error", "failure_category": "unavailable_evidence",
        "summary": "GitHub Actions diagnostics unavailable: authentication failed", "details": "",
    }]

    sched._after_ci_check(
        task, check, results,
        {"head": pr.head_sha, "ci_note": "- **CI** failed, but diagnostics are unavailable.",
         "ci_failure_identity": f"actions:{pr.head_sha}:failure"},
        TickReport(),
    )

    assert not sched.state.get(task.id).get("implementation_failure_escalations")


def test_persisted_nonimplementation_review_blockers_do_not_escalate_or_consume_attempts(
    sched, fake_github,
):
    task, pr = _open_task(sched, fake_github)
    attempts = task.attempts
    review = _review()
    review["criteria"] = [{
        "criterion": "Verify behavior in the external environment",
        "met": False,
        "failure_category": "unavailable_evidence",
        "reason": "The evidence service cannot be reached.",
    }]
    review["findings"] = [
        {
            "severity": "blocking",
            "failure_category": "infrastructure",
            "file": "",
            "line": None,
            "summary": "The required test runner is unavailable.",
            "fix": "Restore the runner before evaluating the source.",
        },
    ]
    reviewed = _review_run(sched, task, pr.head_sha, review)

    sched._apply_review(task, reviewed, review, TickReport(), emitted=False)

    state = sched.state.get(task.id)
    assert not state.get("implementation_failure_escalations")
    assert sched.store.task(task.id).attempts == attempts
    assert reviewed.result["findings"] == review["findings"]

    restarted = Scheduler(Store(sched.store.root), github=fake_github)
    assert restarted.state.get(task.id)["last_review"]["findings"] == review["findings"]
    assert not restarted.state.get(task.id).get("implementation_failure_escalations")
    assert restarted.store.task(task.id).attempts == attempts


@pytest.mark.parametrize("category", ["infrastructure", "admission", "unavailable_evidence"])
def test_repeated_nonimplementation_review_blocker_stalls_without_escalating(
    sched, fake_github, category,
):
    task, pr = _open_task(sched, fake_github)
    review = _review()
    review["criteria"] = []
    review["findings"] = [{
        "severity": "blocking",
        "failure_category": category,
        "file": "",
        "line": None,
        "summary": "The required test runner is unavailable.",
        "fix": "Restore the runner before evaluating the source.",
    }]

    for _ in range(2):
        reviewed = _review_run(sched, task, pr.head_sha, review)
        sched._apply_review(task, reviewed, review, TickReport(), emitted=False)

    state = sched.state.get(task.id)
    assert state["needs_human"]["kind"] == "stall"
    assert not state.get("implementation_failure_escalations")


def test_repeated_implementation_review_blocker_records_unchanged_attempt(
    sched, fake_github,
):
    task, pr = _open_task(sched, fake_github)
    review = _review()
    review["criteria"] = []

    for _ in range(2):
        reviewed = _review_run(sched, task, pr.head_sha, review)
        sched._apply_review(task, reviewed, review, TickReport(), emitted=False)

    signals = [
        event["signal"]
        for event in sched.state.get(task.id)["implementation_failure_escalations"]
    ]
    assert signals.count("verification_rejected") == 2
    assert signals.count("repeated_unchanged_attempt") == 1


@pytest.mark.parametrize(
    ("category", "expects_unchanged_escalation"),
    [("infrastructure", False), ("implementation", True)],
)
def test_review_classification_survives_dispatch_and_unchanged_revision(
    sched, fake_github, monkeypatch, category, expects_unchanged_escalation,
):
    task, pr = _open_task(sched, fake_github)
    review = _review()
    review["criteria"] = []
    review["findings"][0]["failure_category"] = category
    reviewed = _review_run(sched, task, pr.head_sha, review)

    sched._apply_review(task, reviewed, review, TickReport(), emitted=False)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nochange")
    revise = sched.dispatch(task, mode="revise", runner=sched.runner_for(task))

    # The fake reports success without changing the existing branch or description. Seed
    # the prior accepted identities so reap exercises the real no-change stall path.
    worktree = sched.worktree_for(task)
    state = sched.state.get(task.id)
    state["last_diff_hash"] = gitops.diff_hash(worktree, revise.base)
    state["last_pr_body_hash"] = hashlib.sha1(b"b").hexdigest()[:16]
    sched.state.save()

    sched.tick()

    state = sched.state.get(task.id)
    assert state["needs_human"]["kind"] == "stall"
    unchanged_routes = [
        event for event in state.get("implementation_failure_escalations") or []
        if event["signal"] == "repeated_unchanged_attempt"
    ]
    assert bool(unchanged_routes) is expects_unchanged_escalation
    assert revise.env_snapshot["implementation_failure_eligible"] is expects_unchanged_escalation


def test_exact_provider_ci_failure_escalates_after_usable_analysis(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    pr.checks = "SUCCESS"
    identity = f"worker_check:{pr.head_sha}:failure"

    _finish_ci(
        sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha,
        failure_identity=identity,
    )

    route = sched.state.get(task.id)["implementation_failure_escalations"][-1]
    assert route["signal"] == "failed_final_verification"
    assert route["identity"] == identity
    assert task.difficulty == "hard"


def test_ci_preserves_an_operator_resolved_or_superseded_feedback_record(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    st = sched.state.get(task.id)
    st["last_review"] = _review(finding="old finding must stay resolved")
    st["pending_feedback"] = (
        "## Operator recovery note\n\nThe old finding was resolved and superseded. "
        "Preserve this manual instruction only."
    )
    sched.state.save()

    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)

    pending = st["pending_feedback"]
    assert "Preserve this manual instruction only" in pending
    assert "six focused tests failed" in pending
    assert "old finding must stay resolved" not in pending


def test_fresh_review_replaces_prior_review_finding_but_keeps_current_ci(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    old = _review(summary="old review", finding="old review finding")
    old_run = _review_run(sched, task, pr.head_sha, old)
    sched._apply_review(task, old_run, old, TickReport(), emitted=False)
    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)

    fresh = _review(summary="fresh review", finding="new current finding")
    fresh_run = _review_run(sched, task, pr.head_sha, fresh)
    sched._apply_review(task, fresh_run, fresh, TickReport(), emitted=False)

    pending = sched.state.get(task.id)["pending_feedback"]
    assert "new current finding" in pending
    assert "old review finding" not in pending
    assert "six focused tests failed" in pending
    sources = sched.state.get(task.id)["pending_feedback_sources"]
    assert sources["head"] == pr.head_sha
    assert "review" in sources["parts"] and "ci" in sources["parts"]


def test_ci_feedback_keeps_stable_github_item_identity_on_replay(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    fake_github.feedback[pr.number] = Feedback(items=[{
        "kind": "line comment",
        "author": "josh",
        "body": "Keep the current-head guard",
        "path": "src/garden/scheduler/poll.py",
        "line": 190,
        "created": "2026-09-09T11:15:00Z",
    }])
    check = _ci_run(sched, task, pr.head_sha)

    _finish_ci(sched, task, check, pr.head_sha)
    first = sched.state.get(task.id)["pending_feedback"]
    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)

    second = sched.state.get(task.id)["pending_feedback"]
    assert second == first
    assert second.count("Keep the current-head guard") == 1


def test_fresh_approval_clears_the_known_review_part_but_keeps_current_ci(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    rejected = _review(finding="finding fixed by the current head")
    rejected_run = _review_run(sched, task, pr.head_sha, rejected)
    sched._apply_review(task, rejected_run, rejected, TickReport(), emitted=False)
    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)
    assert "finding fixed by the current head" in sched.state.get(task.id)["pending_feedback"]

    approved = _approval()
    approved_run = _review_run(sched, task, pr.head_sha, approved)
    sched._apply_review(task, approved_run, approved, TickReport(), emitted=False)

    pending = sched.state.get(task.id)["pending_feedback"]
    assert "finding fixed by the current head" not in pending
    assert "six focused tests failed" in pending
    sources = sched.state.get(task.id)["pending_feedback_sources"]
    assert "review" not in sources["parts"] and "ci" in sources["parts"]


def test_operator_edited_rendered_feedback_survives_a_fresh_review(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    original = _review(finding="original review finding")
    original_run = _review_run(sched, task, pr.head_sha, original)
    sched._apply_review(task, original_run, original, TickReport(), emitted=False)
    st = sched.state.get(task.id)
    st["pending_feedback"] += (
        "\n\n## Operator recovery note\n\nKeep the manual scope decision and its evidence."
    )
    sched.state.save()

    fresh = _review(summary="fresh complete review", finding="fresh review finding")
    fresh_run = _review_run(sched, task, pr.head_sha, fresh)
    sched._apply_review(task, fresh_run, fresh, TickReport(), emitted=False)

    pending = st["pending_feedback"]
    assert "Keep the manual scope decision and its evidence" in pending
    assert "fresh review finding" in pending


def test_current_head_successful_ci_resolution_removes_only_the_ci_part(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    review = _review()
    reviewed = _review_run(sched, task, pr.head_sha, review)
    sched._apply_review(task, reviewed, review, TickReport(), emitted=False)
    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)
    assert "six focused tests failed" in sched.state.get(task.id)["pending_feedback"]

    pr.checks = "SUCCESS"
    resolved = _ci_run(sched, task, pr.head_sha)
    sched._after_ci_check(
        task,
        resolved,
        [{"name": "actions", "status": "pass", "summary": "reran successfully", "reran": True}],
        {"head": pr.head_sha, "ci_note": "- **CI** is failing on this branch (failed checks: test)."},
        TickReport(),
    )

    pending = sched.state.get(task.id)["pending_feedback"]
    assert "Environment errors retry forever" in pending
    assert "six focused tests failed" not in pending
    assert "CI** is failing" not in pending
    sources = sched.state.get(task.id)["pending_feedback_sources"]
    assert "review" in sources["parts"] and "ci" not in sources["parts"]


def test_ci_dispatch_freezes_the_pr_head_in_the_run_and_continuation(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    sched.cfg.data["checks"] = {**sched.cfg.data["checks"],
                                "ci": [{"name": "ci analysis", "command": "true"}]}
    pr.checks = "FAILURE"
    pr.failed_checks = ["unit"]
    pr.updated_at = "new-ci-failure"

    sched.poll(task, TickReport())

    check = sched.state.get(task.id)["check_run"]
    run = sched._run_by_id(task, check["run_id"])
    assert check["cont"]["head"] == pr.head_sha
    assert run.env_snapshot["ci_head"] == pr.head_sha


def test_controller_owned_ci_diagnostic_is_head_bound_and_redacted_for_revision(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    task.runner = "remote"
    sched.store.save(task)
    sched.cfg.data["checks"] = {**sched.cfg.data["checks"], "ci": [{
        "name": "controller actions", "python": "garden.checks:github_actions_failures",
        "execution_owner": "controller",
    }]}
    pr.checks = "FAILURE"
    pr.failed_checks = ["actions"]
    pr.updated_at = "controller-ci-failure"

    sched.poll(task, TickReport())

    check = sched.state.get(task.id)["check_run"]
    run = sched._run_by_id(task, check["run_id"])
    payload = json.loads((run.path / "checks_input.json").read_text())
    assert run.runner == "local"
    assert payload["execution_owner"] == "controller"

    run.status = "done"
    run.env_snapshot["ci_head"] = pr.head_sha
    run.save()
    sched._after_ci_check(task, run, [{
        "name": "actions", "status": "fail", "failure_category": "implementation",
        "summary": "token=controller-secret test failure",
        "details": "token=controller-secret\\nfailed test_example",
    }], check["cont"], TickReport())
    assert "test_example" in sched.state.get(task.id)["pending_feedback"]
    revise = sched.dispatch(task, mode="revise", runner=sched.runner_for(task))

    brief = agent_context(revise)
    assert pr.head_sha in brief
    assert "test_example" in brief
    assert "controller-secret" not in brief and "token=<redacted>" in brief


def test_controller_ci_handoff_redacts_complete_authorization_headers(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    task.runner = "remote"
    sched.store.save(task)
    secret_values = ("bearer-controller-secret", "basic-controller-secret")
    check = _ci_run(sched, task, pr.head_sha)

    sched._after_ci_check(task, check, [{
        "name": "actions",
        "status": "fail",
        "summary": f"Authorization: Bearer {secret_values[0]}",
        "details": (
            f"authorization=Basic {secret_values[1]}; request rejected\n"
            "failed test_authorization"
        ),
    }], {"head": pr.head_sha, "ci_note": "CI failed"}, TickReport())
    revise = sched.dispatch(task, mode="revise", runner=sched.runner_for(task))

    brief = agent_context(revise)
    assert "Authorization: <redacted>" in brief
    assert "authorization=<redacted>; request rejected" in brief
    assert "failed test_authorization" in brief
    assert all(secret not in brief for secret in secret_values)


def test_distinct_same_second_comments_survive_and_edits_replace_the_same_id(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    common = {"kind": "line comment", "author": "josh", "created": "2026-09-09T11:15:00Z",
              "path": "a.py", "line": 3, "commit_id": "original-comment-head"}
    first = {**common, "id": 10, "body": "Handle the empty response"}
    second = {**common, "id": 11, "body": "Retain the ownership guard"}
    sched._apply_feedback(task, pr, Feedback(items=[first, second]), "", TickReport())
    assert "Handle the empty response" in sched.state.get(task.id)["pending_feedback"]
    assert "Retain the ownership guard" in sched.state.get(task.id)["pending_feedback"]

    edited = {**first, "body": "Handle the empty and malformed response"}
    sched._apply_feedback(task, pr, Feedback(items=[edited, second]), "", TickReport())
    pending = sched.state.get(task.id)["pending_feedback"]
    assert "Handle the empty response" not in pending
    assert pending.count("Handle the empty and malformed response") == 1
    assert pending.count("Retain the ownership guard") == 1
    assert "on commit `original-comment-head`" in pending


def test_manual_revision_handoff_keeps_review_and_ci_without_dispatch(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    sched.cfg.data["auto_revise"] = False
    review = _review()
    reviewed = _review_run(sched, task, pr.head_sha, review)
    sched._apply_review(task, reviewed, review, TickReport(), emitted=False)
    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)

    st = sched.state.get(task.id)
    assert task.status == Status.CHANGES_REQUESTED and st.get("needs_human")
    assert "Environment errors retry forever" in st["pending_feedback"]
    assert "six focused tests failed" in st["pending_feedback"]
    assert not any(candidate.id == task.id for candidate, _, _ in sched.dispatch_queue())


def test_project_manual_revision_handoff_keeps_review_feedback_without_dispatch(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    sched.cfg.data["products"][task.product]["configuration"] = {
        "overrides": {"auto_revise": False},
    }
    review = _review()

    sched._apply_review(
        task, _review_run(sched, task, pr.head_sha, review), review, TickReport(), emitted=False,
    )

    st = sched.state.get(task.id)
    assert task.status == Status.CHANGES_REQUESTED and st.get("needs_human")
    assert "Environment errors retry forever" in st["pending_feedback"]
    assert not any(candidate.id == task.id for candidate, _, _ in sched.dispatch_queue())


def test_project_auto_revision_avoids_global_manual_handoff(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    sched.cfg.data["auto_revise"] = False
    sched.cfg.data["products"][task.product]["configuration"] = {
        "overrides": {"auto_revise": True},
    }

    sched._apply_feedback(
        task,
        pr,
        Feedback(items=[{"kind": "comment", "id": 42, "body": "Fix this"}]),
        "",
        TickReport(),
    )

    st = sched.state.get(task.id)
    assert task.status == Status.CHANGES_REQUESTED
    assert not st.get("needs_human")


def test_project_manual_handoff_keeps_description_only_feedback(sched, fake_github):
    task, pr = _open_task(sched, fake_github)
    sched.cfg.data["products"][task.product]["configuration"] = {
        "overrides": {"auto_revise": False},
    }
    review = {
        "verdict": "request_changes",
        "summary": "The description needs context.",
        "description_ok": False,
        "description_feedback": "Explain the user-visible outcome.",
        "criteria": [],
        "findings": [],
    }

    sched._apply_review(
        task, _review_run(sched, task, pr.head_sha, review), review, TickReport(), emitted=False,
    )

    st = sched.state.get(task.id)
    assert task.status == Status.CHANGES_REQUESTED and st.get("needs_human")
    assert "Explain the user-visible outcome" in st["pending_feedback"]
    assert not any(candidate.id == task.id for candidate, _, _ in sched.dispatch_queue())


def test_restart_after_ci_feedback_save_does_not_recharge_a_rerun(sched, fake_github, monkeypatch):
    task, pr = _open_task(sched, fake_github)
    check = _ci_run(sched, task, pr.head_sha)
    review = _review()
    sched._apply_review(task, _review_run(sched, task, pr.head_sha, review), review, TickReport(), emitted=False)
    _finish_ci(sched, task, _ci_run(sched, task, pr.head_sha), pr.head_sha)
    original_apply = sched._apply_feedback

    def crash_after_save(*args, **kwargs):
        original_apply(*args, **kwargs)
        raise RuntimeError("crash after feedback save")

    monkeypatch.setattr(sched, "_apply_feedback", crash_after_save)
    results = [{"name": "actions", "status": "pass", "reran": True}]
    import pytest
    with pytest.raises(RuntimeError, match="crash after feedback save"):
        sched._after_ci_check(task, check, results, {"head": pr.head_sha}, TickReport())
    fresh = Scheduler(Store(sched.store.root), github=fake_github)
    assert fresh.state.get(task.id)["ci_reruns"] == 1
    fresh._after_ci_check(fresh.store.task(task.id), check, results, {"head": pr.head_sha}, TickReport())
    assert fresh.state.get(task.id)["ci_reruns"] == 1
    assert "Environment errors retry forever" in fresh.state.get(task.id)["pending_feedback"]
