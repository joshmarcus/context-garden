import copy
import hashlib
import json
from pathlib import Path

import pytest

from garden.brief import build_brief
from garden.inbox import build_inbox
from garden.model import Status
from garden.now1 import strip_for_run
from garden.review import (
    ambiguous_unverified,
    enforce_criteria_verdict,
    feedback_from_review,
    feedback_with_operator_note,
    interaction_evidence_gaps,
    interaction_requirement,
    parse_review,
    review_brief,
    review_item_id,
    review_to_markdown,
    validation_plan,
    visual_source_digest,
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
    review_run = sched._run_by_id(sched.store.task("DM-001"), run_id)
    assert st.get("last_review_head") == review_run.env_snapshot["review_head"]

    # a new process on the same garden: state.json is the only thing that survives it
    fresh = Scheduler(Store(sched.store.root), github=fake_github, log=print)
    st2 = fresh.state.get("DM-001")
    assert st2.get("last_review", {}).get("verdict") == "approve"
    assert st2.get("last_review_run") == run_id
    assert st2.get("last_review_head") == review_run.env_snapshot["review_head"]


@pytest.mark.parametrize("failure", ["unclaimed timeout", "admission timeout", "startup environment failure"])
def test_unstarted_review_failures_keep_one_durable_current_head_continuation(
        sched, fake_github, failure):
    """The three observed pre-claim failures refund the round and survive restart once."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000,
                                "recovery_attempts": 2, "recovery_backoff_seconds": 300}
    sched.tick()
    sched.tick()
    task = sched.store.task("DM-001")
    st = sched.state.get(task.id)
    run = next(r for r in sched.runs.runs_for(task.id) if r.run_id == st["review_run"])
    for name in ("stdout.json", "final.md", "remote_result.json"):
        (run.path / name).unlink(missing_ok=True)
    run.runner = "remote"
    run.pid = None
    run.host = ""
    run.claimed_at = ""
    run.status = "timeout"
    run.finished_at = "2026-09-08T16:00:00+00:00"
    run.error = failure
    run.save()

    rep = TickReport()
    assert sched.reap_review(task, rep)
    assert st["review_rounds"] == 0
    assert st["pending_reviews"] == [{"kind": "review", "count_round": True}]
    assert st["review_recovery"]["started"] is False
    assert st["review_recovery"]["head"] == run.env_snapshot["review_head"]
    assert [item.get("kind") for item in build_inbox(sched.store, sched)].count("review_recovery") == 1

    fresh = Scheduler(Store(sched.store.root), github=fake_github, log=print)
    fresh.dispatch_ready(TickReport())
    recovered = fresh.state.get(task.id)
    assert recovered["pending_reviews"] == [{"kind": "review", "count_round": True}]
    assert recovered["review_recovery"]["attempts"] == 1


def test_claimed_review_recovery_preserves_logical_round_and_exhausts_to_decision(sched, fake_github):
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "max_diff_chars": 60000,
                                "recovery_attempts": 1, "recovery_backoff_seconds": 0}
    sched.tick()
    sched.tick()
    task = sched.store.task("DM-001")
    st = sched.state.get(task.id)
    run = next(r for r in sched.runs.runs_for(task.id) if r.run_id == st["review_run"])
    run.runner = "remote"
    run.claimed_at = "2026-09-08T16:00:00+00:00"
    run.status = "timeout"
    run.error = "claimed worker timed out"
    run.save()

    assert sched.reap_review(task, TickReport())
    assert st["review_rounds"] == 1
    assert st["pending_reviews"] == [{"kind": "review", "count_round": False}]
    st["review_run"] = "missing-after-restart"
    rep = TickReport()
    assert sched.reap_review(task, rep)
    assert st["needs_human"]["kind"] == "review_recovery_exhausted"
    assert not st.get("pending_reviews")
    assert not st.get("last_review")


def test_timed_out_review_applies_a_collected_verdict_once(sched, monkeypatch):
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"]["enabled"] = True
    for _ in range(5):
        sched.tick()
        if sched.state.get("DM-001").get("review_run"):
            break
    task = sched.store.task("DM-001")
    st = sched.state.get(task.id)
    run = sched._run_by_id(task, st["review_run"])
    assert run is not None
    (run.path / "exit_code").unlink()
    collected = []
    runner_type = type(sched.runner_for(task, run.runner, run.harness))
    original_collect = runner_type.collect

    def collect_once(self, finished):
        collected.append(finished.run_id)
        return original_collect(self, finished)

    def timeout(finished, _runner):
        finished.status = "timeout"
        finished.error = "claimed worker timed out"
        finished.save()
        return True

    monkeypatch.setattr(runner_type, "collect", collect_once)
    monkeypatch.setattr(sched, "_finished_or_timed_out", timeout)

    assert sched.reap_review(task, TickReport())
    assert collected == [run.run_id]
    assert st["last_review_run"] == run.run_id
    assert st["last_review"]["verdict"] == "approve"
    assert st["review_rounds"] == 1
    assert not st.get("review_run")
    assert not st.get("pending_reviews")
    assert not st.get("review_recovery")
    assert sched.reap_review(task, TickReport()) is False
    assert collected == [run.run_id]


def test_review_audit_preserves_the_round_of_a_lost_started_review(sched):
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    sched.cfg.data["review"].update({"enabled": True, "max_rounds": 1})
    st = sched.state.get(task.id)
    st.update({"head_sha": "current", "review_rounds": 1})
    lost = sched.runs.new_run(task.id, "local", mode="review")
    lost.status = "timeout"
    lost.pid = 123
    lost.error = "host stopped after claim"
    lost.env_snapshot = {"review_head": "current", "count_round": True}
    lost.save()

    rep = TickReport()
    sched._audit_review_continuations(sched.store.tasks(), rep)

    assert st["review_rounds"] == 1
    assert st["pending_reviews"] == [{"kind": "review", "count_round": False}]
    assert st["review_recovery"]["started"] is True
    assert st["review_recovery"]["last_run"] == lost.run_id
    assert rep.transitions == ["DM-001 review recovery queued (1/2)"]


@pytest.mark.parametrize("event_already_emitted", [False, True])
def test_review_audit_applies_a_lost_terminal_result_once(
        sched, event_already_emitted):
    from garden import gitops

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    sched.cfg.data["review"].update({"enabled": True, "max_rounds": 1})
    wt = gitops.prepare_worktree(
        sched.repo_for(task), sched.worktree_for(task),
        task.branch or task.default_branch(), sched.base_for(task))
    head = gitops.head_sha(wt)
    st = sched.state.get(task.id)
    st.update({"head_sha": head, "review_rounds": 1})
    completed = sched.runs.new_run(task.id, "local", mode="review")
    completed.status = "done"
    completed.result = {"verdict": "approve", "summary": "valid terminal verdict",
                        "criteria": [], "findings": []}
    completed.env_snapshot = {"review_head": head, "count_round": True}
    completed.save()
    if event_already_emitted:
        sched.events.emit("run_finished", task.id, run=completed.run_id,
                          mode="review", status="approve")

    first = TickReport()
    sched._audit_review_continuations(sched.store.tasks(), first)
    sched._audit_review_continuations(sched.store.tasks(), first)

    assert st["last_review_run"] == completed.run_id
    assert st["last_review"]["verdict"] == "approve"
    assert not st.get("review_run")
    assert not st.get("pending_reviews")
    finished = [event for event in sched.events.read(task_id=task.id, kinds=["run_finished"])
                if event.get("run") == completed.run_id]
    assert len(finished) == 1


def test_served_tick_recovers_an_unstarted_review_with_original_round_intent(sched, fake_github):
    import yaml
    from fastapi.testclient import TestClient

    from garden.web.app import create_app

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    config_path = sched.store.root / "garden.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["review"].update({"enabled": True, "max_rounds": 2, "recovery_attempts": 2,
                             "recovery_backoff_seconds": 300})
    config_path.write_text(yaml.safe_dump(config))
    st = sched.state.get(task.id)
    st.update({"head_sha": "current", "review_rounds": 1})
    lost = sched.runs.new_run(task.id, "local", mode="review")
    lost.status = "failed"
    lost.error = "startup failed before execution was confirmed"
    lost.env_snapshot = {"review_head": "current", "count_round": True}
    lost.save()
    sched.state.save()

    client = TestClient(create_app(Store(sched.store.root), watch=False, github=fake_github))
    response = client.post("/tick")

    assert response.status_code == 200
    recovered = Scheduler(Store(sched.store.root), github=fake_github).state.get(task.id)
    assert recovered["review_rounds"] == 0
    assert recovered["pending_reviews"] == [{"kind": "review", "count_round": True}]
    assert recovered["review_recovery"]["started"] is False
    assert recovered["review_recovery"]["last_run"] == lost.run_id


def test_review_audit_restores_a_lost_additional_round_once(sched):
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2}
    sched.cfg.data["products"][task.product]["automerge_min_review_rounds"] = 2
    st = sched.state.get(task.id)
    st.update({"head_sha": "current", "review_rounds": 1, "last_review": {"verdict": "approve"}})
    prior = sched.runs.new_run(task.id, "local", mode="review")
    prior.status = "done"
    prior.env_snapshot = {"review_head": "current"}
    prior.result = {"verdict": "approve"}
    prior.save()
    st["last_review_run"] = prior.run_id
    assert sched.cfg.product(task.product)["automerge_min_review_rounds"] == 2
    assert sched._review_round_pending(st)
    assert sched.store.tasks()[task.id].status == Status.IN_REVIEW

    rep = TickReport()
    sched._audit_review_continuations(sched.store.tasks(), rep)
    sched._audit_review_continuations(sched.store.tasks(), rep)

    assert st["pending_reviews"] == [{"kind": "review", "count_round": True}]
    assert rep.transitions == ["DM-001 missing review continuation restored"]


def test_review_audit_replaces_stale_head_recovery_with_a_fresh_counted_round(sched, fake_github):
    from garden import gitops

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.pr = fake_github.create_pr(
        "test/demo", task.branch or task.default_branch(), task.default_branch(), task.title, "").url
    sched.store.save(task)
    sched.cfg.data["review"].update({"enabled": True, "max_rounds": 2})
    wt = gitops.prepare_worktree(
        sched.repo_for(task), sched.worktree_for(task),
        task.branch or task.default_branch(), sched.base_for(task))
    current = gitops.head_sha(wt)
    st = sched.state.get(task.id)
    st.update({
        "head_sha": current,
        "review_rounds": 0,
        "pending_reviews": [{"kind": "review", "count_round": False}],
        "review_recovery": {
            "head": "obsolete-head",
            "attempts": 1,
            "limit": 2,
            "retry_at": "2999-01-01T00:00:00+00:00",
            "reason": "old head review was lost",
            "owner": "scheduler",
            "started": True,
            "last_run": "old-review",
        },
    })

    rep = TickReport()
    sched._audit_review_continuations(sched.store.tasks(), rep)
    sched._audit_review_continuations(sched.store.tasks(), rep)

    assert st["pending_reviews"] == [{"kind": "review", "count_round": True}]
    assert st["review_recovery"]["head"] == current
    assert st["review_recovery"]["attempts"] == 0
    assert rep.transitions == [
        "DM-001 stale review recovery discarded",
        "DM-001 missing review continuation restored",
    ]

    sched._drain_pending_reviews(sched.store.tasks(), rep)
    fresh = sched._run_by_id(task, st["review_run"])
    assert fresh is not None
    assert fresh.env_snapshot["review_head"] == current
    assert fresh.env_snapshot["count_round"] is True
    assert st["review_rounds"] == 1
    assert not st.get("pending_reviews")
    assert not any(str((run.env_snapshot or {}).get("review_head") or "") == "obsolete-head"
                   for run in sched.runs.runs_for(task.id))

    sched._audit_review_continuations(sched.store.tasks(), rep)
    sched._drain_pending_reviews(sched.store.tasks(), rep)
    assert st["review_run"] == fresh.run_id
    assert len([run for run in sched.runs.runs_for(task.id) if run.mode == "review"]) == 1


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


def test_queued_reviews_take_shared_capacity_before_lower_priority_work(sched):
    """CG-372: a queued critical review claims a released local slot before ready work.

    Reviews, workers and detached checks share ``resources.max_parallel``.  This is
    deliberately an admission test rather than a reservation: only an eligible queued
    review starts, and the usual one-slot limits still apply.
    """
    from garden.scheduler import TickReport

    critical = sched.store.task("DM-001")
    critical.priority = 0
    critical.status = Status.IN_REVIEW
    sched.store.save(critical)
    sched.state.get(critical.id)["pending_reviews"] = [{"kind": "review", "count_round": True}]

    lower = sched.store.task("DM-002")
    lower.depends_on = []
    lower.priority = 3
    sched.store.save(lower)

    sched.cfg.data["max_parallel"] = 1
    sched.cfg.data["review_parallel"] = 1
    sched.cfg.data["resources"] = {"max_parallel": 1}
    rep = TickReport()
    sched.dispatch_ready(rep)

    assert rep.dispatched == ["DM-001(review)"], rep.errors
    assert sched.review_slots_free() == 0
    assert not any(run.task_id == lower.id and run.mode == "work" for run in sched.runs.active())
    assert not sched.state.get(critical.id).get("pending_reviews")


def test_pending_reviews_admit_remote_independently_of_occupied_local_capacity(sched):
    """A local hold is per-backend; the reviewer ceiling remains global."""
    local = sched.store.task("DM-001")
    remote = sched.store.task("DM-002")
    for task, order in ((local, 10), (remote, 20)):
        task.status = Status.IN_REVIEW
        task.priority = 0
        task.order = order
        task.depends_on = []
        sched.store.save(task)
        sched.state.get(task.id)["pending_reviews"] = [
            {"kind": "review", "count_round": True},
        ]
    remote.runner = "remote"
    sched.store.save(remote)
    sched.cfg.data["review_parallel"] = 1
    sched.cfg.data["resources"] = {"max_parallel": 1}

    occupant = sched.runs.new_run("occupied-local", "local", mode="work")
    occupant.status = "running"
    occupant.save()
    rep = TickReport()
    sched._drain_pending_reviews(sched.store.tasks(), rep)

    assert rep.dispatched == ["DM-002(review)"], rep.errors
    remote_run = sched.runs.latest(remote.id)
    assert remote_run is not None and remote_run.runner == "remote" and remote_run.mode == "review"
    assert sched.state.get(remote.id)["review_rounds"] == 1
    assert sched.state.get(local.id)["pending_reviews"] == [
        {"kind": "review", "count_round": True},
    ]
    assert sched.state.get(local.id).get("review_rounds", 0) == 0
    assert sched.review_wait_reason(local)[0] == "slots"  # the remote run holds the global ceiling

    # Releasing only the reviewer ceiling still leaves the local backend accurately held.
    remote_run.status = "done"
    remote_run.save()
    assert sched.review_wait_reason(local)[0] == "local"
    held = TickReport()
    sched._drain_pending_reviews(sched.store.tasks(), held)
    assert held.dispatched == [] and held.errors == []
    assert sched.state.get(local.id).get("review_rounds", 0) == 0

    # Once local physical capacity recovers, the original queued round starts exactly once.
    occupant.status = "done"
    occupant.save()
    recovered = TickReport()
    sched._drain_pending_reviews(sched.store.tasks(), recovered)
    assert recovered.dispatched == ["DM-001(review)"], recovered.errors
    local_run = sched.runs.latest(local.id)
    assert local_run is not None and local_run.runner == "local" and local_run.mode == "review"
    assert sched.state.get(local.id)["review_rounds"] == 1
    assert not sched.state.get(local.id).get("pending_reviews")


def test_pending_remote_persona_ignores_occupied_local_capacity(sched):
    """A queued persona uses its task's remote backend, not local review capacity."""
    local = sched.store.task("DM-001")
    local.status = Status.IN_REVIEW
    local.priority = 0
    local.order = 10
    sched.store.save(local)
    sched.state.get(local.id)["pending_reviews"] = [
        {"kind": "review", "count_round": True},
    ]

    remote = sched.store.task("DM-002")
    remote.status = Status.IN_REVIEW
    remote.priority = 0
    remote.order = 20
    remote.depends_on = []
    remote.runner = "remote"
    remote.branch = remote.default_branch()
    sched.store.save(remote)
    sched.state.get(remote.id)["pending_reviews"] = [
        {"kind": "persona", "name": "security", "required": False},
    ]

    sched.cfg.data["review_parallel"] = 1
    sched.cfg.data["resources"] = {"max_parallel": 1}
    occupant = sched.runs.new_run("occupied-local", "local", mode="work")
    occupant.status = "running"
    occupant.save()

    rep = TickReport()
    sched._drain_pending_reviews(sched.store.tasks(), rep)

    assert rep.dispatched == ["DM-002(persona:security)"], rep.errors
    persona_run = sched.runs.latest(remote.id)
    assert persona_run is not None
    assert (persona_run.runner, persona_run.mode) == ("remote", "persona")
    assert sched.state.get(local.id)["pending_reviews"] == [
        {"kind": "review", "count_round": True},
    ]
    assert sched.state.get(local.id).get("review_rounds", 0) == 0


def test_review_atomic_local_admission_race_requeues_without_charging_round(sched, monkeypatch):
    from garden.scheduler.resources import ResourcePressureError

    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    item = {"kind": "review", "count_round": True}

    def lose_atomic_admission(*_args, **_kwargs):
        raise ResourcePressureError("slot claimed")

    monkeypatch.setattr(sched, "dispatch_review", lose_atomic_admission)
    rep = TickReport()
    sched._dispatch_or_defer_reviews(task, [item], rep)

    assert rep.dispatched == [] and rep.errors == []
    assert sched.state.get(task.id)["pending_reviews"] == [item]
    assert sched.state.get(task.id).get("review_rounds", 0) == 0



def test_queued_critical_review_precedes_a_lower_priority_check(sched):
    from garden.scheduler import TickReport
    from garden.scheduler.resources import ResourcePressureError

    critical = sched.store.task("DM-001")
    critical.priority = 0
    critical.status = Status.IN_REVIEW
    sched.store.save(critical)
    sched.state.get(critical.id)["pending_reviews"] = [{"kind": "review"}]
    lower = sched.store.task("DM-002")
    lower.priority = 3
    sched.store.save(lower)
    sched.cfg.data["review_parallel"] = 1
    sched.cfg.data["resources"] = {"max_parallel": 1}
    rep = TickReport()
    with pytest.raises(ResourcePressureError):
        sched._dispatch_check_run(lower, worktree=sched.worktree_for(lower),
                                  branch=lower.default_branch(), base="main", specs=[],
                                  stage="pre_pr", cont={}, rep=rep)
    assert rep.dispatched == ["DM-001(review)"]
    assert not sched.state.get(lower.id).get("check_run")
    assert not any(r.task_id == lower.id and r.mode == "check" for r in sched.runs.active())

def test_queued_reviews_use_task_order_to_break_equal_priority_ties(sched):
    """Queued reviews are strict by priority and deterministic by task order then id."""
    from garden.scheduler import TickReport

    first = sched.store.task("DM-001")
    second = sched.store.task("DM-002")
    for task, order in ((first, 20), (second, 10)):
        task.priority = 0
        task.order = order
        task.status = Status.IN_REVIEW
        task.depends_on = []
        sched.store.save(task)
        sched.state.get(task.id)["pending_reviews"] = [{"kind": "review", "count_round": True}]

    sched.cfg.data["review_parallel"] = 1
    rep = TickReport()
    sched.dispatch_ready(rep)

    assert rep.dispatched == ["DM-002(review)"], rep.errors
    assert sched.state.get(first.id)["pending_reviews"] == [{"kind": "review", "count_round": True}]



def test_queued_review_explanation_matches_equal_priority_drain_order(sched):
    first = sched.store.task("DM-001")
    second = sched.store.task("DM-002")
    for task, order in ((first, 10), (second, 20)):
        task.priority = 0
        task.order = order
        task.status = Status.IN_REVIEW
        sched.store.save(task)
        sched.state.get(task.id)["pending_reviews"] = [{"kind": "review"}]
    assert sched._queued_review_predecessor(first) is None
    assert sched._queued_review_predecessor(second).id == first.id

def test_new_equal_priority_review_waits_for_an_established_queue_member(sched):
    """A task cannot repeatedly reclaim the slot while a band-mate is already queued."""
    from garden.scheduler import TickReport

    first = sched.store.task("DM-001")
    second = sched.store.task("DM-002")
    first.priority = second.priority = 0
    first.status = second.status = Status.IN_REVIEW
    sched.store.save(first)
    sched.store.save(second)
    sched.state.get(second.id)["pending_reviews"] = [{"kind": "review", "count_round": True}]

    rep = TickReport()
    sched._dispatch_or_defer_reviews(first, [{"kind": "review", "count_round": True}], rep)

    assert rep.dispatched == []
    assert sched.state.get(first.id)["pending_reviews"] == [{"kind": "review", "count_round": True}]
    assert sched.state.get(second.id)["pending_reviews"] == [{"kind": "review", "count_round": True}]


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


def test_revision_feedback_retains_long_findings_and_criterion_only_rejections(garden):
    long_fix = "preserve every detail " * 799 + "final detail"
    review = {
        "summary": "A complete review record",
        "criteria": [{"criterion": "The rejected outcome is explained.", "met": False,
                      "reason": "the empty result has no source link",
                      "evidence": "test_revision_feedback_retains_long_findings"}],
        "findings": [{"severity": "blocking", "file": "src/garden/review.py", "line": 677,
                      "summary": "The finding remains actionable", "fix": long_fix}],
    }

    feedback = feedback_from_review(review, run_id="DM-001-review-2", source_head="a" * 40)

    assert "DM-001-review-2" in feedback and "a" * 40 in feedback
    assert "The rejected outcome is explained." in feedback
    assert "the empty result has no source link" in feedback
    assert "test_revision_feedback_retains_long_findings" in feedback
    assert long_fix in feedback
    store = Store(garden)
    brief = build_brief(store, store.task("DM-001"), review_feedback=feedback)
    assert long_fix in brief.text
    assert "test_revision_feedback_retains_long_findings" in brief.text


def test_operator_triage_and_recovery_notes_preserve_review_provenance(sched):
    task = sched.store.task("DM-001")
    task.pr = "https://example.test/pull/1"
    sched.store.save(task)
    review = {"summary": "Prior review", "criteria": [{"criterion": "Original criterion", "met": False,
              "reason": "not yet verified", "evidence": "review evidence"}],
              "findings": [{"severity": "blocking", "file": "a.py", "line": 4,
                            "summary": "Original finding", "fix": "Make the original fix."},
                           {"severity": "nit", "summary": "Finding without a supplied fix"}],
              "description_ok": False, "description_feedback": "Old description feedback",
              "improvements": [{"area": "docs", "suggestion": "Old optional suggestion"}]}
    source_run = sched.runs.new_run(task.id, "local", mode="review")
    source_run.status = "done"
    source_run.env_snapshot = {"review_head": "a" * 40}
    source_run.save()
    st = sched.state.get(task.id)
    st.update(last_review=review, last_review_run=source_run.run_id, head_sha="b" * 40)

    sched.triage(task, changes="Use the new handoff instead.")
    triage = st["pending_feedback"]
    assert "Operator triage note" in triage and "Applicable automated review record" in triage
    assert "remains applicable and this note supplements it" in triage
    assert "Original finding" in triage and "Original criterion" in triage
    assert source_run.run_id in triage and "a" * 40 in triage
    assert "b" * 40 not in triage
    assert st["last_review_head"] == "a" * 40

    task.status = Status.AWAITING_TRIAGE
    sched.store.save(task)
    sched.triage(task, changes="The prior review is resolved.", supersede_review=True)
    superseded = st["pending_feedback"]
    assert "Superseded automated review record" in superseded
    assert "do not repeat its requests" in superseded
    assert "Original finding" in superseded and "a" * 40 in superseded
    assert "Applicable automated review" not in superseded
    assert "Findings to address" not in superseded
    assert "Automated review provenance" in superseded
    assert "Recorded findings" in superseded
    assert "Recorded PR description assessment" in superseded
    assert "Recorded optional improvements" in superseded
    assert "put the new description" not in superseded
    assert "Take or decline each item" not in superseded
    assert "determine the smallest correct change" not in superseded

    recovery = feedback_with_operator_note(
        review, "Retry after the operator cleared the stop.", kind="recovery",
        run_id=source_run.run_id, source_head="a" * 40,
    )
    assert "Operator recovery note" in recovery
    assert "remains applicable and this note supplements it" in recovery
    assert "Original finding" in recovery and "review evidence" in recovery


def test_operator_triage_resolves_selected_finding_and_keeps_unmatched_review(sched):
    task = sched.store.task("DM-001")
    task.pr = "https://example.test/pull/1"
    sched.store.save(task)
    fixed = {"severity": "blocking", "file": "fixed.py", "line": 4,
             "summary": "Already fixed", "fix": "Keep the correction."}
    outstanding = {"severity": "blocking", "file": "open.py", "line": 9,
                   "summary": "Still outstanding", "fix": "Implement this change."}
    review = {"summary": "Mixed review", "criteria": [],
              "findings": [fixed, outstanding]}
    st = sched.state.get(task.id)
    st.update(last_review=review, last_review_run="review-1", last_review_head="a" * 40)
    fixed_id = review_item_id("finding", fixed)
    outstanding_id = review_item_id("finding", outstanding)

    sched.triage(task, changes="The first finding is resolved.",
                 resolve_review_items=[fixed_id])

    feedback = st["pending_feedback"]
    resolved, applicable = feedback.split("## Applicable automated review record", 1)
    assert "Resolved automated review items" in resolved
    assert "Already fixed" in resolved and fixed_id in resolved
    assert "Still outstanding" not in resolved
    assert "Applicable automated review" not in resolved
    assert "Findings to address" not in resolved
    assert "Automated review provenance" in resolved
    assert "Recorded findings" in resolved
    assert "Still outstanding" in applicable and outstanding_id in applicable
    assert "Already fixed" not in applicable
    assert "Applicable automated review" in applicable
    assert "Findings to address" in applicable
    assert "Unmatched items remain applicable" in feedback

    task.status = Status.AWAITING_TRIAGE
    sched.store.save(task)
    with pytest.raises(RuntimeError, match="unknown review item"):
        sched.triage(task, changes="bad selection",
                     resolve_review_items=["finding:000000000000"])
    with pytest.raises(RuntimeError, match="cannot be combined"):
        sched.triage(task, changes="ambiguous operation", supersede_review=True,
                     resolve_review_items=[fixed_id])


def test_served_triage_and_recovery_handoffs_preserve_applicable_review(sched, fake_github):
    from fastapi.testclient import TestClient

    from garden.web.app import create_app

    task = sched.store.task("DM-001")
    task.status = Status.AWAITING_TRIAGE
    task.pr = "https://example.test/pull/101"
    sched.store.save(task)
    review = {"summary": "Review from the examined head", "criteria": [],
              "findings": [{"severity": "blocking", "file": "a.py", "line": 4,
                            "summary": "Keep this finding", "fix": "Apply the retained fix."}]}
    st = sched.state.get(task.id)
    st.update(last_review=review, last_review_run="DM-001-review-1",
              last_review_head="a" * 40, head_sha="b" * 40)
    sched.state.save()
    client = TestClient(create_app(
        Store(sched.store.root), watch=False, host="testserver", github=fake_github))

    response = client.post(
        "/tasks/DM-001/triage-changes", data={"note": "Also cover the empty case."},
        headers={"referer": "http://testserver/tasks/DM-001"}, follow_redirects=False)
    assert response.status_code == 303
    brief = client.get("/tasks/DM-001/brief?revise=true")
    assert brief.status_code == 200
    assert "Operator triage note" in brief.text
    assert "remains applicable and this note supplements it" in brief.text
    assert "Keep this finding" in brief.text and "a" * 40 in brief.text
    assert "b" * 40 not in brief.text

    recovered = Scheduler(Store(sched.store.root), github=fake_github)
    task = recovered.store.task(task.id)
    task.status = Status.IN_REVIEW
    recovered.store.save(task)
    state = recovered.state.get(task.id)
    state.pop("pending_feedback", None)
    recovered._set_needs_human(task, "stall", "simulated failed handoff")
    recovered.state.save()
    response = client.post(
        "/tasks/DM-001/retry", headers={"referer": "http://testserver/tasks/DM-001"},
        follow_redirects=False)
    assert response.status_code == 303
    recovery_brief = client.get("/tasks/DM-001/brief?revise=true")
    assert recovery_brief.status_code == 200
    assert "Operator recovery note" in recovery_brief.text
    assert "Keep this finding" in recovery_brief.text and "a" * 40 in recovery_brief.text

    no_review = Scheduler(Store(sched.store.root), github=fake_github)
    second = no_review.store.task("DM-002")
    second.status = Status.AWAITING_TRIAGE
    second.pr = "https://example.test/pull/102"
    no_review.store.save(second)
    response = client.post(
        "/tasks/DM-002/triage-changes", data={"note": "Handle the empty state."},
        headers={"referer": "http://testserver/tasks/DM-002"}, follow_redirects=False)
    assert response.status_code == 303
    empty_brief = client.get("/tasks/DM-002/brief?revise=true")
    assert empty_brief.status_code == 200
    assert "Operator triage note" in empty_brief.text
    assert "Applicable automated review" not in empty_brief.text


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
    assert parser["reasons"] == [{"item": "no rendered evidence", "reason": "no rendered or lifecycle behavior changed"}]
    assert parser["checks"] == [{"item": "configured pre-PR checks",
                                  "reason": "parser or brief behavior changed without rendered behavior"}]

    page = validation_plan(["src/garden/web/pages/task.py"], "Tighten task layout",
                           visual_scope={"behavior": "Tighter task layout"})
    assert page["pages"] == ["task"]
    assert page["interaction"] is True

    shared = validation_plan(["src/garden/web/templates/base.html"], "Update shared rail style",
                             visual_scope={"behavior": "Updated shared rail style"})
    assert shared["pages"] == ["board", "inbox"]
    assert any("representative consumers" in reason["reason"] for reason in shared["reasons"])
    assert shared["checks"] == [{"item": "configured pre-PR checks",
                                  "reason": "changed behavior requires the configured pre-PR checks"}]


def test_validation_plan_requires_bounded_inspection_for_unknown_ui_scope():
    plan = validation_plan(["src/garden/web/widgets/unmapped.py"], "New component")

    assert plan["pages"] == []
    assert plan["unknown_ui"] == ["src/garden/web/widgets/unmapped.py"]
    assert any(reason["item"] == "bounded UI inspection" for reason in plan["reasons"])


@pytest.mark.parametrize("changed", [
    ["src/garden/web/app.py"],
    ["src/garden/web/common.py"],
    ["src/garden/web/actions/control.py"],
    ["docs/design/captures/board-1280-light.png"],
    ["docs/design/snapshot.json"],
])
def test_validation_plan_does_not_infer_visual_evidence_from_nonvisual_or_generated_paths(changed):
    plan = validation_plan(changed, "No rendered or visual behavior changes")

    assert plan["pages"] == []


def test_validation_plan_requires_one_page_capture_for_declared_layout_change():
    plan = validation_plan(["src/garden/web/pages/task.py"], "Tighten task layout",
                           visual_scope={"behavior": "Tighter task layout"})

    assert plan["pages"] == ["task"]
    assert "visible behavior" in plan["reasons"][0]["reason"]


def test_shared_path_without_visible_behavior_keeps_functional_evidence_without_captures():
    plan = validation_plan(["src/garden/web/app.py", "src/garden/web/templates/base.html"],
                           "Add authentication route wiring")

    assert plan["pages"] == []
    assert any(row["item"] == "no screenshot scope" for row in plan["reasons"])


def test_declared_visual_shared_app_change_uses_representative_consumers():
    plan = validation_plan(["src/garden/web/app.py"], "Render a visible shared navigation rail",
                           visual_scope={"behavior": "Visible shared navigation rail"})

    assert plan["pages"] == ["board", "inbox"]


def test_visual_source_digest_ignores_generated_capture_artifacts(tmp_path):
    page = tmp_path / "src/garden/web/pages/task.py"
    page.parent.mkdir(parents=True)
    page.write_text("VISIBLE = True\n")
    plan = validation_plan(["src/garden/web/pages/task.py"], "Tighten task layout",
                           visual_scope={"behavior": "Tighter task layout"})
    before = visual_source_digest(tmp_path, plan)
    capture = tmp_path / "docs/design/captures/task-1280-light.png"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"generated evidence")

    assert visual_source_digest(tmp_path, plan) == before


