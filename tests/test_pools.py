"""Tier and review pools share quota across harness accounts (CG-230)."""

from __future__ import annotations

from garden.runs import Run


def _configure_pool(sched) -> None:
    sched.cfg.data["models"] = {"medium": [
        {"harness": "claude", "model": "sonnet", "weight": 2},
        {"harness": "codex", "model": "gpt-std", "weight": 1},
    ]}


def test_weighted_tier_pool_spreads_ten_choices_and_skips_a_paused_harness(sched):
    task = sched.store.task("DM-001")
    _configure_pool(sched)
    task.difficulty = "medium"

    runs = [sched.dispatch(task) for _ in range(10)]
    labels = [run.pool_member for run in runs]
    assert labels.count("claude:sonnet") == 7
    assert labels.count("codex:gpt-std") == 3

    sched.pause_harness("claude", "quota limit")
    assert {sched.select_pool_member(task, "medium")["label"] for _ in range(3)} == {"codex:gpt-std"}


def test_quota_aware_pool_halves_a_recently_limited_member_until_probe_succeeds(sched):
    task = sched.store.task("DM-001")
    task.difficulty = "medium"
    sched.cfg.data["models"] = {"medium": [
        {"harness": "claude", "model": "sonnet"},
        {"harness": "codex", "model": "gpt-std"},
    ]}
    limited = Run(task_id=task.id, run_id="quota", dir="", runner="local", harness="claude",
                  model="sonnet", pool_member="claude:sonnet")
    sched.note_pool_quota_member(limited)

    labels = [sched.select_pool_member(task, "medium")["label"] for _ in range(12)]
    assert labels.count("claude:sonnet") == 4
    assert labels.count("codex:gpt-std") == 8

    sched.pause_harness("claude", "quota limit")
    sched.resume_harness("claude", by="probe")
    labels = [sched.select_pool_member(task, "medium")["label"] for _ in range(12)]
    assert labels.count("claude:sonnet") == labels.count("codex:gpt-std") == 6


def test_round_robin_pool_alternates_and_task_pin_wins(sched):
    task = sched.store.task("DM-001")
    task.difficulty = "medium"
    sched.cfg.data["dispatch"] = {"spread": "round_robin"}
    _configure_pool(sched)
    assert [sched.select_pool_member(task, "medium")["label"] for _ in range(4)] == [
        "claude:sonnet", "codex:gpt-std", "claude:sonnet", "codex:gpt-std",
    ]
    task.harness, task.model = "codex", "gpt-std"
    assert sched.select_pool_member(task, "medium")["label"] == "codex:gpt-std"


def test_dispatch_records_the_selected_pool_member(sched):
    task = sched.store.task("DM-001")
    task.difficulty = "medium"
    _configure_pool(sched)
    run = sched.dispatch(task)
    assert (run.harness, run.model, run.pool_member) == ("claude", "sonnet", "claude:sonnet")


def test_review_pool_alternates_and_skips_paused_harness(sched):
    task = sched.store.task("DM-001")
    sched.cfg.data["review"]["pool"] = [
        {"harness": "claude", "model": "sonnet"},
        {"harness": "codex", "model": "gpt-std"},
    ]
    choices = [sched.select_pool_member(task, "medium", review=True)["label"] for _ in range(4)]
    assert choices == ["claude:sonnet", "codex:gpt-std", "claude:sonnet", "codex:gpt-std"]
    sched.pause_harness("claude", "quota limit")
    assert sched.select_pool_member(task, "medium", review=True)["label"] == "codex:gpt-std"


def test_review_pool_accepts_harness_model_shorthand(sched):
    task = sched.store.task("DM-001")
    sched.cfg.data["review"]["pool"] = ["claude:sonnet", "codex:gpt-std"]
    assert [sched.select_pool_member(task, "medium", review=True)["label"] for _ in range(2)] == [
        "claude:sonnet", "codex:gpt-std",
    ]


def test_review_pool_model_beats_a_harness_review_model(sched):
    task = sched.store.task("DM-001")
    sched.cfg.data["harnesses"]["claude"]["review_model"] = "opus"
    sched.cfg.data["review"]["pool"] = [{"harness": "claude", "model": "sonnet"}]
    run = sched.dispatch_review(task)
    assert (run.model, run.pool_member) == ("sonnet", "claude:sonnet")
