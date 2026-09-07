import copy
import hashlib
import json

import pytest

from garden.model import Status
from garden.now1 import strip_for_run
from garden.review import (
    enforce_criteria_verdict,
    feedback_from_review,
    interaction_evidence_gaps,
    interaction_requirement,
    parse_review,
    review_brief,
    review_to_markdown,
    validation_plan,
)
from garden.scheduler import Scheduler, TickReport
from garden.store import Store


def _writer_run(sched, task_id, harness, model):
    run = sched.runs.new_run(task_id, "local", mode="work")
    run.harness = harness
    run.model = model
    run.status = "done"
    run.save()
    return run


def _review_ladder(sched):
    sched.cfg.data["review"]["ladder"] = [
        "codex:gpt-5.6-luna",
        "claude:claude-sonnet-5",
        "codex:gpt-5.6-terra",
        "codex:gpt-5.6-sol",
        "claude:claude-fable-5-1",
        "codex:gpt-6-astra",
    ]


def test_review_verdict_survives_a_scheduler_restart(sched, fake_github):
    """A verdict the scheduler reaped in its last tick is on disk (state.json) before the
    process ends: a fresh Scheduler on the same garden reads it back. Guards the 2026-09-05
    incident, when a restart lost a review verdict the old process had reaped in its last tick."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched
    sched.tick()  # reap review -> approve verdict recorded and saved at the tick's end
    st = sched.state.get("DM-001")
    assert st.get("last_review", {}).get("verdict") == "approve"
    run_id = st.get("last_review_run")
    assert run_id

    # a new process on the same garden: state.json is the only thing that survives it
    fresh = Scheduler(Store(sched.store.root), github=fake_github, log=print)
    st2 = fresh.state.get("DM-001")
    assert st2.get("last_review", {}).get("verdict") == "approve"
    assert st2.get("last_review_run") == run_id


def test_review_ladder_routes_across_harnesses_and_records_the_writer(sched):
    """A review uses the next configured harness:model pair, not the PR's harness."""
    _review_ladder(sched)
    task = sched.store.task("DM-001")
    expected = [
        ("codex", "gpt-5.6-terra", "codex", "gpt-5.6-sol"),
        ("claude", "claude-fable-5-1", "codex", "gpt-6-astra"),
        ("codex", "gpt-5.6-luna", "claude", "claude-sonnet-5"),
    ]
    for writer_harness, writer_model, reviewer_harness, reviewer_model in expected:
        _writer_run(sched, task.id, writer_harness, writer_model)
        run = sched.dispatch_review(task)
        assert (run.harness, run.model) == (reviewer_harness, reviewer_model)
        assert run.env_snapshot["writer_harness"] == writer_harness
        assert run.env_snapshot["writer_model"] == writer_model
    assert "reviewed by claude-sonnet-5, one above gpt-5.6-luna" in task.body


def test_review_ladder_top_rung_reviews_itself_and_unlisted_writer_falls_back(sched):
    _review_ladder(sched)
    task = sched.store.task("DM-001")
    _writer_run(sched, task.id, "codex", "gpt-6-astra")
    top = sched.dispatch_review(task)
    assert (top.harness, top.model) == ("codex", "gpt-6-astra")

    _writer_run(sched, task.id, "other", "not-on-the-ladder")
    fallback = sched.dispatch_review(task)
    assert (fallback.harness, fallback.model) == ("claude", "sonnet")
    assert "writer_model" not in fallback.env_snapshot


def test_review_ladder_defers_when_the_selected_reviewer_harness_is_paused(sched):
    _review_ladder(sched)
    task = sched.store.task("DM-001")
    writer = _writer_run(sched, task.id, "codex", "gpt-5.6-luna")
    sched.pause_harness("claude", "quota limit")
    from garden.scheduler import TickReport

    rep = TickReport()
    sched._dispatch_or_defer_reviews(task, [{"kind": "review"}], rep, work_run=writer)
    assert rep.dispatched == []
    assert sched.state.get(task.id)["pending_reviews"] == [{"kind": "review"}]


def test_review_brief_and_parse(garden):
    store = Store(garden)
    t = store.task("DM-001")
    text = review_brief(store, t, branch="b", base="main", pr_title="T", pr_body="B", diff="+++ x\n-a\n+b", max_diff_chars=1000)
    assert "GARDEN_REVIEW:" in text and "## Diff" in text and "```diff" in text and "Operating rules" not in text
    big = review_brief(store, t, branch="b", base="main", pr_title="T", pr_body="", diff="x" * 2000, max_diff_chars=100)
    assert "git diff main...HEAD" in big and "(empty)" in big
    rev = parse_review('junk\nGARDEN_REVIEW: {"verdict": "request_changes", "summary": "s", "description_ok": false, "description_feedback": "d", "findings": [{"severity": "blocking", "file": "a.py", "line": 2, "summary": "bug"}]}')
    assert rev["verdict"] == "request_changes"
    md = review_to_markdown(rev, "r1")
    assert "request changes" in md and "`a.py`:2" in md and "**PR description**" in md
    fb = feedback_from_review(rev)
    assert "blocking" in fb and "pr_body" in fb
    assert parse_review("nothing") == {}


@pytest.mark.parametrize("criterion", [
    {"criterion": "The outcome works.", "met": False, "evidence": "test_outcome"},
    {"criterion": "The outcome works.", "met": True},
])
def test_unmet_or_evidenceless_criterion_forces_request_changes(criterion):
    review = enforce_criteria_verdict({"verdict": "approve", "criteria": [criterion], "findings": []})

    assert review["verdict"] == "request_changes"
    assert review["findings"][-1]["severity"] == "blocking"
    assert "The outcome works." in review["findings"][-1]["summary"]