def test_review_brief_distinguishes_required_validation_from_available_captures(garden):
    store = Store(garden)
    plan = validation_plan(["src/garden/web/pages/task.py"], "Task layout", head="head-a",
                           visual_scope={"behavior": "Tighter task layout"})
    text = review_brief(store, store.task("DM-001"), branch="b", base="main", pr_title="T", pr_body="B",
                        diff="+x", max_diff_chars=1000,
                        captures=["/tmp/task-1280-light.png", "/tmp/inbox-1280-light.png"], plan=plan)

    assert '"pages": [\n    "task"\n  ]' in text
    assert "not the available" in text


def test_one_page_review_does_not_turn_available_captures_into_a_fourteen_page_demand(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Tighter task layout"}
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names",
                        lambda *_: ["src/garden/web/pages/task.py"])
    run = _review_after_completed_empty_replay(sched, task)

    assert run.env_snapshot["capture_pages"] == ["task"]
    assert run.env_snapshot["validation_plan"]["pages"] == ["task"]


def test_review_reuses_the_current_head_precheck_validation_plan(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Tighter task layout"}
    plan = validation_plan(["src/garden/web/pages/task.py"], "Task layout", head="head-a",
                           visual_scope=task.extra["visual_scope"])
    check = sched.runs.new_run(task.id, "local", mode="check")
    check.status = "done"
    check.env_snapshot = {"validation_plan": plan}
    check.save()
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/criteria.py"])
    monkeypatch.setattr("garden.scheduler.review.gitops.head_sha", lambda *_: "head-a")

    run = _review_after_completed_empty_replay(sched, task)

    assert run.env_snapshot["validation_plan"] == plan
    assert run.env_snapshot["capture_pages"] == ["task"]


def test_review_admits_trusted_capture_infrastructure_advisory_with_fallback_evidence(sched, monkeypatch):
    from garden import gitops

    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Tighter task layout"}
    sched.cfg.data.setdefault("review", {})["capture_infrastructure_policy"] = "advisory"
    wt = gitops.prepare_worktree(sched.repo_for(task), sched.worktree_for(task),
                                 task.branch or task.default_branch(), sched.base_for(task))
    head = gitops.head_sha(wt)
    plan = validation_plan(
        ["src/garden/web/pages/task.py"], task.title, head=head,
        visual_scope=task.extra["visual_scope"], capture_infrastructure_policy="advisory",
    )
    check = sched.runs.new_run(task.id, "local", mode="check")
    check.status = "done"
    check.env_snapshot = {"validation_plan": plan, "generated_ui_check_indices": [0]}
    check.result = {"checks": [{
        "name": "ui", "status": "fail", "summary": "UI check did not produce all PNGs",
        "captures": ["/tmp/task.html", "/tmp/task.txt"], "pages": ["task"],
        "capture_infrastructure": {
            "source": "garden.walkthrough:ui_check", "kind": "browser_unavailable",
            "diagnostic": "Chromium could not launch in the capture child",
        },
    }]}
    check.save()
    sched.state.get(task.id)["interaction_replay"] = {"head": head}
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names",
                        lambda *_: ["src/garden/web/pages/task.py"])

    review_run = sched.dispatch_review(task)
    brief = (review_run.path / "brief.md").read_text()

    assert review_run.env_snapshot["capture_pages"] == []
    assert review_run.env_snapshot["validation_check_current"] is True
    assert "UI capture infrastructure advisory" in brief
    assert "screenshot attempt remains recorded as failed" in brief
    assert "/tmp/task.html" in brief and "/tmp/task.txt" in brief
    assert "**ui**: fail" in brief


