"""Operating-profile concurrency multipliers and compatibility."""

from __future__ import annotations

from garden.events import EventLog
from garden.profiles import BUILTIN_PROFILES, resolve, scaled_concurrency, stops


def test_builtin_profiles_are_three_concurrency_choices():
    assert list(BUILTIN_PROFILES) == ["economy", "default", "fast"]
    assert [stop["multiplier"] for stop in BUILTIN_PROFILES.values()] == [0.5, 1.0, 2.0]


def test_multiplier_rounding_and_disabled_values_are_deterministic():
    assert scaled_concurrency(3, 0.5) == 1
    assert scaled_concurrency(1, 0.5) == 1
    assert scaled_concurrency(0, 0.5) == 0
    assert scaled_concurrency(3, 2) == 6


def test_builtins_only_scale_configured_concurrency_and_reload_baseline(sched):
    sched.cfg.data["max_parallel"] = 3
    sched.cfg.data["review_parallel"] = 5
    sched.cfg.data["models"] = {"medium": "configured-model"}
    sched.cfg.data["observe"]["profile"] = "watch"
    sched.set_operating_profile("economy", by="test")
    assert sched.effective_max_parallel() == 1
    assert sched.review_parallel_limit() == 2
    assert sched.effective("models") == {"medium": "configured-model"}
    assert sched.effective("observe.profile") == "watch"
    sched.cfg.data["max_parallel"] = 7
    sched.cfg.data["review_parallel"] = 3
    assert sched.effective_max_parallel() == 3
    assert sched.review_parallel_limit() == 1
    sched.set_operating_profile("fast", by="test")
    assert sched.effective_max_parallel() == 14
    assert sched.review_parallel_limit() == 6


def test_default_and_legacy_selections_use_configured_baseline(sched):
    sched.cfg.data["max_parallel"] = 7
    sched.cfg.data["review_parallel"] = 3
    assert sched.operating_profile_name() == "default"
    assert sched.effective_max_parallel() == 7
    sched.cfg.data["operating_profile"] = "balanced"
    assert sched.operating_profile_name() == "default"
    assert sched.review_parallel_limit() == 3
    sched.set_operating_profile("plain", by="test")
    assert sched.operating_profile_name() == "default"


def test_custom_legacy_name_is_preserved(sched):
    sched.cfg.data["profiles"] = {"balanced": {"workers": 9, "reviews": 4}}
    sched.cfg.data["operating_profile"] = "balanced"
    assert sched.operating_profile_name() == "balanced"
    assert sched.effective_max_parallel() == 9
    assert stops(sched.cfg)["balanced"] == {"workers": 9, "reviews": 4}


def test_profile_change_is_live_and_clear_reveals_default(sched):
    sched.set_operating_profile("fast", by="test")
    sched.set_operating_profile("", by="test")
    assert sched.operating_profile_name() == "default"
    events = EventLog(sched.cfg.garden_dir / "events.jsonl").read(kinds=["profile_changed"])
    assert [(e["from"], e["to"]) for e in events] == [("default", "fast"), ("fast", "")]


def test_resolve_preserves_custom_profiles():
    class Config:
        def get(self, key, default=None):
            return {"max_parallel": 5, "review_parallel": 2,
                    "profiles": {"night": {"workers": 1}}}.get(key, default)

    assert resolve(Config(), "night") == {"workers": 1}