def test_review_fixes_and_improvements_reach_comment_and_revise_brief(garden):
    store = Store(garden)
    review = parse_review('GARDEN_REVIEW: {"verdict":"request_changes","summary":"s","findings":[{"severity":"blocking","file":"a.py","line":2,"summary":"bug","fix":"Guard the empty value in parse()."},{"severity":"high","file":"b.py","line":3,"summary":"edge case","fix":"Handle the empty collection."},{"severity":"nit","file":"c.py","line":4,"summary":"unclear name","fix":"Rename result to parsed_value."}],"improvements":[{"area":"naming","suggestion":"Rename x to parsed_value.","why":"It reads at the caller.","effort":"small"}]}')
    assert review["findings"][0]["fix"].startswith("Guard")
    assert review["improvements"][0]["effort"] == "small"
    # Older reviewers have neither field and remain parseable.
    old = parse_review('GARDEN_REVIEW: {"verdict":"approve","summary":"old","findings":[]}')
    assert "improvements" not in old
    markdown = review_to_markdown(review)
    assert "**Fix:** Guard the empty value" in markdown
    assert "**Improvements**" in markdown and "Rename x to parsed_value" in markdown
    feedback = feedback_from_review(review)
    assert "Guard the empty value" in feedback
    assert "**automated review** blocking (`a.py`:2): bug" in feedback
    assert "**automated review** high (`b.py`:3): edge case" in feedback
    assert "**automated review** nit (`c.py`:4): unclear name" in feedback
    assert "Handle the empty collection." in feedback
    assert "Rename result to parsed_value." in feedback
    assert "Optional improvements" in feedback and "improvements_declined" in feedback
    task = store.task("DM-001")
    task.pr = "https://example.test/pull/1"
    brief = review_brief(store, task, branch="b", base="main", pr_title="T", pr_body="B", diff="+x",
                         max_diff_chars=100, reask_missing_fixes=True)
    assert "Follow-up required" in brief and "`fix` for every blocking finding" in brief


def test_review_without_blocking_fix_is_reasked_once(sched, fake_github, monkeypatch):
    from tests.fake_claude import REVIEWS

    REVIEWS["review-no-fix"] = {"verdict": "request_changes", "summary": "needs a test",
                                "description_ok": True,
                                "findings": [{"severity": "blocking", "file": "a.py", "line": 1,
                                              "summary": "missing test"}]}
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-no-fix")
    sched.tick()
    sched.tick()
    rep = sched.tick()
    assert "DM-001 review re-asked for blocking fixes" in rep.transitions
    rerun = sched.runs.latest("DM-001")
    assert rerun.mode == "review"
    assert "Follow-up required" in (rerun.path / "brief.md").read_text()
    assert sched.store.task("DM-001").status == Status.IN_REVIEW


def test_review_brief_includes_ui_capture_paths(garden, tmp_path):
    store = Store(garden)
    shot = tmp_path / "board-390-dark.png"
    text = review_brief(store, store.task("DM-001"), branch="b", base="main", pr_title="T",
                        pr_body="B", diff="+x", max_diff_chars=1000, captures=[str(shot)])
    assert "## Rendered UI captures" in text
    assert str(shot) in text
    assert "pages_seen" in text


def test_interaction_review_brief_names_running_app_states_and_head(garden):
    store = Store(garden)
    required, scalability, reason = interaction_requirement(
        ["src/garden/scheduler/review.py"], "Keep lifecycle review dependable",
    )
    assert required and not scalability and "scheduler/review.py" in reason
    text = review_brief(
        store, store.task("DM-001"), branch="b", base="main", pr_title="T", pr_body="B",
        diff="+x", max_diff_chars=1000, interaction_required=True, review_head="abc123",
        interaction_reason=reason,
    )
    assert "Running-application interaction required" in text
    assert "affected journey" in text and "empty state" in text and "failure followed" in text
    assert "`abc123`" in text and '"environment": "disposable"' in text
    assert "screenshots and test-client" in text
    assert interaction_requirement(["src/garden/costs.py"], "Refactor aggregation") == (
        False, False, "non-UI change",
    )


@pytest.mark.parametrize(("behavior", "path"), [
    ("worker interruption", "src/garden/run_supervisor.py"),
    ("changed PR head", "src/garden/gitops.py"),
    ("restart recovery", "src/garden/scheduler/state.py"),
    ("no_change outcome", "src/garden/outcomes.py"),
    ("runner execution", "src/garden/runner/local.py"),
    ("run records", "src/garden/runs.py"),
    ("harness execution", "src/garden/harness.py"),
])
def test_lifecycle_implementations_require_interaction_evidence(behavior, path):
    required, scalability, reason = interaction_requirement([path], "Lifecycle reliability")
    assert required and not scalability, behavior
    assert path in reason, behavior


@pytest.mark.parametrize("path", ["src/garden/review.py", "src/garden/stabilization.py"])
def test_review_and_phase_close_implementations_require_interaction_evidence(path):
    required, scalability, _ = interaction_requirement([path], "Internal policy cleanup")
    assert required and not scalability


@pytest.mark.parametrize("path", [
    "src/garden/browser.py", "src/garden/profiles.py", "src/garden/web/pages/task.py",
    "src/garden/tui/app.py", "src/garden/scheduler/human.py",
])
def test_behavior_owning_surfaces_require_interaction_evidence(path):
    required, _, _ = interaction_requirement([path], "Behavior change")
    assert required


@pytest.mark.parametrize("path", [
    "src/garden/model.py", "src/garden/checkrun.py", "src/garden/checks.py",
])
def test_status_and_check_recovery_surfaces_require_interaction_evidence(path):
    required, scalability, reason = interaction_requirement([path], "Lifecycle behavior")
    assert required and not scalability
    assert path in reason