def test_review_keeps_application_ui_failure_blocking_under_advisory_policy(sched, monkeypatch):
    from garden import gitops

    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Tighter task layout"}
    sched.cfg.data.setdefault("review", {})["capture_infrastructure_policy"] = "advisory"
    wt = gitops.prepare_worktree(sched.repo_for(task), sched.worktree_for(task),
                                 task.branch or task.default_branch(), sched.base_for(task))
    head = gitops.head_sha(wt)
    plan = validation_plan(
        ["src/garden/web/pages/task.py"], task.title, head=head,
        visual_scope=task.extra["visual_scope"], capture_infrastructure_policy="advisory",
    )
    check = sched.runs.new_run(task.id, "local", mode="check")
    check.status = "done"
    check.env_snapshot = {"validation_plan": plan, "generated_ui_check_indices": [0]}
    check.result = {"checks": [{
        "name": "ui", "status": "fail", "failure_kind": "product",
        "summary": "decision-card walkthrough page is missing", "captures": [], "pages": ["task"],
    }]}
    check.save()
    sched.state.get(task.id)["interaction_replay"] = {"head": head}
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names",
                        lambda *_: ["src/garden/web/pages/task.py"])

    review_run = sched.dispatch_review(task)
    brief = (review_run.path / "brief.md").read_text()

    assert review_run.env_snapshot["capture_pages"] == ["task"]
    assert "UI capture infrastructure advisory" not in brief
    assert "decision-card walkthrough page is missing" in brief


