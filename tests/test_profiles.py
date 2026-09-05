"""Tests for operating profiles (CG-221): the slider from efficient to fast that sets
workers, the tier map, the review tier, retro.difficulty and the observe profile together."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from garden.events import EventLog
from garden.observe import resolve as observe_resolve
from garden.profiles import BUILTIN_PROFILES, describe, stops


def _cli(garden: Path, *args: str):
    from typer.testing import CliRunner

    from garden.cli import app

    cwd = os.getcwd()
    os.chdir(garden)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(cwd)


def test_builtin_profiles_ordered_efficient_to_fast():
    assert list(BUILTIN_PROFILES) == ["economy", "balanced", "fast"]
    for stop in BUILTIN_PROFILES.values():
        assert stop["workers"] < 10  # sane bound; also exercises every field is present
        assert set(stop) <= {"workers", "reviews", "models", "review_difficulty", "retro_difficulty", "observe"}


def test_custom_stop_is_valid_with_only_some_fields():
    """A partial stop (a garden that only wants to shrink worker count for a name) doesn't
    need every field; describe() and stops() both treat missing fields as "leave it be"."""
    cfg_data = {"profiles": {"night": {"workers": 1}}}

    class FakeCfg:
        def get(self, key, default=None):
            return cfg_data.get(key, default)

    merged = stops(FakeCfg())
    assert merged["night"] == {"workers": 1}
    assert merged["economy"] == BUILTIN_PROFILES["economy"]
    assert describe(merged["night"]) == "1 workers"


def test_a_garden_can_override_a_builtin_stop_outright():
    cfg_data = {"profiles": {"fast": {"workers": 20}}}

    class FakeCfg:
        def get(self, key, default=None):
            return cfg_data.get(key, default)

    merged = stops(FakeCfg())
    assert merged["fast"] == {"workers": 20}


def test_switch_from_economy_to_fast_moves_all_four_knobs(sched):
    """The task brief's own acceptance test: switching the stop changes dispatch's worker
    count and tier map, the review tier, and the observe cadence, all within the same tick
    (no restart) — with no dispatch/tick required, since effective()/model_for()/observe.resolve
    read the live override directly."""
    store = sched.store
    task = list(store.tasks().values())[0]
    task.model = ""  # no per-task pin, so the profile's tier map is what answers model_for
    runner = sched.runner_for(task)

    sched.set_operating_profile("economy", by="test")
    assert sched.effective_max_parallel() == BUILTIN_PROFILES["economy"]["workers"]
    assert sched.review_parallel_limit() == BUILTIN_PROFILES["economy"]["reviews"]
    assert sched.effective("review.difficulty") == "easy"
    assert sched.model_for(task, runner, "medium") == BUILTIN_PROFILES["economy"]["models"]["medium"]
    settings = observe_resolve(store.config, sched)
    assert settings.profile == "quiet"

    sched.set_operating_profile("fast", by="test")
    assert sched.effective_max_parallel() == BUILTIN_PROFILES["fast"]["workers"]
    assert sched.review_parallel_limit() == BUILTIN_PROFILES["fast"]["reviews"]
    assert sched.effective("review.difficulty") == "medium"
    assert sched.model_for(task, runner, "medium") == BUILTIN_PROFILES["fast"]["models"]["medium"]
    settings = observe_resolve(store.config, sched)
    assert settings.profile == "watch"


def test_more_specific_override_wins_over_the_stop(sched):
    """`max_parallel` set directly (the pre-CG-221 mechanism) still wins over an active stop —
    the design's own example of "more specific"."""
    sched.set_operating_profile("economy", by="test")
    assert sched.effective_max_parallel() == BUILTIN_PROFILES["economy"]["workers"]
    sched.set_override("max_parallel", 9, by="test")
    assert sched.effective_max_parallel() == 9

    store = sched.store
    task = list(store.tasks().values())[0]
    task.model = ""
    runner = sched.runner_for(task)
    sched.overrides()[f"harnesses.{runner.harness.name}.models.medium"] = "pinned-model"
    assert sched.model_for(task, runner, "medium") == "pinned-model"


def test_no_profile_active_falls_back_to_plain_config(sched):
    assert sched.operating_profile_name() == ""
    assert sched.operating_profile() == {}
    assert sched.effective_max_parallel() == int(sched.cfg.get("max_parallel"))


def test_set_operating_profile_emits_profile_changed_event(sched):
    sched.set_operating_profile("balanced", by="test")
    sched.set_operating_profile("fast", by="test")
    events = EventLog(sched.cfg.garden_dir / "events.jsonl").read(kinds=["profile_changed"])
    assert [(e["from"], e["to"]) for e in events] == [("", "balanced"), ("balanced", "fast")]


def test_set_operating_profile_clear(sched):
    sched.set_operating_profile("fast", by="test")
    assert sched.operating_profile_name() == "fast"
    sched.set_operating_profile("", by="test")
    assert sched.operating_profile_name() == ""


def test_unknown_profile_name_raises(sched):
    with pytest.raises(ValueError):
        sched.set_operating_profile("nonexistent", by="test")


def test_cli_profile_set_show_and_clear(garden: Path):
    r = _cli(garden, "profile", "fast")
    assert r.exit_code == 0 and "operating profile: fast" in r.output

    r = _cli(garden, "profile")
    assert r.exit_code == 0 and "active: fast" in r.output

    r = _cli(garden, "profile", "--clear")
    assert r.exit_code == 0

    r = _cli(garden, "profile")
    assert "active: (none" in r.output


def test_cli_profile_unknown_name_errors(garden: Path):
    r = _cli(garden, "profile", "nonexistent")
    assert r.exit_code == 1