@pytest.mark.parametrize("path", [
    "src/garden/brief.py", "src/garden/criteria.py", "src/garden/events.py",
    "src/garden/validation.py", "src/garden/charts.py",
    "src/garden/scheduler/report.py",
])
def test_offline_and_formatting_modules_keep_proportionate_validation(path):
    assert interaction_requirement([path], "Internal refactor") == (False, False, "non-UI change")


def test_explicit_change_metadata_can_require_interaction_evidence():
    required, scalability, reason = interaction_requirement(
        ["src/garden/brief.py"], "Interaction-evidence: required",
    )
    assert required and not scalability
    assert reason == "change metadata requires interaction evidence"


def test_validation_plan_scopes_backend_parser_page_and_shared_ui_changes():
    backend = validation_plan(["src/garden/scheduler/human.py"], "Change incident control")
    assert backend["pages"] == []
    assert backend["interaction"] is True
    assert backend["reasons"][-1]["reason"].endswith("scheduler/human.py")

    parser = validation_plan(["src/garden/criteria.py"], "Parse result markers")
    assert parser["pages"] == []
    assert parser["interaction"] is False
    assert parser["reasons"] == [{"item": "focused relevant checks", "reason": "no rendered or lifecycle behavior changed"}]

    page = validation_plan(["src/garden/web/pages/task.py"], "Tighten task layout")
    assert page["pages"] == ["task"]
    assert page["interaction"] is True

    shared = validation_plan(["src/garden/web/templates/base.html"], "Update shared rail style")
    assert shared["pages"] == ["*"]
    assert any("every consumer" in reason["reason"] for reason in shared["reasons"])


def test_validation_plan_requires_bounded_inspection_for_unknown_ui_scope():
    plan = validation_plan(["src/garden/web/widgets/unmapped.py"], "New component")

    assert plan["pages"] == []
    assert plan["unknown_ui"] == ["src/garden/web/widgets/unmapped.py"]
    assert any(reason["item"] == "bounded UI inspection" for reason in plan["reasons"])


def test_review_brief_distinguishes_required_validation_from_available_captures(garden):
    store = Store(garden)
    plan = validation_plan(["src/garden/web/pages/task.py"], "Task layout", head="head-a")
    text = review_brief(store, store.task("DM-001"), branch="b", base="main", pr_title="T", pr_body="B",
                        diff="+x", max_diff_chars=1000,
                        captures=["/tmp/task-1280-light.png", "/tmp/inbox-1280-light.png"], plan=plan)

    assert '"pages": [\n    "task"\n  ]' in text
    assert "not the available" in text


def test_one_page_review_does_not_turn_available_captures_into_a_fourteen_page_demand(sched, monkeypatch):
    task = sched.store.task("DM-001")
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names",
                        lambda *_: ["src/garden/web/pages/task.py"])
    run = sched.dispatch_review(task)

    assert run.env_snapshot["capture_pages"] == ["task"]
    assert run.env_snapshot["validation_plan"]["pages"] == ["task"]


def test_review_reuses_the_current_head_precheck_validation_plan(sched, monkeypatch):
    task = sched.store.task("DM-001")
    plan = validation_plan(["src/garden/web/pages/task.py"], "Task layout", head="head-a")
    check = sched.runs.new_run(task.id, "local", mode="check")
    check.status = "done"
    check.env_snapshot = {"validation_plan": plan}
    check.save()
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/criteria.py"])
    monkeypatch.setattr("garden.scheduler.review.gitops.head_sha", lambda *_: "head-a")

    run = sched.dispatch_review(task)

    assert run.env_snapshot["validation_plan"] == plan
    assert run.env_snapshot["capture_pages"] == ["task"]


def test_review_omits_artifacts_from_a_stale_head_check(sched, monkeypatch):
    task = sched.store.task("DM-001")
    stale = sched.runs.new_run(task.id, "local", mode="check")
    stale.status = "done"
    stale.env_snapshot = {"validation_plan": validation_plan(["src/garden/web/pages/task.py"], "layout", head="old")}
    stale.result = {"checks": [{"name": "ui", "pages": ["task"], "captures": ["/tmp/stale.png"]}]}
    stale.save()
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/criteria.py"])
    monkeypatch.setattr("garden.scheduler.review.gitops.head_sha", lambda *_: "current")

    run = sched.dispatch_review(task)

    assert run.env_snapshot["validation_plan"]["head"] == "current"
    assert run.env_snapshot["capture_pages"] == []
    assert run.env_snapshot["validation_check_current"] is False
    assert "/tmp/stale.png" not in (run.path / "brief.md").read_text()


def test_worker_brief_carries_the_frozen_validation_plan(sched, monkeypatch):
    task = sched.store.task("DM-001")
    monkeypatch.setattr("garden.scheduler.dispatch.gitops.diff_names", lambda *_: ["src/garden/web/pages/task.py"])
    monkeypatch.setattr("garden.scheduler.dispatch.gitops.head_sha", lambda *_: "head-a")

    run = sched.dispatch(task)

    assert run.env_snapshot["validation_plan"]["head"] == "head-a"
    assert run.env_snapshot["validation_plan"]["pages"] == ["task"]
    assert "## Validation plan" in (run.path / "brief.md").read_text()


def test_review_rejects_unmapped_unknown_ui_scope_and_accepts_consumer_mapping(sched, monkeypatch):
    task = sched.store.task("DM-001")
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/web/widgets/unmapped.py"])
    monkeypatch.setattr("garden.scheduler.review.interaction_evidence_gaps", lambda *args, **kwargs: [])
    for mapping, expected in (([], True), ([{"path": "src/garden/web/widgets/unmapped.py", "consumers": ["task"]}], False)):
        run = sched.dispatch_review(task)
        review = {"verdict": "approve", "summary": "looks good", "pages_seen": [], "ui_scope": mapping,
                  "scope_expansions": [], "criteria": [], "description_ok": True,
                  "description_feedback": "", "description_rewrite": "", "findings": [], "improvements": []}
        (run.path / "stdout.json").write_text(json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "GARDEN_REVIEW: " + json.dumps(review), "usage": {},
        }))
        sched.reap_review(task, TickReport())
        result = sched.runs.latest(task.id).result
        summaries = [finding["summary"] for finding in result["findings"]]
        assert any("Bounded UI inspection incomplete" in text for text in summaries) is expected