@pytest.mark.parametrize("functional_failure", [False, True])
def test_pre_pr_collection_waives_only_trusted_capture_infrastructure(
    sched, monkeypatch, functional_failure,
):
    from garden import gitops

    task = sched.store.task("DM-001")
    sched.cfg.data.setdefault("review", {})["capture_infrastructure_policy"] = "advisory"
    worktree = gitops.prepare_worktree(
        sched.repo_for(task), sched.worktree_for(task), task.default_branch(), sched.base_for(task)
    )
    worker = sched.runs.new_run(task.id, "local", mode="work")
    worker.status = "done"
    worker.result = {"pr_body": "Focused behavior is covered by the served fixture."}
    worker.save()
    plan = validation_plan(
        ["src/garden/web/pages/task.py"], task.title, head=gitops.head_sha(worktree),
        visual_scope={"behavior": "Tighter task layout"},
        capture_infrastructure_policy="advisory",
    )
    check = sched.runs.new_run(task.id, "local", mode="check")
    check.status = "done"
    check.env_snapshot = {"validation_plan": plan, "generated_ui_check_indices": [0]}
    check.save()
    results = [{
        "name": "ui", "status": "fail", "summary": "UI check did not produce all PNGs",
        "captures": ["/tmp/task.html", "/tmp/task.txt"], "pages": ["task"],
        "capture_infrastructure": {
            "source": "garden.walkthrough:ui_check", "kind": "browser_unavailable",
            "diagnostic": "Chromium could not launch",
        },
    }]
    if functional_failure:
        results.append({"name": "focused behavior", "status": "fail", "summary": "served fixture returned 500"})
    opened = []
    blocked = []
    monkeypatch.setattr(sched, "_open_pr_after_checks", lambda *args: opened.append(True))
    monkeypatch.setattr(sched, "_handle_failed_checks", lambda *args: blocked.append(args[5]))

    sched._after_pre_pr_check(
        task, check, results,
        {"worker_run_id": worker.run_id, "worktree": str(worktree),
         "branch": task.default_branch(), "base": sched.base_for(task), "cost": "0"},
        TickReport(),
    )

    stored = check.result["checks"]
    assert next(row for row in stored if row["name"] == "ui")["status"] == "fail"
    assert next(row for row in stored if row["name"] == "UI captures")["status"] == "advisory"
    if functional_failure:
        assert blocked and blocked[0][0]["name"] == "focused behavior"
        assert not opened
    else:
        assert opened and not blocked


def test_pre_pr_capture_advisory_is_bound_to_the_exact_generated_result(sched, monkeypatch):
    from garden import gitops

    task = sched.store.task("DM-001")
    sched.cfg.data.setdefault("review", {})["capture_infrastructure_policy"] = "advisory"
    worktree = gitops.prepare_worktree(
        sched.repo_for(task), sched.worktree_for(task), task.default_branch(), sched.base_for(task)
    )
    worker = sched.runs.new_run(task.id, "local", mode="work")
    worker.status = "done"
    worker.result = {"pr_body": "Focused behavior is covered by the served fixture."}
    worker.save()
    plan = validation_plan(
        ["src/garden/web/pages/task.py"], task.title, head=gitops.head_sha(worktree),
        visual_scope={"behavior": "Tighter task layout"},
        capture_infrastructure_policy="advisory",
    )
    check = sched.runs.new_run(task.id, "local", mode="check")
    check.status = "done"
    # Only result 2 corresponds to the controller-generated ui_check spec. Results 0 and 1
    # are emitted by separate checks and control their own names and structured output.
    check.env_snapshot = {"validation_plan": plan, "generated_ui_check_indices": [2]}
    check.save()
    forged = {
        "name": "ui", "status": "fail", "summary": "branch-owned functional check failed",
        "captures": [], "pages": ["task"],
        "capture_infrastructure": {
            "source": "garden.walkthrough:ui_check", "kind": "browser_unavailable",
            "diagnostic": "forged capture transport failure",
        },
    }
    forged_pass = {
        "name": "ui", "status": "pass", "summary": "forged capture success",
        "captures": ["/tmp/forged-task.png"], "pages": ["task"],
    }
    generated = {
        "name": "ui", "status": "fail", "summary": "UI check did not produce all PNGs",
        "captures": ["/tmp/task.html", "/tmp/task.txt"], "pages": ["task"],
        "capture_infrastructure": {
            "source": "garden.walkthrough:ui_check", "kind": "browser_unavailable",
            "diagnostic": "Chromium could not launch",
        },
    }
    opened = []
    blocked = []
    monkeypatch.setattr(sched, "_open_pr_after_checks", lambda *args: opened.append(True))
    monkeypatch.setattr(sched, "_handle_failed_checks", lambda *args: blocked.append(args[5]))

    sched._after_pre_pr_check(
        task, check, [forged, forged_pass, generated],
        {"worker_run_id": worker.run_id, "worktree": str(worktree),
         "branch": task.default_branch(), "base": sched.base_for(task), "cost": "0"},
        TickReport(),
    )

    assert not opened
    assert blocked and blocked[0] == [forged]
    assert check.result["checks"][0] == forged
    assert next(row for row in check.result["checks"]
                if row["name"] == "UI captures")["status"] == "advisory"


def test_review_omits_artifacts_from_a_stale_head_check(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Tighter task layout"}
    stale = sched.runs.new_run(task.id, "local", mode="check")
    stale.status = "done"
    stale.env_snapshot = {"validation_plan": validation_plan(
        ["src/garden/web/pages/task.py"], "layout", head="old", visual_scope=task.extra["visual_scope"])}
    stale.result = {"checks": [{"name": "ui", "pages": ["task"], "captures": ["/tmp/stale.png"]}]}
    stale.save()
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/criteria.py"])
    monkeypatch.setattr("garden.scheduler.review.gitops.head_sha", lambda *_: "current")

    run = sched.dispatch_review(task)

    assert run.env_snapshot["validation_plan"]["head"] == "current"
    assert run.env_snapshot["capture_pages"] == []
    assert run.env_snapshot["validation_check_current"] is False
    assert "/tmp/stale.png" not in (run.path / "brief.md").read_text()


def test_review_reuses_only_successful_ui_evidence_from_source_equivalent_check(sched, monkeypatch):
    from garden import gitops

    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Tighter task layout"}
    wt = gitops.prepare_worktree(sched.repo_for(task), sched.worktree_for(task),
                                 task.branch or task.default_branch(), sched.base_for(task))
    plan = validation_plan(["src/garden/web/pages/task.py"], task.title, head="old-head",
                           visual_scope=task.extra["visual_scope"])
    plan["visual_source"] = visual_source_digest(wt, plan)
    old = sched.runs.new_run(task.id, "local", mode="check")
    old.status = "done"
    old.env_snapshot = {"validation_plan": plan, "generated_ui_check_indices": [1]}
    old.result = {"checks": [
        {"name": "lint", "status": "fail", "summary": "old failure"},
        {"name": "ui", "status": "pass", "pages": ["task"], "captures": ["/tmp/task.png"]},
    ]}
    old.save()
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names",
                        lambda *_: ["src/garden/web/pages/task.py"])

    run = _review_after_completed_empty_replay(sched, task)

    assert run.env_snapshot["validation_check_current"] is False
    assert '"name": "lint"' not in (run.path / "brief.md").read_text()
    assert "/tmp/task.png" in (run.path / "brief.md").read_text()


def test_worker_brief_carries_the_frozen_validation_plan(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Tighter task layout"}
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
        run = _review_after_completed_empty_replay(sched, task)
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
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: [path])
    task = sched.store.task("DM-001")
    run = _review_after_completed_empty_replay(sched, task)

    assert run.env_snapshot["interaction_required"] is True
    assert run.process_finished()
    sched.reap_review(task, TickReport())

    persisted = sched.runs.latest(task.id)
    assert persisted is not None
    assert persisted.result["verdict"] == "request_changes"
    assert "Running-app evidence incomplete" in persisted.result["findings"][-1]["summary"]


def test_reap_review_rejects_truthy_malformed_interaction_evidence(sched, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    run = _review_after_completed_empty_replay(sched, task)
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
    assert "affected interaction is missing or failed" in persisted.result["findings"][-1]["summary"]


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
    review = {"criteria": [{"criterion": "Empty prompt is clear"}], "interaction": {
        "head": "head-a", "environment": "disposable", "command": "garden qa --scripted",
        "states": states, "events": events, "artifacts": [str(artifact)], "automated_checks": ["pytest"],
        "unverified": [],
    }}
    assert interaction_evidence_gaps(review, required=True, scalability=False, expected_head="head-a") == []

    review["interaction"]["head"] = "old-head"
    review["interaction"]["states"]["failure_recovery"]["status"] = "fail"
    review["interaction"]["unverified"] = [{
        "scope": "required", "criterion": "Empty prompt is clear",
        "outcome": "empty prompt copy", "reason": "the empty response was not inspected",
    }]
    gaps = interaction_evidence_gaps(review, required=True, scalability=False, expected_head="head-a")
    assert any("stale" in gap for gap in gaps)
    assert any("failure/recovery" in gap for gap in gaps)
    assert any("required outcome remains unverified" in gap for gap in gaps)


def test_out_of_scope_limitation_is_visible_but_does_not_block_cg430_shape(tmp_path):
    review = {"verdict": "approve", "summary": "all required outcomes passed",
              "criteria": [{"criterion": f"criterion {index}", "met": True, "evidence": "passed"}
                           for index in range(5)],
              "interaction": _performed_interaction()}
    review["interaction"]["unverified"] = [{
        "scope": "limitation",
        "observation": "The September 8 launch failures were not reproduced; their cause remains unverified.",
    }]

    assert interaction_evidence_gaps(
        review, required=True, scalability=False, expected_head="head-a",
    ) == []
    assert enforce_criteria_verdict(review)["verdict"] == "approve"
    assert "September 8 launch failures" in review_to_markdown(review)


def test_required_gap_blocks_and_legacy_string_needs_clarification():
    review = {"interaction": _performed_interaction(), "criteria": []}
    review["interaction"]["unverified"] = [{
        "scope": "required", "affected_flow": "adopted-zombie cleanup",
        "outcome": "recovery was not observed", "reason": "fixture stopped after failure",
    }]
    assert any("affected flow: adopted-zombie cleanup" in gap for gap in interaction_evidence_gaps(
        review, required=True, scalability=False, expected_head="head-a",
        affected_flow="adopted-zombie cleanup",
    ))

    review["interaction"]["unverified"] = ["cause remains unknown"]
    assert ambiguous_unverified(review) == ["cause remains unknown"]
    assert interaction_evidence_gaps(
        review, required=True, scalability=False, expected_head="head-a",
    ) == []


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


def test_unlimited_review_cap_dispatches_beyond_the_former_limit_under_review_admission(sched, fake_github, monkeypatch):
    """A null cap keeps the normal work/review/revise lifecycle going past two rounds.

    The third dispatch proves that the unlimited setting is not merely accepted by the
    config helper: it still goes through the normal one-slot review admission path.
    """
    sched.cfg.data["stack"] = False
    sched.cfg.data["review_parallel"] = 1
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": None, "friction_after": 4,
                                "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-desc")

    sched.tick()  # dispatch work
    sched.tick()  # reap work -> review round 1
    sched.tick()  # reap review 1 -> revise
    sched.tick()  # reap revise -> review round 2
    sched.tick()  # reap review 2 -> revise
    rep = sched.tick()  # reap revise -> review round 3, beyond the former cap

    assert "DM-001(review)" in rep.dispatched
    st = sched.state.get("DM-001")
    assert st["review_rounds"] == 3
    assert len(sched.review_runs_active()) == 1
    assert sched.review_slots_free() == 0


def test_review_cap_recovery_keeps_actionable_feedback_after_a_scheduler_restart(sched, fake_github, monkeypatch):
    """`garden review` recovers a capped PR without losing the earlier actionable review."""
    sched.cfg.data["stack"] = False
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 1, "friction_after": None,
                                "max_diff_chars": 60000}
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-bad")

    sched.tick()  # dispatch work
    sched.tick()  # reap work -> review round 1
    sched.tick()  # reap review -> actionable feedback -> revise
    sched.tick()  # reap revise -> finite cap stop

    task = sched.store.task("DM-001")
    assert sched.state.get(task.id)["needs_human"]["kind"] == "review_cap"
    original_feedback = sched.state.get(task.id)["last_review"]["findings"][0]["summary"]
    assert original_feedback == "missing test"
    assert any(original_feedback in comment for comment in fake_github.comments)

    # Match the CLI's scheduler construction after the cap card has gone stale on disk.
    recovered = Scheduler(Store(sched.store.root), github=fake_github, log=print)
    task = recovered.store.task("DM-001")
    monkeypatch.setenv("FAKE_CLAUDE_REVIEW", "review-ok")
    run = recovered.review_again(task)
    assert run.mode == "review"
    assert not recovered.state.get(task.id).get("needs_human")
    assert recovered.state.get(task.id)["last_review"]["findings"][0]["summary"] == original_feedback
    assert any(original_feedback in comment for comment in fake_github.comments)

    rep = recovered.tick()
    assert "DM-001 review: approve" in rep.transitions
    assert any(original_feedback in comment for comment in fake_github.comments)


def test_unlimited_review_cap_records_one_loop_friction_signal(sched):
    """A soft threshold remains observable and non-blocking under an unlimited cap."""
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": None, "friction_after": 3}
    task = sched.store.task("DM-001")
    task.pr = "https://github.com/test/demo/pull/71"
    sched.store.save(task)
    st = sched.state.get(task.id)
    st.update(review_rounds=3, review_heads=["head-a", "head-b"],
              pending_feedback="- **reviewer**: add the missing assertion")

    assert sched._review_round_pending(st)
    sched._record_review_loop_friction(task, st)
    sched._record_review_loop_friction(task, st)

    friction = (sched.store.phase("demo", "p1").path / "docs" / "friction.md").read_text()
    assert friction.count("Review loop: 3 rounds") == 1
    assert "head-a, head-b" in friction
    assert "newly discovered defect" in friction
    assert not st.get("needs_human")
    signals = sched.events.read(task_id=task.id, kinds=["review_loop_friction"])
    assert len(signals) == 1