def test_scalability_claim_in_pr_description_requires_load_evidence():
    required, scalability, _ = interaction_requirement(
        ["docs/design.md"], "Documentation task", "Routine update", "PR title",
        "Keeps p95 latency bounded with larger histories",
    )
    assert not required and scalability


@pytest.mark.parametrize("path", [
    "src/garden/runner/local.py",
    "src/garden/runs.py",
    "src/garden/gitops.py",
    "src/garden/harness.py",
])
def test_scheduler_rejects_nominal_approval_without_lifecycle_interaction(
    sched, fake_github, monkeypatch, path,
):
    monkeypatch.setattr("garden.scheduler.review.produce_interaction_replay", lambda *args: None)
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: [path])
    task = sched.store.task("DM-001")
    run = sched.dispatch_review(task)

    assert run.env_snapshot["interaction_required"] is True
    assert run.process_finished()
    sched.reap_review(task, TickReport())

    persisted = sched.runs.latest(task.id)
    assert persisted is not None
    assert persisted.result["verdict"] == "request_changes"
    assert "Running-app evidence incomplete" in persisted.result["findings"][-1]["summary"]


def test_reap_review_rejects_truthy_malformed_interaction_evidence(sched, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.produce_interaction_replay", lambda *args: None)
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    run = sched.dispatch_review(task)
    malformed = {
        "verdict": "approve", "summary": "looks good", "pages_seen": [], "criteria": [],
        "description_ok": True, "description_feedback": "", "description_rewrite": "",
        "findings": [], "improvements": [],
        "interaction": {
            "head": run.env_snapshot["review_head"], "environment": "disposable", "command": "true",
            "states": {name: {"status": "pass", "actions": "clicked", "observed": True}
                       for name in ("affected", "empty", "failure_recovery")},
            "artifacts": ["/etc/hosts"], "automated_checks": "pytest", "unverified": False,
        },
    }
    envelope = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "GARDEN_REVIEW: " + json.dumps(malformed), "usage": {},
    }
    (run.path / "stdout.json").write_text(json.dumps(envelope))

    sched.reap_review(task, TickReport())

    persisted = sched.runs.latest(task.id)
    assert persisted is not None and persisted.result["verdict"] == "request_changes"
    assert "structured interaction artifact" in persisted.result["findings"][-1]["summary"]


def interaction_events() -> list[dict[str, object]]:
    return [
        {"kind": "http_request", "state": state, "outcome": outcome, "method": "POST",
         "url": f"http://127.0.0.1:8765/{state}", "status_code": status,
         "observed": observed}
        for state, outcome, status, observed in (
            ("affected", "success", 200, "requested change completed"),
            ("empty", "empty", 200, "empty queue shown"),
            ("failure", "failure", 503, "service unavailable shown"),
            ("recovery", "success", 200, "request succeeded after retry"),
        )
    ]


def test_scheduler_manifest_proves_independent_head_bound_execution(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "producer": "garden.scheduler.interaction-replay/v1", "head": "head-a",
        "nonce": "issued-by-scheduler", "started_at": "2026-09-07T10:00:00+00:00",
        "finished_at": "2026-09-07T10:00:01+00:00", "status": "pass",
        "environment": "disposable", "flows": [{
            "name": "affected flow", "ok": True,
            "requests": [{"at": 1.0, "method": "POST", "url": "http://127.0.0.1/action",
                          "status_code": 303}],
        }], "states": {state: {"status": "pass", "action": state, "observed": "observed"}
                        for state in ("affected", "empty", "failure", "recovery")},
        "events": [{"state": state, "action": state, "observed": "observed", "at": at}
                   for at, state in enumerate(("affected", "failure", "recovery", "empty"), 1)],
    }))
    assert interaction_evidence_gaps(
        {}, required=True, scalability=False, expected_head="head-a",
        replay_manifest=manifest, replay_nonce="wrong",
    )[0] == "running-application interaction evidence was not reported"
    review = {"interaction": {}}
    gaps = interaction_evidence_gaps(
        review, required=True, scalability=False, expected_head="head-a",
        replay_manifest=manifest, replay_nonce="wrong",
        replay_digest=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    assert any("provenance" in gap for gap in gaps)
    gaps = interaction_evidence_gaps(
        review, required=True, scalability=False, expected_head="head-a",
        replay_manifest=manifest, replay_nonce="issued-by-scheduler",
        replay_digest=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    assert not any("scheduler-produced" in gap for gap in gaps)


def test_interaction_evidence_must_be_performed_current_complete_and_replayable(tmp_path):
    artifact = tmp_path / "journey.json"
    states = {
        name: {"status": "pass", "actions": [f"POST /{name}"], "observed": "state changed"}
        for name in ("affected", "empty", "failure_recovery")
    }
    events = interaction_events()
    artifact.write_text(json.dumps({"head": "head-a", "states": states, "events": events}))
    review = {"interaction": {
        "head": "head-a", "environment": "disposable", "command": "garden qa --scripted",
        "states": states, "events": events, "artifacts": [str(artifact)], "automated_checks": ["pytest"],
        "unverified": [],
    }}
    assert interaction_evidence_gaps(review, required=True, scalability=False, expected_head="head-a") == []

    review["interaction"]["head"] = "old-head"
    review["interaction"]["states"]["failure_recovery"]["status"] = "fail"
    review["interaction"]["unverified"] = ["empty prompt copy"]
    gaps = interaction_evidence_gaps(review, required=True, scalability=False, expected_head="head-a")
    assert any("stale" in gap for gap in gaps)
    assert any("failure/recovery" in gap for gap in gaps)
    assert any("remain unverified" in gap for gap in gaps)


@pytest.mark.parametrize("events", [
    [
        {"kind": "http_request", "state": "affected", "outcome": "success", "method": "GET",
         "url": "http://localhost/affected", "status_code": 200, "observed": "ok"},
        {"kind": "http_request", "state": "empty", "outcome": "empty", "method": "GET",
         "url": "http://localhost/empty", "status_code": 200, "observed": "ok"},
        {"kind": "http_request", "state": "failure", "outcome": "failure", "method": "GET",
         "url": "http://localhost/failure", "status_code": 200, "observed": "ok"},
        {"kind": "http_request", "state": "recovery", "outcome": "success", "method": "GET",
         "url": "http://localhost/recovery", "status_code": 200, "observed": "ok"},
    ],
    [
        {"kind": "http_request", "state": state, "outcome": "success", "method": "GET",
         "url": f"http://localhost/{state}", "status_code": 200, "observed": "ok"}
        for state in ("affected", "empty", "failure_recovery")
    ],
])
def test_label_only_success_responses_do_not_prove_empty_and_failure_recovery(tmp_path, events):
    states = {name: {"status": "pass", "actions": ["request"], "observed": "ok"}
              for name in ("affected", "empty", "failure_recovery")}
    artifact = tmp_path / "journey.json"
    artifact.write_text(json.dumps({"head": "h", "states": states, "events": events}))
    review = {"interaction": {
        "head": "h", "environment": "disposable", "command": "serve fixture",
        "states": states, "events": events, "artifacts": [str(artifact)],
        "automated_checks": [], "unverified": [],
    }}

    assert interaction_evidence_gaps(review, required=True, scalability=False, expected_head="h")


def test_recovery_must_follow_failure_chronologically():
    events = interaction_events()
    events[2], events[3] = events[3], events[2]

    gaps = interaction_evidence_gaps(
        {"interaction": {"head": "h", "environment": "disposable", "command": "serve fixture",
                         "states": {}, "events": events, "artifacts": [],
                         "automated_checks": [], "unverified": []}},
        required=True, scalability=False, expected_head="h",
    )

    assert any("chronologically" in gap for gap in gaps)


@pytest.mark.parametrize("action", ["echo screenshot-only", "opened screenshot.png"])
def test_screenshot_only_placeholders_are_not_performed_interaction(tmp_path, action):
    states = {
        name: {"status": "pass", "actions": [action], "observed": "opened screenshot.png"}
        for name in ("affected", "empty", "failure_recovery")
    }
    events = [
        {"kind": "browser_action", "state": state, "outcome": outcome, "action": action,
         "target": "screenshot.png", "observed": "opened screenshot.png"}
        for state, outcome in (("affected", "success"), ("empty", "empty"),
                               ("failure", "failure"), ("recovery", "success"))
    ]
    artifact = tmp_path / "journey.json"
    artifact.write_text(json.dumps({"head": "h", "states": states, "events": events}))
    review = {"interaction": {
        "head": "h", "environment": "disposable", "command": action,
        "states": states, "events": events, "artifacts": [str(artifact)],
        "automated_checks": [], "unverified": [],
    }}

    gaps = interaction_evidence_gaps(review, required=True, scalability=False, expected_head="h")

    assert any("non-image action" in gap for gap in gaps)


def test_scalability_claim_requires_served_load_distribution_and_scan_counts(tmp_path):
    artifact = tmp_path / "latencies.json"
    states = {name: {"status": "pass", "actions": ["request"], "observed": "ok"}
              for name in ("affected", "empty", "failure_recovery")}
    events = interaction_events()
    artifact.write_text(json.dumps({"head": "h", "states": states, "events": events}))
    required, scalability, _ = interaction_requirement([], "Keep p95 latency bounded after cache expiry")
    assert not required and scalability
    review = {"interaction": {
        "head": "h", "environment": "disposable", "command": "serve fixture",
        "states": states, "events": events,
        "artifacts": [str(artifact)], "automated_checks": [], "unverified": [],
        "scalability": {"served_app": "http://localhost:8783", "history_sizes": [100, 6000],
                        "cache_expiry_intervals": 3, "executing_processes": 2,
                        "latencies": [0.1, 0.2], "read_scan_counts": {"reads": 3, "scans": 0},
                        "load_kind": "controlled"},
    }}
    assert interaction_evidence_gaps(review, required=False, scalability=True, expected_head="h") == []
    del review["interaction"]["scalability"]["read_scan_counts"]
    assert "read_scan_counts" in interaction_evidence_gaps(
        review, required=False, scalability=True, expected_head="h",
    )[0]


@pytest.mark.parametrize(("field", "value", "message"), [
    ("history_sizes", [100], "history_sizes"),
    ("history_sizes", [1000, 100], "history_sizes"),
    ("history_sizes", [100, 100], "history_sizes"),
    ("cache_expiry_intervals", 1, "cache_expiry_intervals"),
    ("cache_expiry_intervals", "3", "cache_expiry_intervals"),
    ("executing_processes", 0, "executing_processes"),
    ("executing_processes", True, "executing_processes"),
    ("latencies", [0.1], "latencies"),
    ("latencies", [0.1, "slow"], "latencies"),
    ("read_scan_counts", {"reads": 3}, "read_scan_counts"),
    ("read_scan_counts", {"reads": 3, "scans": "one"}, "read_scan_counts"),
    ("load_kind", "synthetic-ish", "load_kind"),
])
def test_scalability_evidence_rejects_malformed_boundaries(tmp_path, field, value, message):
    artifact = tmp_path / "latencies.json"
    states = {name: {"status": "pass", "actions": ["request"], "observed": "ok"}
              for name in ("affected", "empty", "failure_recovery")}
    events = interaction_events()
    artifact.write_text(json.dumps({"head": "h", "states": states, "events": events}))
    interaction = {
        "head": "h", "environment": "disposable", "command": "serve fixture",
        "states": states, "events": events,
        "artifacts": [str(artifact)], "automated_checks": [], "unverified": [],
        "scalability": {"served_app": "http://localhost:8783", "history_sizes": [100, 6000],
                        "cache_expiry_intervals": 3, "executing_processes": 2,
                        "latencies": [0.1, 0.2], "read_scan_counts": {"reads": 3, "scans": 0},
                        "load_kind": "controlled"},
    }
    malformed = copy.deepcopy(interaction)
    malformed["scalability"][field] = value
    gaps = interaction_evidence_gaps(
        {"interaction": malformed}, required=False, scalability=True, expected_head="h",
    )
    assert any(message in gap for gap in gaps)


def test_second_review_dispatch_supersedes_the_first(sched, fake_github):
    """CG-144: dispatching a second review while the first is still `running` (a person
    pressed "one more review", or the poll re-reviewed a fresh push) closes the first as
    `superseded` with its cost recorded, rather than leaving it running forever with
    nothing left pointing at it."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    sched.tick()  # reap work -> PR opened -> first review dispatched
    t = sched.store.task("DM-001")
    st = sched.state.get("DM-001")
    run1_id = st["review_run"]
    assert run1_id
    run1 = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run1_id)
    assert run1.status == "running" and run1.process_finished()  # finished, not yet reaped

    run2 = sched.dispatch_review(t)

    superseded = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run1_id)
    assert superseded.status == "superseded"
    assert superseded.finished_at
    assert superseded.cost_usd == 0.02  # the finished run's cost is still recorded
    assert st["review_run"] == run2.run_id != run1_id
    # the superseded run no longer counts as active
    assert run1_id not in {r.run_id for r in sched.runs.active()}


def test_revise_with_pr_comment(sched, fake_github, monkeypatch):
    """Workers can include pr_comment in the result to explain revisions."""

    # Focus on DM-001's review cycle: without this, DM-002 stacks on DM-001's open PR
    # and runs its own review rounds concurrently, so the fixed per-tick assertions
    # below become order-dependent on a loaded machine.
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "revise-with-comment")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched
    sched.tick()  # reap review -> request_changes -> revise dispatched
    rep = sched.tick()  # reap revise -> PR body updated + pr_comment posted -> second review dispatched
    # Verify the pr_comment was posted as a separate comment
    assert any("I addressed the feedback by adding the missing test." in c for c in fake_github.comments)
    # Verify the standard revision comment was also posted
    assert any("Pushed a revision round:" in c for c in fake_github.comments)
    # Verify the pr_comment is not duplicated into the PR body/description
    assert not any("I addressed the feedback" in u.get("body", "") for u in fake_github.updated)
    # Verify the follow-up automated review can see the response, so it doesn't repeat the same finding
    assert "DM-001(review)" in rep.dispatched
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "I addressed the feedback by adding the missing test." in brief
    assert "not part of the description" in brief


def test_review_flow(sched, fake_github, monkeypatch):

    # Focus on DM-001's review cycle: without this, DM-002 stacks on DM-001's open PR
    # and runs its own review rounds concurrently, so the fixed per-tick assertions
    # below become order-dependent on a loaded machine.
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    st = sched.state.get("DM-001")
    assert st["review_run"] and st["review_rounds"] == 1
    run = sched.runs.latest("DM-001")
    assert run.mode == "review" and "GARDEN_REVIEW" in (run.path / "brief.md").read_text()
    rep = sched.tick()  # reap review -> request_changes -> revise dispatched
    assert "DM-001 -> changes_requested (review)" in rep.transitions and "DM-001(revise)" in rep.dispatched
    assert any("Automated review: request changes" in c for c in fake_github.comments)
    brief = (sched.runs.latest("DM-001").path / "brief.md").read_text()
    assert "missing test" in brief and "PR description" in brief
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-ok")
    rep = sched.tick()  # reap revise -> PR body updated -> second review
    assert fake_github.updated and fake_github.updated[-1]["body"]
    assert "DM-001(review)" in rep.dispatched
    rep = sched.tick()
    assert "DM-001 review: approve" in rep.transitions
    sched.store.invalidate()
    assert sched.store.task("DM-001").status.value == "in_review"
    assert sched.state.get("DM-001")["review_rounds"] == 2
    # cap reached: a further round would not start
    assert sched.state.get("DM-001")["last_review"]["verdict"] == "approve"


def test_review_parses_description_rewrite():
    rev = parse_review('GARDEN_REVIEW: {"verdict": "request_changes", "summary": "s", "description_ok": false, '
                       '"description_feedback": "d", "description_rewrite": "## What\\n\\nBetter.", "findings": []}')
    assert rev["description_rewrite"] == "## What\n\nBetter."


def test_review_brief_advertises_description_rewrite(garden):
    store = Store(garden)
    text = review_brief(store, store.task("DM-001"), branch="b", base="main", pr_title="T", pr_body="B",
                        diff="+a", max_diff_chars=1000)
    assert "description_rewrite" in text
    assert "rewrite the description yourself" in text


def test_review_brief_marks_an_amended_criterion(garden):
    store = Store(garden)
    task = store.task("DM-001")
    task.body += "\n## Acceptance criteria\n\n- [ ] The revised outcome works.\n"
    task.extra["criteria_amended"] = [{"index": 0, "text": "The revised outcome works.", "reason": "The original was false."}]
    text = review_brief(store, task, branch="b", base="main", pr_title="T", pr_body="B", diff="+a", max_diff_chars=1000)
    assert "## Amended acceptance criteria" in text
    assert "amended — The original was false." in text


def test_review_description_only_rewrite_applied_without_a_round(sched, fake_github, monkeypatch):
    """description_ok false, no blocking finding, rewrite supplied: the scheduler updates the PR
    body through the API and starts no revise round."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-rewrite")
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched (as review-rewrite)
    assert "DM-001(review)" in rep.dispatched
    rep = sched.tick()  # reap review -> apply the rewrite, no round
    assert any("description rewritten by the reviewer" in t for t in rep.transitions)
    assert "DM-001(revise)" not in rep.dispatched
    assert not any("changes_requested" in t for t in rep.transitions)
    # the corrected body reached GitHub
    assert fake_github.updated and fake_github.updated[-1]["body"] == "## What\n\nThe corrected description."
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert task.status.value == "in_review"
    assert "description rewritten by the reviewer" in task.body


def test_description_only_revise_dispatches_on_easy_tier(sched, fake_github, monkeypatch):
    """CG-109: a review with no code findings, only a description fix, is a paragraph
    rewrite, so the revise round should not cost a code-review-tier model."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-desc")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched
    rep = sched.tick()  # reap review -> request_changes -> revise dispatched
    assert "DM-001 -> changes_requested (review)" in rep.transitions and "DM-001(revise)" in rep.dispatched
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"
    assert run.model == "haiku"  # the easy tier, not the task's (medium) tier
    assert run.difficulty == "easy"
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert task.difficulty == "medium"  # the task's own tier is unchanged
    assert "description only; easy tier" in task.body


def test_revise_with_code_finding_keeps_task_tier(sched, fake_github, monkeypatch):
    """A revise round with a blocking code finding is a real review round, so it keeps
    the task's own tier rather than dropping to easy."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched
    rep = sched.tick()  # reap review -> request_changes -> revise dispatched
    assert "DM-001(revise)" in rep.dispatched
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"
    assert run.model == "sonnet"  # the task's own (medium) tier
    assert run.difficulty == "medium"
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert "description only; easy tier" not in task.body


def test_approve_with_description_rewrite_applies_directly(sched, fake_github, monkeypatch):
    """CG-140: an approve verdict with description_ok false and a rewrite applies the
    rewrite through the GitHub API and stores no pending feedback; the task stays
    in_review with no revise round."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-approve-rewrite")
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    rep = sched.tick()  # reap review -> approve, apply the rewrite, no round
    assert any("description rewritten by the reviewer" in t for t in rep.transitions)
    assert "DM-001(revise)" not in rep.dispatched
    assert not any("changes_requested" in t for t in rep.transitions)
    assert fake_github.updated and fake_github.updated[-1]["body"] == "## What\n\nThe corrected description."
    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert task.status.value == "in_review"
    assert not str(sched.state.get("DM-001").get("pending_feedback") or "").strip()


def test_approve_with_empty_rewrite_dispatches_description_round(sched, fake_github, monkeypatch):
    """CG-140: an approve verdict with description_ok false and no rewrite dispatches a
    description-only revise round instead of leaving feedback parked on an in_review task."""

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-approve-desc")
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    rep = sched.tick()  # reap review -> approve but description flagged -> revise dispatched
    assert "DM-001 -> changes_requested (description round)" in rep.transitions
    assert "DM-001(revise)" in rep.dispatched
    run = sched.runs.latest("DM-001")
    assert run.mode == "revise"
    assert run.difficulty == "easy"  # description-only: the easy tier, not the task's own


def test_orphaned_review_run_is_closed_not_left_running(sched, fake_github):

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched
    assert "DM-001(review)" in rep.dispatched
    review_run_id = sched.state.get("DM-001")["review_run"]
    assert review_run_id

    # the task moves on (e.g. the PR is merged by a human) before the tick that
    # would have read the review's verdict; the reap gate on t.status.pr_open now
    # fails, and the run would otherwise be stuck "running" forever.
    task = sched.store.task("DM-001")
    task.status = Status.DONE
    sched.store.save(task)

    rep = sched.tick()

    assert not any(r.task_id == "DM-001" and r.run_id == review_run_id for r in sched.runs.active())
    run = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == review_run_id)
    assert run.status in ("done", "failed")
    assert run.cost_usd == 0.02  # usage/cost still recorded from the fake worker's output
    assert any(f"{review_run_id} closed (obsolete)" in t for t in rep.transitions)
    # no verdict posted and the task's own status is left alone
    assert sched.store.task("DM-001").status == Status.DONE
    assert not sched.state.get("DM-001").get("review_run")
    assert not any("request_changes" in c or "approve" in c for c in fake_github.comments)


def test_finished_review_on_a_ready_task_is_collected_without_reusing_stale_head(sched, fake_github):
    """A failed rebase can return a task to ready before its review is collected."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched and finished
    st = sched.state.get("DM-001")
    run_id = st["review_run"]
    run = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run_id)
    run.env_snapshot["review_head"] = "head-before-the-failed-rebase"
    run.save()
    task = sched.store.task("DM-001")
    task.status = Status.READY
    sched.store.save(task)
    sched.pause(by="test")

    assert run_id in sched.unreaped_run_ids()
    strip = strip_for_run(run, {task.id: task}, sched.store, {})
    assert strip["state"] == "finishing"
    assert strip["verdict"] == "finished; awaiting collection"

    rep = sched.tick()

    closed = next(r for r in sched.runs.runs_for("DM-001") if r.run_id == run_id)
    assert closed.status == "done" and closed.cost_usd == 0.02
    assert not sched.state.get("DM-001").get("review_run")
    assert not sched.state.get("DM-001").get("last_review_run")
    assert sched.store.task("DM-001").status == Status.READY
    assert any(f"{run_id} closed (obsolete)" in item for item in rep.transitions)
    finished = [e for e in sched.events.read(task_id="DM-001", kinds=["run_finished"])
                if e.get("run") == run_id]
    assert len(finished) == 1

    sched.tick()
    finished_again = [e for e in sched.events.read(task_id="DM-001", kinds=["run_finished"])
                      if e.get("run") == run_id]
    assert len(finished_again) == 1