@pytest.mark.parametrize(("state", "feedback", "expected"), [
    ({"pending_feedback_rebase": True}, "fix it", "mechanical rebase/head change"),
    ({}, "read the screenshot capture", "stale/missing infrastructure evidence"),
    ({"pending_feedback_easy": True}, "rewrite the summary", "description-only correction"),
    ({}, "fix it", "newly discovered defect"),
    ({"review_feedback_history": ["fix it", "fix it"]}, "fix it", "repeated unaddressed finding"),
    ({}, "", "lost feedback/state transition"),
    ({"last_review": {"summary": "recorded"}}, "", "unknown"),
])
def test_review_loop_cause_classifies_each_supported_or_unknown_diagnosis(state, feedback, expected):
    assert Scheduler._review_loop_cause(state, feedback) == expected


def test_finite_review_cap_and_invalid_optional_values_are_unambiguous(sched):
    sched.cfg.data["review"] = {"enabled": True, "max_rounds": 2, "friction_after": None}
    st = sched.state.get("DM-001")
    st["review_rounds"] = 2
    assert not sched._review_round_pending(st)

    sched.cfg.data["review"]["max_rounds"] = 0
    with pytest.raises(ValueError, match="null or a positive integer"):
        sched.cfg.review_max_rounds()


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


def _review_after_completed_empty_replay(sched, task):
    from garden import gitops

    wt = gitops.prepare_worktree(sched.repo_for(task), sched.worktree_for(task),
                                task.branch or task.default_branch(), sched.base_for(task))
    sched.state.get(task.id)["interaction_replay"] = {"head": gitops.head_sha(wt)}
    return sched.dispatch_review(task)