def test_maybe_review_never_dispatches_for_a_merged_task(sched, fake_github):
    """CG-142: if a task somehow reaches `_maybe_review` after its PR merged (a race between
    a finishing work run and the poll that already saw the merge), the automated round must
    not fire; it is logged and skipped instead of crashing the tick."""
    from garden.scheduler import TickReport

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    sched.store.save(task)
    sched._transition(sched.store.task("DM-001"), Status.DONE, f"PR merged: {task.pr}")

    rep = TickReport()
    sched._maybe_review(sched.store.task("DM-001"), None, rep)

    assert not sched.runs.runs_for("DM-001")
    assert sched.store.task("DM-001").status == Status.DONE
    assert "could not start" in sched.store.task("DM-001").body


def test_review_cap_reached_flags_needs_human_and_one_more_review_grants_a_round(sched, fake_github, monkeypatch):
    """CG-117: once the cap stops the automated reviewer, the task says so instead of sitting
    silently in review, and the Inbox offers one more round without a human editing state.json."""
    from garden.inbox import build_inbox

    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-desc")
    sched.tick()
    sched.tick()  # reap work -> PR opened -> review dispatched (round 1)
    sched.tick()  # reap review round 1: request_changes (description only) -> revise dispatched
    rep = sched.tick()  # reap revise -> pushed -> review dispatched (round 2)
    assert "DM-001(review)" in rep.dispatched
    assert sched.state.get("DM-001")["review_rounds"] == 2
    sched.tick()  # reap review round 2: request_changes again -> revise dispatched
    rep = sched.tick()  # reap revise -> pushed -> cap already used; no third review dispatched
    assert "DM-001(review)" not in rep.dispatched
    assert "DM-001 review cap reached" in rep.transitions

    sched.store.invalidate()
    task = sched.store.task("DM-001")
    assert "2 automated review round(s) used" in task.body
    assert task.status == Status.IN_REVIEW  # still in review; the loop did not stall it

    st = sched.state.get("DM-001")
    assert st["needs_human"]["kind"] == "review_cap"
    assert st["review_rounds"] == 2

    items = [i for i in build_inbox(sched.store, sched) if i["group"] == "attention" and i["task"] == "DM-001"]
    assert items, "reaching the review cap should raise an Inbox card under 'Needs a decision'"
    it = items[0]
    assert it["pr"] == task.pr
    labels = [a["label"] for a in it["actions"]]
    assert "One more automated review" in labels
    assert "Send back with a note" in labels
    assert "Open PR" in labels

    # "one more review": raises the cap by one round and dispatches right away
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-ok")
    run = sched.review_again(task)
    assert run.mode == "review"
    assert sched.state.get("DM-001")["review_rounds"] == 2  # rolled back one, then re-incremented
    assert not sched.state.get("DM-001").get("needs_human")

    rep = sched.tick()  # reap the extra review: approve, no third cap-reached flag
    assert "DM-001 review: approve" in rep.transitions
    assert not sched.state.get("DM-001").get("needs_human")