def test_interaction_replay_defers_model_review_and_survives_collection(sched, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    check = sched.dispatch_review(task)
    assert check.mode == "check"
    assert not sched.state.get(task.id).get("review_run")
    assert sched.state.get(task.id).get("review_rounds", 0) == 0
    # Repeated requests while the detached check owns the slot must reuse that run.
    assert sched.dispatch_review(task).run_id == check.run_id
    assert len([r for r in sched.runs.runs_for(task.id) if r.mode == "check"]) == 1
    info = sched.state.get(task.id)["check_run"]
    manifest = Path(info["cont"]["manifest"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"fixture": "completed independently before reviewer"}')
    (check.path / "checks.json").write_text(json.dumps([
        {"name": "interaction replay", "status": "pass", "summary": "fixture"}]))
    (check.path / "exit_code").write_text("0")
    sched.state.save()
    sched = Scheduler(Store(sched.store.root), github=sched.github)
    task = sched.store.task(task.id)
    assert sched.reap_check(task, TickReport())
    digest = sched.state.get(task.id)["interaction_replay"]["digest"]
    assert digest == hashlib.sha256(manifest.read_bytes()).hexdigest()
    run = sched.dispatch_review(task)
    assert run.mode == "review"
    assert run.env_snapshot["interaction_replay_digest"] == digest
    assert sched.state.get(task.id)["review_rounds"] == 1


def test_interaction_replay_is_the_matching_current_validation_check(sched, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    monkeypatch.setattr("garden.scheduler.review.gitops.head_sha", lambda *_: "current-head")
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    stale = sched.runs.new_run(task.id, "local", mode="check")
    stale.status = "done"
    stale.env_snapshot = {"validation_plan": validation_plan(
        ["src/garden/review.py"], task.title, head="old-head")}
    stale.save()
    runner_type = type(sched.runner_for(task, "local"))
    monkeypatch.setattr(runner_type, "start_checks", lambda *_args: None)

    replay = sched.dispatch_review(task)
    assert replay.mode == "check"
    assert replay.env_snapshot["validation_plan"]["head"] == "current-head"
    info = sched.state.get(task.id)["check_run"]
    manifest = Path(info["cont"]["manifest"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"head":"current-head","events":[]}')
    (replay.path / "checks.json").write_text(json.dumps([
        {"name": "interaction replay", "status": "pass", "summary": "served journey passed"}]))
    (replay.path / "exit_code").write_text("0")
    assert sched.reap_check(task, TickReport())

    review = sched.dispatch_review(task)
    assert review.mode == "review"
    assert review.env_snapshot["validation_plan"]["head"] == "current-head"
    assert review.env_snapshot["validation_check_current"] is True


def test_remote_authored_interaction_replay_stays_on_controller_and_records_ownership(sched, monkeypatch):
    """A replay command contains controller paths, so task runner inheritance is unsafe."""
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    task.runner = "remote"
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    submitted = []
    runner_type = type(sched.runner_for(task, "local"))
    monkeypatch.setattr(runner_type, "start_checks",
                        lambda _self, run, _worktree, payload: submitted.append((run, payload)))

    check = sched.dispatch_review(task)

    assert check.runner == "local"
    assert check.env_snapshot["check_execution"] == {
        "backend": "local", "provenance": "controller-owned replay inputs",
    }
    assert sched.state.get(task.id)["check_run"]["backend"] == "local"
    command = submitted[0][1]["specs"][0]["command"]
    assert str(sched.worktree_for(task)) in command
    assert str(sched.cfg.garden_dir / "interaction-replays") in command


def test_remote_authored_portable_check_remains_remote(sched):
    task = sched.store.task("DM-001")
    task.runner = "remote"
    sched.store.save(task)

    check = sched._dispatch_check_run(
        task, worktree=sched.store.root, branch=task.default_branch(), base="main",
        specs=[{"name": "portable", "command": "true"}], stage="ci", cont={}, rep=TickReport(),
    )

    assert check.runner == "remote"
    assert check.env_snapshot["check_execution"] == {
        "backend": "remote", "provenance": "portable check payload",
    }


def test_interaction_replay_retry_preserves_local_backend_and_continuation(sched, monkeypatch):
    task = sched.store.task("DM-001")
    task.runner = "remote"
    sched.store.save(task)
    runner_type = type(sched.runner_for(task, "local"))
    monkeypatch.setattr(runner_type, "start_checks", lambda *_args: None)
    cont = {"head": "controller-head", "nonce": "replay-nonce", "manifest": "/controller/replay.json"}
    first = sched._dispatch_check_run(
        task, worktree=sched.worktree_for(task), branch=task.default_branch(), base="main",
        specs=[{"name": "interaction replay", "command": "controller-only"}],
        stage="interaction_replay", cont=cont, rep=TickReport(),
    )
    (first.path / "exit_code").write_text("1\n")

    assert sched.reap_check(task, TickReport())
    info = sched.state.get(task.id)["check_run"]
    retry = sched._run_by_id(task, info["run_id"])
    assert retry is not None and retry.run_id != first.run_id and retry.runner == "local"
    assert task.runner == "remote"
    assert info["backend"] == "local"
    assert info["cont"] == cont
    assert retry.env_snapshot["check_execution"] == first.env_snapshot["check_execution"]

    (retry.path / "checks.json").write_text(json.dumps([
        {"name": "interaction replay", "status": "fail", "summary": "affected replay module was not found"},
    ]))
    (retry.path / "exit_code").write_text("1\n")
    assert sched.reap_check(task, TickReport())
    recovery = sched.state.get(task.id)["recovery_check"]
    assert recovery["stage"] == "interaction_replay"
    assert recovery["cont"] == cont
    assert recovery["cause"] == "affected replay module was not found"
    fresh = Scheduler(Store(sched.store.root), github=sched.github)
    assert fresh.state.get(task.id)["recovery_check"] == recovery
    assert fresh.state.get(task.id)["needs_human"]["kind"] == "check_did_not_run"


def test_scoped_backend_preflight_does_not_reintroduce_capture_all(garden, monkeypatch):
    from garden import gitops
    from garden.preflight import mechanical_results

    monkeypatch.setattr(gitops, "base_ref", lambda *_: "main")
    monkeypatch.setattr(gitops, "git", lambda *args, **kwargs:
                        "src/garden/web/actions/control.py" if "--name-only" in args else "+return True")
    plan = validation_plan(["src/garden/web/actions/control.py"], "Backend pause action")
    assert plan["pages"] == []
    results = mechanical_results(garden, "main", "Pause control", require_description=True,
                                 ui_changed=False, captures=[], required_ui=bool(plan["pages"]))
    assert all(row["status"] == "pass" for row in results)


def test_shared_ui_without_a_current_check_is_not_verified(sched, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names",
                        lambda *_: ["src/garden/web/templates/base.html"])
    task = sched.store.task("DM-001")
    task.extra["visual_scope"] = {"behavior": "Updated shared rail style"}
    run = _review_after_completed_empty_replay(sched, task)
    assert run.env_snapshot["validation_plan"]["pages"] == ["board", "inbox"]
    assert run.env_snapshot["validation_check_current"] is False


@pytest.mark.parametrize(("changed", "title", "pages"), [
    (["src/garden/web/actions/control.py"], "Backend pause action", []),
    (["src/garden/criteria.py"], "Parser behavior", []),
    (["docs/design/captures/board-1280-light.png"], "Record generated capture", []),
    (["src/garden/web/pages/task.py"], "Tighten task layout", ["task"]),
    (["src/garden/web/templates/base.html"], "Update shared rail style", ["board", "inbox"]),
])
def test_precheck_submits_only_the_planned_capture_pages(sched, monkeypatch, changed, title, pages):
    task = sched.store.task("DM-001")
    task.title = title
    if pages:
        task.extra["visual_scope"] = {"behavior": title}
    # Capture the job at the real scheduler/runner boundary; no browser is needed to
    # verify which pages the scheduler actually requests.
    submitted = []
    runner_type = type(sched.runner_for(task, "local"))
    monkeypatch.setattr(runner_type, "start_checks", lambda self, run, wt, payload: submitted.append(payload))
    monkeypatch.setattr("garden.scheduler.checkruns.gitops.diff_names", lambda *_: changed)
    monkeypatch.setattr("garden.scheduler.checkruns.gitops.head_sha", lambda *_: "planned-head")
    run = sched._dispatch_check_run(task, worktree=sched.store.root,
                                    branch=task.default_branch(), base="main", stage="pre_pr",
                                    specs=[{"name": "focused lint", "command": "true"}], cont={}, rep=TickReport())
    ui = [spec for spec in submitted[0]["specs"] if spec["name"] == "ui"]
    assert [spec["pages"] for spec in ui] == ([pages] if pages else [])
    plan = run.env_snapshot["validation_plan"]
    assert plan["head"] == "planned-head"
    assert plan["checks"][0]["item"] == "focused lint"
    assert run.env_snapshot["generated_ui_check_indices"] == ([1] if pages else [])
    if ui:
        assert ui[0]["capture_infrastructure_policy"] == "require"


def test_queued_replays_do_not_recursively_drain_or_duplicate_checks(sched, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    runner_type = type(sched.runner_for(sched.store.task("DM-001"), "local"))
    monkeypatch.setattr(runner_type, "start_checks", lambda *args: None)
    sched.cfg.data["review_parallel"] = 3
    sched.cfg.data["resources"] = {"max_parallel": 5}
    for tid in ("DM-001", "DM-002"):
        task = sched.store.task(tid)
        task.status = Status.IN_REVIEW
        task.priority = 0
        task.depends_on = []
        sched.store.save(task)
        sched.state.get(tid)["pending_reviews"] = [{"kind": "review"}]
    first = TickReport()
    sched._drain_pending_reviews(sched.store.tasks(), first)
    assert first.dispatched == ["DM-001(check:interaction_replay)", "DM-002(check:interaction_replay)"]
    assert len([r for r in sched.runs.active() if r.mode == "check"]) == 2
    second = TickReport()
    sched._drain_pending_reviews(sched.store.tasks(), second)
    assert second.dispatched == []
    assert sched.review_wait_reason(sched.store.task("DM-001"))[0] == "check"


def _performed_interaction(head="head-a"):
    return {
        "head": head, "environment": "disposable", "command": "python -m uvicorn app:app",
        "states": {name: {"status": "pass", "actions": ["request"], "observed": "verified consequence"}
                   for name in ("affected", "empty", "failure_recovery")},
        "events": interaction_events(), "artifacts": [], "automated_checks": ["focused checks passed"],
        "unverified": [],
    }


@pytest.mark.parametrize("missing", ["head", "environment", "command", "artifacts", "automated_checks", "unverified"])
def test_missing_interaction_metadata_is_advisory(missing):
    row = _performed_interaction()
    del row[missing]
    warnings = []
    assert interaction_evidence_gaps(
        {"interaction": row}, required=True, scalability=False, expected_head="head-a",
        metadata_warnings=warnings,
    ) == []
    assert warnings


def test_artifact_may_use_equivalent_schema_and_reviewer_paraphrase(tmp_path):
    artifact = tmp_path / "performed.json"
    artifact.write_text(json.dumps({"head": "head-a", "states": {"affected": "recorded by harness"},
                                    "events": [{"action": "actual request", "observed": "raw response"}]}))
    row = _performed_interaction()
    row["artifacts"] = [str(artifact)]
    warnings = []
    assert interaction_evidence_gaps(
        {"interaction": row}, required=True, scalability=False, expected_head="head-a",
        metadata_warnings=warnings,
    ) == []
    assert warnings == []
    artifact.write_text(json.dumps({"head": "another-commit"}))
    assert any("contradicts" in gap for gap in interaction_evidence_gaps(
        {"interaction": row}, required=True, scalability=False, expected_head="head-a"))


def test_generic_replay_cannot_claim_a_declared_affected_flow(tmp_path):
    manifest = tmp_path / "generic.json"
    manifest.write_text(json.dumps({
        "producer": "garden.scheduler.interaction-replay/v1", "head": "head-a", "nonce": "n",
        "coverage": "generic_smoke", "environment": "disposable", "status": "pass",
    }))
    gaps = interaction_evidence_gaps(
        {"interaction": _performed_interaction()}, required=True, scalability=False,
        expected_head="head-a", replay_manifest=manifest, replay_nonce="n",
        replay_digest=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        affected_flow="harness-pause",
    )
    assert any("declared affected flow: harness-pause" in gap for gap in gaps)


def test_current_task_specific_author_evidence_skips_generic_replay(sched, monkeypatch):
    from garden import gitops

    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    task.extra["interaction_replay"] = {"affected_flow": "harness-pause"}
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    wt = gitops.prepare_worktree(sched.repo_for(task), sched.worktree_for(task),
                                 task.branch or task.default_branch(), sched.base_for(task))
    head = gitops.head_sha(wt)
    work = _writer_run(sched, task.id, "claude", "sonnet")
    work.result = {"interaction": {**_performed_interaction(head), "affected_flow": "harness-pause"}}
    work.env_snapshot["review_source_head"] = head
    work.save()

    review = sched.dispatch_review(task, work_run=work)

    assert review.mode == "review"
    assert review.env_snapshot["author_interaction_reused"] is True
    assert not [run for run in sched.runs.runs_for(task.id) if run.mode == "check"]
    assert "Author's task-specific interaction evidence" in (review.path / "brief.md").read_text()


def test_deferred_review_recovers_exact_external_author_result(sched, monkeypatch):
    from garden import gitops

    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    sched.cfg.data["review"]["enabled"] = True
    task = sched.store.task("DM-001")
    task.pr = "https://example.com/pull/101"
    task.status = Status.IN_REVIEW
    monkeypatch.setattr(sched, "slug_for", lambda *_: "")
    task.extra["interaction_replay"] = {"affected_flow": "harness-pause"}
    sched.store.save(task)
    wt = gitops.prepare_worktree(sched.repo_for(task), sched.worktree_for(task),
                                 task.branch or task.default_branch(), sched.base_for(task))
    head = gitops.head_sha(wt)
    st = sched.state.get(task.id)
    st["head_sha"] = head
    st["review_rounds"] = 0
    source = sched.runs.new_run(task.id, "manual", mode="revise")
    source.harness = "human"
    source.status = "done"
    source.result = {
        "head": head,
        "pr_title": "External result title",
        "pr_body": "External result body",
        "pr_comment": "External operator context retained",
        "verified": [{"criterion": "exact criterion", "evidence": "external result evidence"}],
        "pre_flight": [{"item": "external preflight", "status": "pass", "evidence": "recorded"}],
        "interaction": {**_performed_interaction(head), "affected_flow": "harness-pause"},
    }
    source.env_snapshot = {"criteria": ["exact criterion"]}
    source.save()
    # Force the real defer path, then drain without the in-memory work_run argument.
    st["check_run"] = {"run_id": "busy-check"}
    sched._maybe_review(task, source, TickReport())
    assert st["pending_reviews"] == [{"kind": "review", "count_round": True}]
    persisted_source = sched._run_by_id(task, source.run_id)
    assert persisted_source.env_snapshot["review_source_head"] == head
    st["check_run"] = {}

    rep = TickReport()
    sched._drain_pending_reviews(sched.store.tasks(), rep)

    review = sched.runs.latest(task.id)
    assert review.mode == "review"
    assert review.env_snapshot["author_source_run"] == source.run_id
    assert review.env_snapshot["author_source_head"] == head
    assert review.env_snapshot["author_interaction_reused"] is True
    brief = (review.path / "brief.md").read_text()
    assert f"Source author run: `{source.run_id}`" in brief
    assert f"Source head: `{head}`" in brief
    assert "External operator context retained" in brief
    assert "external result evidence" in brief
    assert "external preflight" in brief
    assert not [run for run in sched.runs.runs_for(task.id) if run.mode == "check"]


def test_deferred_review_refuses_stale_author_result(sched, monkeypatch):
    from garden import gitops

    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    task.extra["interaction_replay"] = {"affected_flow": "harness-pause"}
    sched.store.save(task)
    wt = gitops.prepare_worktree(sched.repo_for(task), sched.worktree_for(task),
                                 task.branch or task.default_branch(), sched.base_for(task))
    head = gitops.head_sha(wt)
    st = sched.state.get(task.id)
    st["head_sha"] = head
    stale = sched.runs.new_run(task.id, "manual", mode="revise")
    stale.status = "done"
    stale.result = {
        "head": "0" * 40,
        "interaction": {**_performed_interaction("0" * 40), "affected_flow": "harness-pause"},
    }
    stale.save()
    st["pending_reviews"] = [{"kind": "review", "count_round": True}]
    submitted = []
    runner_type = type(sched.runner_for(task, "local"))
    monkeypatch.setattr(runner_type, "start_checks",
                        lambda _self, run, _worktree, payload: submitted.append((run, payload)))

    rep = TickReport()
    sched._drain_pending_reviews(sched.store.tasks(), rep)

    runs = sched.runs.runs_for(task.id)
    check = next((run for run in reversed(runs) if run.mode == "check"), None)
    assert check is not None, ([(run.run_id, run.mode, run.status) for run in runs],
                               rep.dispatched, rep.errors, dict(st))
    assert submitted
    assert not sched.state.get(task.id).get("review_run")
    assert sched._review_source_for_head(task, head) is None


def test_declared_affected_replay_module_is_selected_for_remote_author(sched, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    task.runner = "remote"
    task.status = Status.IN_REVIEW
    task.extra["interaction_replay"] = {
        "affected_flow": "harness-pause", "module": "project_qa.harness_pause",
    }
    sched.store.save(task)
    submitted = []
    runner_type = type(sched.runner_for(task, "local"))
    monkeypatch.setattr(runner_type, "start_checks",
                        lambda _self, run, _worktree, payload: submitted.append((run, payload)))

    check = sched.dispatch_review(task)

    assert check.runner == "local"
    assert "-m project_qa.harness_pause" in submitted[0][1]["specs"][0]["command"]
    assert sched.state.get(task.id)["check_run"]["cont"]["affected_flow"] == "harness-pause"


@pytest.mark.parametrize("bug", [False, True])
def test_scheduler_metadata_advisory_preserves_verdict_and_real_findings(sched, monkeypatch, bug):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    run = _review_after_completed_empty_replay(sched, task)
    run.env_snapshot["validation_check_current"] = True
    run.save()
    row = _performed_interaction(run.env_snapshot["review_head"])
    del row["command"]
    findings = ([{"severity": "blocking", "file": "src/garden/review.py", "line": 1,
                 "summary": "A wrong repository can be accepted", "fix": "Reject foreign repository identity"}]
                if bug else [])
    review = {"verdict": "request_changes" if bug else "approve", "summary": "Verified behavior",
              "pages_seen": [], "criteria": [], "description_ok": True, "findings": findings,
              "improvements": [], "interaction": row}
    (run.path / "stdout.json").write_text(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "GARDEN_REVIEW: " + json.dumps(review), "usage": {},
    }))
    sched.reap_review(task, TickReport())
    persisted = sched.runs.latest(task.id).result
    assert persisted["verdict"] == ("request_changes" if bug else "approve")
    notes = [f for f in persisted["findings"] if f["summary"].startswith("Evidence metadata advisory:")]
    assert len(notes) == 1 and notes[0]["severity"] == "nit"
    assert any(f["severity"] == "blocking" for f in persisted["findings"]) is bug


def test_legacy_unverified_gets_one_restart_safe_reviewer_clarification(sched, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    run = _review_after_completed_empty_replay(sched, task)
    run.env_snapshot["validation_check_current"] = True
    run.save()
    review = {"verdict": "approve", "summary": "required behavior passed", "pages_seen": [],
              "criteria": [], "description_ok": True, "findings": [], "improvements": [],
              "interaction": _performed_interaction(run.env_snapshot["review_head"])}
    review["interaction"]["unverified"] = ["the broader launch incident was not reproduced"]
    (run.path / "stdout.json").write_text(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "GARDEN_REVIEW: " + json.dumps(review), "usage": {},
    }))

    rep = TickReport()
    assert sched.reap_review(task, rep)
    assert rep.transitions == ["DM-001 review re-asked to classify unverified observations"]
    clarification = sched.runs.latest(task.id)
    assert clarification.run_id != run.run_id
    assert clarification.env_snapshot["count_round"] is False
    assert clarification.env_snapshot["clarify_unverified"] is True
    assert "broader launch incident" in (clarification.path / "brief.md").read_text()
    assert sched._run_by_id(task, run.run_id).result["interaction"]["unverified"] == review["interaction"]["unverified"]

    fresh = Scheduler(Store(sched.store.root), github=sched.github)
    assert fresh.state.get(task.id)["review_run"] == clarification.run_id


def test_failed_state_blocks_even_when_reviewer_calls_it_a_limitation():
    review = {"interaction": _performed_interaction()}
    review["interaction"]["states"]["affected"]["status"] = "fail"
    review["interaction"]["unverified"] = [{
        "scope": "limitation", "observation": "affected behavior was not demonstrated",
    }]

    gaps = interaction_evidence_gaps(
        review, required=True, scalability=False, expected_head="head-a",
    )
    assert "affected interaction is missing or failed" in gaps


def test_required_target_blocks_even_when_reviewer_calls_it_a_limitation():
    review = {"criteria": [{"criterion": "Recovery is demonstrated"}],
              "interaction": _performed_interaction()}
    review["interaction"]["unverified"] = [{
        "scope": "limitation", "criterion": "Recovery is demonstrated",
        "outcome": "recovery was not observed", "reason": "the fixture stopped",
    }]

    gaps = interaction_evidence_gaps(
        review, required=True, scalability=False, expected_head="head-a",
        expected_criteria=["Recovery is demonstrated"],
    )
    assert any("criterion: Recovery is demonstrated" in gap for gap in gaps)
    assert ambiguous_unverified(
        review, expected_criteria=["Recovery is demonstrated"],
    ) == []


def test_required_gap_cannot_target_a_reviewer_invented_criterion():
    review = {"criteria": [{"criterion": "Invented by reviewer"}],
              "interaction": _performed_interaction()}
    review["interaction"]["unverified"] = [{
        "scope": "required", "criterion": "Invented by reviewer",
        "outcome": "was not observed", "reason": "the reviewer expanded scope",
    }]

    gaps = interaction_evidence_gaps(
        review, required=True, scalability=False, expected_head="head-a",
        expected_criteria=["Frozen task criterion"],
    )
    assert gaps == ["required unverified outcome names no frozen criterion: Invented by reviewer"]
    assert ambiguous_unverified(
        review, expected_criteria=["Frozen task criterion"],
    ) == [json.dumps(review["interaction"]["unverified"][0], sort_keys=True)]


def test_fabricated_required_target_gets_one_reviewer_clarification_then_operator_stop(
        sched, monkeypatch):
    from fastapi.testclient import TestClient

    from garden.web.app import create_app

    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    writer = sched.runs.new_run(task.id, "local", mode="work")
    writer.status = "done"
    writer.env_snapshot = {"criteria": ["Frozen task criterion"]}
    writer.save()
    run = _review_after_completed_empty_replay(sched, task)
    run.env_snapshot["validation_check_current"] = True
    run.save()
    interaction = _performed_interaction(run.env_snapshot["review_head"])
    interaction["unverified"] = [{
        "scope": "required", "criterion": "Invented by reviewer",
        "outcome": "was not observed", "reason": "the reviewer expanded scope",
    }]
    review = {"verdict": "approve", "summary": "Everything passed", "pages_seen": [],
              "criteria": [{"criterion": "Invented by reviewer", "met": True,
                            "evidence": "reviewer assertion"}],
              "description_ok": True, "findings": [], "improvements": [],
              "interaction": interaction}
    (run.path / "stdout.json").write_text(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "GARDEN_REVIEW: " + json.dumps(review), "usage": {},
    }))

    first = TickReport()
    assert sched.reap_review(task, first)
    assert first.transitions == ["DM-001 review re-asked to classify unverified observations"]
    clarification = sched.runs.latest(task.id)
    assert clarification.run_id != run.run_id
    assert clarification.env_snapshot["count_round"] is False
    assert clarification.env_snapshot["criteria"] == ["Frozen task criterion"]
    assert "Invented by reviewer" in (clarification.path / "brief.md").read_text()
    task = sched.store.task(task.id)
    task.pr = "https://example.com/pull/101"
    sched.store.save(task)
    assert task.status == Status.IN_REVIEW
    assert not sched.state.get(task.id).get("pending_feedback")
    assert not [candidate for candidate in sched.runs.runs_for(task.id) if candidate.mode == "revise"]

    (clarification.path / "stdout.json").write_text(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "GARDEN_REVIEW: " + json.dumps(review), "usage": {},
    }))
    second = TickReport()
    assert sched.reap_review(task, second)
    assert second.transitions == ["DM-001 reviewer clarification needs operator attention"]
    stopped = sched._run_by_id(task, clarification.run_id)
    assert stopped.status == "failed"
    assert stopped.result["interaction"]["unverified"] == interaction["unverified"]
    state = sched.state.get(task.id)
    assert state["needs_human"]["kind"] == "review_clarification"
    assert state["needs_human"]["owner"] == "reviewer"
    assert not state.get("review_run")
    assert not state.get("last_review")
    assert not state.get("pending_feedback")
    assert sched.store.task(task.id).status == Status.IN_REVIEW
    assert not [candidate for candidate in sched.runs.runs_for(task.id) if candidate.mode == "revise"]

    page = TestClient(create_app(Store(sched.store.root), watch=False, github=sched.github)).get("/inbox")
    assert page.status_code == 200
    assert "Reviewer clarification needs attention" in page.text
    assert "implementation author has not been asked to change code" in page.text
    assert "One more automated review" in page.text


def test_reviewer_clarification_survives_restart_and_reconnects_saved_continuation(
        sched, fake_github, monkeypatch):
    monkeypatch.setattr("garden.scheduler.review.gitops.diff_names", lambda *_: ["src/garden/review.py"])
    task = sched.store.task("DM-001")
    task.status = Status.IN_REVIEW
    sched.store.save(task)
    writer = sched.runs.new_run(task.id, "local", mode="work")
    writer.status = "done"
    writer.env_snapshot = {"criteria": ["Frozen task criterion"]}
    writer.save()
    source = _review_after_completed_empty_replay(sched, task)
    source.env_snapshot["validation_check_current"] = True
    entry = {"scope": "required", "criterion": "Invented by reviewer",
             "outcome": "was not observed", "reason": "the reviewer expanded scope"}
    source.result = {"verdict": "approve", "summary": "Everything passed", "pages_seen": [],
                     "criteria": [{"criterion": "Invented by reviewer", "met": True,
                                   "evidence": "reviewer assertion"}],
                     "description_ok": True, "findings": [], "improvements": [],
                     "interaction": {**_performed_interaction(source.env_snapshot["review_head"]),
                                     "unverified": [entry]}}
    source.status = "done"
    source.env_snapshot["clarification_pending"] = [json.dumps(entry, sort_keys=True)]
    source.save()
    sched.state.save()

    # Restart after the malformed result was saved but before its clarification dispatched.
    fresh = Scheduler(Store(sched.store.root), github=fake_github)
    restarted_task = fresh.store.task(task.id)
    rep = TickReport()
    assert fresh.reap_review(restarted_task, rep)
    clarification = fresh.runs.latest(task.id)
    assert clarification.run_id != source.run_id
    assert clarification.env_snapshot["clarifies_review_run"] == source.run_id
    assert clarification.env_snapshot["count_round"] is False
    assert fresh.state.get(task.id)["review_run"] == clarification.run_id
    assert not fresh.state.get(task.id).get("pending_feedback")
    assert not [run for run in fresh.runs.runs_for(task.id) if run.mode == "revise"]
    assert rep.transitions == ["DM-001 review re-asked to classify unverified observations"]

    # Simulate the narrower crash after the clarification run was saved but before its pointer
    # replaced the source pointer. The next process reconnects that exact run, not a duplicate.
    fresh.state.get(task.id)["review_run"] = source.run_id
    fresh.state.save()
    run_ids = [run.run_id for run in fresh.runs.runs_for(task.id)]
    restarted_again = Scheduler(Store(sched.store.root), github=fake_github)
    rep = TickReport()
    assert restarted_again.reap_review(restarted_again.store.task(task.id), rep)
    assert restarted_again.state.get(task.id)["review_run"] == clarification.run_id
    assert [run.run_id for run in restarted_again.runs.runs_for(task.id)] == run_ids
    assert rep.transitions == ["DM-001 review clarification continuation restored"]


def test_worker_and_reviewer_share_evidence_contract_and_complete_event_example(garden):
    from garden.brief import EVIDENCE_GUIDANCE, build_brief

    store = Store(garden)
    task = store.task("DM-001")
    author = build_brief(store, task).text
    reviewer = review_brief(store, task, branch="b", base="main", pr_title="T", pr_body="B",
                            diff="+x", max_diff_chars=1000)
    assert EVIDENCE_GUIDANCE in author and EVIDENCE_GUIDANCE in reviewer
    assert "Missing artifact metadata alone is advisory" in author
    assert '"events": [{"kind": "http_request"' in reviewer
    assert "A missing item is a blocking finding" not in reviewer