def test_review_after_stale_base_rebase_round_does_not_count_toward_review_cap(sched, fake_github):
    """CG-139: a revise round that only resolved a stale-base rebase conflict (CG-131) by hand
    re-reads code the reviewer already approved, so the review that follows it must not count
    toward review.max_rounds — otherwise a busy merge queue rebasing several clean PRs in a row
    sends them all to the review cap at once for no code reason."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000}
    sched.tick()
    rep = sched.tick()  # reap work -> PR opened -> review dispatched (round 1)
    assert "DM-001(review)" in rep.dispatched
    sched.tick()  # reap review -> approve
    assert sched.state.get("DM-001")["review_rounds"] == 1
    sched.store.invalidate()
    assert sched.store.task("DM-001").status == Status.IN_REVIEW

    # Reproduce exactly the state reap.py's _handle_failed_checks (is_rebase=True) leaves behind
    # for a stale base whose mechanical rebase failed to apply cleanly: feedback to resolve the
    # conflict by hand, flagged as a rebase round rather than a fix the worker was asked to make.
    st = sched.state.get("DM-001")
    st["pending_feedback"] = "- **garden**: resolve the rebase conflict by hand."
    st["pending_feedback_rebase"] = True
    sched.state.save()
    task = sched.store.task("DM-001")
    task.status = Status.CHANGES_REQUESTED
    sched.store.save(task)

    rep = sched.tick()  # dispatches the exempt revise round (rebases counter, not revisions)
    assert "DM-001(revise)" in rep.dispatched
    assert sched.state.get("DM-001")["rebases"] == 1
    assert sched.state.get("DM-001")["revisions"] == 0

    rep = sched.tick()  # reap the revise round -> checks pass -> pushed -> review dispatched again
    assert "DM-001(review)" in rep.dispatched
    # the review ran, but it must not have counted: still 1, not 2
    assert sched.state.get("DM-001")["review_rounds"] == 1

    sched.tick()  # reap the free review -> approve
    assert sched.state.get("DM-001")["review_rounds"] == 1
    sched.store.invalidate()
    assert sched.store.task("DM-001").status == Status.IN_REVIEW
    assert not sched.state.get("DM-001").get("needs_human")
